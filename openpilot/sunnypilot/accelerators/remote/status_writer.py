"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

把加速器链路的运行状态周期落盘给 C3 UI 读的写入器。

C3 界面（`selfdrive/ui/sunnypilot/accelerator_link.py`）按**冻结结构**读取
`/data/models/accelerator_status.json`：

    {"ts", "enabled", "present", "ready", "big_active", "state",
     "connected", "address", "last_error",
     "frames_sent", "frames_ok", "timeouts", "failures",
     "p50_ms", "p95_ms", "p99_ms", "max_ms", "backoff_s", "recent"}

本模块只做「读状态对象 → 组装字节 → 原子写盘」，不订阅 cereal、不起线程，
因此离线可单测。三条硬约束：

* **节流**：`maybe_write()` 由每帧路径调用，但真正落盘最多每 `interval` 秒一次
  （默认 1Hz）；节流用单调时钟，绝不让磁盘 IO 进入每帧关键路径。
* **原子**：同目录临时文件 + `fsync` + `os.replace`，UI 永远读不到半截 JSON。
* **不抛异常**：写失败只记日志（`swaglog` 可用时用它），返回 False，
  绝不把异常冒泡到 modeld 实时循环。

`recent` 只保留最近 `RECENT_LIMIT` 条 `{"t": epoch秒, "total_ms": float}`；每帧
调用时会按 `frames_ok` 计数增量记录一个延迟点，即使本次被节流不写盘也不丢点。

错误留痕：状态文件的键集合是**冻结**的（UI 侧 `normalize_status` 依赖），因此
不往快照里加字段，而是把 `last_error` 的**变化**滚动追加到独立文件
`/data/models/accelerator_errors.log`（每行 `<epoch秒> <错误>`，最多
`ERROR_HISTORY_LIMIT` 行）。仅在错误值发生变化且非空时写盘，与 ≤1Hz 节流同频，
绝不抛异常、不阻塞实时循环。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from collections import deque

from . import switching

# 默认落盘路径；允许构造参数 / 环境变量 / accelerator.json 的 status_path 覆盖
STATUS_PATH = '/data/models/accelerator_status.json'
STATUS_PATH_ENV = 'ACCELERATOR_STATUS_PATH'
# 真正写盘的最小间隔（秒）：≤1Hz，避免每帧写盘
WRITE_INTERVAL = 1.0
# 配置（enabled/address）的重新读取间隔：配置改动由 UI 低频触发，无需每帧读文件
CONFIG_INTERVAL = 2.0
# recent 只保留最近这么多条延迟点
RECENT_LIMIT = 300
# 错误留痕：滚动日志路径与保留条数（独立文件，不改冻结的状态结构）
ERROR_LOG_PATH = '/data/models/accelerator_errors.log'
ERROR_LOG_ENV = 'ACCELERATOR_ERROR_LOG'
ERROR_HISTORY_LIMIT = 20

# recent 单点的键集合（冻结）
RECENT_KEYS = frozenset({'t', 'total_ms'})
# 状态文件的键集合（冻结，UI 侧 normalize_status 依赖）
STATUS_KEYS = frozenset({
  'ts', 'enabled', 'present', 'ready', 'big_active', 'state',
  'connected', 'address', 'last_error',
  'frames_sent', 'frames_ok', 'timeouts', 'failures',
  'p50_ms', 'p95_ms', 'p99_ms', 'max_ms', 'backoff_s', 'recent',
  # 链路层「自动恢复」遥测：让 UI 能明确显示「重连中(退避 Ns)」/「已恢复·外置运行」/
  # 「本地兜底」，而不必从 frames_ok 的停滞去猜。
  'reconnect_armed', 'reconnect_backoff_s', 'reconnect_attempts', 'recovered_at',
})
# state 的合法取值，与 switching.BIG_MODEL_STATES 一致
STATE_VALUES = tuple(switching.BIG_MODEL_STATES)


def resolve_path(path: str | None = None) -> str:
  """本次写入使用的状态文件路径（显式参数 > 环境变量 > 默认常量）。"""
  return path or os.environ.get(STATUS_PATH_ENV) or STATUS_PATH


def resolve_error_log(path: str | None = None) -> str:
  """错误留痕文件路径（显式参数 > 环境变量 > 默认常量）。"""
  return path or os.environ.get(ERROR_LOG_ENV) or ERROR_LOG_PATH


def _as_float(value, default: float = 0.0) -> float:
  """只接受有限实数；bool/NaN/inf/非数值一律回落默认值。"""
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return default
  return float(value) if math.isfinite(value) else default


def _as_int(value, default: int = 0) -> int:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return default
  return int(value) if math.isfinite(value) else default


def _log_warning(message: str) -> None:
  """写失败只记日志：优先走设备日志，缺失时静默（绝不抛异常）。"""
  try:
    from openpilot.common.swaglog import cloudlog
    cloudlog.warning(message)
  except Exception:
    pass


def _load_config() -> dict:
  """惰性读取 accelerator.json；任何失败都返回空 dict（由调用方回落默认）。"""
  try:
    from . import config
    return config.load()
  except Exception:
    return {}


class StatusWriter:
  """链路状态 → `/data/models/accelerator_status.json` 的节流原子写入器。"""

  def __init__(self, path: str | None = None, *, interval: float = WRITE_INTERVAL,
               recent_limit: int = RECENT_LIMIT, clock=time.monotonic, time_source=time.time,
               config_loader=None, config_interval: float = CONFIG_INTERVAL,
               error_log_path: str | None = None, error_limit: int = ERROR_HISTORY_LIMIT):
    self.path = resolve_path(path)
    self._interval = max(0.0, _as_float(interval, WRITE_INTERVAL))
    self._config_interval = max(0.0, _as_float(config_interval, CONFIG_INTERVAL))
    self._clock = clock
    self._time = time_source
    self._config_loader = config_loader or _load_config
    self._recent = deque(maxlen=max(1, int(recent_limit)))
    self._lock = threading.Lock()
    self._last_attempt: float | None = None
    self._last_frames_ok = 0
    self._config_cache: dict | None = None
    self._config_at: float | None = None
    # 错误留痕：只在 last_error 发生变化且非空时追加一行，_error_lock 保护滚动整理
    self._error_log = resolve_error_log(error_log_path)
    self._error_limit = max(1, int(error_limit))
    self._last_error_seen: str | None = None
    self._error_lock = threading.Lock()

  # -- recent 延迟点 -----------------------------------------------------------

  def record(self, total_ms, t=None) -> None:
    """记录一个延迟点（每帧调用，开销极小）；非有限值直接忽略。"""
    value = _as_float(total_ms, float('nan'))
    if not math.isfinite(value):
      return
    try:
      stamp = float(self._time() if t is None else t)
    except (TypeError, ValueError):
      return
    with self._lock:
      self._recent.append({'t': stamp, 'total_ms': value})

  def recent_points(self) -> list[dict]:
    with self._lock:
      return [dict(point) for point in self._recent]

  # -- 错误留痕 ---------------------------------------------------------------

  def error_history(self) -> list[str]:
    """读取滚动错误日志（尾部最多 `error_limit` 行）；任何失败返回空列表。"""
    try:
      with open(self._error_log, encoding='utf-8') as f:
        lines = f.readlines()
    except OSError:
      return []
    return [line.rstrip('\n') for line in lines[-self._error_limit:]]

  def _note_error(self, last_error) -> None:
    """last_error 变化且非空时追加一行；绝不抛异常、不阻塞。"""
    try:
      message = last_error.strip() if isinstance(last_error, str) and last_error.strip() else None
      if message == self._last_error_seen:
        return
      self._last_error_seen = message
      if message is None:
        return
      stamp = float(self._time())
      directory = os.path.dirname(self._error_log) or '.'
      with self._error_lock:
        os.makedirs(directory, exist_ok=True)
        with open(self._error_log, 'a', encoding='utf-8') as f:
          f.write(f'{stamp:.3f} {message}\n')
        self._trim_error_log()
    except Exception:
      pass

  def _trim_error_log(self) -> None:
    """超过上限时只保留末尾 `error_limit` 行；失败静默。"""
    try:
      with open(self._error_log, encoding='utf-8') as f:
        lines = f.readlines()
      if len(lines) <= self._error_limit:
        return
      tmp_path = self._error_log + '.tmp'
      with open(tmp_path, 'w', encoding='utf-8') as f:
        f.writelines(lines[-self._error_limit:])
      os.replace(tmp_path, self._error_log)
    except OSError:
      pass

  def _record_from_status(self, status) -> None:
    """按 frames_ok 增量把最新延迟追加进 recent；节流期间也不丢点。"""
    if status is None:
      return
    try:
      frames_ok = int(getattr(status, 'frames_ok', 0))
      latency = getattr(status, 'last_latency_ms', 0.0)
    except Exception:
      return
    with self._lock:
      if frames_ok == self._last_frames_ok:
        return
      self._last_frames_ok = frames_ok
      if frames_ok <= 0:
        return
    self.record(latency)

  # -- 配置（低频缓存） --------------------------------------------------------

  def config(self) -> dict:
    """当前配置快照；最多每 `config_interval` 秒读一次文件，绝不抛异常。"""
    now = self._clock()
    with self._lock:
      if (self._config_cache is not None and self._config_at is not None
          and now - self._config_at < self._config_interval):
        return dict(self._config_cache)
    try:
      cfg = self._config_loader()
    except Exception:
      cfg = None
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    with self._lock:
      self._config_cache = cfg
      self._config_at = now
    return dict(cfg)

  # -- 组装快照 ---------------------------------------------------------------

  def snapshot(self, *, status=None, backoff=None, config: dict | None = None,
               enabled: bool | None = None, address: str | None = None,
               present: bool | None = None, ready: bool | None = None,
               big_active: bool | None = None, state: str | None = None,
               last_error: str | None = None, connected: bool | None = None,
               failures: int | None = None,
               reconnect_armed: bool | None = None, reconnect_backoff_s: float | None = None,
               reconnect_attempts: int | None = None, recovered_at: float | None = None) -> dict:
    """按冻结结构组装一份纯 JSON 快照（键集合 == STATUS_KEYS）。"""
    cfg = config if isinstance(config, dict) else self.config()
    if enabled is None:
      enabled = bool(cfg.get('enabled', False))
    if address is None:
      raw_address = cfg.get('address')
      address = raw_address if isinstance(raw_address, str) else ''
    address = str(address or '')
    if present is None:
      present = bool(address.strip())

    info = {}
    if status is not None:
      try:
        info = status.to_dict()
      except Exception:
        info = {}
    if connected is None:
      connected = bool(info.get('connected', False))
    if ready is None:
      ready = connected
    if big_active is None:
      big_active = bool(info.get('big_active', False))
    if failures is None:
      failures = _as_int(info.get('frames_failed', 0))
    if state is None:
      state = switching.big_model_state(stopped=not enabled, active_is_big=bool(big_active),
                                        available=bool(ready), failures=int(failures))
    if last_error is None:
      last_error = info.get('last_error')
    if reconnect_armed is None:
      reconnect_armed = bool(info.get('reconnect_armed', False))
    if reconnect_backoff_s is None:
      reconnect_backoff_s = _as_float(info.get('reconnect_backoff_s', 0.0), 0.0)
    if reconnect_attempts is None:
      reconnect_attempts = _as_int(info.get('reconnect_attempts', 0), 0)
    if recovered_at is None:
      recovered_at = _as_float(info.get('recovered_at', 0.0), 0.0)

    return {
      'ts': float(self._time()),
      'enabled': bool(enabled),
      'present': bool(present),
      'ready': bool(ready),
      'big_active': bool(big_active),
      'state': str(state),
      'connected': bool(connected),
      'address': address,
      'last_error': last_error if isinstance(last_error, str) else None,
      'frames_sent': _as_int(info.get('frames_sent', 0)),
      'frames_ok': _as_int(info.get('frames_ok', 0)),
      'timeouts': _as_int(info.get('timeouts', 0)),
      'failures': _as_int(failures),
      'p50_ms': _as_float(info.get('p50_ms', 0.0)),
      'p95_ms': _as_float(info.get('p95_ms', 0.0)),
      'p99_ms': _as_float(info.get('p99_ms', 0.0)),
      'max_ms': _as_float(info.get('max_ms', 0.0)),
      'backoff_s': _as_float(getattr(backoff, 'delay', 0.0)),
      'recent': self.recent_points(),
      'reconnect_armed': bool(reconnect_armed),
      'reconnect_backoff_s': _as_float(reconnect_backoff_s, 0.0),
      'reconnect_attempts': _as_int(reconnect_attempts, 0),
      'recovered_at': _as_float(recovered_at, 0.0),
    }

  # -- 落盘 -------------------------------------------------------------------

  def write(self, payload: dict) -> bool:
    """原子写盘：同目录 tmp + fsync + os.replace；失败只记日志并返回 False。"""
    target = self.path
    directory = os.path.dirname(target) or '.'
    tmp_path = None
    try:
      os.makedirs(directory, exist_ok=True)
      fd, tmp_path = tempfile.mkstemp(prefix='.accelerator_status-', suffix='.json', dir=directory)
      with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
      os.replace(tmp_path, target)
      tmp_path = None
      return True
    except (OSError, ValueError, TypeError) as exc:
      _log_warning(f'加速器状态写盘失败 {target}: {exc}')
      return False
    finally:
      if tmp_path is not None:
        try:
          os.unlink(tmp_path)
        except OSError:
          pass

  def maybe_write(self, **kwargs) -> bool:
    """每帧可调用：先记录延迟点，再按 `interval` 节流真正落盘；绝不抛异常。"""
    status = kwargs.get('status')
    try:
      self._record_from_status(status)
      last_error = kwargs.get('last_error')
      if last_error is None and status is not None:
        last_error = getattr(status, 'last_error', None)
      self._note_error(last_error)
    except Exception:
      pass

    now = self._clock()
    with self._lock:
      if self._last_attempt is not None and (now - self._last_attempt) < self._interval:
        return False
      self._last_attempt = now
    try:
      payload = self.snapshot(**kwargs)
    except Exception as exc:
      _log_warning(f'加速器状态组装失败: {exc}')
      return False
    return self.write(payload)
