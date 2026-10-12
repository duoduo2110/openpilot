"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

「断链后同一 modeld 进程内自动恢复」的离线回归（本地回环假服务端）：

1. **残留句柄不再让重连短路** —— 服务端重启（新进程、新会话）后，旧 socket 句柄
   仍在，`connect()` 过去会直接 return，重连成为空操作。必须拆掉重建 + 重新 HELLO。
2. **服务端 code 4（Inference before HELLO）判为链路级** —— 该帧作废并把链路判死，
   触发重新握手；绝不永久卡在「能发不能算」。
3. **后台重连监督器** —— 武装后链路掉线即按 1s→2s→…→30s 退避自动重建，无需重启
   进程；退避阶梯与 30s 封顶都被钉住。
4. **退避期间同帧回退本地小模型**（JoinedModel 层）—— 断链期间不出现控制空白。
5. **重新接入仍在安全窗口内** —— 行车中链路恢复也不换入，直到 disengaged。
6. **设置页 off→on 立即重武装并触发一次重连**（不等退避）。
7. **状态落盘含重连/恢复遥测**，UI 三态文案判据可用。
"""
from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from openpilot.sunnypilot.accelerators.remote import switching
from openpilot.sunnypilot.accelerators.remote.client import (RECONNECT_BASE_S, RECONNECT_MAX_S,
                                                               RemoteLinkError, RemotePipelineClient)
from openpilot.sunnypilot.accelerators.remote.joined import JoinedModel
from openpilot.sunnypilot.accelerators.remote.status import RemoteStatus
from openpilot.sunnypilot.accelerators.remote.status_writer import StatusWriter
from openpilot.sunnypilot.accelerators.tests._fake_server import FakeRemoteServer

SIG = 'ab' * 32
TS = 1_234_567_890


def inputs(seed: int = 0) -> dict[str, np.ndarray]:
  return {
    'warped': np.full((2, 6, 4, 8), seed, dtype=np.uint8),
    'packed': np.full(4, float(seed), dtype=np.float32),
  }


def wait_until(predicate, timeout: float = 5.0) -> bool:
  end = time.monotonic() + timeout
  while time.monotonic() < end:
    if predicate():
      return True
    time.sleep(0.005)
  return bool(predicate())


class _Base(unittest.TestCase):
  def setUp(self):
    self.server = FakeRemoteServer()
    self.server.start()
    self.addCleanup(self.server.close)

  def make_client(self, **kwargs) -> RemotePipelineClient:
    client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=400.0,
                                  status=RemoteStatus(), **kwargs)
    self.addCleanup(client.close)
    client.connect_and_hello()
    return client

  def restart_server(self, **kwargs):
    """在**同一端口**上重启服务端：模拟 PC 侧进程重启（会话全新、没握过手）。"""
    import socket as _socket
    port = self.server.port
    self.server.close()
    self.server = FakeRemoteServer(**kwargs)
    self.server._listener.close()
    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    listener.bind((self.server.host, port))
    listener.listen(1)
    listener.settimeout(0.2)
    self.server._listener = listener
    self.server.host, self.server.port = listener.getsockname()
    self.server.start()
    self.addCleanup(self.server.close)


class StaleHandleTests(_Base):
  def test_connect_rebuilds_stale_socket_instead_of_short_circuit(self):
    """根因回归：`_sock is not None` 不再让 connect()/connect_and_hello() 空转。"""
    client = self.make_client()
    hellos_before = len(self.server.hellos)
    # 人为制造「句柄还在、但链路已死」：置 _hello_done=False
    client._hello_done = False
    self.assertIsNotNone(client._sock)
    client.connect_and_hello()
    self.assertTrue(client._hello_done)
    # 且服务端收到了一次**新的** HELLO（不是复用旧句柄的空转）
    self.assertEqual(len(self.server.hellos), hellos_before + 1)

  def test_reconnect_rebuilds_and_reshakes(self):
    client = self.make_client()
    self.restart_server()
    client.reconnect()
    self.assertTrue(client.connected)
    self.assertEqual(len(self.server.hellos), 1)   # 新服务端只见到这一次 HELLO
    out = client.infer(frame_id=3, ts_eof=TS, tensors=inputs())
    self.assertEqual(float(out[0][0]), 3.0)        # 真的能推理

  def test_on_link_error_fully_detaches_socket(self):
    client = self.make_client()
    client._on_link_error('boom')
    self.assertIsNone(client._sock)
    self.assertFalse(client.connected)
    self.assertFalse(client.status.connected)


class ServerErrorCode4Tests(_Base):
  def test_code4_fails_frame_and_kills_link(self):
    """code 4 必须判链路级：本帧失败 + 链路判死（等重新握手），不是「只丢一帧」。"""
    self.restart_server(behavior=lambda rec: ('error', 4, 'Inference before HELLO handshake'))
    client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=400.0,
                                  status=RemoteStatus())
    self.addCleanup(client.close)
    client.connect_and_hello()
    with self.assertRaises(RemoteLinkError) as ctx:
      client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    self.assertIn('4', str(ctx.exception))
    self.assertFalse(client.connected)
    self.assertIsNone(client._sock)

  def test_code4_recovers_without_restarting_process(self):
    """端到端：服务端拒(code 4) → 重建+重握手 → 同一 client 进程恢复推理。"""
    self.restart_server(behavior=lambda rec: ('error', 4, 'Inference before HELLO handshake'))
    client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=400.0,
                                  status=RemoteStatus())
    self.addCleanup(client.close)
    client.connect_and_hello()
    with self.assertRaises(RemoteLinkError):
      client.infer(frame_id=1, ts_eof=TS, tensors=inputs())

    # 服务端恢复正常行为后，同一个 client 重建即可继续跑
    self.restart_server()
    client.reconnect()
    self.assertTrue(client.connected)
    out = client.infer(frame_id=2, ts_eof=TS, tensors=inputs())
    self.assertEqual(float(out[0][0]), 2.0)
    self.assertEqual(client.status.frames_ok, 1)


class StallWatchdogTests(_Base):
  """无进展看门狗：bulk/ffs 上「对端消失不报错」的唯一检出手段。"""

  def _pump_stall(self, client, seconds: float) -> bool:
    """持续提交（每帧丢弃 handle，模拟流水线空转），返回是否被判死。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
      try:
        client.discard(client.submit(1, TS, inputs()))
      except RemoteLinkError as exc:
        if 'stalled' in str(exc):
          return True
        if 'in flight' in str(exc):
          time.sleep(0.01)
          continue
        return True
      time.sleep(0.01)
    return False

  def test_no_progress_kills_link_and_wakes_supervisor(self):
    client = self.make_client()
    # 服务端收包但**永不回包**（复刻 ffs：device 侧 write 成功、read 阻塞不返回）
    self.restart_server(behavior=lambda rec: ('drop',))
    client.reconnect()
    client._stall_since = None
    client._cold_until = 0.0

    self.assertTrue(self._pump_stall(client, 5.0),
                    '连续提交却无任何成功帧 → 必须判定链路已死')
    self.assertFalse(client.connected, '判定后必须彻底断链')
    self.assertIsNone(client._sock, '判定后 socket 句柄必须被摘掉（否则重连会短路）')
    self.assertFalse(client.status.connected)

  def test_stall_then_armed_supervisor_recovers(self):
    """看门狗判死 → 武装着的监督器按退避重建 → 服务端恢复后重新出帧。"""
    client = self.make_client()
    client.arm_reconnect(True)
    self.addCleanup(lambda: client.arm_reconnect(False))
    self.restart_server(behavior=lambda rec: ('drop',))
    client.reconnect()
    baseline = client.status.frames_ok

    # 一边持续提交（触发看门狗），一边等服务端恢复
    def _pump():
      end = time.monotonic() + 25.0
      while time.monotonic() < end:
        try:
          client.discard(client.submit(1, TS, inputs()))
        except Exception:
          pass
        time.sleep(0.02)

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()
    # 看门狗生效：帧一直不落地（服务端不回包）
    time.sleep(3.0)
    self.assertEqual(client.status.frames_ok, baseline, '停摆期间不得有任何成功帧')
    self.assertGreaterEqual(client.reconnect_attempts + (0 if client.connected else 1), 0)
    # 服务端恢复回包行为 → 监督器自动重建，流水线重新出帧
    self.restart_server()
    self.assertTrue(wait_until(lambda: client.connected, timeout=25.0),
                    '武装着的监督器应在退避内自动重建链路')
    pump.join(timeout=1.0)
    out = client.infer(frame_id=7, ts_eof=TS, tensors=inputs())
    self.assertEqual(float(out[0][0]), 7.0, '恢复后必须重新出帧')
    self.assertGreater(client.status.frames_ok, baseline)

  def test_stall_watchdog_silent_on_healthy_link(self):
    client = self.make_client()
    client._stall_since = None
    client._cold_until = 0.0
    for i in range(25):
      client.infer(frame_id=i, ts_eof=TS, tensors=inputs())
    self.assertTrue(client.connected, '健康链路绝不能被看门狗误杀')
    self.assertEqual(client.status.frames_ok, 25)

  def test_cold_grace_suppresses_stall_detection(self):
    client = self.make_client()
    client._stall_since = None
    client._cold_until = time.monotonic() + 5.0   # 模拟刚重连
    # 宽限内连续提交不应被判死（在途名额满属于正常背压，与判死无关）
    for i in range(2):
      client.submit(i, TS, inputs())
    self.assertTrue(client.connected)


class ReconnectSupervisorTests(_Base):
  def test_backoff_staircase_caps_at_30s(self):
    client = self.make_client()
    self.assertEqual(client._reconnect_backoff_next(), RECONNECT_BASE_S)
    self.assertEqual(client._reconnect_backoff_next(), 2.0)
    self.assertEqual(client._reconnect_backoff_next(), 4.0)
    seen = [client._reconnect_backoff_next() for _ in range(8)]
    self.assertEqual(max(seen), RECONNECT_MAX_S)
    self.assertEqual(seen[-1], RECONNECT_MAX_S)
    self.assertEqual(RECONNECT_MAX_S, 30.0)
    self.assertEqual(RECONNECT_BASE_S, 1.0)

  def test_armed_supervisor_recovers_after_server_restart(self):
    client = self.make_client()
    client.arm_reconnect(True)
    self.addCleanup(lambda: client.arm_reconnect(False))
    epoch_before = client.recovery_epoch
    # 服务端整体重启（新会话）
    self.restart_server()
    # 客户端仍自认为已连接，直到第一次失败把它判死
    try:
      client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    except Exception:
      pass
    self.assertTrue(
      wait_until(lambda: client.connected, timeout=8.0),
      '后台监督器应在退避内自动恢复链路，无需重启进程',
    )
    self.assertGreater(client.recovery_epoch, epoch_before)
    out = client.infer(frame_id=9, ts_eof=TS, tensors=inputs())
    self.assertEqual(float(out[0][0]), 9.0)

  def test_disarm_stops_supervisor_and_closes_link(self):
    client = self.make_client()
    client.arm_reconnect(True)
    self.assertTrue(client.reconnect_armed)
    client.arm_reconnect(False)
    self.assertFalse(client.reconnect_armed)
    self.assertFalse(client.connected)
    self.restart_server()
    time.sleep(1.5)
    self.assertFalse(client.connected, '解除武装后不得再自动重连')

  def test_rearm_immediate_resets_backoff(self):
    client = self.make_client()
    client.arm_reconnect(True)
    self.addCleanup(lambda: client.arm_reconnect(False))
    for _ in range(3):
      client._reconnect_backoff_next()
    self.assertGreater(client.reconnect_backoff_s, 0.0)
    # 设置页 off→on：立即归零退避并唤醒
    client.arm_reconnect(False)
    client.arm_reconnect(True, immediate=True)
    self.assertEqual(client.reconnect_backoff_s, 0.0)
    self.assertEqual(client.reconnect_attempts, 0)


# -- JoinedModel 层：同帧回退 + 安全窗口 + 开关 -------------------------------


class _Clock:
  def __init__(self, now=0.0):
    self.now = now

  def __call__(self):
    return self.now


class _FakeSM:
  def __init__(self, enabled=False, lat=False, long=False, valid=True, recv_at=0.0):
    import types
    self.valid = {'selfdriveState': valid, 'carControl': valid}
    self.seen = {'selfdriveState': True}
    self.recv_time = {'selfdriveState': recv_at}
    self._sds = types.SimpleNamespace(enabled=enabled)
    self._cc = types.SimpleNamespace(latActive=lat, longActive=long)

  def __getitem__(self, key):
    return {'selfdriveState': self._sds, 'carControl': self._cc}[key]


class _FakeClient:
  """最小可用的 client 替身：connected 可切换、recovery_epoch 可推进。"""

  def __init__(self, connected=True):
    self.connected = connected
    self.recovery_epoch = 1
    self.reconnect_armed = False
    self.reconnect_backoff_s = 0.0
    self.reconnect_attempts = 0
    self.arms = []
    self.requests = 0

  def arm_reconnect(self, armed, immediate=False):
    self.reconnect_armed = bool(armed)
    self.arms.append((bool(armed), bool(immediate)))

  def request_reconnect(self):
    self.requests += 1

  def in_flight(self):
    return 0


class _FakeRemote:
  def __init__(self, connected=True):
    self.client = _FakeClient(connected)
    self.chestnut = False
    self.output_context = None
    self.resets = 0
    self.armed_resync = 0
    self.lat_delay = 0.0
    self.PLANPLUS_CONTROL = 1.0
    self.runs = 0
    self._ready = None
    self._pending = None

  @property
  def available(self):
    return bool(self.client.connected)

  def arm_reconnect(self, armed, immediate=False):
    self.client.arm_reconnect(armed, immediate=immediate)

  def request_reconnect(self):
    self.client.request_reconnect()

  def arm_reset(self):
    self.resets += 1

  def set_frame(self, frame_id, timestamp_eof, context=None):
    # 复刻真实重叠流水线：set_frame 存的是**本帧**上下文，run 发布的是**上一帧**结果
    self._pending = {'frame_id': int(frame_id), 'timestamp_eof': int(timestamp_eof), **(context or {})}

  def run(self, bufs, transforms, inputs, after_enqueue=None):
    self.runs += 1
    if after_enqueue is not None:
      after_enqueue()
    ready, self._ready = self._ready, self._pending
    self._pending = None
    if ready is None:
      self.chestnut = False
      self.output_context = None
      return None
    self.chestnut = True
    self.output_context = ready
    return {'lead_prob': np.full((1, 1), 9.0, dtype=np.float32)}

  def get_action_from_model(self, *a, **k):
    return 'remote-action'


class _FakeSmall:
  def __init__(self):
    import types
    self.constants = types.SimpleNamespace(DESIRE_LEN=8, MODEL_FREQ=20, T_IDXS=[0.0])
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

  @property
  def mlsim(self):
    return False

  def warmup(self):
    pass

  def run(self, bufs, transforms, inputs, after_enqueue=None):
    self.runs += 1
    if after_enqueue is not None:
      after_enqueue()
    return {'lead_prob': np.full((1, 1), -1.0, dtype=np.float32)}

  def get_action_from_model(self, *a, **k):
    return 'small-action'


BUF = {'img': object(), 'big_img': object()}
T = {'img': np.eye(3, dtype=np.float32), 'big_img': np.eye(3, dtype=np.float32)}
IN = {'desire': np.zeros(8, dtype=np.float32)}


class SameFrameFallbackTests(unittest.TestCase):
  def test_link_down_keeps_publishing_small_same_frame(self):
    remote = _FakeRemote(connected=False)
    small = _FakeSmall()
    clock = _Clock(0.0)
    joined = JoinedModel(remote, small, clock=clock, enabled_loader=lambda: True)
    for i in (1, 2, 3):
      clock.now += 0.05
      joined.update_runtime(_FakeSM(recv_at=clock.now), i, i * 100)
      out = joined.run(BUF, T, IN)
      self.assertIsNotNone(out, '断链期间绝不能出现控制空白')
      self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
    self.assertEqual(small.runs, 3)
    self.assertEqual(remote.runs, 0)
    self.assertFalse(joined.chestnut)


class SafetyWindowTests(unittest.TestCase):
  def test_rejoin_after_recovery_still_requires_window(self):
    """行车中链路恢复**不**换入；disengaged 后才换入。"""
    remote = _FakeRemote(connected=True)
    small = _FakeSmall()
    clock = _Clock(0.0)
    joined = JoinedModel(remote, small, clock=clock, enabled_loader=lambda: True)

    # 行车中（engaged）：即使链路可用也不换入
    clock.now += 0.05
    joined.update_runtime(_FakeSM(enabled=True, recv_at=clock.now), 1, 100)
    joined.run(BUF, T, IN)
    self.assertEqual(remote.runs, 0)
    self.assertEqual(small.runs, 1)
    self.assertFalse(joined.chestnut)

    # disengaged → 允许换入（首帧无旧结果 → 同帧 small，随后远端出结果）
    clock.now += 0.05
    joined.update_runtime(_FakeSM(recv_at=clock.now), 2, 200)
    joined.run(BUF, T, IN)
    clock.now += 0.05
    joined.update_runtime(_FakeSM(recv_at=clock.now), 3, 300)
    out = joined.run(BUF, T, IN)
    self.assertTrue(joined.chestnut)
    self.assertEqual(float(out['lead_prob'][0, 0]), 9.0)

  def test_window_closed_never_switches_back_mid_drive(self):
    remote = _FakeRemote(connected=True)
    small = _FakeSmall()
    clock = _Clock(0.0)
    joined = JoinedModel(remote, small, clock=clock, enabled_loader=lambda: True)
    for i in range(1, 6):
      clock.now += 0.05
      joined.update_runtime(_FakeSM(enabled=True, recv_at=clock.now), i, i * 100)
      out = joined.run(BUF, T, IN)
      self.assertIsNotNone(out)
      self.assertEqual(float(out['lead_prob'][0, 0]), -1.0)
      self.assertFalse(joined.chestnut)
    self.assertEqual(remote.runs, 0)


class EnabledToggleTests(unittest.TestCase):
  def test_off_to_on_rearms_with_immediate(self):
    remote = _FakeRemote(connected=False)
    small = _FakeSmall()
    clock = _Clock(0.0)
    flag = {'on': False}
    joined = JoinedModel(remote, small, clock=clock,
                         enabled_loader=lambda: flag['on'], enabled_refresh=1.0)
    clock.now += 0.05
    joined.update_runtime(_FakeSM(recv_at=clock.now), 1, 100)
    joined.run(BUF, T, IN)
    self.assertTrue(remote.client.arms, '开关为关时也应先解除武装一次')

    flag['on'] = True
    clock.now += 1.5
    joined.update_runtime(_FakeSM(recv_at=clock.now), 2, 200)
    joined.run(BUF, T, IN)
    armed = [a for a in remote.client.arms if a[0]]
    self.assertTrue(armed, 'off→on 必须重新武装重连监督器')
    self.assertTrue(armed[-1][1], 'off→on 必须带 immediate=True（不等退避）')
    self.assertEqual(joined._backoff.delay, 0.0, 'off→on 必须清零换入退避')
    self.assertEqual(joined._retry_at, 0.0)

  def test_enabled_false_disarms_supervisor(self):
    remote = _FakeRemote(connected=True)
    small = _FakeSmall()
    clock = _Clock(0.0)
    flag = {'on': True}
    joined = JoinedModel(remote, small, clock=clock,
                         enabled_loader=lambda: flag['on'], enabled_refresh=1.0)
    clock.now += 0.05
    joined.update_runtime(_FakeSM(recv_at=clock.now), 1, 100)
    joined.run(BUF, T, IN)
    self.assertTrue(remote.client.reconnect_armed)

    flag['on'] = False
    clock.now += 1.5
    joined.update_runtime(_FakeSM(recv_at=clock.now), 2, 200)
    joined.run(BUF, T, IN)
    self.assertFalse(remote.client.reconnect_armed)


class StatusPublishTests(unittest.TestCase):
  def test_link_publish_kwargs_expose_reconnect_telemetry(self):
    remote = _FakeRemote(connected=False)
    remote.client.reconnect_armed = True
    remote.client.reconnect_backoff_s = 8.0
    remote.client.reconnect_attempts = 3
    joined = JoinedModel(remote, _FakeSmall(), clock=_Clock(0.0), enabled_loader=lambda: True)
    kwargs = joined._link_publish_kwargs()
    self.assertEqual(kwargs['reconnect_armed'], True)
    self.assertEqual(kwargs['reconnect_backoff_s'], 8.0)
    self.assertEqual(kwargs['reconnect_attempts'], 3)

  def test_status_writer_round_trips_reconnect_fields(self):
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
      writer = StatusWriter(os.path.join(d, 'status.json'))
      snapshot = writer.snapshot(status=None, reconnect_armed=True, reconnect_backoff_s=4.0,
                                 reconnect_attempts=2, recovered_at=1234.5)
      self.assertTrue(snapshot['reconnect_armed'])
      self.assertEqual(snapshot['reconnect_backoff_s'], 4.0)
      self.assertEqual(snapshot['reconnect_attempts'], 2)
      self.assertEqual(snapshot['recovered_at'], 1234.5)

  def test_remote_status_round_trip(self):
    status = RemoteStatus()
    status.set_reconnect(True, 2.5, 4)
    status.mark_recovered(999.0)
    restored = RemoteStatus.from_dict(status.to_dict())
    self.assertTrue(restored.reconnect_armed)
    self.assertEqual(restored.reconnect_backoff_s, 2.5)
    self.assertEqual(restored.reconnect_attempts, 4)
    self.assertEqual(restored.recovered_at, 999.0)


class LinkDataTests(unittest.TestCase):
  def setUp(self):
    from openpilot.selfdrive.ui.sunnypilot import accelerator_link_data as mod
    self.mod = mod
    self.now = 1_000_000.0

  def _status(self, **kw):
    base = {'ts': self.now, 'state': 'running', 'enabled': True, 'big_active': False,
            'connected': True, 'reconnect_armed': True, 'reconnect_backoff_s': 0.0,
            'reconnect_attempts': 0, 'recovered_at': 0.0}
    base.update(kw)
    return base

  def test_reconnecting_shows_backoff(self):
    text = self.mod.link_mode_text(
      self._status(connected=False, state='retrying', reconnect_backoff_s=16.0), self.now)
    self.assertEqual(text, '重连中（退避 16s）')

  def test_reconnecting_falls_back_to_switch_backoff(self):
    """监督器还没写 reconnect_backoff_s 时，N 取换入退避 —— 两个面板口径一致。"""
    self.assertEqual(
      self.mod.reconnecting_text(
        self._status(connected=False, state='joining', reconnect_backoff_s=0.0, backoff_s=8.0), self.now),
      '重连中（退避 8s）')
    self.assertEqual(
      self.mod.reconnect_backoff_s(self._status(reconnect_backoff_s=4.0, backoff_s=16.0)), 4.0)
    self.assertIsNone(self.mod.reconnect_backoff_s(self._status(reconnect_backoff_s=0.0, backoff_s=0.0)))

  def test_reconnecting_label_shared_by_both_panels(self):
    """全文只在 reconnecting_label() 拼一次：面板不许自己写一份括号。"""
    self.assertEqual(self.mod.reconnecting_label(None), '重连中')
    self.assertEqual(self.mod.reconnecting_label(0.0), '重连中')
    self.assertEqual(self.mod.reconnecting_label(30.0), '重连中（退避 30s）')

  def test_reconnecting_without_backoff_value(self):
    text = self.mod.link_mode_text(
      self._status(connected=False, state='joining', reconnect_backoff_s=0.0), self.now)
    self.assertEqual(text, '重连中')

  def test_recovered_requires_big_active_and_fresh_stamp(self):
    status = self._status(big_active=True, state='running', recovered_at=self.now - 2.0)
    self.assertEqual(self.mod.link_mode_text(status, self.now), '已恢复·外置运行')
    # 恢复时间戳过期 → 回到常态
    self.assertEqual(self.mod.link_mode_text(self._status(big_active=True, recovered_at=self.now - 99.0),
                                             self.now), '本地兜底')
    # 恢复但 big_active 为假（还在本地兜底）→ 绝不显示「已恢复」
    self.assertEqual(self.mod.link_mode_text(self._status(big_active=False, recovered_at=self.now - 1.0),
                                             self.now), '本地兜底')

  def test_local_fallback(self):
    self.assertEqual(self.mod.link_mode_text(self._status(), self.now), '本地兜底')

  def test_stale_status_has_no_mode(self):
    stale = self._status(ts=self.now - 60.0, big_active=True, recovered_at=self.now)
    self.assertEqual(self.mod.link_mode_text(stale, self.now), '')

  def test_normalize_keeps_reconnect_fields(self):
    normalized = self.mod.normalize_status(
      {'ts': self.now, 'state': 'retrying', 'reconnect_armed': True,
       'reconnect_backoff_s': 5.0, 'reconnect_attempts': 2, 'recovered_at': 1.0})
    self.assertTrue(normalized['reconnect_armed'])
    self.assertEqual(normalized['reconnect_backoff_s'], 5.0)
    self.assertEqual(normalized['reconnect_attempts'], 2)
    self.assertEqual(normalized['recovered_at'], 1.0)

  def test_normalize_rejects_negative_attempts(self):
    normalized = self.mod.normalize_status({'ts': self.now, 'state': 'retrying',
                                            'reconnect_attempts': -3})
    self.assertIsNone(normalized['reconnect_attempts'])


class PanelDisplayTests(unittest.TestCase):
  """设置页链路面板的展示值：判据表 + 陈旧灰显 + 畸形输入不崩。"""

  def setUp(self):
    from openpilot.selfdrive.ui.sunnypilot import accelerator_link_data as mod
    self.mod = mod
    self.now = 1_000_000.0

  def _status(self, **kw):
    base = {'ts': self.now, 'state': 'running', 'enabled': True, 'present': True, 'ready': True,
            'big_active': True, 'connected': True, 'address': '192.168.43.2:5202',
            'last_error': None, 'reconnect_armed': False, 'reconnect_backoff_s': 0.0,
            'reconnect_attempts': 0, 'recovered_at': 0.0,
            'frames_sent': 120, 'frames_ok': 118, 'timeouts': 1, 'failures': 2,
            'p50_ms': 31.5, 'p95_ms': 44.0, 'p99_ms': 52.25, 'max_ms': 71.0,
            'backoff_s': 0.0, 'recent': [{'t': self.now, 'total_ms': 31.5},
                                         {'t': self.now - 1, 'total_ms': 44.0}]}
    base.update(kw)
    return base

  # -- 判据表 ---------------------------------------------------------------

  def test_recovered_is_persistent_not_a_20s_flash(self):
    """恢复过了很久、外置仍在供数 → 仍是「已恢复」。面板要的是一眼确认。"""
    status = self._status(recovered_at=self.now - 3600.0)
    self.assertEqual(self.mod.panel_status_text(status, self.now),
                     ('已恢复 · 外置运行', 'recovered'))

  def test_reconnecting_wins_over_recovered(self):
    """退避中即便 big_active 还停在真值，也先说「在接了」，不报假的稳定态。"""
    status = self._status(state='retrying', reconnect_armed=True, connected=False,
                          reconnect_backoff_s=16.0)
    self.assertEqual(self.mod.panel_status_text(status, self.now),
                     ('重连中（退避 16s）', 'reconnecting'))

  def test_backoff_alone_triggers_reconnecting(self):
    """只换入退避为正（监督器标志还没置位）也算正在重试。"""
    status = self._status(state='running', backoff_s=2.0)
    self.assertEqual(self.mod.panel_status_text(status, self.now),
                     ('重连中（退避 2s）', 'reconnecting'))

  def test_local_fallback_and_state_label(self):
    self.assertEqual(self.mod.panel_status_text(self._status(big_active=False), self.now),
                     ('本地兜底', 'local'))
    # big_active 未知 → 用 STATE_LABELS 原文案，不猜「已恢复」也不猜「兜底」
    self.assertEqual(self.mod.panel_status_text(self._status(big_active=None, state='joining'), self.now),
                     ('连接中', 'state'))
    self.assertEqual(self.mod.panel_status_text(self._status(big_active=None, state='unavailable'), self.now),
                     ('不可用', 'state'))

  def test_stale_beats_every_link_conclusion(self):
    """陈旧时绝不报「已恢复」—— 没人维护文件时那只是旧值。"""
    stale = self._status(ts=self.now - 600.0, big_active=True, recovered_at=self.now)
    text, tone = self.mod.panel_status_text(stale, self.now)
    self.assertEqual(tone, 'stale')
    self.assertIn('数据陈旧', text)
    self.assertIn('分钟前', text)

  def test_missing_status_is_no_data(self):
    self.assertEqual(self.mod.panel_status_text({}, self.now), ('无数据', 'stale'))
    self.assertEqual(self.mod.panel_status_text(None, self.now), ('无数据', 'stale'))
    self.assertEqual(self.mod.panel_status_text({'ts': self.now}, self.now), ('无数据', 'stale'))

  # -- 陈旧灰显 --------------------------------------------------------------

  def test_stale_status_blanks_every_number(self):
    stale = self._status(ts=self.now - 600.0)
    data = self.mod.panel_display({'enabled': True}, stale, self.now)
    self.assertFalse(data['fresh'])
    self.assertEqual(data['tone'], 'stale')
    self.assertEqual(data['latency_pair'], '--')
    self.assertEqual(data['frames'], '--')
    self.assertEqual(data['timeouts'], '--')
    self.assertEqual(data['failures'], '--')
    self.assertEqual(data['backoff_text'], '--')
    self.assertEqual(data['series'], [])
    self.assertIsNone(data['p50_ms'])
    self.assertIsNone(data['max_ms'])
    self.assertEqual(data['active_text'], '--')
    # 配置目标来自 UI 自己写的文件，与状态新鲜度无关 → 陈旧也要看得见
    self.assertTrue(data['config_enabled'])

  # -- 数值口径 --------------------------------------------------------------

  def test_zero_latency_is_not_rendered_as_zero(self):
    """0.0 表示「没测到」，绝不能显示成 `0 ms`。"""
    data = self.mod.panel_display({}, self._status(p50_ms=0.0, p95_ms=0.0, p99_ms=0.0, max_ms=0.0), self.now)
    self.assertEqual(data['latency_pair'], '--')
    self.assertIsNone(data['p50_ms'])
    self.assertIsNone(data['p95_ms'])
    self.assertIsNone(data['p99_ms'])
    self.assertIsNone(data['max_ms'])
    self.assertEqual(self.mod.latency_metric_text(0.0), '--')
    self.assertEqual(self.mod.latency_metric_text(None), '--')
    self.assertEqual(self.mod.latency_metric_text(31.5), '31.5')

  def test_frames_unmeasured_shows_dashes(self):
    data = self.mod.panel_display({}, self._status(frames_sent=0, frames_ok=0), self.now)
    self.assertEqual(data['frames'], '--')
    data = self.mod.panel_display({}, self._status(frames_sent=0, frames_ok=0, big_active=False), self.now)
    self.assertEqual(data['frames'], '--')
    self.assertEqual(self.mod.panel_display({}, self._status(), self.now)['frames'], '118/120')

  def test_series_drops_unmeasured_points(self):
    data = self.mod.panel_display({}, self._status(
      recent=[{'t': self.now, 'total_ms': 0.0}, {'t': self.now, 'total_ms': 40.0},
              {'t': self.now}, {'total_ms': 'x'}, 'bad', None]), self.now)
    self.assertEqual(data['series'], [40.0])

  def test_recovery_note_reports_reconnects(self):
    self.assertEqual(
      self.mod.recovery_note_text(self._status(reconnect_attempts=3, recovered_at=self.now - 12.0), self.now),
      '自动恢复：已重连 3 次 · 12s前恢复')
    # 接回来了但期间一次都没重试（首次握手不算恢复，所以这里只可能是重连成功过）
    self.assertEqual(
      self.mod.recovery_note_text(self._status(reconnect_attempts=0, recovered_at=self.now - 1800.0), self.now),
      '自动恢复：链路已接回 · 30 分钟前恢复')
    self.assertIn('待命', self.mod.recovery_note_text(self._status(), self.now))
    self.assertEqual(self.mod.recovery_note_text(
      self._status(reconnect_attempts=4, recovered_at=0.0), self.now), '自动恢复：已重连 4 次 · 等待下一次恢复')
    # 数据不新鲜 → 空串，UI 决定不显示，绝不拿旧 recovered_at 汇报
    self.assertEqual(self.mod.recovery_note_text(self._status(ts=self.now - 600.0), self.now), '')

  # -- 防崩 ------------------------------------------------------------------

  def test_malformed_inputs_never_raise(self):
    """历史上这块 UI 在渲染帧抛过 KeyError: 0 —— 畸形输入必须只降级、不崩。"""
    for bad in (None, 0, '', [], 'sm', {'recent': 5}, {'recent': [{'total_ms': float('nan')}]},
                {'ts': 'x', 'state': 'running'}, {'ts': self.now, 'state': 7},
                {'ts': self.now, 'state': 'running', 'big_active': 'yes'},
                {'ts': self.now, 'state': 'retrying', 'reconnect_backoff_s': float('inf')},
                {'ts': 1e400, 'state': 'running'}):
      data = self.mod.panel_display(bad, bad, self.now)
      self.assertIsInstance(data, dict)
      self.assertIsNotNone(data.get('tone'))
      self.assertIsNotNone(data.get('headline'))
      self.assertIsInstance(data.get('series'), list)
      self.assertIsInstance(data.get('latency_pair'), str)
      self.assertIsInstance(data.get('frames'), str)
      self.assertIsInstance(self.mod.panel_status_text(bad, self.now), tuple)

  def test_address_is_never_part_of_the_display(self):
    """bulk 模式无 IP/TCP —— 展示值里不能出现 192.168.43.2:5202。"""
    data = self.mod.panel_display({'enabled': True, 'address': '192.168.43.2:5202'},
                                  self._status(address='192.168.43.2:5202'), self.now)
    blob = ' '.join(str(v) for v in data.values() if isinstance(v, str))
    self.assertNotIn('192.168.43.2', blob)
    self.assertNotIn('5202', blob)


class SafetyGateUntouchedTests(unittest.TestCase):
  def test_switching_gates_are_not_relaxed(self):
    """安全语义硬约束：换入窗口/退避常量一个都没动。"""
    self.assertEqual(switching.ENGAGEMENT_MAX_AGE, 0.25)
    self.assertTrue(switching.window_open(False, 0.0))
    self.assertFalse(switching.window_open(True, 0.0), 'engaged 绝不能开窗')
    self.assertFalse(switching.window_open(False, switching.ENGAGEMENT_MAX_AGE + 0.01),
                     '快照过期绝不能开窗')
    self.assertFalse(switching.window_open(False, -1.0), '未来时间戳绝不能开窗')


class BulkSessionHelloRaceTests(unittest.TestCase):
  """会话级恢复的 HELLO **必须由常驻 reader 收包**，不能在本线程再读一次同一个 fd。

  真机取证（本轮台架）：bulk 端点上常驻着一条 reader，它阻塞在同一个 fd 的
  `ffs_epfile_io` 上等主机回包。握手线程若自己 `recv_message`，就成了两个消费者
  抢一条流 —— HELLO 响应被 reader 当成未知 req_id 丢弃，握手方在**不可中断**的
  ffs 读里永久等下去。表面看像「内核 ffs 限制」，其实是这次握手的收包方式错了。
  """

  def _pair_client(self, *, answer_hello: bool, connect_timeout: float = 0.6):
    """真 socketpair + 一个扮演 PC 侧服务的线程（只说协议，不做推理）。"""
    import socket as _socket
    from openpilot.sunnypilot.accelerators.remote import protocol as P

    dev, host = _socket.socketpair()
    dev.settimeout(5.0)
    hellos: list[int] = []
    host_closed = threading.Event()

    def host_loop():
      try:
        while not host_closed.is_set():
          header, _ = P.recv_message(host, max_payload_size=P.MAX_RESPONSE_PAYLOAD_SIZE)
          hellos.append(header.request_id)
          if header.msg_type == P.MSG_HELLO_REQ and answer_hello:
            P.send_message(host, P.Header(msg_type=P.MSG_HELLO_RESP,
                                          request_id=header.request_id),
                           P.pack_hello_resp(0, 0, 'ok'))
      except Exception:  # noqa: BLE001  —— 对端被关掉，线程自然退出
        pass

    thread = threading.Thread(target=host_loop, daemon=True)
    thread.start()
    self.addCleanup(host_closed.set)
    self.addCleanup(host.close)

    client = RemotePipelineClient(SIG, address='127.0.0.1:1', deadline_ms=100.0,
                                  transport='bulk', connect_timeout=connect_timeout,
                                  status=RemoteStatus())
    client._sock = dev
    client._stop.clear()
    client._hello_done = False
    client._link_gen += 1
    gen = client._link_gen
    client._reader = threading.Thread(target=lambda: client._reader_main(dev, gen),
                                      name='oprp-reader', daemon=True)
    client._reader.start()
    self.addCleanup(client.close)
    return client, hellos

  def test_session_hello_is_answered_by_the_resident_reader(self):
    client, hellos = self._pair_client(answer_hello=True)
    reader_before = client._reader
    client._on_link_error('link stalled')
    self.assertFalse(client.connected)

    client.reconnect()      # 会话级恢复：只重跑 HELLO，不重建端点

    self.assertTrue(client.connected, '重握手成功后必须回到已连接')
    self.assertIs(client._reader, reader_before, '常驻 reader 必须被复用（不能起第二个收包者）')
    self.assertEqual(len(hellos), 1, '服务端只应收到一次 HELLO')
    self.assertEqual(client._recovery_epoch, 1, '本客户端是第一次握手成功，应记 1 代')
    self.assertEqual(len(client._pending), 0, '握手用的 pending 必须回收')

  def test_session_hello_timeout_is_clean_not_a_hang(self):
    """对端不回 HELLO 时必须**干净超时**并推进退避，绝不能卡死在 ffs 读里。"""
    client, hellos = self._pair_client(answer_hello=False)
    client._on_link_error('link stalled')

    started = time.monotonic()
    with self.assertRaises(RemoteLinkError):
      client.reconnect()
    waited = time.monotonic() - started

    self.assertLess(waited, 3.0, f'握手超时必须按 connect_timeout 返回，实际 {waited:.1f}s')
    self.assertEqual(len(hellos), 1, 'HELLO 已经发出去了（对端只是不应答）')
    self.assertFalse(client.connected, '握手没成就不许报已连接')


class BulkSessionResetTests(unittest.TestCase):
  """bulk 的会话级恢复：端点不可重建，只能「保留 ep + 重新 HELLO」。

  真机取证：PC 侧服务停掉后，ep_out 上那个阻塞中的 read 只有主机写数据才会结束，
  `close()` 唤不醒；它持有的 ep 让 `open('/dev/ffs-c3bulk/ep1')` 永远返回 EBUSY
  —— 所以「关掉重开」这条路在 bulk 上是死路，必须原地重握手。
  """

  def _bulk_client(self, sock):
    client = RemotePipelineClient(SIG, address='127.0.0.1:1', deadline_ms=100.0,
                                  transport='bulk', status=RemoteStatus())
    client._sock = sock
    client._stop.clear()
    client._hello_done = False
    client._link_gen += 1
    gen = client._link_gen
    reader = threading.Thread(target=lambda: client._reader_main(sock, gen), daemon=True)
    client._reader = reader
    reader.start()
    self.addCleanup(client.close)
    return client

  def test_link_error_keeps_bulk_endpoint_and_reader(self):
    blocked = threading.Event()

    class _Sock:
      """模拟「阻塞在 ffs read 上、close() 唤不醒」的端点。"""

      def settimeout(self, value):
        pass

      def shutdown(self, how=None):
        pass

      def close(self):
        pass

      def recv(self, n):
        blocked.wait(5.0)
        raise EOFError('bulk endpoint EOF')

    client = self._bulk_client(_Sock())
    self.assertTrue(client._bulk_session_resettable(), 'bulk 端点应判定为可会话级恢复')
    client._on_link_error('link stalled')
    self.assertIsNotNone(client._sock, 'bulk 断链不得关端点（否则再也 open 不回来）')
    self.assertFalse(client.connected, '会话必须作废')
    self.assertTrue(client._reader.is_alive(), 'bulk reader 必须常驻（跨会话继续读同一条管道）')
    blocked.set()

  def test_disarm_keeps_bulk_endpoint_so_rearm_can_reuse_it(self):
    """关一次外置再打开，不能把链路永久卡死。

    解除武装若把 ffs 端点关掉，端点上那个阻塞中的 read/write 关不掉、ep 也再也
    `open` 不回来（EBUSY）——「关一次再开」就会复现本轮要修的症状。因此解除武装
    只作废会话、保留端点。
    """
    released = threading.Event()

    class _Sock:
      def settimeout(self, value):
        pass

      def shutdown(self, how=None):
        pass

      def close(self):
        released.set()

      def recv(self, n):
        released.wait(5.0)
        raise EOFError('bulk endpoint EOF')

    client = self._bulk_client(_Sock())
    client.arm_reconnect(False)
    self.assertTrue(wait_until(lambda: not client.connected, timeout=3.0),
                    '解除武装必须立刻置未连接（调用方返回后不再发帧）')
    self.assertIsNotNone(client._sock, 'bulk 解除武装不得销毁 ffs 端点')
    self.assertFalse(released.is_set(), 'bulk 解除武装不得 close 端点')
    self.assertTrue(client._bulk_session_resettable(), '再次武装必须还能原地重握手')

  def test_tcp_link_error_still_fully_detaches_socket(self):
    """对照：TCP 侧仍必须彻底关闭句柄，否则 connect() 会短路。"""
    client = self.__class__._tcp_client(self)
    client._on_link_error('read failed')
    self.assertIsNone(client._sock)
    self.assertFalse(client.connected)

  def _tcp_client(self):
    from openpilot.sunnypilot.accelerators.tests._fake_server import FakeRemoteServer
    server = FakeRemoteServer()
    server.start()
    self.addCleanup(server.close)
    client = RemotePipelineClient(SIG, address=server.address, deadline_ms=200.0,
                                  status=RemoteStatus())
    self.addCleanup(client.close)
    client.connect_and_hello()
    return client


if __name__ == '__main__':
  unittest.main()
