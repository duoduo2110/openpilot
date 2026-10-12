"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

OPRP 流水线客户端的离线单测：本地回环假服务端（线程 + socket）。

重点覆盖：HELLO 握手、req_id 配对、**1 帧流水线与乱序回包配对**、绝对 deadline
超时、迟到响应丢弃、reset 语义、链路/协议错误分类，以及禁止上行张量的拦截。
"""
from __future__ import annotations

import time
import unittest

import numpy as np

from openpilot.sunnypilot.accelerators.remote.client import (
  FORBIDDEN_INPUTS,
  RemoteLinkError,
  RemotePipelineClient,
  RemoteServerError,
  RemoteTimeout,
  RemoteHandshakeError,
  parse_remote_address,
)
from openpilot.sunnypilot.accelerators.remote.protocol import (CAP_ZLIB_TENSOR,
                                                               FLAG_RESET_TEMPORAL,
                                                               FLAG_ZLIB_TENSOR)
from openpilot.sunnypilot.accelerators.remote.status import RemoteStatus
from openpilot.sunnypilot.accelerators.tests._fake_server import FakeRemoteServer

SIG = 'ab' * 32
TS = 1_234_567_890


def inputs(seed: int = 0) -> dict[str, np.ndarray]:
  return {
    'warped': np.full((2, 6, 4, 8), seed, dtype=np.uint8),
    'packed': np.full(4, float(seed), dtype=np.float32),
  }


class ClientTestBase(unittest.TestCase):
  def setUp(self):
    self.server = FakeRemoteServer()
    self.server.start()
    self.addCleanup(self.server.close)

  def make_client(self, deadline_ms: float = 200.0, pipeline_depth: int = 1, hello: bool = True):
    client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=deadline_ms,
                                  pipeline_depth=pipeline_depth, status=RemoteStatus())
    self.addCleanup(client.close)
    if hello:
      client.connect_and_hello()
    return client

  @staticmethod
  def wait_until(predicate, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
      if predicate():
        return True
      time.sleep(0.005)
    return predicate()

  def restart_server(self, **kwargs):
    self.server.close()
    self.server = FakeRemoteServer(**kwargs)
    self.server.start()
    self.addCleanup(self.server.close)

  def server_with_behavior(self, behavior, reverse_batch: int = 1):
    self.restart_server(behavior=behavior, reverse_batch=reverse_batch)


class HandshakeTests(ClientTestBase):
  def test_hello_succeeds_and_carries_signature(self):
    client = self.make_client()
    self.assertTrue(client.connected)
    # 客户端会声明 ZLIB 能力位；服务端默认 caps=0 未协商成功 → 不压缩。
    self.assertEqual(self.server.hellos, [(SIG, CAP_ZLIB_TENSOR)])
    self.assertFalse(client.compression_enabled)

  def test_zlib_negotiated_when_server_supports_it(self):
    self.restart_server(hello_caps=CAP_ZLIB_TENSOR)
    client = self.make_client()
    self.assertTrue(client.connected)
    self.assertTrue(client.compression_enabled)
    # 无压缩能力位（老服务端）时客户端仍可用，只是退回老格式。
    self.assertEqual(FLAG_ZLIB_TENSOR & FLAG_RESET_TEMPORAL, 0)

  def test_hello_rejection_raises_handshake_error(self):
    self.restart_server(hello_status=3, hello_message='bad signature')
    client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=200.0)
    self.addCleanup(client.close)
    client.connect()
    with self.assertRaises(RemoteHandshakeError):
      client.hello()
    self.assertFalse(client.connected)

  def test_hello_unexpected_msg_type_raises(self):
    self.restart_server(hello_msg_type=0x0004)
    client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=200.0)
    self.addCleanup(client.close)
    client.connect()
    with self.assertRaises(RemoteHandshakeError):
      client.hello()

  def test_submit_before_hello_rejected(self):
    client = self.make_client(hello=False)
    client.connect()
    with self.assertRaises(RemoteLinkError):
      client.submit(1, TS, inputs())


class PipelineTests(ClientTestBase):
  def test_single_frame_infer(self):
    client = self.make_client()
    outputs = client.infer(frame_id=5, ts_eof=TS, tensors=inputs())
    self.assertEqual(len(outputs), 2)
    np.testing.assert_array_equal(outputs[0], np.full((4,), 5.0, dtype=np.float32))
    self.assertEqual(self.server.received[0].frame_id, 5)

  def test_compressed_uplink_roundtrips_through_negotiated_server(self):
    """协商成功后，上行按可压缩布局发送；服务端解压后张量应完好。"""
    self.restart_server(hello_caps=CAP_ZLIB_TENSOR)
    client = self.make_client()
    self.assertTrue(client.compression_enabled)

    big = np.arange(2 * 6 * 128 * 256, dtype=np.uint8).reshape(2, 6, 128, 256)
    payload = {'warped': big, 'packed': np.full(4, 1.5, dtype=np.float32)}
    out = client.infer(frame_id=9, ts_eof=TS, tensors=payload)
    np.testing.assert_array_equal(out[0], np.full((4,), 9.0, dtype=np.float32))
    self.assertEqual(self.server.received[0].frame_id, 9)
    np.testing.assert_array_equal(self.server.received[0].tensor_arrays['warped'], big)

  def test_pipeline_and_out_of_order_pairing(self):
    """先提交两帧，服务端**逆序**回包；客户端仍按 req_id 正确配对。

    服务端在收到 2 个请求前不回任何包，所以严格串行的一问一答客户端必然超时，
    这条用例同时钉死了「支持 1 帧流水线」与「乱序回包配对正确」两点。
    """
    self.restart_server(reverse_batch=2)
    client = self.make_client(deadline_ms=1000.0)

    h1 = client.submit(frame_id=1, ts_eof=TS, tensors=inputs(seed=1))
    h2 = client.submit(frame_id=2, ts_eof=TS, tensors=inputs(seed=2))
    self.assertTrue(self.server.wait_for_frames(2), 'server never saw both pipelined requests')
    self.assertEqual([r.frame_id for r in self.server.received], [1, 2])

    # 服务端先回第 2 帧，再回第 1 帧
    self.assertTrue(self.wait_until(lambda: self.server.sent_order == [2, 1]),
                    f'expected reversed send order, got {self.server.sent_order}')

    out1 = client.collect(h1)
    out2 = client.collect(h2)
    np.testing.assert_array_equal(out1[0], np.full((4,), 1.0, dtype=np.float32))
    np.testing.assert_array_equal(out2[0], np.full((4,), 2.0, dtype=np.float32))
    self.assertEqual(client.in_flight, 0)
    self.assertEqual(client.status.frames_ok, 2)

  def test_in_flight_limit_enforced_at_depth_one(self):
    client = self.make_client(pipeline_depth=1)
    client.submit(1, TS, inputs())
    client.submit(2, TS, inputs())
    with self.assertRaisesRegex(RemoteLinkError, 'in flight'):
      client.submit(3, TS, inputs())

  def test_absolute_deadline_is_started_at_submit(self):
    self.server_with_behavior(lambda rec: ('drop',))
    client = self.make_client(deadline_ms=100_000.0)
    handle = client.submit(1, TS, inputs(), deadline=time.monotonic() + 0.05)
    started = time.monotonic()
    with self.assertRaises(RemoteTimeout):
      client.collect(handle)
    # 用的是 handle 的绝对 deadline（50ms），而不是默认的 100s
    self.assertLess(time.monotonic() - started, 1.0)


class TimeoutTests(ClientTestBase):
  def test_deadline_timeout_then_late_response_discarded(self):
    def behavior(rec):
      return ('delay', 0.3) if rec.frame_id == 1 else ('ok',)

    self.server_with_behavior(behavior)
    client = self.make_client(deadline_ms=80.0)

    with self.assertRaises(RemoteTimeout):
      client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    self.assertEqual(client.status.timeouts, 1)
    self.assertTrue(client.connected, 'deadline miss must not kill the link')

    # 等迟到的第 1 帧响应被 reader 线程读掉并丢弃，再跑第 2 帧
    self.assertTrue(self.wait_until(lambda: self.server.sent_order == [1]))
    time.sleep(0.1)
    outputs = client.infer(frame_id=2, ts_eof=TS, tensors=inputs())
    np.testing.assert_array_equal(outputs[0], np.full((4,), 2.0, dtype=np.float32))
    self.assertEqual(client.status.frames_ok, 1)

  def test_wrong_req_id_response_is_not_matched(self):
    self.server_with_behavior(lambda rec: ('wrong_req_id', 1000))
    client = self.make_client(deadline_ms=150.0)
    with self.assertRaises(RemoteTimeout):
      client.infer(frame_id=9, ts_eof=TS, tensors=inputs())


class ResetTests(ClientTestBase):
  def test_request_reset_arms_next_submit_only(self):
    client = self.make_client()
    client.request_reset()
    out1 = client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    out2 = client.infer(frame_id=2, ts_eof=TS, tensors=inputs())

    self.assertTrue(self.server.received[0].reset)
    self.assertFalse(self.server.received[1].reset)
    np.testing.assert_array_equal(out1[1], np.array(float(FLAG_RESET_TEMPORAL), dtype=np.float32))
    np.testing.assert_array_equal(out2[1], np.array(0.0, dtype=np.float32))

  def test_explicit_reset_flag(self):
    client = self.make_client()
    client.infer(frame_id=3, ts_eof=TS, tensors=inputs(), reset=True)
    client.infer(frame_id=4, ts_eof=TS, tensors=inputs(), flags=FLAG_RESET_TEMPORAL)
    self.assertTrue(self.server.received[0].reset)
    self.assertTrue(self.server.received[1].reset)

  def test_reset_temporal_alias(self):
    client = self.make_client()
    client.reset_temporal()
    client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    self.assertTrue(self.server.received[0].reset)


class ErrorPathTests(ClientTestBase):
  def test_forbidden_tensor_not_sent(self):
    client = self.make_client()
    for name in sorted(FORBIDDEN_INPUTS):
      with self.assertRaises(ValueError):
        client.submit(1, TS, {name: np.zeros(2, dtype=np.float32)})
    self.assertEqual(self.server.received, [])

  def test_empty_tensors_rejected(self):
    client = self.make_client()
    with self.assertRaises(ValueError):
      client.submit(1, TS, {})

  def test_server_error_frame_keeps_link(self):
    self.server_with_behavior(lambda rec: ('error', 9, 'boom') if rec.frame_id == 1 else ('ok',))
    client = self.make_client()
    with self.assertRaisesRegex(RemoteServerError, 'server error 9'):
      client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    self.assertTrue(client.connected)
    out = client.infer(frame_id=2, ts_eof=TS, tensors=inputs())
    np.testing.assert_array_equal(out[0], np.full((4,), 2.0, dtype=np.float32))

  def test_frame_id_mismatch_kills_link(self):
    self.server_with_behavior(lambda rec: ('mismatch_frame',))
    client = self.make_client()
    with self.assertRaises(RemoteLinkError):
      client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    self.assertFalse(client.connected)

  def test_bad_payload_crc_kills_link(self):
    self.server_with_behavior(lambda rec: ('bad_payload_crc',))
    client = self.make_client()
    with self.assertRaises(RemoteLinkError):
      client.infer(frame_id=1, ts_eof=TS, tensors=inputs())
    self.assertFalse(client.connected)

  def test_link_close_fails_pending_request(self):
    self.server_with_behavior(lambda rec: ('drop',))
    client = self.make_client()
    handle = client.submit(frame_id=1, ts_eof=TS, tensors=inputs(), deadline=time.monotonic() + 3.0)
    self.assertTrue(self.server.wait_for_frames(1))
    self.server.close()
    with self.assertRaises(RemoteLinkError):
      client.collect(handle)
    self.assertFalse(client.connected)

  def test_close_is_idempotent(self):
    client = self.make_client()
    client.close()
    client.close()
    self.assertFalse(client.connected)


class AddressTests(unittest.TestCase):
  def test_parse_remote_address(self):
    self.assertEqual(parse_remote_address('192.168.43.2:5202'), ('192.168.43.2', 5202))
    self.assertEqual(parse_remote_address('localhost:6000'), ('localhost', 6000))
    self.assertEqual(parse_remote_address('10.0.0.1'), ('10.0.0.1', 5202))
    self.assertEqual(parse_remote_address(''), ('192.168.43.2', 5202))
    self.assertEqual(parse_remote_address('[::1]:9999'), ('::1', 9999))


if __name__ == '__main__':
  unittest.main()
