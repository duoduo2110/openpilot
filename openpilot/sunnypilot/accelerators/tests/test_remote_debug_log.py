"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

远程模型调试日志的离线单测：关闭时零写盘、开启后事件格式、有界轮转、
非标量字段过滤与字符串截断。全部在临时目录里跑，不碰 /data。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from openpilot.sunnypilot.accelerators.remote import debug_log


class RemoteDebugLogTests(unittest.TestCase):
  def setUp(self):
    self.dir = tempfile.TemporaryDirectory()
    self.addCleanup(self.dir.cleanup)
    self.path = os.path.join(self.dir.name, 'remote_debug.log')

  def _make(self, enabled_flag, **kwargs):
    box = {'on': enabled_flag}
    log = debug_log.RemoteDebugLog(path=self.path, refresh_s=0.1,
                                   flag_loader=lambda: box['on'], **kwargs)
    return log, box

  def test_disabled_writes_nothing(self):
    log, _ = self._make(False)
    for i in range(100):
      log.log('frame_ok', frame=i, total_ms=1.0)
    log.flush()
    self.assertFalse(os.path.exists(self.path))
    self.assertEqual(log.written, 0)

  def test_enabled_writes_header_and_events(self):
    log, _ = self._make(True)
    log.log('connect_ok', host='192.168.43.2', port=5202, transport='tcp')
    log.log('frame_ok', frame=7, req=8, total_ms=12.5, transport='tcp')
    log.flush()
    log.close()
    lines = open(self.path, encoding='utf-8').read().strip().split('\n')
    self.assertTrue(lines[0].startswith('# remote debug log enabled'))
    self.assertIn('connect_ok', lines[1])
    self.assertIn('transport=tcp', lines[1])
    self.assertIn('frame=7', lines[2])
    self.assertIn('total_ms=12.500', lines[2])

  def test_toggle_on_after_off(self):
    log, box = self._make(False)
    log.log('frame_ok', frame=1)
    log.flush()
    self.assertFalse(os.path.exists(self.path))
    box['on'] = True
    log._checked_at = None
    log.log('frame_ok', frame=2)
    log.flush()
    log.close()
    self.assertTrue(os.path.exists(self.path))

  def test_bounded_rotation(self):
    log, _ = self._make(True, max_bytes=4096)
    for i in range(2000):
      log.log('frame_ok', frame=i, total_ms=1.0)
    log.flush()
    log.close()
    size = os.path.getsize(self.path)
    backup = self.path + '.1'
    self.assertLessEqual(size, 4096)
    self.assertTrue(os.path.exists(backup))
    self.assertLessEqual(os.path.getsize(backup), 4096)

  def test_fields_are_sanitized(self):
    log, _ = self._make(True)
    log.log('weird', ok=True, n=3, obj=object(), note='a\nb', long='x' * 500)
    log.flush()
    log.close()
    line = open(self.path, encoding='utf-8').read().strip().split('\n')[-1]
    self.assertIn('ok=1', line)
    self.assertIn('n=3', line)
    self.assertNotIn('obj=', line)
    self.assertIn('note=a b', line)
    self.assertNotIn('\n', line)
    # 字符串被截断到 MAX_STR
    self.assertLessEqual(len(line), 200)
    self.assertNotIn('x' * (debug_log.MAX_STR + 1), line)

  def test_stats_and_no_raise_on_bad_event(self):
    log, _ = self._make(True)
    log.log(object())  # 非字符串事件名也不得抛异常
    log.flush()
    log.close()
    self.assertEqual(log.stats()['path'], self.path)


if __name__ == '__main__':
  unittest.main()
