"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

「外置大模型」在 modeld 侧的远端 ModelState：对 modeld 暴露与原生
`ModelState` **完全一致的鸭子接口**，但策略推理发到本机服务端（OPRP）。

职责边界
--------
* **本地只做 QCOM warp**：加载配置里的 warp-only 工件（默认
  `/data/models/remote_warp.pkl`），用原生 `WARP_INPUTS` 队列产出 `warped`
  `(2,6,H,W) uint8`；绝不构造 policy 侧大队列（避免 Adreno 显存浪费）。
* **上行只允许** `warped` + `desire` / `traffic_convention` / `action_t`
  这些外部标量；`prev_feat` / `img_q` / `tfm` 等内部态由服务端持有，禁止上行。
* **回传原始 outputs**，本地复用原生 `Parser` 与 output_slices 切片语义解析，
  返回与 `ModelState.run` 同构的 `dict[str, np.ndarray]`。
* **时序绑定**：本帧的 `frame_id` / `ts_eof` 由调用方注入（`set_frame`），
  缺省时从 `bufs` 的 `frame_id` 推导；禁止用 N+1 的上下文发布 N 的输出。

重叠流水线（延迟一帧发布）
--------------------------
`run()` 被 modeld 每帧调用一次（相机帧 N），但**不同步等本帧结果**：

1. 主线程做本帧的 QCOM warp（GPU 上下文只能主线程用）并打包 job，立刻交给 worker；
2. worker 只做网络 `submit`（写上线、不等待回包）；主线程随即去 `collect`
   **上一帧**的 handle 并解析返回。于是服务端执行 policy(N) 与客户端等待/解析
   result(N-1) 在时间上重叠，帧周期 ≈ 服务端单帧 RTT，而不是 RTT + warp；
3. `run(N)` 返回的是**第 N-1 帧的远端结果**，其 `output_context`
   记录该结果所属帧的完整上下文（frame_id/ts_eof/调用方注入的发布字段）。
4. 若当前没有可发布的上一帧结果（启动/换入首帧、或流水线追赶中），
   `run()` 返回 `None`，由 `JoinedModel` 同帧跑本地 small 并发布。

降级与清理：worker/`collect`/解析的**任何链路级**异常都会清空流水线（丢弃在途
handle 与排队 job）并置 `_need_reset`，再原样抛出给 `JoinedModel`，由它同帧降级
到 small；worker 异常绝不泄漏、也绝不挂死主循环。**单帧 `RemoteTimeout` 例外**：
它只表示这一帧没赶上 deadline，链路仍然健康（客户端会丢弃迟到响应），因此
`run()` 吞掉它、返回 `None` 让调用方同帧降级，不 abort 流水线、不触发链路退避，
下一帧继续在远端跑——否则 60ms deadline 下 ~8% 的抖动帧会不断把链路打进退避。

本模块保持「导入即轻量」：tinygrad / openpilot 重型依赖（load_oob、
get_nv12_info、make_warp_queues…）全部延迟到运行时导入，因此离线单测可注入
伪造的 metadata / warp / queue_factory，不需要真机或 tinygrad。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from openpilot.sunnypilot.modeld_v2.constants import ModelConstants
from openpilot.sunnypilot.modeld_v2.parse_model_outputs import Parser
from openpilot.sunnypilot.accelerators.remote.client import RemoteTimeout, pin_network_thread
from openpilot.sunnypilot.accelerators.remote.debug_log import log_event
from openpilot.sunnypilot.accelerators.remote.protocol import FLAG_RESET_TEMPORAL

# 与 sunnypilot.modeld_v2.compile_modeld 一致（在此复述以免导入 tinygrad）
WARP_INPUTS = ('tfm', 'big_tfm')

# 服务端只接受这些外部标量；lateral_control_params 跟本地队列走但不上行
REMOTE_SCALAR_INPUTS = ('traffic_convention', 'lateral_control_params', 'action_t')
REMOTE_UPLINK_SCALARS = ('traffic_convention', 'action_t')

DEFAULT_WARP_PATH = '/data/models/remote_warp.pkl'
DEFAULT_CONTRACT_PATH = '/data/models/contract_metadata.json'

_SLICE_MARKER = '__slice__'
_TUPLE_MARKER = '__tuple__'

# worker 空转轮询间隔（秒）：只在无 job 时短暂阻塞，不占用实时核
_PIPELINE_POLL_S = 0.1
# worker 等待「在途请求名额」的最长时间（秒）：等不到就丢本帧，绝不死锁
_PIPELINE_SLOT_TIMEOUT_S = 0.08
# 取上一帧 handle 时的极短等待（秒）：覆盖首帧/pipeline 刚启动的入队空窗
_PIPELINE_TAKE_WAIT_S = 0.05
# 冷启动宽限：新链路头几帧等 TCP 接收窗口打开；不计入 steady-state 指标
_PIPELINE_COLD_FRAMES = 2
_PIPELINE_COLD_DEADLINE_S = 2.0
# submit worker 单个 job 的耗时上限（秒）：超过且链路已断即判为「卡死在 bulk 的
# ffs 写里」，换一个新 worker。取 2.0s 是因为冷启动宽限本身就有 2.0s。
_STUCK_JOB_S = 2.0


class RemoteModelError(RuntimeError):
  """远端本帧失败（超时/链路/协议/输出非法）；调用方须同帧降级到本地小模型。"""


def _on_comma_hardware() -> bool:
  """COMMA_HARDWARE 的惰性读取；探测失败按非 Comma 处理。"""
  try:
    from openpilot.common.hardware import COMMA_HARDWARE
    return bool(COMMA_HARDWARE)
  except Exception:
    return False


def _to_numpy(value: Any) -> np.ndarray:
  """tinygrad Tensor 或 ndarray 统一取 numpy 副本。"""
  if hasattr(value, 'numpy'):
    return np.asarray(value.numpy())
  return np.asarray(value)


def _decode_json_metadata(obj: Any) -> Any:
  """契约 JSON 的 `__slice__` / `__tuple__` 逆编码（与设备侧约定一致）。"""
  if isinstance(obj, dict):
    if set(obj.keys()) == {_SLICE_MARKER}:
      parts = obj[_SLICE_MARKER]
      return slice(*(_decode_json_metadata(p) for p in parts))
    if set(obj.keys()) == {_TUPLE_MARKER}:
      return tuple(_decode_json_metadata(item) for item in obj[_TUPLE_MARKER])
    return {k: _decode_json_metadata(v) for k, v in obj.items()}
  if isinstance(obj, list):
    return [_decode_json_metadata(item) for item in obj]
  return obj


def _metadata_complete(metadata: Any) -> bool:
  """能否仅凭这份 metadata 构造队列并解析输出。"""
  return (isinstance(metadata, dict) and bool(metadata.get('input_shapes'))
          and bool(metadata.get('output_slices')))


def _unwrap_metadata(raw: Any) -> Any:
  """兼容根级 metadata 与 `{'metadata': {...}}` 包装。"""
  if isinstance(raw, dict) and isinstance(raw.get('metadata'), dict):
    inner = raw['metadata']
    if _metadata_complete(inner):
      return inner
  return raw


def _detect_desire_key(input_shapes: dict) -> str | None:
  """契约里的 desire 输入可能叫 `desire` 或 `desire_pulse`。"""
  for key in ('desire', 'desire_pulse'):
    if key in input_shapes:
      return key
  return next((key for key in input_shapes if key.startswith('desire')), None)


def _scalar_shapes(metadata: dict) -> dict[str, tuple[int, ...]]:
  """外部标量在本地 numpy 视图里的形状（与服务端队列一致）。"""
  input_shapes = metadata['input_shapes']
  shapes: dict[str, tuple[int, ...]] = {}
  desire_key = _detect_desire_key(input_shapes)
  if desire_key is not None:
    shapes['desire'] = (int(input_shapes[desire_key][2]),)
  for key in REMOTE_SCALAR_INPUTS:
    if key in input_shapes:
      shapes[key] = tuple(int(x) for x in input_shapes[key])
  return shapes


class RemoteModelState:
  """远端策略版 ModelState：本地 QCOM warp + OPRP 远端 policy + 本地解析。"""

  def __init__(self, cam_w: int, cam_h: int, client,
               *, metadata: dict | None = None, warp_jit: Callable | None = None,
               warp_path: str | None = None, contract_path: str | None = None,
               device: str | None = None, small=None, status=None,
               queue_factory: Callable | None = None,
               parser: Parser | None = None,
               prime: bool | None = None):
    self.cam_w = int(cam_w)
    self.cam_h = int(cam_h)
    self.client = client
    self.status = status
    self.small = small
    self._warp_path = warp_path or DEFAULT_WARP_PATH
    self._contract_path = contract_path or DEFAULT_CONTRACT_PATH

    jits = None
    if metadata is None or warp_jit is None:
      jits = self._load_artifact()

    if metadata is None:
      metadata = jits.get('metadata') if isinstance(jits, dict) else None
    metadata = _unwrap_metadata(metadata)
    if not _metadata_complete(metadata):
      # pkl 内 metadata 不足以构造队列/解析输出 → 回退到契约 JSON
      metadata = self._load_contract_metadata()
      self.metadata_source = 'contract_json'
    else:
      self.metadata_source = 'artifact_metadata'
    self.metadata = metadata

    if warp_jit is None:
      warp_jit = jits[(self.cam_w, self.cam_h)]
    self.warp_jit = warp_jit

    self.WARP_DEV = device or (metadata.get('warp_dev', 'QCOM') if _on_comma_hardware() else 'CPU')

    input_shapes = metadata['input_shapes']
    self._vision_input_names = [key for key in input_shapes if 'img' in key]
    if not self._vision_input_names:
      raise RemoteModelError('metadata.input_shapes 缺少视觉输入（img/big_img）')
    self._road_key = next((key for key in self._vision_input_names if 'big' not in key), self._vision_input_names[0])
    self._wide_key = next((key for key in self._vision_input_names if 'big' in key), self._road_key)

    self.vision_output_slices = metadata['output_slices']
    self.constants = getattr(small, 'constants', None) or ModelConstants()
    self.parser = parser or Parser()

    features_buffer = input_shapes.get('features_buffer')
    self.frame_skip = 1 if not features_buffer or features_buffer[1] >= 99 else 4

    self.generation = getattr(small, 'generation', None)
    self.LAT_SMOOTH_SECONDS = float(getattr(small, 'LAT_SMOOTH_SECONDS', 0.0))
    self.LONG_SMOOTH_SECONDS = float(getattr(small, 'LONG_SMOOTH_SECONDS', 0.0))
    self.MIN_LAT_CONTROL_SPEED = float(getattr(small, 'MIN_LAT_CONTROL_SPEED', 0.3))
    self.PLANPLUS_CONTROL = float(getattr(small, 'PLANPLUS_CONTROL', 1.0))
    self.lat_delay = float(getattr(small, 'lat_delay', 0.0))

    self._prime = prime if prime is not None else (queue_factory is None)
    self.frame_copy_size = 0
    self.frame_buf_params: dict[str, tuple] = {}
    self.full_frames: dict = {}
    self._blob_cache: dict = {}
    self.input_queues: dict = {}
    self.numpy_inputs: dict = {}
    if queue_factory is not None:
      self.input_queues, self.numpy_inputs = queue_factory(self)
      if not self.numpy_inputs:
        raise RemoteModelError('queue_factory 未提供 numpy_inputs')
    else:
      self._build_queues()
      if self._prime:
        self._prime_warp()

    self._desire_key = next((key for key in self.numpy_inputs if key.startswith('desire')), 'desire')
    self.prev_desire = np.zeros(int(self.constants.DESIRE_LEN), dtype=np.float32)
    self._remote_active = False
    self._need_reset = True
    self._frame = (0, 0)
    self._frame_context: dict | None = None

    # -- 重叠流水线状态（全部由主线程持有/更新，worker 只读 job 内的副本） ------
    self._pipeline_lock = threading.Lock()
    self._jobs: queue.Queue = queue.Queue()
    self._handles: deque = deque()          # worker 已 submit、待主线程 collect 的 handle
    self._contexts: dict[int, dict] = {}    # frame_id -> 该帧发布上下文（主线程读写）
    self._idle = threading.Event()
    self._idle.set()
    self._stop = threading.Event()
    self._worker: threading.Thread | None = None
    self._worker_error: BaseException | None = None
    # worker 当前正在处理的 job 的起始时刻（monotonic）；0 = 空闲。
    # 只用于识别「卡在 bulk ffs 写里再也回不来」的 worker。
    self._worker_job_since = 0.0
    self._generation = 0                    # 每次流水线重置 +1；worker 据此丢弃过期 job
    self._cold_frames = 0                   # 重连后的冷启动宽限帧数（worker 读/写）
    self._last_pub_frame = -1
    self._output_context: dict | None = None
    # 客户端成功握手次数的本地副本：变化即代表「链路恢复过」
    self._seen_recovery_epoch = 0

  # -- 工件/队列加载 ----------------------------------------------------------

  def _load_artifact(self) -> dict:
    """读取 warp-only 工件（分块 + out-of-band 反序列化），返回 jits 字典。"""
    from openpilot.common.file_chunker import open_file_chunked
    from openpilot.selfdrive.modeld.helpers import load_oob
    with open_file_chunked(self._warp_path) as f:
      return load_oob(f)

  def _load_contract_metadata(self) -> dict:
    """回退路径：读取契约 JSON 并还原 slice/tuple 类型。"""
    with open(self._contract_path, encoding='utf-8') as f:
      raw = json.load(f)
    metadata = _decode_json_metadata(_unwrap_metadata(raw))
    if not _metadata_complete(metadata):
      raise RemoteModelError(f'契约 metadata 不完整: {self._contract_path}')
    return metadata

  def _build_queues(self) -> None:
    """只建 warp 侧队列 + 标量 numpy 视图；绝不建 policy 侧大队列。"""
    from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
    from openpilot.selfdrive.modeld.compile_modeld import nv12_copy_size
    from openpilot.sunnypilot.modeld_v2.compile_modeld import make_warp_queues

    nv12_info = get_nv12_info(self.cam_w, self.cam_h)
    self.frame_copy_size = nv12_copy_size(*nv12_info[:3])
    self.frame_buf_params = dict.fromkeys(self._vision_input_names, nv12_info)

    queues, npy = make_warp_queues(device=self.WARP_DEV)
    for name, shape in _scalar_shapes(self.metadata).items():
      npy[name] = np.zeros(shape, dtype=np.float32)
    self.input_queues, self.numpy_inputs = queues, npy

  def _prime_warp(self) -> None:
    """用零帧预跑一次 warp，避免首帧承担编译/分配延迟。"""
    from tinygrad.tensor import Tensor

    self.full_frames = {
      key: Tensor(np.zeros(self.frame_buf_params[key][3], dtype=np.uint8), device=self.WARP_DEV).contiguous().realize()
      for key in self._vision_input_names
    }
    self.warp_jit(**{k: self.input_queues[k] for k in WARP_INPUTS},
                  frame=self.full_frames[self._road_key], big_frame=self.full_frames[self._wide_key])
    self.full_frames.clear()
    self._blob_cache.clear()

  # -- 鸭子接口 ---------------------------------------------------------------

  @property
  def vision_input_names(self) -> list[str]:
    return self._vision_input_names

  @property
  def desire_key(self) -> str:
    return self._desire_key

  @property
  def chestnut(self) -> bool:
    """本帧输出确实由远端产生时为 True；否则（含降级）为 False。"""
    return self._remote_active

  @property
  def mlsim(self) -> bool:
    return bool(self.generation is not None and self.generation >= 11)

  @property
  def available(self) -> bool:
    return self.client is not None and bool(getattr(self.client, 'connected', False))

  @property
  def output_context(self) -> dict | None:
    """`run()` 最近一次返回的远端结果**所属帧**的上下文；无远端结果时为 None。

    上下文字段由调用方（JoinedModel/modeld）在 `set_frame` 时注入，此处只保证
    `frame_id` / `timestamp_eof` 与本帧 job 的绑定一致。
    """
    return self._output_context

  def warmup(self) -> None:
    """只复位本地视图与流水线，不发起网络请求。"""
    for buf in self.numpy_inputs.values():
      buf[:] = 0
    self.prev_desire[:] = 0
    self.full_frames.clear()
    self._blob_cache.clear()
    self._remote_active = False
    self.reset_pipeline()

  def set_frame(self, frame_id: int, timestamp_eof: int, context: dict | None = None) -> None:
    """由调用方注入本帧时序与发布上下文（禁止用 N+1 的上下文发布 N 的输出）。"""
    self._frame = (int(frame_id), int(timestamp_eof))
    self._frame_context = dict(context) if isinstance(context, dict) else None

  def arm_reset(self) -> None:
    """下一次上行带 FLAG_RESET_TEMPORAL，让服务端清空 history。"""
    self._need_reset = True
    self._cold_frames = _PIPELINE_COLD_FRAMES

  def _sync_link_recovery(self) -> None:
    """检测「链路刚刚恢复」，据此重置时序状态并放宽冷启动 deadline。

    客户端每成功握手一次 `recovery_epoch` +1（首次握手不算）。断链恢复后服务端
    的 history 是全新的，必须让下一帧带 FLAG_RESET_TEMPORAL；同时给前几帧放宽
    deadline（新链路的 TCP/ffs 慢启动在真机上首帧可达 ~1.1s）。
    """
    epoch = int(getattr(self.client, 'recovery_epoch', 0) or 0)
    if epoch == self._seen_recovery_epoch:
      return
    first = self._seen_recovery_epoch == 0
    self._seen_recovery_epoch = epoch
    if first:
      return  # 首次握手：那不是「恢复」，保持既有 arm_reset 语义
    self._need_reset = True
    self._cold_frames = _PIPELINE_COLD_FRAMES
    self._abort_pipeline(arm_reset=False)
    log_event('link_recovered', recovery_epoch=epoch)

  def arm_reconnect(self, armed: bool = True, *, immediate: bool = False) -> None:
    """透传：武装/解除链路层的后台重连监督器（设置页 off→on 走 immediate=True）。"""
    arm = getattr(self.client, 'arm_reconnect', None)
    if callable(arm):
      arm(bool(armed), immediate=bool(immediate))

  def request_reconnect(self) -> None:
    """透传：请求链路层尽快重连一次。"""
    request = getattr(self.client, 'request_reconnect', None)
    if callable(request):
      request()

  def reconnect(self) -> None:
    """请求链路恢复。

    有后台重连监督器时**只发信号、不阻塞** —— modeld 主线程是 SCHED_FIFO 实时线程，
    在这里做 connect + HELLO 可能阻塞数秒（bulk 端点在主机消失时甚至会阻塞在 ffs
    写队列里不返回），会把整个控制循环连同相机一起卡死。无监督器时才退化为同步
    重连（`client.reconnect()`：**先彻底拆掉旧链路**再 connect + 重新 HELLO，
    绝不因为「socket 句柄还在」而空转）。
    """
    client = self.client
    if client is None:
      raise RemoteModelError('远端客户端不存在')
    request = getattr(client, 'request_reconnect', None)
    if callable(request):
      request()
      return
    client.reconnect()
    self.arm_reset()
    if self.status is not None:
      self.status.set_connected(bool(getattr(client, 'connected', False)))

  def close(self) -> None:
    """停止 worker、丢弃在途请求并关闭链路；幂等。"""
    self._stop.set()
    try:
      self._jobs.put_nowait(None)
    except queue.Full:
      pass
    worker, self._worker = self._worker, None
    if worker is not None and worker.is_alive() and worker is not threading.current_thread():
      worker.join(timeout=0.5)
    self._abort_pipeline(arm_reset=False)
    try:
      self.client.close()
    except Exception:
      pass

  # -- 流水线控制（供测试/台架/JoinedModel 使用） ------------------------------

  def wait_idle(self, timeout: float = 1.0) -> bool:
    """等待 worker 处理完当前排队的 job（含 submit 完成）。仅用于测试/台架。"""
    return self._idle.wait(max(0.0, float(timeout)))

  def abort_pipeline(self) -> None:
    """丢弃全部在途 handle 与排队 job，使下一次上行重新带 reset。"""
    self._abort_pipeline()

  def reset_pipeline(self) -> None:
    """清空流水线计数与上下文（warmup/换入前调用）。"""
    self._abort_pipeline(arm_reset=False)
    with self._pipeline_lock:
      self._last_pub_frame = -1
      self._output_context = None
    self._frame_context = None

  # -- 帧路径 -----------------------------------------------------------------

  def _frame_tensor(self, key: str, buf) -> Any:
    """把 VisionBuf 包成 tinygrad uint8 blob（按指针缓存，零拷贝）。"""
    from tinygrad.tensor import Tensor

    ptr = np.frombuffer(buf.data, dtype=np.uint8).ctypes.data
    cache_key = (key, ptr)
    if cache_key not in self._blob_cache:
      self._blob_cache[cache_key] = Tensor.from_blob(ptr, (self.frame_buf_params[key][3],),
                                                     dtype='uint8', device=self.WARP_DEV)
    return self._blob_cache[cache_key]

  def _frame_binding(self, bufs: dict) -> tuple[int, int]:
    """本帧时序：优先注入值，否则从 VisionBuf.frame_id 推导（ts 缺省 0）。"""
    if self._frame != (0, 0):
      return self._frame
    for buf in bufs.values():
      frame_id = getattr(buf, 'frame_id', None)
      if frame_id is not None:
        return int(frame_id), int(getattr(buf, 'timestamp_eof', 0) or 0)
    return 0, 0

  def _parse_outputs(self, raw: list[np.ndarray]) -> dict[str, np.ndarray]:
    if not raw:
      raise RemoteModelError('远端响应没有 raw_outputs')
    model_output = np.asarray(raw[0]).flatten()
    if not np.all(np.isfinite(model_output)):
      raise RemoteModelError('远端输出包含非有限值')

    sliced = {k: model_output[np.newaxis, v] for k, v in self.vision_output_slices.items()}
    outputs = self.parser.parse_outputs(sliced)

    if 'desired_curvature' in outputs and 'prev_desired_curv' in self.numpy_inputs:
      buf = self.numpy_inputs['prev_desired_curv']
      buf[0, :-1] = buf[0, 1:]
      buf[0, -1, :] = outputs['desired_curvature'][0, :] if not self.mlsim else 0
    return outputs

  # -- 帧路径（重叠流水线） ----------------------------------------------------

  def _build_uplink(self, inputs: dict) -> dict[str, np.ndarray]:
    """上行标量的**可写副本**：worker 稍后发送时不能被下一帧覆盖。

    一律从已按契约形状规整过的本地视图取（`_build_job` 负责写入），
    这样调用方传入的 `(2,)` 不会污染契约要求的 `(1, 2)`。
    """
    tensors: dict[str, np.ndarray] = {}
    for key in ('desire', *REMOTE_UPLINK_SCALARS):
      if key not in self.numpy_inputs:
        continue
      tensors[key] = np.array(self.numpy_inputs[key], dtype=np.float32, copy=True)
    return tensors

  def _build_job(self, bufs: dict, transforms: dict[str, np.ndarray],
                 inputs: dict[str, np.ndarray], frame_id: int, ts_eof: int, flags: int) -> dict:
    """主线程侧：更新标量/脉冲视图、做 QCOM warp（GPU 单线程）、打包待发送 job。

    QCOM warp 与 tinygrad 只能用主线程做（GPU 上下文非线程安全）；后台 worker
    只负责网络 submit，于是主线程 `collect(上一帧)` 与服务端执行本帧在时间上重叠。
    """
    desire_key = self.desire_key
    desire_in = inputs[desire_key]
    desire_in[0] = 0
    self.numpy_inputs[desire_key][:] = np.where(desire_in - self.prev_desire > .99, desire_in, 0)
    self.prev_desire[:] = desire_in

    for key in REMOTE_SCALAR_INPUTS:
      if key in self.numpy_inputs and key in inputs:
        self.numpy_inputs[key][:] = np.asarray(inputs[key], dtype=np.float32).reshape(self.numpy_inputs[key].shape)

    for name, source in (('tfm', self._road_key), ('big_tfm', self._wide_key)):
      if name in self.numpy_inputs and source in transforms:
        self.numpy_inputs[name][:, :] = np.asarray(transforms[source], dtype=np.float32).reshape(3, 3)

    frames = {key: self._frame_tensor(key, buf) for key, buf in bufs.items()}
    warped = self.warp_jit(**{k: self.input_queues[k] for k in WARP_INPUTS},
                           frame=frames[self._road_key], big_frame=frames[self._wide_key])
    # GPU 只允许主线程触碰：tinygrad/QCOM 的 `.numpy()` 在 QCOM 上是**零拷贝视图**
    # （实测 owndata=False，底层直指设备显存，见设备取证），`astype(copy=False)` /
    # `ascontiguousarray()` 都不会真正复制。若把这种视图交给 worker，worker 发送时
    # 就会去读显存，与主线程下一帧的 warp 并发写同一块 buffer（数据竞争 + 违反
    # 「GPU 只在主线程」）。这里强制复制到**私有 CPU 缓冲**再交给 worker。
    warped_np = np.array(_to_numpy(warped), dtype=np.uint8, order='C', copy=True)
    assert warped_np.flags.owndata and warped_np.base is None, \
      'warp 输出必须复制为私有 CPU 缓冲后才能跨线程交给 worker'
    uplink = self._build_uplink(inputs)
    return {
      'frame_id': int(frame_id),
      'ts_eof': int(ts_eof),
      'flags': int(flags),
      'tensors': {'warped': warped_np, **uplink},
      'context': dict(self._frame_context) if self._frame_context else {},
      'generation': self._generation,
    }

  def run(self, bufs: dict, transforms: dict[str, np.ndarray], inputs: dict[str, np.ndarray],
          after_enqueue: Callable[[], None] | None = None) -> dict[str, np.ndarray] | None:
    """本帧**先 warp+submit**，再取**上一帧已就绪**的远端结果返回。

    顺序很关键：先在主线程做本帧 warp 并交给 worker 发送，服务端算 policy(N)
    就与本帧随后 `collect(N-1)` 的等待在时间上重叠；若反过来先 collect 再 warp，
    服务端算完才轮到 warp，流水线退化成 `(RTT + warp)`/帧（真机实测 ~74ms/帧，
    与串行基线持平，重叠收益为零）。

    没有任何可发布的旧结果时返回 None（调用方同帧跑 small）。
    单帧 `RemoteTimeout` 被吞掉并返回 None：链路仍健康，只丢这一帧结果，不做
    链路级退避、不 abort 已在途的当前帧，下一帧继续流水线。
    """
    self._surface_worker_error()
    frame_id, ts_eof = self._frame_binding(bufs)
    self._ensure_worker()

    # 链路恢复检测（必须在 submit 之前）：重连后要重置时序状态并清掉旧流水线。
    self._sync_link_recovery()

    # 1) 主线程独占 GPU 做本帧 warp，并立刻交给 worker submit（服务端尽早开工，
    #    与第 2 步的等待重叠）。
    #
    # 链路不可用（正在退避重连）时**跳过 warp 与 submit**：既省掉一次无用的 GPU
    # warp，也保证不会在「服务端已不认这条连接」的链路上持续发推理而只换来 code 4。
    # 仍然继续走第 2 步收取上一帧 handle，让既有在途请求的失败原因照常冒泡。
    if self.client.connected:
      self._submit_frame(bufs, transforms, inputs, frame_id, ts_eof)
      if after_enqueue is not None:
        after_enqueue()
    else:
      request = getattr(self.client, 'request_reconnect', None)
      if callable(request):
        try:
          request()
        except Exception:
          pass
      log_event('link_down_skip', frame=int(frame_id))

    # 2) 再取上一帧结果：它的请求早在上一帧就上线，等待里不含本帧 warp。
    handle = self._take_handle(int(frame_id), wait=_PIPELINE_TAKE_WAIT_S)
    if handle is None:
      self._remote_active = False
      if self.status is not None:
        self.status.set_big_active(False)
      self._last_pub_frame = max(self._last_pub_frame, int(frame_id))
      self._output_context = None
      return None

    try:
      raw = self.client.collect(handle)
    except RemoteTimeout:
      # 单帧超时：客户端保留链路并丢弃迟到响应，这里只丢这一帧结果，
      # 让调用方同帧降级；绝不触发链路级退避。
      log_event('downgrade_small', frame=int(frame_id), reason='remote_timeout')
      self._remote_active = False
      if self.status is not None:
        self.status.set_big_active(False)
      self._last_pub_frame = max(self._last_pub_frame, int(frame_id))
      self._output_context = None
      return None
    except Exception as exc:
      log_event('downgrade_small', frame=int(frame_id), reason=f'pipeline_error: {exc}')
      self._abort_pipeline()  # 链路/协议异常：清空在途与排队，下次上行带 reset
      self._remote_active = False
      if self.status is not None:
        self.status.set_big_active(False)
      self._last_pub_frame = max(self._last_pub_frame, int(frame_id))
      self._output_context = None
      raise

    try:
      outputs = self._parse_outputs(raw)
    except Exception:
      self._abort_pipeline()
      self._remote_active = False
      if self.status is not None:
        self.status.set_big_active(False)
      self._last_pub_frame = max(self._last_pub_frame, int(frame_id))
      self._output_context = None
      raise

    frame = int(handle.frame_id)
    self._last_pub_frame = max(self._last_pub_frame, frame)
    context = self._contexts.pop(frame, None)
    if not isinstance(context, dict):
      context = {}
    # 结果所属帧的时序以提交时快照为准，绝不回落到「当前帧」的时序
    context['frame_id'] = frame
    context['timestamp_eof'] = int(handle.timestamp_eof)
    self._output_context = context
    self._remote_active = True
    if self.status is not None:
      self.status.set_big_active(True)
    return outputs

  def _submit_frame(self, bufs: dict, transforms: dict[str, np.ndarray],
                    inputs: dict[str, np.ndarray], frame_id: int, ts_eof: int) -> None:
    """warp 本帧并把 submit 排给 worker（主线程独占 GPU；worker 只发网络）。"""
    flags = FLAG_RESET_TEMPORAL if self._need_reset else 0
    self._need_reset = False
    job = self._build_job(bufs, transforms, inputs, frame_id, ts_eof, flags)
    with self._pipeline_lock:
      self._contexts[int(frame_id)] = job['context']
      self._idle.clear()
      self._jobs.put(job)

  # -- 后台 worker -------------------------------------------------------------

  def _ensure_worker(self) -> None:
    self._reap_stuck_worker()
    worker = self._worker
    if worker is not None and worker.is_alive():
      return
    self._stop.clear()
    self._worker_job_since = 0.0
    worker = threading.Thread(target=self._worker_main, name='oprp-warp', daemon=True)
    self._worker = worker
    worker.start()

  def _reap_stuck_worker(self) -> None:
    """回收「卡死」的 submit worker：它可能永久阻塞在 bulk 的 ffs 写上。

    bulk 伪 socket 在对端消失时既不报错也不返回（那一帧的 `sendall` 会一直卡在
    满的 ffs 写队列里），于是这个 worker 再也回不来。链路层已经把它判死并重建，
    但线程本身还在 —— 若不换掉，流水线会把新 job 塞进一个永远不会被消费的队列。
    这里按「正在处理的 job 耗时超限且链路已断」判死并换一个线程；旧线程是
    daemon，等它自己醒来说明 fd 已关，届时它会走正常错误路径退出。
    """
    worker = self._worker
    if worker is None or not worker.is_alive() or self._worker_job_since <= 0.0:
      return
    if getattr(self.client, 'connected', False):
      return
    stuck = time.monotonic() - self._worker_job_since
    if stuck < _STUCK_JOB_S:
      return
    log_event('worker_reaped', stuck_s=round(stuck, 1))
    self._worker = None          # 让 _ensure_worker 另起一个
    self._worker_job_since = 0.0
    self._abort_pipeline()

  def _worker_main(self) -> None:
    pin_network_thread()
    while not self._stop.is_set():
      try:
        job = self._jobs.get(timeout=_PIPELINE_POLL_S)
      except queue.Empty:
        with self._pipeline_lock:
          if self._jobs.empty():
            self._idle.set()
        continue
      if job is None or self._stop.is_set():
        break
      self._worker_job_since = time.monotonic()
      try:
        handle = self._process_job(job)
      except Exception as exc:  # 交给主线程在下一帧原样抛出（同帧降级）
        with self._pipeline_lock:
          if self._worker_error is None:
            self._worker_error = exc
        return
      finally:
        self._worker_job_since = 0.0
      if handle is not None:
        with self._pipeline_lock:
          stale = job['generation'] != self._generation or self._stop.is_set()
          if not stale:
            self._handles.append(handle)
        if stale:  # 已被重置：连同 handle 一起丢弃，避免在途请求残留
          self._discard_handle(handle)

  def _process_job(self, job: dict):
    """worker：只做网络 submit（warp 已由主线程完成），因此不触碰 GPU。"""
    if not self._wait_for_slot(job['generation']):
      # 名额等不到（主线程尚未 collect）：本帧放弃发送，流水线不因此死锁
      self._contexts.pop(job['frame_id'], None)
      return None
    with self._pipeline_lock:
      if job['generation'] != self._generation or self._stop.is_set():
        self._contexts.pop(job['frame_id'], None)
        return None
    # 冷启动宽限：新链路的第一帧要等 TCP 慢启动/接收窗口打开（实测首帧发送可
    # 达 ~1.1s），按逐帧 deadline 会必然超时并把链路判死。只对「重连后的第一帧」
    # 放宽，不计入 steady-state 指标，也不改变 deadline_ms 的稳态语义。
    deadline = None
    if self._cold_frames > 0:
      self._cold_frames -= 1
      deadline = time.monotonic() + _PIPELINE_COLD_DEADLINE_S
    try:
      return self.client.submit(job['frame_id'], job['ts_eof'], job['tensors'],
                                flags=job['flags'], deadline=deadline)
    except Exception as exc:
      # 名额竞争属于正常流水线压力：不当作链路故障，只丢本帧
      if self.client.in_flight >= self.client.max_in_flight and 'in flight' in str(exc):
        self._contexts.pop(job['frame_id'], None)
        return None
      raise

  def _wait_for_slot(self, generation: int) -> bool:
    """等待一个在途名额，避免 submit 撞上 max_in_flight 上限；被重置时立即放弃。"""
    deadline = time.monotonic() + _PIPELINE_SLOT_TIMEOUT_S
    while not self._stop.is_set():
      if generation != self._generation:
        return False
      if self.client.in_flight < self.client.max_in_flight:
        return True
      if time.monotonic() >= deadline:
        return False
      time.sleep(0.002)
    return False

  def _take_handle(self, current_frame_id: int, wait: float = 0.0):
    """取最旧的可发布 handle；只接受**严格早于本帧**且不旧于已发布帧的结果。

    `wait` 允许短暂等待 worker 把本帧之前的 handle 入队（流水线刚启动时会有
    一个极短的空窗），避免因调度抖动误判为「无结果」而降级。
    """
    deadline = time.monotonic() + max(0.0, float(wait))
    while True:
      with self._pipeline_lock:
        while self._handles:
          handle = self._handles[0]
          frame = int(handle.frame_id)
          if frame >= int(current_frame_id):
            return None  # 只有本帧/未来帧的 handle：留给下一帧，本轮交由 small 发布
          self._handles.popleft()
          if frame < self._last_pub_frame:
            self._discard_handle(handle)  # 已跳过该帧（stall/追赶），丢弃避免回退
            continue
          return handle
      if time.monotonic() >= deadline:
        return None
      time.sleep(0.001)

  def _surface_worker_error(self) -> None:
    with self._pipeline_lock:
      error = self._worker_error
      self._worker_error = None
    if error is not None:
      self._abort_pipeline()
      raise RemoteModelError(f'远端后台 warp/submit 失败: {error}') from error

  def _discard_handle(self, handle) -> None:
    self._contexts.pop(int(getattr(handle, 'frame_id', -1)), None)
    discard = getattr(self.client, 'discard', None)
    if callable(discard):
      try:
        discard(handle)
      except Exception:
        pass

  def _abort_pipeline(self, arm_reset: bool = True) -> None:
    log_event('pipeline_abort', reset=bool(arm_reset))
    with self._pipeline_lock:
      self._generation += 1
      handles = list(self._handles)
      self._handles.clear()
      self._contexts.clear()
    for handle in handles:
      self._discard_handle(handle)
    while True:
      try:
        self._jobs.get_nowait()
      except queue.Empty:
        break
    if arm_reset:
      self._need_reset = True

  # -- 与原生 ModelState 同构的动作换算（逻辑照抄 modeld_v2.modeld.ModelState） ----
  def get_action_from_model(self, model_output: dict[str, np.ndarray], prev_action,
                            lat_action_t: float, long_action_t: float, v_ego: float):
    from openpilot.cereal import log
    from openpilot.selfdrive.controls.lib.drive_helpers import get_accel_from_plan, smooth_value
    from openpilot.sunnypilot.modeld_v2.constants import Plan
    from openpilot.sunnypilot.modeld_v2.fill_model_msg import get_curvature_from_output

    if 'action' not in model_output:
      plan = model_output['plan'][0]
      desired_accel = get_accel_from_plan(plan[:, Plan.VELOCITY][:, 0], plan[:, Plan.ACCELERATION][:, 0],
                                          self.constants.T_IDXS, action_t=long_action_t)
      curvature_plan = (plan + (self.PLANPLUS_CONTROL - 1.0) * model_output['planplus'][0]
                        if 'planplus' in model_output and self.PLANPLUS_CONTROL != 1.0 else plan)
      desired_curvature = get_curvature_from_output(model_output, curvature_plan, v_ego, lat_action_t, self.mlsim)
    else:
      desired_accel = model_output['action'][0, 1]
      desired_curvature = model_output['action'][0, 0] / (max(1.0, v_ego))**2

    stop = v_ego < 0.3 and desired_accel < 0.1
    desired_accel = smooth_value(desired_accel, prev_action.desiredAcceleration, self.LONG_SMOOTH_SECONDS)

    if self.generation is not None and self.generation >= 10:  # 与原生一致：post FOF 平滑
      if v_ego > self.MIN_LAT_CONTROL_SPEED:
        desired_curvature = smooth_value(desired_curvature, prev_action.desiredCurvature, self.LAT_SMOOTH_SECONDS)
      else:
        desired_curvature = prev_action.desiredCurvature

    return log.ModelDataV2.Action(desiredCurvature=float(desired_curvature),
                                  desiredAcceleration=float(desired_accel), shouldStop=bool(stop))
