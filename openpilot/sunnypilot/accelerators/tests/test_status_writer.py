"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

链路状态落盘器 `status_writer.StatusWriter` 的离线单测：
* 冻结键集合精确一致（UI 侧 normalize_status 依赖）；
* 字段映射（计数、分位、退避、配置、换入状态）；
* 原子写（tmp 清理干净、内容为完整 JSON）；
* 时间节流（同秒不重复写盘、跨过 interval 放行）；
* recent 截断到上限且节流期间也不丢点；
* 目录不可写时 write/maybe_write 返回 False 且不抛异常。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from openpilot.sunnypilot.accelerators.remote import switching
from openpilot.sunnypilot.accelerators.remote.status import RemoteStatus
from openpilot.sunnypilot.accelerators.remote.status_writer import (ERROR_HISTORY_LIMIT,
                                                                     RECENT_KEYS, RECENT_LIMIT,
                                                                     STATE_VALUES, STATUS_KEYS,
                                                                     StatusWriter, resolve_error_log,
                                                                     resolve_path)


class _Clock:
  """可控单调时钟，供节流测试推进时间。"""

  def __init__(self, now: float = 0.0):
    self.now = now

  def __call__(self) -> float:
    return self.now


def make_writer(path: str, *, interval: float = 1.0, recent_limit: int = RECENT_LIMIT,
                clock=None, time_source=None, config=None, config_interval: float = 2.0) -> StatusWriter:
  clock = clock or _Clock(0.0)
  time_source = time_source or (lambda: 1000.0)
  cfg = config if config is not None else {'enabled': True, 'address': '192.168.43.2:5202'}
  return StatusWriter(path, interval=interval, recent_limit=recent_limit, clock=clock,
                      time_source=time_source, config_loader=lambda: dict(cfg),
                      config_interval=config_interval,
                      error_log_path=os.path.join(os.path.dirname(path), 'accelerator_errors.log'))


class SnapshotTests(unittest.TestCase):
  def test_keys_exact_frozen(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      writer.record(12.5)
      snapshot = writer.snapshot(status=RemoteStatus())
      self.assertEqual(set(snapshot), set(STATUS_KEYS))
      self.assertEqual(len(snapshot), len(STATUS_KEYS))
      self.assertEqual(set(snapshot['recent'][0]), set(RECENT_KEYS))
      json.dumps(snapshot)  # 必须可 JSON 序列化

  def test_field_mapping(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      status = RemoteStatus()
      status.set_connected(True)
      status.set_big_active(False)
      for value in (10.0, 20.0, 30.0):
        status.record_submit(int(value))
        status.record_success(value, frame_id=int(value))
      status.record_failure('boom', timeout=True)
      backoff = switching.Backoff()
      backoff.delay = 5.0
      backoff.failures = 1

      snapshot = writer.snapshot(status=status, backoff=backoff)
      self.assertEqual((snapshot['ts'], snapshot['enabled'], snapshot['present']),
                       (1000.0, True, True))
      self.assertEqual(snapshot['address'], '192.168.43.2:5202')
      self.assertTrue(snapshot['connected'])
      self.assertTrue(snapshot['ready'])
      self.assertFalse(snapshot['big_active'])
      self.assertEqual(snapshot['last_error'], 'boom')
      self.assertEqual((snapshot['frames_sent'], snapshot['frames_ok'],
                        snapshot['timeouts'], snapshot['failures']), (3, 3, 1, 1))
      self.assertEqual((snapshot['p50_ms'], snapshot['p95_ms'], snapshot['p99_ms'], snapshot['max_ms']),
                       (20.0, 30.0, 30.0, 30.0))
      self.assertEqual(snapshot['backoff_s'], 5.0)
      self.assertEqual(snapshot['state'], 'ready')

  def test_state_values_follow_switching(self):
    with tempfile.TemporaryDirectory() as d:
      path = os.path.join(d, 'status.json')
      writer = make_writer(path, config={'enabled': True, 'address': 'a:1'})
      self.assertEqual(writer.snapshot(status=None)['state'], 'joining')
      self.assertEqual(writer.snapshot(status=None, ready=True)['state'], 'ready')
      self.assertEqual(writer.snapshot(status=None, ready=True, big_active=True)['state'], 'running')
      self.assertEqual(writer.snapshot(status=None, failures=2)['state'], 'retrying')

      disabled = make_writer(path, config={'enabled': False, 'address': 'a:1'})
      self.assertEqual(disabled.snapshot(status=None, ready=True, big_active=True)['state'], 'unavailable')

      for enabled, ready, big, failures in ((True, False, False, 0), (True, False, False, 3),
                                            (True, True, False, 1), (True, True, True, 0),
                                            (False, True, True, 5)):
        state = writer.snapshot(status=None, enabled=enabled, ready=ready, big_active=big,
                                failures=failures)['state']
        self.assertIn(state, STATE_VALUES)
        self.assertEqual(state, switching.big_model_state(stopped=not enabled, active_is_big=big,
                                                          available=ready, failures=failures))

  def test_config_fallback_and_override(self):
    with tempfile.TemporaryDirectory() as d:
      path = os.path.join(d, 'status.json')
      blank = make_writer(path, config={})
      snapshot = blank.snapshot(status=None)
      self.assertFalse(snapshot['enabled'])
      self.assertFalse(snapshot['present'])
      self.assertEqual(snapshot['address'], '')
      self.assertEqual(snapshot['state'], 'unavailable')

      explicit = blank.snapshot(status=None, enabled=True, address='10.0.0.2:5202')
      self.assertTrue(explicit['enabled'])
      self.assertTrue(explicit['present'])
      self.assertEqual(explicit['address'], '10.0.0.2:5202')


class WriteTests(unittest.TestCase):
  def test_atomic_write_leaves_only_target(self):
    with tempfile.TemporaryDirectory() as d:
      path = os.path.join(d, 'accelerator_status.json')
      writer = make_writer(path)
      writer.record(12.5)
      self.assertTrue(writer.maybe_write(status=RemoteStatus()))
      self.assertEqual(os.listdir(d), ['accelerator_status.json'])
      with open(path, encoding='utf-8') as f:
        payload = json.load(f)
      self.assertEqual(set(payload), set(STATUS_KEYS))
      self.assertEqual(payload['recent'][0]['total_ms'], 12.5)

  def test_throttle_limits_disk_writes(self):
    with tempfile.TemporaryDirectory() as d:
      clock = _Clock(0.0)
      writer = make_writer(os.path.join(d, 'status.json'), clock=clock, interval=1.0)
      self.assertTrue(writer.maybe_write(status=RemoteStatus()))
      clock.now = 0.5
      self.assertFalse(writer.maybe_write(status=RemoteStatus()))
      clock.now = 0.999
      self.assertFalse(writer.maybe_write(status=RemoteStatus()))
      clock.now = 1.0
      self.assertTrue(writer.maybe_write(status=RemoteStatus()))

  def test_recent_truncated_to_limit(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      for index in range(RECENT_LIMIT + 50):
        writer.record(float(index), t=float(index))
      points = writer.recent_points()
      self.assertEqual(len(points), RECENT_LIMIT)
      self.assertEqual((points[0]['total_ms'], points[-1]['total_ms']), (50.0, 349.0))
      self.assertEqual(len(writer.snapshot(status=None)['recent']), RECENT_LIMIT)

  def test_recent_limit_localized(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'), recent_limit=5)
      for index in range(20):
        writer.record(float(index))
      self.assertEqual([p['total_ms'] for p in writer.recent_points()], [15.0, 16.0, 17.0, 18.0, 19.0])

  def test_recent_records_even_when_throttled(self):
    with tempfile.TemporaryDirectory() as d:
      clock = _Clock(0.0)
      writer = make_writer(os.path.join(d, 'status.json'), clock=clock)
      status = RemoteStatus()
      status.record_success(11.0, frame_id=1)
      self.assertTrue(writer.maybe_write(status=status))
      clock.now = 0.1
      status.record_success(22.0, frame_id=2)
      self.assertFalse(writer.maybe_write(status=status))
      self.assertEqual([point['total_ms'] for point in writer.recent_points()], [11.0, 22.0])

  def test_write_failure_does_not_raise(self):
    with tempfile.TemporaryDirectory() as d:
      blocker = os.path.join(d, 'blocker')
      with open(blocker, 'w', encoding='utf-8') as f:
        f.write('x')
      path = os.path.join(blocker, 'status.json')  # 父路径是普通文件 → makedirs 失败
      writer = make_writer(path)
      self.assertFalse(writer.write({'ts': 1.0}))
      self.assertFalse(writer.maybe_write(status=RemoteStatus()))

  def test_non_finite_inputs_are_ignored(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      writer.record(float('nan'))
      writer.record(float('inf'))
      writer.record('nope')
      self.assertEqual(writer.recent_points(), [])


class ResolvePathTests(unittest.TestCase):
  def test_env_override_and_default(self):
    key = 'ACCELERATOR_STATUS_PATH'
    previous = os.environ.pop(key, None)
    try:
      self.assertEqual(resolve_path('/tmp/x.json'), '/tmp/x.json')
      self.assertEqual(resolve_path(), '/data/models/accelerator_status.json')
      os.environ[key] = '/tmp/from-env.json'
      self.assertEqual(resolve_path(), '/tmp/from-env.json')
    finally:
      os.environ.pop(key, None)
      if previous is not None:
        os.environ[key] = previous


class ErrorLogTests(unittest.TestCase):
  def _err_path(self, d: str) -> str:
    return os.path.join(d, 'accelerator_errors.log')

  def test_change_appends_and_dedupes(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      status = RemoteStatus()
      status.record_failure('frame 1 timed out', timeout=True)
      writer.maybe_write(status=status)
      writer.maybe_write(status=status)  # 同一错误值：不重复写
      status.record_failure('frame 2 timed out', timeout=True)
      writer.maybe_write(status=status)
      lines = writer.error_history()
      self.assertEqual(len(lines), 2)
      self.assertIn('frame 1 timed out', lines[0])
      self.assertIn('frame 2 timed out', lines[1])

  def test_clear_does_not_write_and_next_error_writes(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      status = RemoteStatus()
      status.record_failure('boom')
      writer.maybe_write(status=status)
      status.clear_error()
      writer.maybe_write(status=status)  # 清空不写盘
      self.assertEqual(len(writer.error_history()), 1)
      status.record_failure('boom')  # 再次出现同样错误：值已变化，重新留痕
      writer.maybe_write(status=status)
      self.assertEqual(len(writer.error_history()), 2)

  def test_rolling_cap(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      status = RemoteStatus()
      for index in range(ERROR_HISTORY_LIMIT + 7):
        status.record_failure(f'err {index}')
        writer.maybe_write(status=status)
      lines = writer.error_history()
      self.assertEqual(len(lines), ERROR_HISTORY_LIMIT)
      self.assertIn('err 7', lines[0])
      self.assertIn(f'err {ERROR_HISTORY_LIMIT + 6}', lines[-1])

  def test_unwritable_path_never_raises(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      # 用「父路径是普通文件」制造真正不可创建的错误日志目录：makedirs 必失败
      blocker = os.path.join(d, 'blocker')
      with open(blocker, 'w', encoding='utf-8') as f:
        f.write('x')
      writer._error_log = os.path.join(blocker, 'nested', 'errors.log')
      status = RemoteStatus()
      status.record_failure('boom')
      writer.maybe_write(status=status)  # 绝不抛异常
      self.assertEqual(writer.error_history(), [])

  def test_snapshot_keys_unchanged_by_error_log(self):
    with tempfile.TemporaryDirectory() as d:
      writer = make_writer(os.path.join(d, 'status.json'))
      status = RemoteStatus()
      status.record_failure('boom')
      writer.maybe_write(status=status)
      self.assertEqual(set(writer.snapshot(status=status)), set(STATUS_KEYS))


class ResolveErrorLogTests(unittest.TestCase):
  def test_env_override_and_default(self):
    key = 'ACCELERATOR_ERROR_LOG'
    previous = os.environ.pop(key, None)
    try:
      self.assertEqual(resolve_error_log('/tmp/x.log'), '/tmp/x.log')
      self.assertEqual(resolve_error_log(), '/data/models/accelerator_errors.log')
      os.environ[key] = '/tmp/from-env.log'
      self.assertEqual(resolve_error_log(), '/tmp/from-env.log')
    finally:
      os.environ.pop(key, None)
      if previous is not None:
        os.environ[key] = previous


if __name__ == '__main__':
  unittest.main()
