"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

UDP 分片传输的离线单测（与 docs/UDP分片传输规范-UDP-Chunk-Transport-Spec.md 对齐）：

协议原语层（全部走真实 UDP loopback）：
- 正常往返、乱序重组、重复片丢弃、多 msg_id 聚合不串包；
- CRC 损坏 / 超限 / 分片头非法 → 丢弃并最终超时；
- deadline 超时抛既有 RemoteTimeout；
- **完整消息与 TCP send_message() 逐字节一致**。

客户端集成层（真实 UDP 回环假服务端）：
- transport='udp' 时 HELLO 握手 + 单帧 infer + 1 帧流水线乱序回包；
- 默认 transport 仍为 tcp（零回归）；非法取值回落 tcp；
- UDP 单帧超时后链路保留。
"""
from __future__ import annotations

import socket
import struct
import threading
import time
import unittest
import zlib

import numpy as np

from openpilot.sunnypilot.accelerators.remote import config
from openpilot.sunnypilot.accelerators.remote import protocol as P
from openpilot.sunnypilot.accelerators.remote.client import (
  RemotePipelineClient,
  RemoteTimeout,
)
from openpilot.sunnypilot.accelerators.remote.status import RemoteStatus

SIG = 'ab' * 32
TS = 1_234_567_890
LOCALHOST = '127.0.0.1'


def _datagrams(header: P.Header, payload: bytes, chunk_data: int = P.CHUNK_DATA_DEFAULT):
  """按规范手工切片，返回 (datagram 列表, msg_len, msg_crc)。"""
  message = P.build_wire_message(header, payload)
  msg_len = len(message)
  msg_crc = zlib.crc32(payload) & 0xFFFFFFFF
  msg_id = header.request_id & 0xFFFFFFFF
  total = (msg_len + chunk_data - 1) // chunk_data
  out = []
  for idx in range(total):
    data = message[idx * chunk_data:(idx + 1) * chunk_data]
    out.append(struct.pack(P.CHUNK_HDR_FORMAT, msg_id, idx, total, msg_len, msg_crc) + data)
  return out, msg_len, msg_crc


class _UdpFixture:
  """一对绑定在 127.0.0.1 的 UDP socket：send_sock 发、recv_sock 收。"""

  def __init__(self):
    self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.recv_sock.bind((LOCALHOST, 0))
    self.send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.send_sock.bind((LOCALHOST, 0))

  @property
  def addr(self):
    return self.recv_sock.getsockname()

  def send(self, datagrams):
    for d in datagrams:
      self.send_sock.sendto(d, self.addr)

  def read_message(self, timeout: float = 1.0):
    """收集所有分片并按 chunk_idx 重组，返回 (datagram 列表, 完整消息)。"""
    self.recv_sock.settimeout(timeout)
    parts: dict[int, bytes] = {}
    total = None
    collected = []
    while total is None or len(parts) < total:
      d, _ = self.recv_sock.recvfrom(P.MAX_DATAGRAM_SIZE)
      collected.append(d)
      _msg_id, idx, chunk_total, _ml, _crc = struct.unpack(
        P.CHUNK_HDR_FORMAT, d[:P.CHUNK_HDR_SIZE])
      parts[idx] = d[P.CHUNK_HDR_SIZE:]
      total = chunk_total
    return collected, b''.join(parts[i] for i in range(total))

  def close(self):
    for s in (self.recv_sock, self.send_sock):
      try:
        s.close()
      except OSError:
        pass


class UdpSendTests(unittest.TestCase):
  def setUp(self):
    self.fx = _UdpFixture()
    self.addCleanup(self.fx.close)

  def test_chunking_and_header_fields(self):
    payload = bytes(range(256)) * 3
    header = P.Header(P.MSG_INFERENCE_REQ, request_id=0xABCD, frame_id=1)
    chunk = 100
    datagrams, msg_len, msg_crc = _datagrams(header, payload, chunk_data=chunk)

    P.send_message_udp(self.fx.send_sock, self.fx.addr, header, payload, chunk_data=chunk)
    collected, assembled = self.fx.read_message()

    self.assertEqual(len(collected), len(datagrams))
    self.assertEqual(assembled, P.build_wire_message(header, payload))
    for d in collected:
      msg_id, idx, total, ml, crc = struct.unpack(P.CHUNK_HDR_FORMAT, d[:P.CHUNK_HDR_SIZE])
      self.assertEqual((msg_id, total, ml), (0xABCD, len(datagrams), msg_len))
      self.assertEqual(crc, msg_crc)
      self.assertLessEqual(len(d) - P.CHUNK_HDR_SIZE, chunk)
      self.assertLess(idx, total)

  def test_wire_message_is_byte_identical_to_tcp_send_message(self):
    """UDP 重组出的完整消息必须与 TCP send_message() 的字节流完全一致。"""
    payload = b'\x00\x01\x02' * 5000 + b'tail'
    tcp_header = P.Header(P.MSG_INFERENCE_REQ, request_id=77, frame_id=9, timestamp_eof=TS,
                          flags=P.FLAG_RESET_TEMPORAL)
    udp_header = P.Header(P.MSG_INFERENCE_REQ, request_id=77, frame_id=9, timestamp_eof=TS,
                          flags=P.FLAG_RESET_TEMPORAL)

    # TCP 侧：socketpair 上写出并逐字节读回
    a, b = socket.socketpair()
    self.addCleanup(a.close)
    self.addCleanup(b.close)
    P.send_message(a, tcp_header, payload)
    expected_len = P.HEADER_SIZE + len(payload) + 4
    tcp_bytes = bytearray()
    b.settimeout(1.0)
    while len(tcp_bytes) < expected_len:
      data = b.recv(expected_len - len(tcp_bytes))
      self.assertTrue(data, 'TCP 侧提前 EOF')
      tcp_bytes.extend(data)

    # UDP 侧：分片发送后重组
    P.send_message_udp(self.fx.send_sock, self.fx.addr, udp_header, payload)
    _collected, udp_bytes = self.fx.read_message()

    self.assertEqual(bytes(tcp_bytes), udp_bytes)
    self.assertEqual(udp_bytes, udp_header.pack(len(payload)) + payload
                     + struct.pack('!I', zlib.crc32(payload) & 0xFFFFFFFF))

  def test_invalid_chunk_data_rejected(self):
    with self.assertRaises(P.ProtocolError):
      P.send_message_udp(self.fx.send_sock, self.fx.addr, P.Header(P.MSG_HELLO_REQ), b'x',
                         chunk_data=0)


class UdpRecvTests(unittest.TestCase):
  def setUp(self):
    self.fx = _UdpFixture()
    self.addCleanup(self.fx.close)
    # 40KB 载荷 → 默认 chunk(16384) 下跨多片，覆盖乱序/重复/聚合
    self.payload = b'udp-payload-' + bytes(range(200)) * 200
    self.header = P.Header(P.MSG_INFERENCE_RESP, request_id=5, frame_id=2, timestamp_eof=TS)
    self.datagrams, _, _ = _datagrams(self.header, self.payload)
    self.assertGreater(len(self.datagrams), 2)

  def _recv(self, deadline=0.5):
    return P.recv_message_udp(self.fx.recv_sock, deadline=time.monotonic() + deadline)

  def test_roundtrip_in_order(self):
    self.fx.send(self.datagrams)
    header, payload = self._recv()
    self.assertEqual((header.request_id, header.frame_id, payload),
                     (5, 2, self.payload))

  def test_out_of_order_reassembly(self):
    self.fx.send(list(reversed(self.datagrams)))
    header, payload = self._recv()
    self.assertEqual((header.request_id, payload), (5, self.payload))

  def test_duplicate_chunks_ignored(self):
    duplicated = []
    for d in self.datagrams:
      duplicated.append(d)
      duplicated.append(d)  # 每片都发两遍
    self.fx.send(duplicated)
    header, payload = self._recv()
    self.assertEqual(payload, self.payload)

  def test_interleaved_msg_ids_do_not_mix(self):
    """两个 msg_id 交错到达：以 msg_id 聚合，不把别条消息的片拼进来。"""
    other_payload = b'other-message' * 2000  # >16KB → other 也跨多片
    other_header = P.Header(P.MSG_INFERENCE_RESP, request_id=6)
    other, _, _ = _datagrams(other_header, other_payload)
    self.assertGreater(len(other), 1)

    # 交错发送：other 仅首片先到（未收齐），随后本消息所有片（本消息先收齐）
    self.fx.send([other[0]] + list(self.datagrams))
    header, payload = self._recv()
    self.assertEqual((header.request_id, payload), (5, self.payload))

  def test_crc_corrupt_message_dropped_then_valid_delivered(self):
    corrupt = list(self.datagrams)
    bad = bytearray(corrupt[-1])
    bad[-1] ^= 0xFF  # 破坏 payload 字节 → 重组后 CRC 不符
    corrupt[-1] = bytes(bad)

    self.fx.send(corrupt)
    with self.assertRaises(RemoteTimeout):
      self._recv(deadline=0.2)
    # 链路未卡死：随后一条合法消息仍能收到
    self.fx.send(self.datagrams)
    header, payload = self._recv(deadline=0.5)
    self.assertEqual(payload, self.payload)

  def test_crc_field_corruption_dropped(self):
    corrupt = list(self.datagrams)
    bad = bytearray(corrupt[0])
    bad[12:16] = struct.pack('!I', 0xDEADBEEF)  # msg_crc 字段被改坏
    corrupt[0] = bytes(bad)
    self.fx.send(corrupt)
    with self.assertRaises(RemoteTimeout):
      self._recv(deadline=0.2)

  def test_deadline_timeout_raises(self):
    with self.assertRaises(RemoteTimeout):
      self._recv(deadline=0.1)

  def test_expired_deadline_raises_immediately(self):
    started = time.monotonic()
    with self.assertRaises(RemoteTimeout):
      P.recv_message_udp(self.fx.recv_sock, deadline=time.monotonic() - 1.0)
    self.assertLess(time.monotonic() - started, 0.5)

  def test_short_datagram_ignored(self):
    self.fx.send([b'\x00\x01\x02'])  # 不足分片头
    self.fx.send(self.datagrams)
    header, payload = self._recv()
    self.assertEqual(payload, self.payload)

  def test_oversize_payload_rejected(self):
    datagrams, _, _ = _datagrams(self.header, b'x' * 64, chunk_data=16)
    self.fx.send(datagrams)
    with self.assertRaises(RemoteTimeout):
      P.recv_message_udp(self.fx.recv_sock, deadline=time.monotonic() + 0.2,
                         max_payload_size=8, chunk_data=16)

  def test_chunk_total_mismatch_dropped(self):
    corrupt = list(self.datagrams)
    bad = bytearray(corrupt[0])
    chunk_idx, chunk_total = struct.unpack('!H H', bad[4:8])
    bad[4:8] = struct.pack('!H H', chunk_idx, chunk_total + 1)
    corrupt[0] = bytes(bad)
    self.fx.send(corrupt)
    with self.assertRaises(RemoteTimeout):
      self._recv(deadline=0.2)

  def test_expect_addr_filters_foreign_source(self):
    wrong_addr = (LOCALHOST, self.fx.send_sock.getsockname()[1] + 1)
    self.fx.send(self.datagrams)
    with self.assertRaises(RemoteTimeout):
      P.recv_message_udp(self.fx.recv_sock, deadline=time.monotonic() + 0.2,
                         expect_addr=wrong_addr)
    # 正确来源仍可接收
    self.fx.send(self.datagrams)
    header, payload = P.recv_message_udp(self.fx.recv_sock, deadline=time.monotonic() + 0.5,
                                         expect_addr=self.fx.send_sock.getsockname())
    self.assertEqual(payload, self.payload)


class _FakeUdpServer(threading.Thread):
  """极简 UDP 假服务端：HELLO + INFERENCE（可选逆序回包 / 丢帧）。

  独立实现分片解析以便同时拿到客户端来源地址；测试输入均为单分片（小张量），
  但配备完整重组逻辑以免依赖消息大小。
  """

  def __init__(self, reverse_batch: int = 1, drop_frame_ids=()):
    super().__init__(daemon=True, name='fake-oprp-udp-server')
    self.reverse_batch = max(1, int(reverse_batch))
    self.drop_frame_ids = set(drop_frame_ids)
    self.received: list[int] = []
    self.error: Exception | None = None
    self._stop_evt = threading.Event()
    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self._sock.bind((LOCALHOST, 0))
    self._sock.settimeout(0.2)
    self._pending: list[tuple] = []

  @property
  def address(self):
    host, port = self._sock.getsockname()
    return f'{host}:{port}'

  def run(self):
    try:
      while not self._stop_evt.is_set():
        try:
          header, payload, addr = self._recv_raw()
        except socket.timeout:
          continue
        except OSError:
          break
        try:
          self._handle(header, payload, addr)
        except Exception as e:  # 记到属性供断言，别让线程静默死
          self.error = e
          break
    finally:
      try:
        self._sock.close()
      except OSError:
        pass

  def _recv_raw(self):
    """读一条完整消息（跨 msg_id 独立缓冲），返回 (Header, payload, 来源地址)。"""
    buffers: dict[int, dict] = {}
    while True:
      datagram, addr = self._sock.recvfrom(P.MAX_DATAGRAM_SIZE)
      if len(datagram) < P.CHUNK_HDR_SIZE:
        continue
      msg_id, idx, total, msg_len, _crc = struct.unpack(
        P.CHUNK_HDR_FORMAT, datagram[:P.CHUNK_HDR_SIZE])
      buf = buffers.setdefault(msg_id, {'parts': {}, 'total': total, 'msg_len': msg_len})
      buf['parts'][idx] = datagram[P.CHUNK_HDR_SIZE:]
      if len(buf['parts']) == buf['total']:
        message = b''.join(buf['parts'][i] for i in range(buf['total']))
        header = P.Header.unpack(message[:P.HEADER_SIZE])
        return header, message[P.HEADER_SIZE:msg_len - 4], addr

  def _handle(self, header, payload, addr):
    if header.msg_type == P.MSG_HELLO_REQ:
      P.unpack_hello_req(payload)
      P.send_message_udp(self._sock, addr,
                         P.Header(P.MSG_HELLO_RESP, request_id=header.request_id),
                         P.pack_hello_resp(0, 0, 'OK'))
      return
    if header.msg_type != P.MSG_INFERENCE_REQ:
      return
    self.received.append(header.frame_id)
    self._pending.append((header, addr))
    if len(self._pending) >= self.reverse_batch:
      batch, self._pending = self._pending, []
      for req_header, peer in reversed(batch):
        if req_header.frame_id in self.drop_frame_ids:
          continue
        resp = P.Header(P.MSG_INFERENCE_RESP, request_id=req_header.request_id,
                        frame_id=req_header.frame_id, timestamp_eof=req_header.timestamp_eof)
        body = P.pack_tensor_dict({
          'raw_outputs_0': np.full((4,), float(req_header.frame_id), dtype=np.float32),
        })
        P.send_message_udp(self._sock, peer, resp, body)

  def close(self):
    self._stop_evt.set()
    if self.is_alive():
      self.join(timeout=1.0)

  def wait_for_frames(self, count: int, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
      if len(self.received) >= count:
        return True
      time.sleep(0.005)
    return len(self.received) >= count


class UdpClientTests(unittest.TestCase):
  def setUp(self):
    self.server = _FakeUdpServer()
    self.server.start()
    self.addCleanup(self.server.close)

  def make_client(self, deadline_ms: float = 500.0, pipeline_depth: int = 1,
                  transport: str = 'udp'):
    client = RemotePipelineClient(SIG, address=self.server.address, deadline_ms=deadline_ms,
                                  pipeline_depth=pipeline_depth, status=RemoteStatus(),
                                  transport=transport)
    self.addCleanup(client.close)
    return client

  @staticmethod
  def inputs(seed: int = 0):
    return {
      'warped': np.full((2, 6, 4, 8), seed, dtype=np.uint8),
      'packed': np.full(4, float(seed), dtype=np.float32),
    }

  def test_default_transport_is_tcp(self):
    client = RemotePipelineClient(SIG, address='127.0.0.1:1')
    self.addCleanup(client.close)
    self.assertEqual(client.transport, 'tcp')

  def test_invalid_transport_falls_back_to_tcp(self):
    client = RemotePipelineClient(SIG, address='127.0.0.1:1', transport='quic')
    self.addCleanup(client.close)
    self.assertEqual(client.transport, 'tcp')

  def test_udp_handshake_and_single_infer(self):
    client = self.make_client()
    client.connect_and_hello()
    self.assertTrue(client.connected)
    self.assertEqual(client.transport, 'udp')
    outputs = client.infer(frame_id=5, ts_eof=TS, tensors=self.inputs())
    np.testing.assert_array_equal(outputs[0], np.full((4,), 5.0, dtype=np.float32))
    self.assertEqual(client.status.frames_ok, 1)
    self.assertIsNone(self.server.error)

  def test_udp_pipeline_out_of_order_responses(self):
    self.server.close()
    self.server = _FakeUdpServer(reverse_batch=2)
    self.server.start()
    self.addCleanup(self.server.close)

    client = self.make_client(deadline_ms=1000.0)
    client.connect_and_hello()
    h1 = client.submit(frame_id=1, ts_eof=TS, tensors=self.inputs(seed=1))
    h2 = client.submit(frame_id=2, ts_eof=TS, tensors=self.inputs(seed=2))
    self.assertTrue(self.server.wait_for_frames(2))
    out1 = client.collect(h1)
    out2 = client.collect(h2)
    np.testing.assert_array_equal(out1[0], np.full((4,), 1.0, dtype=np.float32))
    np.testing.assert_array_equal(out2[0], np.full((4,), 2.0, dtype=np.float32))
    self.assertEqual(client.in_flight, 0)

  def test_udp_frame_timeout_keeps_link(self):
    self.server.close()
    self.server = _FakeUdpServer(drop_frame_ids={1})
    self.server.start()
    self.addCleanup(self.server.close)

    client = self.make_client(deadline_ms=200.0)
    client.connect_and_hello()
    with self.assertRaises(RemoteTimeout):
      client.infer(frame_id=1, ts_eof=TS, tensors=self.inputs())
    self.assertTrue(client.connected, 'UDP 单帧超时不应杀死链路')
    outputs = client.infer(frame_id=2, ts_eof=TS, tensors=self.inputs())
    np.testing.assert_array_equal(outputs[0], np.full((4,), 2.0, dtype=np.float32))


class ConfigTransportTests(unittest.TestCase):
  def test_default_transport_is_tcp(self):
    self.assertEqual(config.DEFAULTS['transport'], 'tcp')
    self.assertEqual(config.sanitize({})['transport'], 'tcp')

  def test_udp_roundtrip_and_case_insensitive(self):
    self.assertEqual(config.sanitize({'transport': 'UDP'})['transport'], 'udp')

  def test_invalid_transport_falls_back(self):
    for bad in ('quic', '', 5, None, True):
      self.assertEqual(config.sanitize({'transport': bad})['transport'], 'tcp')


if __name__ == '__main__':
  unittest.main()
