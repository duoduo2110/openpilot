"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

换入/重连状态机骨架（纯函数）的离线单测：engaged 门控、快照新鲜度、
指数退避（5s→60s、稳定 60s 后复位 1s）与 acceleratorState 取值。
"""
from __future__ import annotations

import unittest

from openpilot.sunnypilot.accelerators.remote import switching


class GatingTests(unittest.TestCase):
  def test_window_requires_fresh_and_disengaged(self):
    self.assertTrue(switching.window_open(engaged=False, age=0.1))
    self.assertFalse(switching.window_open(engaged=True, age=0.1))
    self.assertFalse(switching.window_open(engaged=False, age=0.5))
    self.assertFalse(switching.window_open(engaged=False, age=-1.0))

  def test_snapshot_freshness_boundaries(self):
    self.assertTrue(switching.snapshot_fresh(0.0))
    self.assertTrue(switching.snapshot_fresh(switching.ENGAGEMENT_MAX_AGE - 1e-6))
    self.assertFalse(switching.snapshot_fresh(switching.ENGAGEMENT_MAX_AGE))

  def test_engagement_from_messages(self):
    self.assertTrue(switching.engagement_from(enabled=True, lat_active=False, long_active=False, valid=True))
    self.assertTrue(switching.engagement_from(enabled=False, lat_active=True, long_active=False, valid=True))
    self.assertTrue(switching.engagement_from(enabled=False, lat_active=False, long_active=True, valid=True))
    self.assertFalse(switching.engagement_from(enabled=False, lat_active=False, long_active=False, valid=True))
    # 无有效信息时保守判 engaged，绝不在盲区换入
    self.assertTrue(switching.engagement_from(enabled=False, lat_active=False, long_active=False, valid=False))


class BackoffTests(unittest.TestCase):
  def test_exponential_growth_and_cap(self):
    failures, delay = 0, None
    seen = []
    for _ in range(6):
      failures, delay = switching.next_backoff(failures, None)
      seen.append(delay)
    self.assertEqual(seen, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0])

  def test_stable_run_resets_to_quick(self):
    failures = 5
    failures, delay = switching.next_backoff(failures, switching.STABLE_SECONDS + 1.0)
    self.assertEqual((failures, delay), (1, switching.REJOIN_DELAY_QUICK))

  def test_unstable_run_keeps_growing_from_failure_one(self):
    failures, delay = switching.next_backoff(5, 3.0)
    self.assertEqual((failures, delay), (6, 60.0))

  def test_backoff_holder(self):
    backoff = switching.Backoff()
    self.assertEqual(backoff.on_failure(None), 5.0)
    self.assertEqual(backoff.on_failure(None), 10.0)
    self.assertEqual(backoff.failures, 2)
    backoff.reset()
    self.assertEqual((backoff.failures, backoff.delay), (0, 0.0))


class StateNameTests(unittest.TestCase):
  def test_state_names(self):
    self.assertEqual(switching.big_model_state(stopped=True, active_is_big=False, available=True,
                                               failures=0), 'unavailable')
    self.assertEqual(switching.big_model_state(stopped=False, active_is_big=True, available=True,
                                               failures=0), 'running')
    self.assertEqual(switching.big_model_state(stopped=False, active_is_big=False, available=True,
                                               failures=0), 'ready')
    self.assertEqual(switching.big_model_state(stopped=False, active_is_big=False, available=False,
                                               failures=2), 'retrying')
    self.assertEqual(switching.big_model_state(stopped=False, active_is_big=False, available=False,
                                               failures=0), 'joining')


if __name__ == '__main__':
  unittest.main()
