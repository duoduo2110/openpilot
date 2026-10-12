"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

「外置大模型加速器」后端门面：门面层 accelerators/__init__.py 的落地实现。

本 lane 只交付客户端层骨架——连接、握手、流水线请求、状态与退避纯函数；
modeld 的 seam（换入/换出、small 重跑、SubMaster 订阅）由后续 lane 接线。
因此这里的 load()/prepare() 是**接口 + 惰性连接**，不会自己跑线程池或订阅。

每个函数在任何设备上都安全：配置缺失回落到关闭，依赖缺失由门面层的
_NoBackend 兜底返回负默认，本模块不抛异常给导入方。
"""
from __future__ import annotations

import threading
import time

from . import config, switching
from .client import RemotePipelineClient
from .model_state import RemoteModelState
from .protocol import CAP_ZLIB_TENSOR
from .status import RemoteStatus

# 本模块能导入即说明依赖（numpy 等）齐全；门面层据此回答 installed()
INSTALLED = True

_LOCK = threading.Lock()
_CLIENT: RemotePipelineClient | None = None
_STATUS = RemoteStatus()
_BACKOFF = switching.Backoff()

# 进度：后续 lane 接 UI 时才需要跨进程，本 lane 只做进程内、限频的状态
PROGRESS_MIN_INTERVAL = 0.25
_PROGRESS: dict | None = None
_LAST_PROGRESS = ('', 0.0)


def installed() -> bool:
  """后端与依赖是否可用（客户端层已随仓库交付，返回 True）。"""
  return INSTALLED


def enabled() -> bool:
  """用户在 accelerator.json 里把开关打开了吗？只读配置，不碰链路。"""
  return config.enabled()


def present() -> bool:
  """是否配置了服务端地址（USB/网络无关的「目标在不在」的弱判定）。

    真正的可达性由 ready()（已握手）回答；present() 必须便宜，UI 会高频轮询。
    """
  try:
    return bool(config.address().strip())
  except Exception:
    return False


def ready() -> bool:
  """大模型现在能跑吗？= 客户端已连接且已完成 HELLO。不发起连接。"""
  with _LOCK:
    client = _CLIENT
  return client is not None and client.connected


def prepare() -> bool:
  """本进程是否应该准备换入：依赖在位、地址可解析。

  **刻意不看 enabled()**：开关改由 `JoinedModel` 运行期动态读取，这样 modeld
  已经跑起来时切换「外置/内置」无需重启进程；`enabled` 只影响是否真正换入，
  不影响是否构造加速器对象。绝不抛异常。
  """
  try:
    if not installed():
      return False
    # 地址解析失败即视为不可用（配置损坏不应把 modeld 拖崩）
    RemotePipelineClient(signature='0' * 64, address=config.address(),
                         deadline_ms=config.deadline_s() * 1000.0,
                         pipeline_depth=config.pipeline_depth(),
                         transport=config.transport())
    return True
  except Exception:
    return False


class Engine:
  """一个已建好的加速器引擎：客户端 + 状态。modeld seam 后续 lane 消费。"""

  def __init__(self, client: RemotePipelineClient, status: RemoteStatus):
    self.client = client
    self.status = status

  @property
  def available(self) -> bool:
    return self.client.connected

  def close(self) -> None:
    try:
      self.client.close()
    except Exception:
      pass
    self.status.set_connected(False)
    self.status.set_big_active(False)


def load(cam_w: int, cam_h: int, small) -> 'RemoteModelState | None':
  """构造远端加速器并加载本地 warp-only 工件，**不要求首次握手成功**。

  分两段：
  1) 「真失败才返回 None」——依赖缺失、client 构造失败、warp 工件缺失或 metadata
     不足以构造队列：这些情况下远端根本没有可跑的工件，modeld 照旧跑纯 small；
  2) **握手是 best-effort**——`connect_and_hello()` 失败只记录状态与错误，
     **仍然返回一个「连通性未知」的远端对象**，由 `JoinedModel` 在运行期按开关 +
     退避周期 `reconnect()`。这样笔记本晚于点火启动、或用户运行中打开开关，都能在
     上车后、挂 D 挡前自动连上，无需重启 modeld。

  small 只用于借用标量/平滑等运行期配置。
  """
  global _CLIENT
  if not installed():
    return None
  cfg = config.load()
  try:
    client = RemotePipelineClient(
      signature=str(cfg.get('signature') or '0' * 64),
      address=str(cfg['address']),
      deadline_ms=float(cfg['deadline_ms']),
      pipeline_depth=int(cfg['pipeline_depth']),
      # 传输模式来自 accelerator.json（默认 tcp）；非法值由 client 回落 tcp。
      transport=str(cfg.get('transport') or 'tcp'),
      # 载荷压缩默认关闭（配置 payload_compression）；打开时在 HELLO 声明能力位，
      # 仍需服务端回同一位才真正压缩，老服务端自动退回未压缩格式。
      capabilities=CAP_ZLIB_TENSOR if cfg.get('payload_compression') else 0,
      # 与 RemoteModelState 共用同一个状态对象，计数/分位/big_active 落在同一处，
      # 供 status_writer 统一落盘给 UI。
      status=_STATUS,
    )
  except Exception as e:
    _STATUS.record_failure(f'load failed: {e}')
    return None

  try:
    # 先加载 warp 工件（C3 上需数秒），完成后再握手：服务端约 4s 空闲即踢连接，
    # 若先握手会在加载期间被断开。
    remote = RemoteModelState(
      cam_w=cam_w, cam_h=cam_h, client=client,
      warp_path=cfg.get('warp_path'), contract_path=cfg.get('contract_path'),
      small=small, status=_STATUS,
    )
    remote.warmup()
  except Exception as e:
    # 没有可用工件/队列：无法构造远端，退回纯 small（这是唯一会 return None 的段）
    try:
      client.close()
    except Exception:
      pass
    _STATUS.record_failure(f'load failed: {e}')
    return None

  # 首次连接**不在这里做**：modeld 主线程是 SCHED_FIFO 实时线程，绝不能做可能无限
  # 阻塞的网络写（bulk 端点在主机消失时会阻塞在 ffs 写队列里，把整个控制循环连同
  # 相机一起卡死 —— 真机实测主线程 wchan 卡在 ffs_epfile_io，状态文件直接停更）。
  # 改为武装链路层的后台重连监督器，由它那条普通优先级守护线程去 connect + HELLO；
  # 连不上就按 1s→2s→…→30s 退避持续重试。
  # 武装/解除的最终裁决仍在 `JoinedModel`（按**运行期开关**每秒重读 accelerator.json），
  # 因此设置页 off→on 会立刻重新武装并触发一次重连（不等退避）。
  try:
    arm = getattr(client, 'arm_reconnect', None)
    if callable(arm):
      arm(True, immediate=True)
    else:  # 无监督器的老客户端：退化为同步握手
      client.connect_and_hello()
      _STATUS.set_connected(True)
  except Exception as e:
    _STATUS.record_failure(f'initial connect failed: {e}')

  with _LOCK:
    _CLIENT = client
  return remote


def unavailable_reason() -> str | None:
  """用户开了开关却连不上时的原因，供 offroad 告警；未开启返回 None。"""
  if not enabled():
    return None
  if not installed():
    return 'accelerator client unavailable'
  with _LOCK:
    client = _CLIENT
  if client is None:
    return 'accelerator not connected'
  if not client.connected:
    return 'accelerator link down'
  return None


def status_snapshot() -> dict:
  """给 UI / swaglog 读的状态快照。"""
  return _STATUS.to_dict()


# -- 状态机骨架（纯函数 + 一个退避计数器；订阅与线程在后续 lane） ----------------

def window_open(engaged: bool, age: float) -> bool:
  """sc/sh/cc 全部 disengaged 且快照新鲜时才允许换入/重连。"""
  return switching.window_open(engaged, age)


def note_link_failure(held_seconds: float | None) -> float:
  """记录一次连接失败并推进指数退避，返回下次尝试间隔秒。"""
  delay = _BACKOFF.on_failure(held_seconds)
  _STATUS.record_failure('link lost')
  return delay


def backoff_seconds() -> float:
  return _BACKOFF.delay


def big_model_state() -> str:
  """modelDataV2SP.acceleratorState 的骨架取值。"""
  with _LOCK:
    client = _CLIENT
  return switching.big_model_state(
    stopped=client is None and not enabled(),
    active_is_big=_STATUS.big_active,
    available=ready(),
    failures=_BACKOFF.failures,
  )


def progress() -> dict | None:
  """{stage, frac, msg} 或 None；只读进程内状态，绝不抛异常。"""
  value = _PROGRESS
  return value if isinstance(value, dict) else None


def report_progress(stage: str, frac: float, msg: str = '') -> None:
  """上报进度；同一 stage 内限频 4Hz，结束（frac>=1）总是放行。绝不抛异常。"""
  global _PROGRESS, _LAST_PROGRESS
  last_stage, last_at = _LAST_PROGRESS
  now = time.monotonic()
  if frac < 1.0 and stage == last_stage and now - last_at < PROGRESS_MIN_INTERVAL:
    return
  _LAST_PROGRESS = (stage, now)
  _PROGRESS = {'stage': stage, 'frac': round(float(frac), 4), 'msg': msg}


def clear_progress() -> None:
  global _PROGRESS
  _PROGRESS = None


def shutdown(reason: str = '', timeout: float = 25.0) -> None:
  """设备要下电：在 `timeout` 内断开链路（放线程里做，超时即放弃）。"""
  global _CLIENT
  with _LOCK:
    client, _CLIENT = _CLIENT, None
  if client is None:
    return

  def request():
    try:
      client.close()
    except Exception:
      pass

  t = threading.Thread(target=request, name='accelerator-shutdown', daemon=True)
  t.start()
  t.join(timeout)
  _STATUS.set_connected(False)
  _STATUS.set_big_active(False)
