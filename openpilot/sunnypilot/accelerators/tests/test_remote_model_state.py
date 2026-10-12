"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

远端 `RemoteModelState` + `JoinedModel` 的离线单测：本地回环假 OPRP 服务端
（复用 `_fake_server.py`）+ 假小模型，无需 tinygrad / 真机。

`RemoteModelState.run()` 现在是**重叠流水线**：本帧 warp/submit 交给后台
worker，主线程只取上一帧已就绪的结果返回。因此这些用例显式调用
`wait_idle()`（模拟「下一帧到来时 worker 已完成 submit 与回包」）。

覆盖：
* 正常帧：warp 触发、上行张量集合正确（含 prev_feat 未上行）、输出被解析；
* 延迟一帧：`output_context` 绑定返回结果所属帧，而不是当前帧；
* 远端超时 → 同帧降级且返回小模型结果、后台线程不泄漏不挂死；
* 远端输出非有限 → 同帧降级；
* `chestnut` 属性在远端成功 / 降级两种情况下取值正确；
* 首帧上行带 FLAG_RESET_TEMPORAL；
* 3 帧流水线：返回/发布的 frame_id 与上下文一致，且不与相邻帧错位。
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from openpilot.sunnypilot.accelerators.remote.client import RemotePipelineClient
from openpilot.sunnypilot.accelerators.remote.joined import JoinedModel
from openpilot.sunnypilot.accelerators.remote.model_state import RemoteModelState
from openpilot.sunnypilot.accelerators.remote.protocol import FLAG_RESET_TEMPORAL
from openpilot.sunnypilot.accelerators.remote.status import RemoteStatus
from openpilot.sunnypilot.accelerators.tests import _fake_server
from openpilot.sunnypilot.accelerators.tests._fake_server import FakeRemoteServer

SIG = 'ab' * 32
CAM_W, CAM_H = 1344, 760
DESIRE_LEN = 8


def sigmoid(x: float) -> float:
  return 1.0 / (1.0 + np.exp(-x))


def make_metadata() -> dict:
  return {
    'input_shapes': {
      'img': (1, 12, 128, 256),
      'big_img': (1, 12, 128, 256),
      'desire_pulse': (1, 25, 8),
      'traffic_convention': (1, 2),
      'action_t': (1, 2),
    },
    # 只用 lead_prob（sigmoid）做可预测的解析断言
    'output_slices': {'lead_prob': slice(0, 1)},
  }


class _FakeWarped:
  def numpy(self):
    return np.zeros((2, 6, 128, 256), dtype=np.uint8)


class _FakeWarp:
  def __init__(self):
    self.calls = 0

  def __call__(self, **kwargs):
    self.calls += 1
    assert 'tfm' in kwargs and 'big_tfm' in kwargs and 'frame' in kwargs and 'big_frame' in kwargs
    return _FakeWarped()


def make_queue_factory():
  def factory(_self):
    npy = {
      'tfm': np.zeros((3, 3), dtype=np.float32),
      'big_tfm': np.zeros((3, 3), dtype=np.float32),
      'desire': np.zeros((DESIRE_LEN,), dtype=np.float32),
      'traffic_convention': np.zeros((1, 2), dtype=np.float32),
      'action_t': np.zeros((1, 2), dtype=np.float32),
    }
    return {'tfm': npy['tfm'], 'big_tfm': npy['big_tfm']}, npy
  return factory


class _TestableRemote(RemoteModelState):
  """绕过 tinygrad：帧指针改由占位对象代替。"""

  def _frame_tensor(self, key, buf):
    return object()


class _FakeSmall:
  """最小小模型：只提供鸭子接口与可识别的返回值。"""

  def __init__(self):
    self.constants = SimpleNamespace(DESIRE_LEN=DESIRE_LEN, MODEL_FREQ=20, T_IDXS=[0.0])
    self.desire_key = 'desire'
    self.numpy_inputs = {'desire': np.zeros(DESIRE_LEN, np.float32)}
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


class _FakeSM:
  def __init__(self, enabled=False, lat=False, long=False, valid=True, seen=True, recv_at=100.0):
    self.valid = {'selfdriveState': valid, 'carControl': valid}
    self.seen = {'selfdriveState': seen}
    self.recv_time = {'selfdriveState': recv_at}
    self._sds = SimpleNamespace(enabled=enabled)
    self._cc = SimpleNamespace(latActive=lat, longActive=long)

  def __getitem__(self, key):
    return {'selfdriveState': self._sds, 'carControl': self._cc}[key]


class _Clock:
  def __init__(self, now=100.0):
    self.now = now

  def __call__(self):
    return self.now


class RemoteModelStateTests(unittest.TestCase):
  def setUp(self):
    self.server = FakeRemoteServer(signature=SIG)
    self.server.start()
    self.addCleanup(self.server.close)
    self.client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=200.0,
                                       status=RemoteStatus())
    self.addCleanup(self.client.close)
    self.client.connect_and_hello()
    self.warp = _FakeWarp()
    self.remote = self.make_remote()
    self.addCleanup(self.remote.close)
    self.bufs = {'img': SimpleNamespace(data=np.zeros(8, np.uint8), frame_id=2),
                 'big_img': SimpleNamespace(data=np.zeros(8, np.uint8), frame_id=2)}
    self.transforms = {'img': np.eye(3, dtype=np.float32), 'big_img': np.eye(3, dtype=np.float32)}

  def make_remote(self, **kwargs):
    return _TestableRemote(CAM_W, CAM_H, self.client, metadata=make_metadata(),
                           warp_jit=self.warp, queue_factory=make_queue_factory(),
                           device='CPU', **kwargs)

  def inputs(self, desire_index=2):
    desire = np.zeros(DESIRE_LEN, dtype=np.float32)
    if desire_index is not None:
      desire[desire_index] = 1.0
    return {'desire': desire,
            'traffic_convention': np.array([1.0, 0.0], dtype=np.float32),
            'action_t': np.array([0.1, 0.2], dtype=np.float32)}

  def frame(self, remote, frame_id, ts_eof, context=None, inputs=None, bufs=None, wait=True):
    """跑一帧：提交到流水线后等 worker 完成 submit（模拟下一帧到来）。"""
    remote.set_frame(frame_id, ts_eof, context=context)
    outputs = remote.run(bufs or self.bufs, self.transforms, inputs or self.inputs())
    if wait:
      self.assertTrue(remote.wait_idle(2.0), 'worker 未在超时内完成')
    return outputs

  # -- 基础路径 ---------------------------------------------------------------

  def test_normal_frame_is_delayed_one_frame_and_parsed(self):
    # 第 1 帧：远端结果尚未就绪 → 返回 None（调用方同帧跑 small）
    self.assertIsNone(self.frame(self.remote, 1, 111, context={'camera_frame_id': 9, 'v_ego': 3.0}))
    # 第 2 帧：拿到第 1 帧的远端结果
    outputs = self.frame(self.remote, 2, 222, context={'camera_frame_id': 10, 'v_ego': 4.0})

    self.assertEqual(self.warp.calls, 2)
    self.assertTrue(self.remote.chestnut)
    self.assertIn('lead_prob', outputs)
    self.assertAlmostEqual(float(outputs['lead_prob'][0, 0]), sigmoid(1.0), places=5)

    context = self.remote.output_context
    self.assertEqual(context['frame_id'], 1)          # 结果属于第 1 帧
    self.assertEqual(context['timestamp_eof'], 111)
    self.assertEqual(context['camera_frame_id'], 9)   # 注入的发布字段随帧旅行
    self.assertEqual(context['v_ego'], 3.0)

    self.assertEqual(len(self.server.received), 2)
    record = self.server.received[0]
    self.assertEqual((record.frame_id, record.ts_eof), (1, 111))
    self.assertEqual(set(record.tensor_names), {'warped', 'desire', 'traffic_convention', 'action_t'})
    self.assertTrue(record.reset)  # 首帧必须带 FLAG_RESET_TEMPORAL
    self.assertEqual(record.flags & FLAG_RESET_TEMPORAL, FLAG_RESET_TEMPORAL)

  def test_three_frames_bind_context_without_off_by_one(self):
    """N 帧返回的是 N-1 帧的结果，上下文字段逐帧一致，绝不与相邻帧错位。"""
    self.assertIsNone(self.frame(self.remote, 10, 1000, context={'camera_frame_id': 110, 'v_ego': 1.0}))
    outputs = self.frame(self.remote, 11, 1100, context={'camera_frame_id': 111, 'v_ego': 2.0})
    self.assertEqual(self.remote.output_context['frame_id'], 10)
    self.assertEqual(self.remote.output_context['camera_frame_id'], 110)
    self.assertEqual(self.remote.output_context['v_ego'], 1.0)
    self.assertAlmostEqual(float(outputs['lead_prob'][0, 0]), sigmoid(10.0), places=5)

    outputs = self.frame(self.remote, 12, 1200, context={'camera_frame_id': 112, 'v_ego': 3.0})
    self.assertEqual(self.remote.output_context['frame_id'], 11)
    self.assertEqual(self.remote.output_context['camera_frame_id'], 111)
    self.assertEqual(self.remote.output_context['v_ego'], 2.0)
    self.assertAlmostEqual(float(outputs['lead_prob'][0, 0]), sigmoid(11.0), places=5)

    self.assertEqual([r.frame_id for r in self.server.received], [10, 11, 12])

  def test_uplink_scalars_captured_per_frame(self):
    """上行标量必须是提交帧的副本：下一帧改写输入不会污染在途请求。"""
    inputs = {'desire': np.eye(1, DESIRE_LEN, 3, dtype=np.float32).reshape(-1),
              'traffic_convention': np.array([1.0, 0.0], np.float32),
              'action_t': np.array([0.1, 0.2], np.float32)}
    self.frame(self.remote, 1, 1, inputs=inputs, wait=False)
    # 立刻改掉本地输入，模拟下一帧覆盖
    self.remote.numpy_inputs['action_t'][:] = np.array([9.0, 9.0], np.float32)
    self.assertTrue(self.remote.wait_idle(2.0))
    # 服务端收到的应为第 1 帧的副本：已经发出，不可能被后续覆盖影响
    self.assertEqual([r.frame_id for r in self.server.received], [1])
    self.assertEqual(self.remote._jobs.qsize(), 0)

  def test_required_scalar_missing_from_caller_inputs_is_still_uplinked(self):
    """契约要求 action_t，但调用方 inputs 只给小模型键时，也必须上行该标量。"""
    remote = self.make_remote()
    inputs = {'desire': np.zeros(DESIRE_LEN, np.float32)}   # 故意不含 traffic_convention/action_t
    remote.numpy_inputs['action_t'][:] = np.array([0.4, 0.5], np.float32)
    outputs = self.frame(remote, 1, 100, inputs=inputs)     # 首帧 None，请求已发

    record = self.server.received[0]
    self.assertEqual(set(record.tensor_names), {'warped', 'desire', 'traffic_convention', 'action_t'})
    self.assertIsNone(outputs)

  def test_desire_pulse_is_sparse_and_prev_feat_never_uplinked(self):
    self.frame(self.remote, 1, 1, inputs=self.inputs(desire_index=3))
    self.frame(self.remote, 2, 2, inputs=self.inputs(desire_index=3))  # 持续按住，无上升沿

    self.assertEqual([r.frame_id for r in self.server.received], [1, 2])
    self.assertNotIn('prev_feat', self.server.received[0].tensor_names)
    self.assertNotIn('feat_q', self.server.received[0].tensor_names)
    self.assertIn('desire', self.server.received[0].tensor_names)
    # 第二帧无上升沿：本地视图应被清零（pulse 语义）
    np.testing.assert_array_equal(self.remote.numpy_inputs['desire'], np.zeros(DESIRE_LEN, np.float32))

  # -- 降级 -------------------------------------------------------------------

  def test_timeout_degrades_same_frame(self):
    small = _FakeSmall()
    clock = _Clock(100.0)
    joined = JoinedModel(self.remote, small, clock=clock, enabled_loader=lambda: True)
    # 从一开始就不回包：否则帧1的响应可能在下面切换到 drop 之前就抢先返回（时序竞态）
    self.server._behavior = lambda rec: ('drop',)

    joined.update_runtime(_FakeSM(recv_at=clock.now), 5, 555, context={'camera_frame_id': 55})
    first = joined.run(self.bufs, self.transforms, self.inputs())
    self.assertEqual(small.runs, 1)                       # 首帧无远端结果 → small
    self.assertAlmostEqual(float(first['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)
    self.assertIsNone(joined.output_context)

    # 第 2 帧：等待第 1 帧结果 → 超时 → 同帧降级
    self.assertTrue(self.remote.wait_idle(2.0))           # 帧 1 的请求确实已在途
    self.remote._handles[0].deadline = time.monotonic() + 0.05
    clock.now += 0.05
    joined.update_runtime(_FakeSM(recv_at=clock.now), 6, 666, context={'camera_frame_id': 56})
    second = joined.run(self.bufs, self.transforms, self.inputs())
    self.assertEqual(small.runs, 2)
    self.assertAlmostEqual(float(second['lead_prob'][0, 0]), -1.0)  # 小模型结果
    self.assertFalse(joined.chestnut)
    self.assertFalse(self.remote.chestnut)
    # 单帧超时是「这一帧没赶上」，不是链路故障：不得触发链路级退避，
    # 也不得退出远端流水线（_joined 保持 True，下一帧继续用远端）。
    self.assertEqual(joined._backoff.failures, 0)
    self.assertTrue(joined._joined)

  def test_background_failure_degrades_without_hanging(self):
    """worker 路径异常（submit 抛错）必须被主线程原样抛出并同帧降级，不泄漏不挂死。"""
    remote = self.make_remote()

    def explode(*args, **kwargs):
      raise RuntimeError('submit boom')

    remote.client.submit = explode

    small = _FakeSmall()
    clock = _Clock(100.0)
    joined = JoinedModel(remote, small, clock=clock, enabled_loader=lambda: True)
    joined.update_runtime(_FakeSM(recv_at=clock.now), 1, 100, context={'camera_frame_id': 1})
    joined.run(self.bufs, self.transforms, self.inputs())   # 提交（worker 即将抛错，本轮走 small）
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and remote._worker_error is None:
      time.sleep(0.005)

    clock.now += 0.05
    joined.update_runtime(_FakeSM(recv_at=clock.now), 2, 200, context={'camera_frame_id': 2})
    out = joined.run(self.bufs, self.transforms, self.inputs())
    self.assertEqual(small.runs, 2)
    self.assertAlmostEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)
    self.assertGreater(joined._backoff.failures, 0)
    self.assertIn('后台', joined.last_error)
    self.assertFalse(remote._worker.is_alive())

  def test_non_finite_output_degrades_same_frame(self):
    nan_record = {'raw_outputs_0': np.full((4,), np.nan, dtype=np.float32)}
    with mock.patch.object(_fake_server, '_ok_record', lambda *a, **k: nan_record):
      remote = self.make_remote()
      self.addCleanup(remote.close)
      small = _FakeSmall()
      clock = _Clock(100.0)
      joined = JoinedModel(remote, small, clock=clock, enabled_loader=lambda: True)
      joined.update_runtime(_FakeSM(recv_at=clock.now), 6, 666, context={'camera_frame_id': 6})
      joined.run(self.bufs, self.transforms, self.inputs())   # 首帧 small
      self.assertTrue(remote.wait_idle(2.0))
      clock.now += 0.05
      joined.update_runtime(_FakeSM(recv_at=clock.now), 7, 777, context={'camera_frame_id': 7})
      outputs = joined.run(self.bufs, self.transforms, self.inputs())

    self.assertEqual(small.runs, 2)
    self.assertAlmostEqual(float(outputs['lead_prob'][0, 0]), -1.0)
    self.assertFalse(joined.chestnut)
    self.assertIn('non-finite', joined.last_error)

  def test_softmax_parse_uses_writable_copy(self):
    # 协议解出的张量是只读的；softmax 会就地改写，必须走可写副本（flatten），否则崩
    metadata = make_metadata()
    metadata['output_slices'] = {'desire_state': slice(0, 8)}
    remote = _TestableRemote(CAM_W, CAM_H, self.client, metadata=metadata, warp_jit=self.warp,
                             queue_factory=make_queue_factory(), device='CPU')
    self.addCleanup(remote.close)
    record = {'raw_outputs_0': np.full((8,), 1.0, dtype=np.float32)}
    with mock.patch.object(_fake_server, '_ok_record', lambda *a, **k: record):
      remote.set_frame(9, 99, context={'camera_frame_id': 9})
      remote.run(self.bufs, self.transforms, self.inputs())
      self.assertTrue(remote.wait_idle(2.0))
      remote.set_frame(10, 100, context={'camera_frame_id': 10})
      outputs = remote.run(self.bufs, self.transforms, self.inputs())
    self.assertAlmostEqual(float(np.sum(outputs['desire_state'])), 1.0, places=5)

  def test_worker_never_receives_device_backed_warp_view(self):
    """QCOM 的 `.numpy()` 是零拷贝设备视图：交给 worker 前必须复制为私有 CPU 缓冲。

    构造一个返回非拥有视图（owndata=False）的假 warp，捕获真正交给 worker 的
    `warped`；随后改写底层 buffer，证明在途 job 持有的是一份独立副本，而不是
    与主线程下一帧 warp 共享的设备显存。
    """
    backing = np.ones((2, 8, 128, 256), dtype=np.uint8)

    class _ViewWarp:
      def __call__(self, **kwargs):
        return SimpleNamespace(numpy=lambda: backing[:, :6])

    remote = _TestableRemote(CAM_W, CAM_H, self.client, metadata=make_metadata(),
                             warp_jit=_ViewWarp(), queue_factory=make_queue_factory(),
                             device='QCOM')
    self.addCleanup(remote.close)
    self.assertFalse(backing[:, :6].flags.owndata)   # 前提：假 warp 返回的是视图

    captured = {}
    original_submit = remote.client.submit

    def spy(frame_id, ts_eof, tensors, flags=0, deadline=None, reset=False):
      captured['warped'] = (tensors['warped'], np.array(tensors['warped']))
      return original_submit(frame_id, ts_eof, tensors, flags=flags, deadline=deadline, reset=reset)

    remote.client.submit = spy
    self.frame(remote, 1, 100, wait=False)
    self.assertTrue(remote.wait_idle(2.0))

    job_warped, snapshot = captured['warped']
    self.assertTrue(job_warped.flags.owndata)
    self.assertIsNone(job_warped.base)
    self.assertFalse(np.shares_memory(job_warped, backing))
    # 底层设备 buffer 被下一帧 warp 覆盖，也不得影响在途请求
    backing[:] = 7
    np.testing.assert_array_equal(job_warped, snapshot)

  # -- 原始异常（供包装层捕获） ------------------------------------------------

  def test_single_frame_timeout_is_tolerated_without_link_backoff(self):
    """单帧 collect 超时不再抛给包装层：返回 None 让上层同帧降级，链路与流水线保持。"""
    self.frame(self.remote, 1, 100)              # 帧 1：返回 None，请求已在途
    self.server._behavior = lambda rec: ('drop',)
    self.frame(self.remote, 2, 200)              # 帧 2 里 collect(帧 1) 超时 → 返回 None
    self.assertTrue(self.remote.wait_idle(2.0))

    gen = self.remote._generation
    self.remote.set_frame(3, 300, context={'camera_frame_id': 3})
    # 不抛异常；本帧交给上层跑 small
    self.assertIsNone(self.remote.run(self.bufs, self.transforms, self.inputs()))
    self.assertFalse(self.remote.chestnut)
    self.assertIsNone(self.remote.output_context)
    # 未 abort 流水线（generation 不变），下一帧仍可继续收远端结果
    self.assertEqual(self.remote._generation, gen)

  def test_abort_pipeline_clears_in_flight_and_arms_reset(self):
    self.frame(self.remote, 1, 100)
    self.frame(self.remote, 2, 200)
    self.remote.abort_pipeline()                  # 丢弃在途 handle、清空 pending
    self.remote.set_frame(3, 300, context={'camera_frame_id': 3})
    # 重置后：`run` 不会收集到任何旧结果（交由调用方同帧跑 small）
    self.assertIsNone(self.remote.run(self.bufs, self.transforms, self.inputs()))
    # 重置后第一帧上行必须重新带 reset，且是新帧（第 3 帧）
    self.assertTrue(self.remote.wait_idle(2.0))
    record = self.server.received[-1]
    self.assertEqual(record.frame_id, 3)
    self.assertEqual(record.flags & FLAG_RESET_TEMPORAL, FLAG_RESET_TEMPORAL)

  def test_close_stops_worker_and_is_idempotent(self):
    self.frame(self.remote, 1, 100)
    self.remote.close()
    self.remote.close()
    worker = self.remote._worker
    if worker is not None:
      self.assertFalse(worker.is_alive())
    # 关闭后重复调用不得挂死（wait_idle 是阻塞等待，不返回值语义）
    self.remote.wait_idle(0.5)


if __name__ == '__main__':
  unittest.main()
