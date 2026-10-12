"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

加速器链路状态：可序列化、线程安全、供后续 lane 接 UI / 日志。

只记录链路自身的健康度——连接态、最后错误、帧计数、延迟分位、是否 big-active。
不订阅任何 cereal 消息（真正接入 UI/selfdrived 在后续 lane），因此本模块可离线单测。

参考 jetlink 的 status.py 语义：状态对象是「遥测的载体」，模型侧只往里写，
发布方（后续 lane）只读快照。
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

# 延迟分位只对最近这么多帧计算：驱动里一次的采样窗口足够，且内存有界
LATENCY_WINDOW = 256


def _percentile(sorted_samples: list[float], pct: float) -> float:
  """最近秩法（nearest-rank），结果确定、无需插值。"""
  if not sorted_samples:
    return 0.0
  rank = max(1, math.ceil(pct / 100.0 * len(sorted_samples)))
  return sorted_samples[min(rank, len(sorted_samples)) - 1]


@dataclass
class RemoteStatus:
  """链路状态快照；所有写操作持锁，读用 to_dict() 取一致副本。"""

  connected: bool = False
  big_active: bool = False
  last_error: str | None = None
  frames_sent: int = 0
  frames_ok: int = 0
  frames_failed: int = 0
  timeouts: int = 0
  last_frame_id: int = 0
  last_latency_ms: float = 0.0
  # -- 重连/恢复遥测（UI 需要「重连中(退避 Ns)」「已恢复」这两个可见状态）----
  # reconnect_armed: 后台重连监督器是否武装（= 配置开关为开）
  # reconnect_backoff_s: 当前退避秒数；>0 表示正在退避等待
  # reconnect_attempts: 本次掉线以来的重连尝试次数
  # recovered_at: 最近一次「链路恢复」的墙钟 epoch 秒（0.0 = 从未/未恢复）
  reconnect_armed: bool = False
  reconnect_backoff_s: float = 0.0
  reconnect_attempts: int = 0
  recovered_at: float = 0.0
  latencies: list[float] = field(default_factory=list)
  latency_window: int = LATENCY_WINDOW
  _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

  # -- 写 -------------------------------------------------------------------

  def set_connected(self, connected: bool) -> None:
    with self._lock:
      self.connected = bool(connected)

  def set_big_active(self, active: bool) -> None:
    with self._lock:
      self.big_active = bool(active)

  def set_reconnect(self, armed: bool, backoff_s: float, attempts: int) -> None:
    """更新后台重连监督器的可见状态（UI 的「重连中(退避 Ns)」来源）。"""
    with self._lock:
      self.reconnect_armed = bool(armed)
      try:
        value = float(backoff_s)
      except (TypeError, ValueError):
        value = 0.0
      self.reconnect_backoff_s = value if math.isfinite(value) and value > 0 else 0.0
      try:
        self.reconnect_attempts = max(0, int(attempts))
      except (TypeError, ValueError):
        self.reconnect_attempts = 0

  def mark_recovered(self, epoch_seconds: float) -> None:
    """记下「链路刚刚恢复」的墙钟时刻（UI 据此显示「已恢复·外置运行」）。"""
    with self._lock:
      try:
        value = float(epoch_seconds)
      except (TypeError, ValueError):
        return
      if math.isfinite(value) and value > 0:
        self.recovered_at = value

  def record_submit(self, frame_id: int) -> None:
    with self._lock:
      self.frames_sent += 1
      self.last_frame_id = int(frame_id)

  def record_success(self, latency_ms: float, frame_id: int | None = None) -> None:
    with self._lock:
      self.frames_ok += 1
      self.last_latency_ms = float(latency_ms)
      if frame_id is not None:
        self.last_frame_id = int(frame_id)
      self.latencies.append(float(latency_ms))
      if len(self.latencies) > self.latency_window:
        del self.latencies[:len(self.latencies) - self.latency_window]

  def record_failure(self, error: str, timeout: bool = False) -> None:
    with self._lock:
      self.frames_failed += 1
      if timeout:
        self.timeouts += 1
      self.last_error = str(error)

  def clear_error(self) -> None:
    with self._lock:
      self.last_error = None

  # -- 读 -------------------------------------------------------------------

  def _sorted_latencies(self) -> list[float]:
    return sorted(self.latencies)

  @property
  def p50_ms(self) -> float:
    with self._lock:
      return _percentile(self._sorted_latencies(), 50.0)

  @property
  def p95_ms(self) -> float:
    with self._lock:
      return _percentile(self._sorted_latencies(), 95.0)

  @property
  def p99_ms(self) -> float:
    with self._lock:
      return _percentile(self._sorted_latencies(), 99.0)

  @property
  def max_ms(self) -> float:
    with self._lock:
      samples = self.latencies
      return max(samples) if samples else 0.0

  def to_dict(self) -> dict:
    """纯 JSON 类型，供 swaglog / UI / 落盘。"""
    with self._lock:
      samples = list(self.latencies)
      return {
        'connected': self.connected,
        'big_active': self.big_active,
        'last_error': self.last_error,
        'frames_sent': self.frames_sent,
        'frames_ok': self.frames_ok,
        'frames_failed': self.frames_failed,
        'timeouts': self.timeouts,
        'last_frame_id': self.last_frame_id,
        'last_latency_ms': self.last_latency_ms,
        'reconnect_armed': self.reconnect_armed,
        'reconnect_backoff_s': self.reconnect_backoff_s,
        'reconnect_attempts': self.reconnect_attempts,
        'recovered_at': self.recovered_at,
        'p50_ms': _percentile(sorted(samples), 50.0),
        'p95_ms': _percentile(sorted(samples), 95.0),
        'p99_ms': _percentile(sorted(samples), 99.0),
        'max_ms': max(samples) if samples else 0.0,
        'latencies': samples,
      }

  # 别名：后续 lane 的日志代码可能按 jetlink 的叫法读 as_dict()
  as_dict = to_dict

  @classmethod
  def from_dict(cls, data: dict) -> 'RemoteStatus':
    """从 to_dict() 的输出还原（跳过未知字段），坏输入回落全新对象。"""
    if not isinstance(data, dict):
      return cls()
    status = cls()
    status.connected = bool(data.get('connected', False))
    status.big_active = bool(data.get('big_active', False))
    last_error = data.get('last_error')
    status.last_error = str(last_error) if last_error is not None else None
    for key in ('frames_sent', 'frames_ok', 'frames_failed', 'timeouts', 'last_frame_id',
                'reconnect_attempts'):
      value = data.get(key, 0)
      setattr(status, key, int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0)
    for key in ('reconnect_backoff_s', 'recovered_at'):
      value = data.get(key, 0.0)
      try:
        number = float(value)
      except (TypeError, ValueError):
        number = 0.0
      setattr(status, key, number if math.isfinite(number) and number > 0 else 0.0)
    status.reconnect_armed = bool(data.get('reconnect_armed', False))
    value = data.get('last_latency_ms', 0.0)
    status.last_latency_ms = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    samples = data.get('latencies', [])
    if isinstance(samples, list):
      status.latencies = [float(x) for x in samples if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return status

  def __str__(self) -> str:
    snapshot = self.to_dict()
    return (f"RemoteStatus(connected={snapshot['connected']}, big={snapshot['big_active']}, "
            f"ok={snapshot['frames_ok']}/{snapshot['frames_sent']}, fail={snapshot['frames_failed']}, "
            f"timeout={snapshot['timeouts']}, p50={snapshot['p50_ms']:.1f}ms, "
            f"p95={snapshot['p95_ms']:.1f}ms, p99={snapshot['p99_ms']:.1f}ms, "
            f"max={snapshot['max_ms']:.1f}ms, err={snapshot['last_error']!r})")
