"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

`JoinedModel` 换入/降级状态机的离线单测（无 socket、无 tinygrad）：
* 窗口未开（engaged / 快照过期）时走小模型；
* 窗口开启且远端可用时换入远端；
* 换入后不因 engaged 而换出；
* 远端失败同帧降级且 chestnut=False；
* 退避曲线 5s→10s→…→60s，稳定运行 60s 后复位 1s；
* 远端不可用且重连失败时同样计入退避；
* after_enqueue 一帧最多触发一次。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from openpilot.sunnypilot.accelerators.remote import switching
from openpilot.sunnypilot.accelerators.remote.joined import JoinedModel


class _Clock:
  def __init__(self, now=0.0):
    self.now = now

  def __call__(self):
    return self.now


class _FakeSM:
  def __init__(self, enabled=False, lat=False, long=False, valid=True):
    self.valid = {'selfdriveState': valid, 'carControl': valid}
    self.seen = {'selfdriveState': True}
    self.recv_time = {'selfdriveState': 0.0}
    self._sds = SimpleNamespace(enabled=enabled)
    self._cc = SimpleNamespace(latActive=lat, longActive=long)

  def __getitem__(self, key):
    return {'selfdriveState': self._sds, 'carControl': self._cc}[key]


class _FakeRemote:
  """可控远端：fail_mode = none / before / after（after 表示已 enqueue 后才失败）。"""

  def __init__(self, fail_mode='none', available=True, not_ready_frames=0):
    self.fail_mode = fail_mode
    self.available = available
    self.not_ready = int(not_ready_frames)
    self.runs = 0
    self.reconnects = 0
    self.resets = 0
    self.frames = []
    self.lat_delay = 0.0
    self.PLANPLUS_CONTROL = 1.0
    self.warmups = 0
    self.chestnut = False
    self.output_context = None
    self._frame_context = None
    self._ready_context = None

  def arm_reset(self):
    self.resets += 1

  def reconnect(self):
    self.reconnects += 1
    if not self.available:
      raise RuntimeError('reconnect failed')
    self.resets += 1

  def set_frame(self, frame_id, ts_eof, context=None):
    self.frames.append((frame_id, ts_eof))
    # 与真实实现一致：set_frame 只暂存「本帧上下文」，绝不触碰 output_context
    self._frame_context = dict(context or {}, frame_id=int(frame_id), timestamp_eof=int(ts_eof))

  def warmup(self):
    self.warmups += 1

  def run(self, bufs, transforms, inputs, after_enqueue=None):
    """模拟重叠流水线：本帧提交、返回**上一帧**已就绪的结果；无则返回 None。"""
    self.runs += 1
    # 上一帧提交时暂存的上下文，本帧才「就绪」（真实实现里它随 handle 一起排队）
    ready, self._frame_context = getattr(self, '_ready_context', None), self._frame_context
    self._ready_context = self._frame_context
    if self.fail_mode == 'before':
      raise RuntimeError('remote boom')
    if after_enqueue is not None:
      after_enqueue()
    if self.fail_mode == 'after':
      raise RuntimeError('remote boom after enqueue')
    if ready is None or self.runs <= self.not_ready:
      # 本帧无结果（首帧/未就绪）：上下文留空，绝不冒充「本帧结果」
      self.output_context = None
      self.chestnut = False
      return None
    self.output_context = ready
    self.chestnut = True
    return {'lead_prob': np.full((1, 1), 9.0, dtype=np.float32)}

  def get_action_from_model(self, *args, **kwargs):
    return 'remote-action'


class _FakeSmall:
  def __init__(self):
    self.constants = SimpleNamespace(DESIRE_LEN=8, MODEL_FREQ=20, T_IDXS=[0.0])
    self.desire_key = 'desire'
    self.numpy_inputs = {'desire': np.zeros(8, np.float32)}
    self.vision_input_names = ['img', 'big_img']
    self.generation = 1
    self.lat_delay = 0.0
    self.LAT_SMOOTH_SECONDS = 0.0
    self.LONG_SMOOTH_SECONDS = 0.0
    self.MIN_LAT_CONTROL_SPEED = 0.3
    self.PLANPLUS_CONTROL = 1.0
    self.chestnut = False
    self.runs = 0
    self.warmups = 0

  @property
  def mlsim(self):
    return False

  def warmup(self):
    self.warmups += 1

  def run(self, bufs, transforms, inputs, after_enqueue=None):
    self.runs += 1
    if after_enqueue is not None:
      after_enqueue()
    return {'lead_prob': np.full((1, 1), -1.0, dtype=np.float32)}

  def get_action_from_model(self, *args, **kwargs):
    return 'small-action'


class _RecoveringRemote(_FakeRemote):
  """reconnect() 一旦成功即变为可用（模拟笔记本晚于点火启动 / 服务后起）。"""

  def __init__(self, **kw):
    super().__init__(**kw)
    self.aborts = 0

  def abort_pipeline(self):
    self.aborts += 1

  def reconnect(self):
    self.reconnects += 1
    self.available = True
    self.resets += 1


BUF = {'img': object(), 'big_img': object()}
T = {'img': np.eye(3, dtype=np.float32), 'big_img': np.eye(3, dtype=np.float32)}
IN = {'desire': np.zeros(8, dtype=np.float32)}


class JoinedModelTests(unittest.TestCase):
  def make(self, *, fail_mode='none', available=True, clock=None, **kw):
    clock = clock or _Clock(0.0)
    remote = _FakeRemote(fail_mode=fail_mode, available=available,
                         not_ready_frames=kw.pop('not_ready_frames', 0))
    small = _FakeSmall()
    # 既有用例默认「运行期开关开启」；enabled 语义由 EnabledTests 单独覆盖
    kw.setdefault('enabled_loader', lambda: True)
    joined = JoinedModel(remote, small, clock=clock, **kw)
    return joined, remote, small, clock

  def feed(self, joined, clock, sm, frame_id=1, ts_eof=100):
    sm.recv_time['selfdriveState'] = clock.now
    joined.update_runtime(sm, frame_id, ts_eof)

  def test_window_closed_runs_small(self):
    joined, remote, small, clock = self.make()
    self.feed(joined, clock, _FakeSM(enabled=True))
    out = joined.run(BUF, T, IN)
    self.assertEqual((small.runs, remote.runs), (1, 0))
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)

  def test_stale_snapshot_runs_small(self):
    joined, remote, small, clock = self.make()
    sm = _FakeSM()
    clock.now = switching.ENGAGEMENT_MAX_AGE + 1.0  # sm.recv_time 仍是 0.0 → 快照过期
    joined.update_runtime(sm, 1, 100)
    joined.run(BUF, T, IN)
    self.assertEqual((small.runs, remote.runs), (1, 0))

  def test_window_open_joins_remote(self):
    joined, remote, small, clock = self.make(not_ready_frames=1)
    self.feed(joined, clock, _FakeSM())
    out = joined.run(BUF, T, IN)
    # 换入（首帧无远端结果 → 同帧 small），后续帧才发布远端结果
    self.assertEqual((remote.runs, small.runs), (1, 1))
    self.assertFalse(joined.chestnut)
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertGreaterEqual(remote.resets, 1)   # 换入时 arm_reset
    self.assertEqual(remote.frames[0], (1, 100))  # 本帧时序被注入

    clock.now += 0.05
    self.feed(joined, clock, _FakeSM(), frame_id=2, ts_eof=200)
    out = joined.run(BUF, T, IN)
    self.assertEqual((remote.runs, small.runs), (2, 1))
    self.assertTrue(joined.chestnut)
    self.assertEqual(float(out['lead_prob'][0, 0]), 9.0)

  def test_stays_remote_after_engagement(self):
    joined, remote, small, clock = self.make(not_ready_frames=1)
    self.feed(joined, clock, _FakeSM())
    joined.run(BUF, T, IN)                     # 换入（首帧无结果 → small）
    clock.now += 1.0
    self.feed(joined, clock, _FakeSM(enabled=True), frame_id=2, ts_eof=200)  # 已 engaged，窗口关闭
    joined.run(BUF, T, IN)
    self.assertEqual((remote.runs, small.runs), (2, 1))  # 不再换入/换出，远端第 2 帧发布
    self.assertTrue(joined.chestnut)

  def test_failure_degrades_same_frame_with_backoff(self):
    joined, remote, small, clock = self.make(fail_mode='before')
    self.feed(joined, clock, _FakeSM())
    out = joined.run(BUF, T, IN)
    self.assertEqual((small.runs, remote.runs), (1, 1))
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)
    self.assertEqual(joined._backoff.delay, switching.REJOIN_DELAY)
    self.assertIsNotNone(joined.last_error)

    joined.run(BUF, T, IN)  # 退避窗口内不再尝试
    self.assertEqual(remote.runs, 1)

  def test_backoff_curve_5_to_60(self):
    joined, remote, small, clock = self.make(fail_mode='before')
    seen = []
    for now in (0.0, 5.0, 15.0, 35.0, 75.0, 135.0):
      clock.now = now
      self.feed(joined, clock, _FakeSM())
      joined.run(BUF, T, IN)
      seen.append(joined._backoff.delay)
    self.assertEqual(seen, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0])

  def test_stable_run_resets_backoff_to_quick(self):
    joined, remote, small, clock = self.make(fail_mode='before')
    self.feed(joined, clock, _FakeSM())
    joined.run(BUF, T, IN)                 # 换入后立即失败，先建立 using_remote
    joined._using_remote = True
    joined._connected_since = clock.now - (switching.STABLE_SECONDS + 1.0)
    joined._backoff.failures = 5
    joined.run(BUF, T, IN)
    self.assertEqual((joined._backoff.failures, joined._backoff.delay), (1, switching.REJOIN_DELAY_QUICK))

  def test_unavailable_remote_reconnect_failure_backs_off(self):
    joined, remote, small, clock = self.make(available=False)
    self.feed(joined, clock, _FakeSM())
    joined.run(BUF, T, IN)
    self.assertEqual((remote.reconnects, remote.runs, small.runs), (1, 0, 1))
    self.assertEqual(joined._backoff.delay, switching.REJOIN_DELAY)

  def test_after_enqueue_fires_once_when_remote_fails_after_enqueue(self):
    joined, remote, small, clock = self.make(fail_mode='after')
    calls = []
    self.feed(joined, clock, _FakeSM())
    joined.run(BUF, T, IN, after_enqueue=lambda: calls.append(1))
    self.assertEqual(len(calls), 1)
    self.assertEqual(small.runs, 1)

  def test_warmup_forwards_to_both(self):
    joined, remote, small, clock = self.make()
    joined.warmup()
    self.assertEqual((small.warmups, remote.warmups), (1, 1))

  def test_not_ready_first_frame_runs_small_and_keeps_pipeline(self):
    joined, remote, small, clock = self.make(not_ready_frames=1)
    sm = _FakeSM()
    joined.update_runtime(sm, 1, 100, context={'camera_frame_id': 1, 'v_ego': 0.0})
    out = joined.run(BUF, T, IN)
    self.assertEqual((small.runs, remote.runs), (1, 1))
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)
    self.assertIsNone(joined.output_context)
    self.assertTrue(joined.pipeline_active)   # 流水线保持，不重复换入

    # 第 2 帧：首个远端结果（第 1 帧）就绪 → 发布它，并绑定第 1 帧上下文
    clock.now += 0.05
    sm.recv_time['selfdriveState'] = clock.now
    joined.update_runtime(sm, 2, 200, context={'camera_frame_id': 2, 'v_ego': 0.0})
    out = joined.run(BUF, T, IN)
    self.assertEqual((small.runs, remote.runs), (1, 2))
    self.assertEqual(float(out['lead_prob'][0, 0]), 9.0)
    self.assertTrue(joined.chestnut)
    self.assertEqual(joined.output_context['frame_id'], 1)
    self.assertEqual(joined.output_context['camera_frame_id'], 1)

  def test_result_context_is_bound_to_result_frame_not_current(self):
    """远端返回的是**上一帧**结果：上下文字段必须整体随结果帧旅行。"""
    joined, remote, small, clock = self.make(not_ready_frames=1)
    sm = _FakeSM()
    joined.update_runtime(sm, 1, 100, context={'camera_frame_id': 101, 'v_ego': 1.0})
    joined.run(BUF, T, IN)                       # 第 1 帧：无结果 → small
    clock.now += 0.05
    sm.recv_time['selfdriveState'] = clock.now
    joined.update_runtime(sm, 2, 200, context={'camera_frame_id': 102, 'v_ego': 2.0})
    joined.run(BUF, T, IN)                       # 第 2 帧：发布第 1 帧的结果
    self.assertEqual(joined.output_context['frame_id'], 1)
    self.assertEqual(joined.output_context['timestamp_eof'], 100)
    self.assertEqual(joined.output_context['camera_frame_id'], 101)
    self.assertEqual(joined.output_context['v_ego'], 1.0)
    clock.now += 0.05
    sm.recv_time['selfdriveState'] = clock.now
    joined.update_runtime(sm, 3, 300, context={'camera_frame_id': 103, 'v_ego': 3.0})
    joined.run(BUF, T, IN)                       # 第 3 帧：发布第 2 帧的结果
    self.assertEqual(joined.output_context['frame_id'], 2)
    self.assertEqual(joined.output_context['camera_frame_id'], 102)
    self.assertEqual(joined.output_context['v_ego'], 2.0)

  def test_each_frame_publishes_while_pipeline_warms(self):
    """换入后每一帧都必须有可发布输出，且远端结果的上下文严格绑定结果帧。"""
    joined, remote, small, clock = self.make(not_ready_frames=1)
    sm = _FakeSM()
    seen = []
    for i in (1, 2, 3, 4):
      clock.now += 0.05
      sm.recv_time['selfdriveState'] = clock.now
      joined.update_runtime(sm, i, i * 100, context={'camera_frame_id': 10 * i, 'v_ego': float(i)})
      out = joined.run(BUF, T, IN)
      self.assertIsNotNone(out)
      frame = None if joined.output_context is None else joined.output_context['frame_id']
      if frame is not None:  # 远端结果绑定上一帧，绝不与当前帧错位
        self.assertEqual(frame, i - 1)
        self.assertEqual(joined.output_context['camera_frame_id'], 10 * (i - 1))
        self.assertEqual(joined.output_context['v_ego'], float(i - 1))
      seen.append((joined.chestnut, frame))
    self.assertEqual(seen, [(False, None), (True, 1), (True, 2), (True, 3)])


class EnabledRuntimeTests(unittest.TestCase):
  """运行期开关：未连接也能创建；false→true 自动连；true→false 同帧降级。"""

  def feed(self, joined, clock, sm, frame_id=1, ts_eof=100):
    sm.recv_time['selfdriveState'] = clock.now
    joined.update_runtime(sm, frame_id, ts_eof)

  def test_not_connected_run_returns_publishable_small_output(self):
    # (b) 未连接（reconnect 失败）时 run() 绝不抛异常、绝不返回 None，走 small
    clock = _Clock(0.0)
    remote = _FakeRemote(available=False)
    small = _FakeSmall()
    joined = JoinedModel(remote, small, clock=clock, enabled_loader=lambda: True)
    self.feed(joined, clock, _FakeSM())
    out = joined.run(BUF, T, IN)
    self.assertIsNotNone(out)
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)
    self.assertEqual((remote.reconnects, remote.runs, small.runs), (1, 0, 1))

  def test_enabled_false_then_true_joins_on_retry(self):
    # (c) 启动时 enabled=false → 只跑 small；运行中置 true → 下一个窗口连上换入
    clock = _Clock(0.0)
    remote = _RecoveringRemote(available=False, not_ready_frames=1)
    small = _FakeSmall()
    enabled = {'value': False}
    joined = JoinedModel(remote, small, clock=clock,
                         enabled_loader=lambda: enabled['value'], enabled_refresh=1.0)

    self.feed(joined, clock, _FakeSM(), frame_id=1, ts_eof=100)
    out = joined.run(BUF, T, IN)
    self.assertEqual((remote.reconnects, remote.runs, small.runs), (0, 0, 1))
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)

    # 打开开关；跨过 1s 缓存后窗口内重连并换入
    enabled['value'] = True
    clock.now += 1.5
    self.feed(joined, clock, _FakeSM(), frame_id=2, ts_eof=200)
    out = joined.run(BUF, T, IN)                 # 换入首帧：无远端结果 → 同帧 small
    self.assertEqual((remote.reconnects, remote.runs), (1, 1))
    self.assertFalse(joined.chestnut)
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)

    clock.now += 0.05
    self.feed(joined, clock, _FakeSM(), frame_id=3, ts_eof=300)
    out = joined.run(BUF, T, IN)                 # 第 2 帧：远端结果就绪
    self.assertTrue(joined.chestnut)
    self.assertEqual(float(out['lead_prob'][0, 0]), 9.0)

  def test_enabled_true_then_false_degrades_same_frame(self):
    # (d) 正在用 remote 时关闭开关 → 同帧降级回 small，发布不中断、流水线被丢弃
    clock = _Clock(0.0)
    remote = _RecoveringRemote(not_ready_frames=1)
    small = _FakeSmall()
    enabled = {'value': True}
    joined = JoinedModel(remote, small, clock=clock,
                         enabled_loader=lambda: enabled['value'], enabled_refresh=1.0)

    self.feed(joined, clock, _FakeSM(), frame_id=1, ts_eof=100)
    joined.run(BUF, T, IN)                       # 换入（首帧 small）
    clock.now += 0.05
    self.feed(joined, clock, _FakeSM(), frame_id=2, ts_eof=200)
    joined.run(BUF, T, IN)                       # 远端供数
    self.assertTrue(joined.chestnut)
    self.assertTrue(joined.pipeline_active)

    enabled['value'] = False
    clock.now += 1.5
    self.feed(joined, clock, _FakeSM(), frame_id=3, ts_eof=300)
    out = joined.run(BUF, T, IN)
    self.assertIsNotNone(out)
    self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)
    self.assertFalse(joined.pipeline_active)
    self.assertGreaterEqual(remote.aborts, 1)
    self.assertEqual(small.runs, 2)

  def test_reconnect_attempts_follow_backoff_5_to_60(self):
    # (e) 未连接时的重试间隔遵循退避：5s→翻倍→60s，退避窗口内不重复尝试
    clock = _Clock(0.0)
    remote = _FakeRemote(available=False)         # reconnect 永远抛错
    small = _FakeSmall()
    joined = JoinedModel(remote, small, clock=clock, enabled_loader=lambda: True)
    seen = []
    for t in (0.0, 1.0, 6.0, 16.0, 36.0, 76.0, 136.0):
      clock.now = t
      self.feed(joined, clock, _FakeSM(), frame_id=int(t) + 1, ts_eof=1)
      joined.run(BUF, T, IN)
      seen.append((remote.reconnects, joined._backoff.delay))
    self.assertEqual(seen, [(1, 5.0), (1, 5.0), (2, 10.0), (3, 20.0),
                            (4, 40.0), (5, 60.0), (6, 60.0)])


if __name__ == '__main__':
  unittest.main()
