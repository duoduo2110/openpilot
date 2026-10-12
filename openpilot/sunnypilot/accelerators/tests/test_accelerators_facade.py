"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

门面层 accelerators/__init__.py 的离线单测：默认返回值安全、依赖缺失时返回
负默认（_NoBackend）、进度上报限频且可清除、shutdown 未开启时立刻返回。
"""
from __future__ import annotations

import unittest
from unittest import mock

from openpilot.sunnypilot import accelerators


class FacadeTests(unittest.TestCase):
  def test_calls_never_raise_and_have_safe_types(self):
    self.assertIsInstance(accelerators.installed(), bool)
    self.assertIsInstance(accelerators.present(), bool)
    self.assertIsInstance(accelerators.ready(), bool)
    self.assertIsInstance(accelerators.enabled(), bool)
    self.assertIsInstance(accelerators.prepare(), bool)
    self.assertIsInstance(accelerators.status_snapshot(), dict)
    reason = accelerators.unavailable_reason()
    self.assertTrue(reason is None or isinstance(reason, str))

  def test_load_returns_none_without_warp_artifact(self):
    """工件缺失（离线环境）时 load 安全返回 None，绝不抛异常。

    注意：`load()` 不再因 enabled=false 而提前返回 —— 开关改由 JoinedModel
    运行期读取，因此这里显式构造「工件缺失」这一真失败路径。
    """
    from openpilot.sunnypilot.accelerators.remote import backend
    cfg = {'enabled': True, 'address': '127.0.0.1:1',
           'warp_path': '/tmp/does-not-exist-remote_warp.pkl',
           'deadline_ms': 60, 'pipeline_depth': 1, 'payload_compression': False}
    with mock.patch.object(backend.config, 'load', lambda *a, **k: dict(cfg)):
      self.assertIsNone(accelerators.load(1920, 1208, None))

  def test_shutdown_is_a_noop_when_disabled(self):
    accelerators.shutdown('unittest', timeout=0.1)  # 不得阻塞、不得抛出

  def test_progress_roundtrip_and_clear(self):
    accelerators.report_progress('connect', 0.5, 'hi')
    progress = accelerators.progress()
    self.assertEqual((progress['stage'], progress['frac'], progress['msg']), ('connect', 0.5, 'hi'))
    accelerators.clear_progress()
    self.assertIsNone(accelerators.progress())

  def test_nobackend_negative_defaults(self):
    nb = accelerators._NoBackend
    self.assertFalse(nb.installed())
    self.assertFalse(nb.enabled())
    self.assertFalse(nb.present())
    self.assertFalse(nb.ready())
    self.assertFalse(nb.prepare())
    self.assertIsNone(nb.load(1, 1, None))
    self.assertIsNone(nb.progress())
    self.assertIsNone(nb.unavailable_reason())
    self.assertEqual(nb.status_snapshot(), {})
    nb.report_progress('stage', 0.5)   # 不得抛出
    nb.clear_progress()
    nb.shutdown()                      # 不得抛出

  def test_facade_uses_nobackend_when_backend_missing(self):
    with mock.patch.object(accelerators, '_resolved', accelerators._NoBackend):
      self.assertFalse(accelerators.installed())
      self.assertFalse(accelerators.enabled())
      self.assertFalse(accelerators.prepare())
      self.assertIsNone(accelerators.load(1, 1, None))
      self.assertIsNone(accelerators.progress())
      self.assertIsNone(accelerators.unavailable_reason())
      accelerators.report_progress('x', 1.0)
      accelerators.shutdown('x', 0.1)
      self.assertEqual(accelerators.status_snapshot(), {})

  def test_backend_is_cached(self):
    first = accelerators._backend()
    self.assertIs(first, accelerators._backend())


if __name__ == '__main__':
  unittest.main()
