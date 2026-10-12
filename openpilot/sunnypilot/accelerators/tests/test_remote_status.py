"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

链路状态对象的离线单测：帧计数、延迟分位（最近秩）、失败/超时计数、
JSON 可序列化与往返还原。
"""
from __future__ import annotations

import json
import threading
import unittest

from openpilot.sunnypilot.accelerators.remote.status import RemoteStatus


class StatusTests(unittest.TestCase):
  def test_percentiles_nearest_rank(self):
    status = RemoteStatus()
    for value in range(1, 101):
      status.record_success(float(value), frame_id=value)
    self.assertEqual(status.p50_ms, 50.0)
    self.assertEqual(status.p95_ms, 95.0)
    self.assertEqual(status.p99_ms, 99.0)
    self.assertEqual(status.max_ms, 100.0)
    self.assertEqual(status.last_latency_ms, 100.0)
    self.assertEqual(status.frames_ok, 100)

  def test_empty_percentiles_are_zero(self):
    status = RemoteStatus()
    self.assertEqual((status.p50_ms, status.p95_ms, status.p99_ms, status.max_ms), (0.0, 0.0, 0.0, 0.0))

  def test_counts_and_errors(self):
    status = RemoteStatus()
    status.record_submit(1)
    status.record_submit(2)
    status.record_success(12.5, frame_id=1)
    status.record_failure('deadline', timeout=True)
    snapshot = status.to_dict()
    self.assertEqual((snapshot['frames_sent'], snapshot['frames_ok'], snapshot['frames_failed']),
                     (2, 1, 1))
    self.assertEqual(snapshot['timeouts'], 1)
    self.assertEqual(snapshot['last_error'], 'deadline')
    self.assertEqual(snapshot['last_frame_id'], 1)
    status.clear_error()
    self.assertIsNone(status.to_dict()['last_error'])

  def test_latency_window_is_bounded(self):
    status = RemoteStatus(latency_window=8)
    for value in range(50):
      status.record_success(float(value))
    self.assertEqual(len(status.to_dict()['latencies']), 8)
    self.assertEqual(status.max_ms, 49.0)

  def test_to_dict_is_json_serializable(self):
    status = RemoteStatus()
    status.set_connected(True)
    status.set_big_active(True)
    status.record_submit(3)
    status.record_success(7.0, frame_id=3)
    payload = json.dumps(status.to_dict())
    restored = RemoteStatus.from_dict(json.loads(payload))
    self.assertEqual(restored.to_dict(), status.to_dict())
    self.assertTrue(restored.connected)
    self.assertTrue(restored.big_active)

  def test_from_dict_tolerates_garbage(self):
    self.assertEqual(RemoteStatus.from_dict(None).connected, False)
    status = RemoteStatus.from_dict({'frames_ok': 'nope', 'last_error': 42, 'latencies': [1, 'x', 2.5]})
    self.assertEqual(status.frames_ok, 0)
    self.assertEqual(status.last_error, '42')
    self.assertEqual(status.latencies, [1.0, 2.5])

  def test_thread_safe_record(self):
    status = RemoteStatus()
    errors = []

    def worker(seed):
      try:
        for _ in range(200):
          status.record_submit(seed)
          status.record_success(1.0, frame_id=seed)
      except Exception as e:  # pragma: no cover - 只在并发写坏时触发
        errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()
    self.assertEqual(errors, [])
    self.assertEqual(status.to_dict()['frames_ok'], 800)


if __name__ == '__main__':
  unittest.main()
