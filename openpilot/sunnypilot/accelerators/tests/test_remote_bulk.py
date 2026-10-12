"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

vendor bulk（FunctionFS）传输的离线单测：真机之外不碰 ffs，用注入的假底层 IO
验证「伪 socket」的线格式契约：

- 设备→主机消息按 16 KiB 对齐（dwc3 TX FIFO workaround），接收端按同一规则丢弃；
- 大消息按 write_chunk 切块写出，逐字节等于对齐后的完整消息；
- recv() 跨多次短读重组，多余字节留在内部缓存，不丢流；
- 读块解析：256 KiB 被硬性封顶回 128 KiB（order-6 失败），64 KiB 合法；
- config 白名单接受 'bulk'，默认仍 'tcp'，非法值回落 'tcp'（零回归）；
- RemotePipelineClient 接受 transport='bulk'，且 select 分支不适用于 bulk。
"""
from __future__ import annotations

import errno
import unittest

from openpilot.sunnypilot.accelerators.remote import bulk_transport as BT
from openpilot.sunnypilot.accelerators.remote import config
from openpilot.sunnypilot.accelerators.remote import protocol as P
from openpilot.sunnypilot.accelerators.remote.client import RemotePipelineClient


class _FakeIo:
  """可注入的底层 IO：读走 rbuf[fd]，写累加到 wbuf 并记录每次 write。"""

  def __init__(self, max_read: int = 1 << 20):
    self.rbuf: dict[int, bytearray] = {}
    self.wbuf = bytearray()
    self.writes: list[tuple[int, bytes]] = []
    self.max_read = max_read

  def feed(self, fd: int, data: bytes) -> None:
    self.rbuf.setdefault(fd, bytearray()).extend(data)

  def read(self, fd: int, buf) -> bytes:
    data = self.rbuf.get(fd, bytearray())
    if not data:
      return b''    # 视为 EOF（BulkEndpoint.recv 会据此抛 EOFError）
    n = min(len(buf), self.max_read, len(data))
    out = bytes(data[:n])
    del data[:n]
    return out

  def write(self, fd: int, mv) -> int:
    self.writes.append((fd, bytes(mv)))
    self.wbuf.extend(mv)
    return mv.nbytes


def _endpoint(io: _FakeIo, read_chunk: int = BT.DEFAULT_READ_CHUNK,
              write_chunk: int = BT.DEFAULT_WRITE_CHUNK) -> BT.BulkEndpoint:
  return BT.BulkEndpoint(ep_in=7, ep_out=8, read_chunk=read_chunk, write_chunk=write_chunk, io=io)


class AlignmentTests(unittest.TestCase):
  def test_pad_math(self):
    self.assertEqual(BT.TX_ALIGN, 16 * 1024)
    self.assertEqual(BT.alignment_pad(0), 0)
    self.assertEqual(BT.alignment_pad(1), BT.TX_ALIGN - 1)
    self.assertEqual(BT.alignment_pad(BT.TX_ALIGN), 0)
    self.assertEqual(BT.alignment_pad(BT.TX_ALIGN + 5), BT.TX_ALIGN - 5)

  def test_sendall_pads_to_chunk_and_receiver_strips(self):
    io = _FakeIo()
    ep = _endpoint(io)
    chunk = ep.write_chunk
    payload = bytes(range(256)) * (384 * 1024 // 256)   # 384 KiB 真实载荷
    header = P.Header(P.MSG_INFERENCE_REQ, request_id=42, frame_id=1)
    message = P.build_wire_message(header, payload)
    sent = ep.sendall(message)

    self.assertEqual(len(io.wbuf), sent)
    self.assertEqual(len(io.wbuf) % chunk, 0)
    self.assertEqual(len(io.wbuf) % BT.TX_ALIGN, 0)
    # 线上前 len(message) 字节与协议逐字节一致，其余为确定性零填充。
    self.assertEqual(bytes(io.wbuf[:len(message)]), message)
    pad = (-len(message)) % chunk
    self.assertEqual(len(io.wbuf) - len(message), pad)
    self.assertEqual(bytes(io.wbuf[len(message):]), b'\x00' * pad)
    # 每个 writev 都是满 chunk，主机才能逐 request 收下。
    for _, w in io.writes:
      self.assertEqual(len(w), chunk)

  def test_sendall_chunks_large_message(self):
    io = _FakeIo()
    ep = _endpoint(io, write_chunk=64 * 1024)
    message = b'x' * (200 * 1024)
    ep.sendall(message)
    self.assertGreater(len(io.writes), 1)
    self.assertEqual(b''.join(w for _, w in io.writes), bytes(io.wbuf))
    for _, chunk in io.writes[:-1]:
      self.assertEqual(len(chunk), 64 * 1024)


class RecvTests(unittest.TestCase):
  def test_recv_reassembles_short_reads_and_buffers_excess(self):
    io = _FakeIo(max_read=1000)
    ep = _endpoint(io)
    io.feed(8, b'A' * 2500)
    self.assertEqual(ep.recv(600), b'A' * 600)     # 第一次短读 1000，缓存 400
    self.assertEqual(ep.recv(600), b'A' * 600)
    self.assertEqual(ep.recv(600), b'A' * 600)
    self.assertEqual(ep.recv(700), b'A' * 700)     # 剩余 700 刚好

  def test_recv_eof_on_empty(self):
    io = _FakeIo()
    ep = _endpoint(io)
    with self.assertRaises(EOFError):
      ep.recv(1)

  def test_read_chunk_parsing(self):
    self.assertEqual(BT._resolve_read_chunk(64 * 1024), 64 * 1024)
    self.assertEqual(BT._resolve_read_chunk(128 * 1024), 128 * 1024)
    # 256 KiB 必现 order-6 失败：非法/超限一律回落到 128 KiB。
    self.assertEqual(BT._resolve_read_chunk(256 * 1024), BT.DEFAULT_READ_CHUNK)
    self.assertEqual(BT._resolve_read_chunk(123), BT.DEFAULT_READ_CHUNK)

  def test_write_chunk_parsing(self):
    self.assertEqual(BT._resolve_write_chunk(64 * 1024), 64 * 1024)
    self.assertEqual(BT._resolve_write_chunk(1000), BT.DEFAULT_WRITE_CHUNK)


class TransportSelectionTests(unittest.TestCase):
  def test_config_accepts_bulk_default_tcp_and_falls_back(self):
    self.assertEqual(config.sanitize({})['transport'], 'tcp')
    self.assertEqual(config.sanitize({'transport': 'bulk'})['transport'], 'bulk')
    self.assertEqual(config.sanitize({'transport': 'BULK'})['transport'], 'bulk')
    self.assertEqual(config.sanitize({'transport': 'quic'})['transport'], 'tcp')
    self.assertEqual(config.sanitize({'transport': 123})['transport'], 'tcp')

  def test_client_accepts_bulk_and_keeps_default_tcp(self):
    c = RemotePipelineClient('ab' * 32, address='127.0.0.1:1', transport='bulk')
    self.assertEqual(c.transport, 'bulk')
    c2 = RemotePipelineClient('ab' * 32, address='127.0.0.1:1')
    self.assertEqual(c2.transport, 'tcp')
    c3 = RemotePipelineClient('ab' * 32, address='127.0.0.1:1', transport='nope')
    self.assertEqual(c3.transport, 'tcp')


class BoundedIOTests(unittest.TestCase):
  """ffs IO 必须有界：对端消失时 readv/writev 会永久阻塞，线程回不来且 ep 无法重开。"""

  def test_write_timeout_is_bounded(self):
    """队列一直满时 sendall 必须在上限内抛错，而不是永久阻塞在 ffs_epfile_io。"""
    import time as _time

    class _FullIo:
      def write(self, fd, mv):
        raise BlockingIOError(errno.EAGAIN, 'queue full')

      def read(self, fd, buf):
        return b''

    original = BT.BULK_WRITE_TIMEOUT_S
    BT.BULK_WRITE_TIMEOUT_S = 0.2
    try:
      ep = self._endpoint_with_fake_fds(_FullIo())
      started = _time.monotonic()
      with self.assertRaises(TimeoutError):
        ep.sendall(b'x' * 32)
      self.assertLess(_time.monotonic() - started, 2.0, '必须在上限内返回')
    finally:
      BT.BULK_WRITE_TIMEOUT_S = original

  def _endpoint_with_fake_fds(self, io):
    """用**保证无效**的 fd 号构造端点：close() 会去 os.close(fd)，若 fd 恰好是
    测试进程里的真实 fd（7/8 常是日志/管道），就会误关进程自己的句柄。"""
    return BT.BulkEndpoint(ep_in=1 << 30, ep_out=(1 << 30) + 1, io=io)

  def test_recv_checks_closed_before_blocking(self):
    """`close()` 之后 recv 必须立刻抛 EOF，而不是继续读。"""
    ep = self._endpoint_with_fake_fds(_FakeIo())
    ep.close()
    with self.assertRaises(EOFError):
      ep.recv(16)

  def test_sendall_after_close_raises(self):
    ep = self._endpoint_with_fake_fds(_FakeIo())
    ep.close()
    with self.assertRaises(OSError):
      ep.sendall(b'x' * 32)

  def test_pollable_flag_defaults_off_for_injected_fds(self):
    ep = _endpoint(_FakeIo())
    self.assertFalse(ep._pollable, '注入的假 fd 不可 select，必须保持旧语义')

  def test_bounded_limits_have_health_margins(self):
    # 上限必须远大于真机实测（单帧 384KiB 写 ~1ms、20Hz），否则会误杀健康链路
    self.assertGreater(BT.BULK_WRITE_TIMEOUT_S, 1.0)
    self.assertLessEqual(BT.BULK_READ_POLL_S, 1.0)


if __name__ == '__main__':
  unittest.main()
