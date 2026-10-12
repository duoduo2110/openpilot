"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

「外置大模型」的换入包装：把远端 `RemoteModelState` 与本地原生小模型
`ModelState` 合成一个对 modeld 暴露同一鸭子接口的 `JoinedModel`。

换入/降级语义（与 switching.py 的纯函数一致）
--------------------------------------------
* 只有「可切换窗口」开启（三路 engaged 全灭且快照新鲜）且远端可用时，才从
  小模型换入远端；换入后远端持续供数，不因车辆 engaged 而换出。
* **远端失败必须同帧降级**：`remote.run()` 抛出任何异常（超时/链路/协议/
  非有限输出）都被吞掉，随即用 `small.run(...)` 重跑**同一帧**并返回其结果；
  绝不返回 None 造成控制空白，也绝不把异常冒泡到 modeld 的永久 latch 路径。
* **重叠流水线**：远端 `run()` 是异步的，返回的是**上一帧**已就绪的结果，
  没有旧结果时返回 None —— 此时同样同帧跑 small 并发布，流水线继续追赶。
* 失败按 switching.Backoff 退避：5s 起、翻倍、上限 60s；稳定运行 60s 后
  再次失败复位为 1s 快速重试。退避窗口内不再尝试换入/重连。
* `modelV2.big` 语义由 `.chestnut` 表达：**本帧发布的输出**确实由远端产生
  为 True，否则 False（首帧/追赶帧走 small，因此为 False）。
* `output_context` 暴露**本帧返回结果所属帧**的发布上下文（远端结果即为该
  远端帧的 frame_id/timestamp_eof/action_t/v_ego 等）；small 降级时为 None，
  由 modeld 沿用当前帧上下文。绝不允许 N 输出绑 N+1 或更旧的上下文。

调用方（modeld）在每帧 run 之前调用 `update_runtime(sm, frame_id, ts_eof,
context=...)` 注入门控快照、本帧时序与发布上下文；未注入时保守判为 engaged、
快照过期 → 永远走小模型。

运行期开关（enabled）
--------------------
`JoinedModel` **不要求构造时已连接**，也不要求构造时 `enabled=True`：
每帧（≤`ENABLED_REFRESH_SECONDS` 秒缓存）重读 `accelerator.json` 的 `enabled`，
据此决定是否换入/是否继续用远端。于是：

* 笔记本晚于点火启动 → 未连接也照常发布 small，并按退避周期（5s 起、上限 60s）
  反复 `reconnect()`，挂 D 挡前连上即换入；
* 运行中把开关由 false 改 true → 在退避窗口内自动换入；
* 运行中把开关由 true 改 false → **同帧降级**回 small 并丢弃远端流水线，
  发布不中断。
"""
from __future__ import annotations

import time
from collections.abc import Callable

from openpilot.sunnypilot.accelerators.remote import switching
from openpilot.sunnypilot.accelerators.remote.debug_log import log_event

# 运行期开关（accelerator.json 的 enabled）重读间隔（秒）：UI 低频改动配置，
# 每帧读盘会拖慢实时循环，这里最多 1s 读一次。
ENABLED_REFRESH_SECONDS = 1.0


def _load_enabled() -> bool:
  """默认开关来源：accelerator.json 的 enabled。

  `config.enabled()` 自身绝不抛异常（文件缺失/损坏回落 `enabled=False`），因此
  读失败时最坏情况是退回本地 small，方向安全。
  """
  try:
    from openpilot.sunnypilot.accelerators.remote import config
    return bool(config.enabled())
  except Exception:
    return True


class _Once:
  """把 after_enqueue 包成「一帧最多触发一次」，保证降级重跑不会重复发送。"""

  def __init__(self, callback: Callable[[], None] | None):
    self._callback = callback
    self._fired = False

  def __call__(self) -> None:
    if self._fired or self._callback is None:
      return
    self._fired = True
    self._callback()


class JoinedModel:
  """小模型常驻 + 远端按窗口换入的合成 ModelState。"""

  def __init__(self, remote, small, *, status=None, clock: Callable[[], float] = time.monotonic,
               max_age: float = switching.ENGAGEMENT_MAX_AGE, backoff: switching.Backoff | None = None,
               status_writer=None, enabled_loader: Callable[[], bool] | None = None,
               enabled_refresh: float = ENABLED_REFRESH_SECONDS):
    self.remote = remote
    self.small = small
    self.status = status
    self._clock = clock
    self._max_age = max_age
    self._backoff = backoff or switching.Backoff()
    # 状态落盘器（可选）：每帧调用但内部按时间节流，不把 IO 放进关键路径
    self._status_writer = status_writer
    # 运行期开关：默认读 accelerator.json 的 enabled，按 enabled_refresh 秒缓存
    self._enabled_loader = enabled_loader or _load_enabled
    self._enabled_refresh = max(0.0, float(enabled_refresh))
    self._enabled_at: float | None = None
    self._enabled_cached = True
    # 链路层后台重连监督器的武装态；None = 尚未同步过（首帧必然下发一次真实状态）
    self._link_armed: bool | None = None

    self._using_remote = False
    self._joined = False
    self._connected_since: float | None = None
    self._retry_at = 0.0
    self._frame = (0, 0)
    self._context: dict | None = None
    self._output_context: dict | None = None
    self._last_pub_frame_id = -1
    self._engaged = True
    self._age = float('inf')
    self.last_error: str | None = None

  # -- 门控与运行期注入 --------------------------------------------------------

  def update_runtime(self, sm, frame_id: int, timestamp_eof: int,
                     context: dict | None = None) -> None:
    """注入本帧时序、发布上下文与 engaged 快照；读取异常时保守判为「不可换入」。"""
    self._frame = (int(frame_id), int(timestamp_eof))
    self._context = dict(context) if isinstance(context, dict) else None
    engaged, age = True, float('inf')
    try:
      sds = sm['selfdriveState']
      cc = sm['carControl']
      valid = bool(sm.valid['selfdriveState'] and sm.valid['carControl'])
      engaged = switching.engagement_from(bool(sds.enabled), bool(cc.latActive), bool(cc.longActive), valid)
      if sm.seen['selfdriveState']:
        age = self._clock() - float(sm.recv_time['selfdriveState'])
    except Exception:
      engaged, age = True, float('inf')
    self._engaged, self._age = engaged, age

  def _remote_enabled(self, now: float) -> bool:
    """运行期开关：按 `enabled_refresh` 秒缓存重读 accelerator.json 的 enabled。

    不在每帧路径读盘；读取异常时保守保持上一次的判定（`_enabled_loader` 自身已
    保证不抛，这里再兜一层，绝不把异常带进实时循环）。

    off→on 的**上升沿**会立刻重新武装链路层的重连监督器并触发一次重连（不等退避），
    这是「设置页切一下就能恢复」的实现点。
    """
    if self._enabled_at is not None and now - self._enabled_at < self._enabled_refresh:
      return self._enabled_cached
    try:
      value = bool(self._enabled_loader())
    except Exception:
      value = self._enabled_cached
    previous, self._enabled_cached = self._enabled_cached, value
    self._enabled_at = now
    if value and not previous:
      self._on_enabled_rising(now)
    return value

  def _on_enabled_rising(self, now: float) -> None:
    """设置页 off→on：**立即**重新武装并触发一次重连（不等退避窗口）。"""
    log_event('enabled_rising')
    self._backoff.reset()
    self._retry_at = 0.0
    self._joined = False
    self._using_remote = False
    arm = getattr(self.remote, 'arm_reconnect', None)
    if callable(arm):
      try:
        arm(True, immediate=True)
      except Exception:
        pass
    else:
      request = getattr(self.remote, 'request_reconnect', None)
      if callable(request):
        try:
          request()
        except Exception:
          pass

  def _sync_link_armed(self, enabled: bool) -> None:
    """把运行期开关同步到链路层的后台重连监督器（仅在状态变化时动作）。"""
    if enabled == self._link_armed:
      return
    self._link_armed = enabled
    arm = getattr(self.remote, 'arm_reconnect', None)
    if callable(arm):
      try:
        arm(bool(enabled), immediate=bool(enabled))
      except Exception:
        pass

  def _window_open(self) -> bool:
    return switching.window_open(self._engaged, self._age, self._max_age)

  def _publish_status(self) -> None:
    """把当前链路状态交给落盘器（内部节流）；任何异常都不得影响实时循环。"""
    writer = self._status_writer
    if writer is None:
      return
    try:
      writer.maybe_write(status=self.status, backoff=self._backoff,
                         ready=self.connected, big_active=self._using_remote,
                         last_error=self._display_error(),
                         **self._link_publish_kwargs())
    except Exception:
      pass

  # 「用户关了开关」不是链路错误，不能拿它盖住链路层真正的失败原因
  # （例如 bulk 端点卡死时链路层给出的可执行提示）。
  _BENIGN_ERROR = 'accelerator disabled by config'

  def _display_error(self) -> str | None:
    if self.last_error != self._BENIGN_ERROR:
      return self.last_error
    client_error = getattr(self.status, 'last_error', None)
    return client_error if isinstance(client_error, str) and client_error else self.last_error

  def _link_publish_kwargs(self) -> dict:
    """链路层重连遥测（供状态落盘器显示「重连中(退避 Ns)」/「已恢复」）。"""
    reconnect = getattr(self.remote, 'client', None)
    armed = getattr(reconnect, 'reconnect_armed', None)
    if armed is None:
      return {}
    try:
      return {
        'reconnect_armed': bool(armed),
        'reconnect_backoff_s': float(getattr(reconnect, 'reconnect_backoff_s', 0.0) or 0.0),
        'reconnect_attempts': int(getattr(reconnect, 'reconnect_attempts', 0) or 0),
        'recovered_at': float(getattr(self.status, 'recovered_at', 0.0) or 0.0),
      }
    except Exception:
      return {}

  def _mark_published(self, frame_id: int) -> None:
    """记录本帧已发布输出的帧号（small=当前帧，remote=结果所属帧）。"""
    try:
      self._last_pub_frame_id = max(self._last_pub_frame_id, int(frame_id))
    except (TypeError, ValueError):
      pass

  def _run_small(self, bufs, transforms, inputs, after_enqueue: Callable[[], None] | None):
    """同帧降级/首帧路径：发布 small 结果，上下文沿用当前帧（None）。

    无论此前是否在远端供数，本帧发布的都不是远端结果，因此 `chestnut` 必须为
    False（`_joined` 独立保留，流水线不会被误判为换出）。
    """
    self._output_context = None
    self._using_remote = False
    self._set_status(False)
    self._mark_published(self._frame[0])
    return self.small.run(bufs, transforms, inputs, after_enqueue)

  def _set_status(self, active: bool) -> None:
    if self.status is not None:
      try:
        self.status.set_big_active(active)
      except Exception:
        pass

  # -- 换入/降级状态机 ---------------------------------------------------------

  def _maybe_join(self, now: float) -> bool:
    """换入判定：**必须仍在既有安全窗口内**（三路 disengaged 且快照新鲜）。

    这里是「远端重新接入」的唯一授权点 —— 链路层的后台重连监督器只负责把 TCP/ffs
    连通并重新 HELLO，绝不触碰换入决策。因此行车中即使链路自己恢复了，本函数也会
    因窗口关闭而返回 False，继续用本地小模型；直到挂挡/巡航关闭后才重新换入。
    """
    if not self._window_open() or now < self._retry_at:
      return False
    if not getattr(self.remote, 'available', False):
      # 链路不可用：请链路层监督器尽快重连（它有独立的 1s→2s→…→30s 退避）。
      # 这里**不**同步做 connect —— 那会在 modeld 的实时线程里阻塞数秒。
      request = getattr(self.remote, 'request_reconnect', None)
      if callable(request):
        try:
          request()
        except Exception:
          pass
        return False
      # 无监督器的老实现：退回同步重连（失败计入退避）
      try:
        self.remote.reconnect()
      except Exception as exc:  # 重连失败同样计入退避
        self._note_failure(exc, now)
        return False
      if not getattr(self.remote, 'available', False):
        return False
    self.remote.arm_reset()
    return True

  def _drop_remote(self) -> None:
    """停止使用远端并丢弃在途流水线（运行期关闭开关时调用）。

    只影响「接下来用谁供数」，不改发布：调用方随后同帧跑 small 并发布，
    因此不会造成控制空白。绝不抛异常。
    """
    self._joined = False
    self._using_remote = False
    self._connected_since = None
    self._output_context = None
    log_event('remote_disabled')
    abort = getattr(self.remote, 'abort_pipeline', None)
    if callable(abort):
      try:
        abort()
      except Exception:
        pass

  def _note_failure(self, exc: Exception, now: float) -> None:
    held = (now - self._connected_since) if self._connected_since is not None else None
    delay = self._backoff.on_failure(held)
    self._retry_at = now + delay
    self._using_remote = False
    self._joined = False
    self._connected_since = None
    self.last_error = str(exc)
    log_event('link_failure', backoff_s=delay, reason=str(exc))
    self._set_status(False)

  def _on_remote_success(self, now: float) -> None:
    if not self._using_remote:
      self._using_remote = True
      self._connected_since = now
    self._set_status(True)

  # -- 鸭子接口 ---------------------------------------------------------------

  @property
  def chestnut(self) -> bool:
    """本帧发布的结果是否由远端大模型产生；用于 modelV2.big 与 chestnutState。"""
    return self._using_remote

  @property
  def using_remote(self) -> bool:
    return self._using_remote

  @property
  def output_context(self) -> dict | None:
    """`run()` 返回结果所属帧的发布上下文；small 降级/首帧时为 None。"""
    return self._output_context

  @property
  def pipeline_active(self) -> bool:
    """是否已换入远端（正在流水线供数，可能尚在等待首个结果）。"""
    return self._joined

  @property
  def connected(self) -> bool:
    return bool(getattr(self.remote, 'available', False))

  @property
  def constants(self):
    return self.small.constants

  @property
  def desire_key(self) -> str:
    return self.small.desire_key

  @property
  def numpy_inputs(self):
    """小模型 ∪ 远端的输入视图：modeld 依赖它决定要构造哪些外部输入。

    远端契约可能要求小模型没有的标量（例如 `action_t`）；只看小模型会让
    modeld 少构造该键，上行就会缺张量被服务端拒绝。
    """
    small_inputs = self.small.numpy_inputs
    remote_inputs = getattr(self.remote, 'numpy_inputs', None)
    if not isinstance(remote_inputs, dict):
      return small_inputs
    merged = dict(remote_inputs)
    merged.update(small_inputs)
    return merged

  @property
  def vision_input_names(self):
    return self.small.vision_input_names

  @property
  def generation(self):
    return getattr(self.small, 'generation', None)

  @property
  def mlsim(self) -> bool:
    return bool(getattr(self.small, 'mlsim', False))

  @property
  def LAT_SMOOTH_SECONDS(self):
    return self.small.LAT_SMOOTH_SECONDS

  @property
  def LONG_SMOOTH_SECONDS(self):
    return self.small.LONG_SMOOTH_SECONDS

  @property
  def MIN_LAT_CONTROL_SPEED(self):
    return self.small.MIN_LAT_CONTROL_SPEED

  @property
  def lat_delay(self):
    return self.small.lat_delay

  @lat_delay.setter
  def lat_delay(self, value) -> None:
    self.small.lat_delay = value
    if hasattr(self.remote, 'lat_delay'):
      self.remote.lat_delay = value

  @property
  def PLANPLUS_CONTROL(self):
    return getattr(self.small, 'PLANPLUS_CONTROL', 1.0)

  @PLANPLUS_CONTROL.setter
  def PLANPLUS_CONTROL(self, value) -> None:
    self.small.PLANPLUS_CONTROL = value
    if hasattr(self.remote, 'PLANPLUS_CONTROL'):
      self.remote.PLANPLUS_CONTROL = value

  def warmup(self) -> None:
    self.small.warmup()
    self._joined = False
    self._using_remote = False
    self._output_context = None
    warm = getattr(self.remote, 'warmup', None)
    if callable(warm):
      warm()

  def get_action_from_model(self, model_output, prev_action, lat_action_t: float,
                            long_action_t: float, v_ego: float):
    target = self.remote if self._using_remote else self.small
    return target.get_action_from_model(model_output, prev_action, lat_action_t, long_action_t, v_ego)

  def run(self, bufs, transforms, inputs, after_enqueue: Callable[[], None] | None = None):
    now = self._clock()
    self._publish_status()

    if not self._remote_enabled(now):
      # 运行期关闭：不换入；若正在用远端则**同帧降级**回 small（丢弃在途流水线），
      # 由 _run_small 发布本帧结果，绝不中断发布。
      if self._using_remote or self._joined:
        self._drop_remote()
        self.last_error = 'accelerator disabled by config'
      self._set_status(False)
      self._sync_link_armed(False)
      return self._run_small(bufs, transforms, inputs, after_enqueue)

    # 开关为开：确保链路层的后台重连监督器已武装（断链后同一进程内自动恢复的来源）。
    self._sync_link_armed(True)

    if not (self._using_remote or self._joined):
      if not self._maybe_join(now):
        self._set_status(False)
        return self._run_small(bufs, transforms, inputs, after_enqueue)
      self._joined = True

    once = _Once(after_enqueue)
    remote_inputs = inputs
    remote_desire = getattr(self.remote, 'desire_key', None)
    if remote_desire is not None and remote_desire != self.desire_key:
      remote_inputs = dict(inputs)
      remote_inputs[remote_desire] = inputs[self.desire_key]

    try:
      set_frame = getattr(self.remote, 'set_frame', None)
      if callable(set_frame):
        set_frame(*self._frame, context=self._context)
      outputs = self.remote.run(bufs, transforms, remote_inputs, once)
      context = getattr(self.remote, 'output_context', None)
      remote_active = bool(getattr(self.remote, 'chestnut', False))
    except Exception as exc:  # 任何远端异常都同帧降级，绝不冒泡
      self._note_failure(exc, now)
      return self._run_small(bufs, transforms, inputs, once)

    if outputs is None or not remote_active or not isinstance(context, dict) or 'frame_id' not in context:
      # 远端结果尚未就绪（启动/换入首帧、流水线追赶）或缺少所属帧上下文：
      # 同帧跑 small 并发布，绝不发布上下文不明的远端输出
      return self._run_small(bufs, transforms, inputs, once)

    frame = int(context['frame_id'])
    if frame < self._last_pub_frame_id or frame >= self._frame[0]:
      # 防御：绝不发布比已发布帧更旧、或不属于已过去帧的结果
      abort = getattr(self.remote, 'abort_pipeline', None)
      if callable(abort):
        try:
          abort()
        except Exception:
          pass
      self.last_error = (f'remote output context frame {frame} out of order '
                         f'(current {self._frame[0]}, published {self._last_pub_frame_id})')
      return self._run_small(bufs, transforms, inputs, once)

    self._on_remote_success(now)
    self._output_context = context
    self._mark_published(frame)
    return outputs
