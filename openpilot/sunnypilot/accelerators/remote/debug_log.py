"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

外置大模型链路的**可选**调试日志。

开关来自 accelerator.json 的 `debug_log`（默认 false；等价 Param `RemoteModelDebugLog`
已强登记于 common/params_keys.h，但当前 prebuilt 的 libparams_c.so 未重编时该 Param
不可读写，故运行期以 JSON 配置为准）。

三条硬性约束：
* **关闭时零开销/零写盘**：每帧只做一次「缓存时间戳比较 + 布尔判断」，绝不 stat/open；
* **不阻塞实时循环**：打开后由后台守护线程异步落盘，调用方只做一次非阻塞入队，
  队列满直接丢行（计数丢弃数），绝不因磁盘 IO 拖慢 modeld；
* **有界、只追加**：写 `/data/accelerator/remote_debug.log`，单文件超过 `MAX_BYTES`
  时轮转为 `.1`（只保留一份旧文件），磁盘占用上限约 2 * MAX_BYTES。

安全：只记录白名单标量（int/float/bool/短字符串），字符串截断并去掉换行；绝不记录
tensor 载荷、HELLO signature 等敏感内容。
"""
from __future__ import annotations

import os
import queue
import threading
import time

# 默认落盘路径；测试/开发可用环境变量或构造参数覆盖，避免污染设备
LOG_PATH = '/data/accelerator/remote_debug.log'
LOG_PATH_ENV = 'ACCELERATOR_DEBUG_LOG'
# 单文件上限；轮转后总占用约 2 倍
MAX_BYTES = 1 << 20  # 1 MiB
BACKUP_SUFFIX = '.1'
# 开关的重新读取间隔：配置由 UI 低频变更，无需每帧读盘
REFRESH_S = 2.0
# 入队上限：满则丢行，绝不阻塞实时线程
QUEUE_MAX = 4096
# 字符串字段最大长度
MAX_STR = 96


def resolve_path(path: str | None = None) -> str:
  """本次写入使用的日志路径（显式参数 > 环境变量 > 默认常量）。"""
  return path or os.environ.get(LOG_PATH_ENV) or LOG_PATH


def _sanitize(value) -> str | None:
  """把字段值规整成单行短字符串；不支持的类型返回 None（即不记录）。"""
  if isinstance(value, bool):
    return '1' if value else '0'
  if isinstance(value, int):
    return str(value)
  if isinstance(value, float):
    return f'{value:.3f}'
  if isinstance(value, str):
    return value.replace('\r', ' ').replace('\n', ' ').strip()[:MAX_STR]
  return None


def _load_flag() -> bool:
  """读 accelerator.json 的 debug_log；任何失败一律视为关闭。"""
  try:
    from . import config
    return bool(config.load().get('debug_log', False))
  except Exception:
    return False


class RemoteDebugLog:
  """有界、异步、线程安全的调试日志写入器。"""

  def __init__(self, path: str | None = None, *, max_bytes: int = MAX_BYTES,
               refresh_s: float = REFRESH_S, clock=time.monotonic, time_source=time.time,
               flag_loader=None):
    self.path = resolve_path(path)
    self.backup_path = self.path + BACKUP_SUFFIX
    try:
      self._max_bytes = max(4096, int(max_bytes))
    except (TypeError, ValueError):
      self._max_bytes = MAX_BYTES
    try:
      self._refresh_s = max(0.1, float(refresh_s))
    except (TypeError, ValueError):
      self._refresh_s = REFRESH_S
    self._clock = clock
    self._time = time_source
    self._flag_loader = flag_loader or _load_flag
    self._enabled = False
    self._checked_at: float | None = None
    self._flag_lock = threading.Lock()
    self._queue: queue.Queue | None = None
    self._thread: threading.Thread | None = None
    self._start_lock = threading.Lock()
    self._header_done = False
    # 诊断计数（verification 用；写入侧更新，读取侧可能读到略旧值）
    self.dropped = 0
    self.written = 0
    self.last_error: str | None = None

  # -- 开关（缓存，零开销路径） ------------------------------------------------

  def enabled(self) -> bool:
    """当前是否启用；最多每 `refresh_s` 秒读一次配置，绝不抛异常。"""
    now = self._clock()
    if self._checked_at is not None and now - self._checked_at < self._refresh_s:
      return self._enabled
    with self._flag_lock:
      now = self._clock()
      if self._checked_at is not None and now - self._checked_at < self._refresh_s:
        return self._enabled
      try:
        self._enabled = bool(self._flag_loader())
      except Exception:
        self._enabled = False
      self._checked_at = now
      return self._enabled

  # -- 写入 ---------------------------------------------------------------------

  def log(self, event: str, **fields) -> bool:
    """记录一条事件；关闭/异常/队列满时立即返回，绝不阻塞、绝不抛异常。"""
    try:
      if not self.enabled():
        return False
      self._ensure_worker()
      if self._queue is None:
        return False
      if not self._header_done:
        self._header_done = True
        self._queue.put_nowait(self._header())
      self._queue.put_nowait(self._format(event, fields))
      return True
    except queue.Full:
      self.dropped += 1
      return False
    except Exception:
      return False

  def _header(self) -> str:
    return f'# remote debug log enabled pid={os.getpid()} ts={self._time():.3f}\n'

  def _format(self, event: str, fields: dict) -> str:
    parts = [f'{self._time():.3f}', _sanitize(str(event)) or 'event']
    for key in sorted(fields):
      value = _sanitize(fields[key])
      if value is None:
        continue
      parts.append(f'{key}={value}')
    return ' '.join(parts) + '\n'

  def _ensure_worker(self) -> None:
    with self._start_lock:
      if self._thread is not None and self._thread.is_alive():
        return
      self._queue = queue.Queue(maxsize=QUEUE_MAX)
      self._thread = threading.Thread(target=self._run, name='remote-debug-log', daemon=True)
      self._thread.start()

  def _run(self) -> None:
    buffer: list[str] = []
    while True:
      try:
        item = self._queue.get()
      except Exception:
        break
      if item is None:
        break
      buffer.append(item)
      # 尽量批量取走已排队行，减少 write 次数（实时性友好）
      while True:
        try:
          buffer.append(self._queue.get_nowait())
        except queue.Empty:
          break
        except Exception:
          break
      self._write_lines(buffer)
      buffer = []

  def _write_lines(self, lines: list[str]) -> None:
    """按 max_bytes 切块落盘，保证单文件不会因批量写而越界。"""
    if not lines:
      return
    chunk: list[str] = []
    chunk_size = 0
    for line in lines:
      size = len(line.encode('utf-8', 'replace'))
      if size > self._max_bytes:
        line = line[:self._max_bytes]
        size = len(line.encode('utf-8', 'replace'))
      if chunk and chunk_size + size > self._max_bytes:
        self._flush_chunk(''.join(chunk).encode('utf-8', 'replace'))
        chunk, chunk_size = [], 0
      chunk.append(line)
      chunk_size += size
      self.written += 1
    if chunk:
      self._flush_chunk(''.join(chunk).encode('utf-8', 'replace'))

  def _flush_chunk(self, data: bytes) -> None:
    try:
      directory = os.path.dirname(self.path) or '.'
      os.makedirs(directory, exist_ok=True)
      size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
      if size + len(data) > self._max_bytes:
        self._rotate()
      with open(self.path, 'ab') as f:
        f.write(data)
    except OSError as exc:
      self.last_error = str(exc)

  def _rotate(self) -> None:
    """把当前日志挪到 `.1`（覆盖旧备份）；失败则尽力删除，绝不影响主流程。"""
    try:
      if os.path.exists(self.path):
        os.replace(self.path, self.backup_path)
    except OSError:
      try:
        os.remove(self.path)
      except OSError:
        pass

  # -- 收尾（测试/关闭时用） ---------------------------------------------------

  def flush(self, timeout: float = 2.0) -> None:
    """等待后台线程把已入队内容写完（尽力而为，绝不抛异常）。"""
    thread = self._thread
    queue_ref = self._queue
    if thread is None or queue_ref is None or not thread.is_alive():
      return
    deadline = self._clock() + max(0.0, float(timeout))
    while not queue_ref.empty() and self._clock() < deadline:
      time.sleep(0.005)

  def close(self) -> None:
    """停止后台线程；幂等，绝不抛异常。"""
    with self._start_lock:
      thread, self._thread = self._thread, None
      queue_ref, self._queue = self._queue, None
    if thread is None or queue_ref is None:
      return
    try:
      queue_ref.put_nowait(None)
    except Exception:
      pass
    try:
      thread.join(timeout=1.0)
    except Exception:
      pass

  def stats(self) -> dict:
    """诊断快照：路径、上限、已写行数、丢弃行数、最后错误。"""
    return {
      'path': self.path,
      'enabled': self._enabled,
      'max_bytes': self._max_bytes,
      'written': self.written,
      'dropped': self.dropped,
      'last_error': self.last_error,
    }


_default: RemoteDebugLog | None = None
_default_lock = threading.Lock()


def default() -> RemoteDebugLog:
  """进程内单例；首次调用才构造（关闭时几乎零成本）。"""
  global _default
  if _default is None:
    with _default_lock:
      if _default is None:
        _default = RemoteDebugLog()
  return _default


def log_event(event: str, **fields) -> bool:
  """模块级便捷入口；任何异常都被吞掉，绝不影响调用方。"""
  try:
    return default().log(event, **fields)
  except Exception:
    return False


def is_enabled() -> bool:
  try:
    return default().enabled()
  except Exception:
    return False


def _reset_for_tests() -> None:
  """仅测试使用：关闭并丢弃单例。"""
  global _default
  with _default_lock:
    if _default is not None:
      _default.close()
    _default = None
