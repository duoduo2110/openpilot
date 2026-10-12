"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

OPRP 协议自包含移植的离线单测：header/payload 双 CRC32、HELLO/ERROR 编解码、
tensor 记录流校验、契约指纹。全部走本地 socketpair，无网络依赖。
"""
from __future__ import annotations

import socket
import struct
import unittest

import numpy as np

from openpilot.sunnypilot.accelerators.remote import protocol as P


class HeaderTests(unittest.TestCase):
  def test_roundtrip_preserves_all_fields(self):
    h = P.Header(P.MSG_INFERENCE_REQ, request_id=7, frame_id=3, timestamp_eof=123456,
                 flags=P.FLAG_RESET_TEMPORAL)
    raw = h.pack(10)
    self.assertEqual(len(raw), struct.calcsize(P.HEADER_FORMAT))
    u = P.Header.unpack(raw)
    self.assertEqual((u.magic, u.version, u.msg_type, u.flags), (P.MAGIC, P.PROTOCOL_VERSION,
                                                                P.MSG_INFERENCE_REQ, P.FLAG_RESET_TEMPORAL))
    self.assertEqual((u.request_id, u.frame_id, u.timestamp_eof, u.payload_len), (7, 3, 123456, 10))

  def test_header_crc_corruption_rejected(self):
    raw = bytearray(P.Header(P.MSG_HELLO_REQ).pack(0))
    raw[-1] ^= 0xFF
    with self.assertRaisesRegex(P.ProtocolError, 'Header CRC mismatch'):
      P.Header.unpack(bytes(raw))

  def test_bad_magic_rejected(self):
    body = struct.pack('!I H H I Q Q Q I', 0xDEADBEEF, P.PROTOCOL_VERSION, P.MSG_HELLO_REQ,
                       0, 1, 2, 3, 0)
    with self.assertRaisesRegex(P.ProtocolError, 'Invalid magic'):
      P.Header.unpack(body + struct.pack('!I', P.zlib.crc32(body) & 0xFFFFFFFF))

  def test_bad_version_rejected(self):
    body = struct.pack('!I H H I Q Q Q I', P.MAGIC, 99, P.MSG_HELLO_REQ, 0, 1, 2, 3, 0)
    with self.assertRaisesRegex(P.ProtocolError, 'Unsupported protocol version'):
      P.Header.unpack(body + struct.pack('!I', P.zlib.crc32(body) & 0xFFFFFFFF))

  def test_unknown_flags_rejected(self):
    body = struct.pack('!I H H I Q Q Q I', P.MAGIC, P.PROTOCOL_VERSION, P.MSG_HELLO_REQ, 0x8000, 1, 2, 3, 0)
    with self.assertRaisesRegex(P.ProtocolError, 'Unknown header flags'):
      P.Header.unpack(body + struct.pack('!I', P.zlib.crc32(body) & 0xFFFFFFFF))

  def test_short_header_rejected(self):
    with self.assertRaisesRegex(P.ProtocolError, 'Header length mismatch'):
      P.Header.unpack(b'not-a-header')


class FramingTests(unittest.TestCase):
  def setUp(self):
    self.sender, self.receiver = socket.socketpair()

  def tearDown(self):
    for s in (self.sender, self.receiver):
      s.close()

  def test_send_recv_roundtrip(self):
    payload = b'hello payload'
    P.send_message(self.sender, P.Header(P.MSG_HELLO_REQ, request_id=5), payload)
    header, got = P.recv_message(self.receiver)
    self.assertEqual((header.msg_type, header.request_id), (P.MSG_HELLO_REQ, 5))
    self.assertEqual(got, payload)

  def test_payload_crc_mismatch_rejected(self):
    payload = b'payload'
    header = P.Header(P.MSG_INFERENCE_RESP, request_id=1)
    self.sender.sendall(header.pack(len(payload)) + payload + struct.pack('!I', 0x12345678))
    with self.assertRaisesRegex(P.ProtocolError, 'Payload CRC mismatch'):
      P.recv_message(self.receiver)

  def test_oversize_payload_rejected_before_read(self):
    header = P.Header(P.MSG_INFERENCE_RESP, request_id=1)
    self.sender.sendall(header.pack(P.MAX_REQUEST_PAYLOAD_SIZE + 1))
    with self.assertRaisesRegex(P.ProtocolError, 'exceeds limit'):
      P.recv_message(self.receiver, max_payload_size=P.MAX_REQUEST_PAYLOAD_SIZE)

  def test_recv_exact_handles_fragmentation(self):
    payload = bytes(range(256)) * 4
    header = P.Header(P.MSG_INFERENCE_RESP)
    blob = header.pack(len(payload)) + payload + struct.pack('!I', P.zlib.crc32(payload) & 0xFFFFFFFF)
    # 分三段写出，模拟 TCP 分片
    for chunk in (blob[:5], blob[5:100], blob[100:]):
      self.sender.sendall(chunk)
    header, got = P.recv_message(self.receiver)
    self.assertEqual(got, payload)

  def test_eof_raises(self):
    self.sender.close()
    with self.assertRaises(EOFError):
      P.recv_message(self.receiver)


class HelloTests(unittest.TestCase):
  SIGNATURE = 'ab' * 32

  def test_hello_req_roundtrip(self):
    payload = P.pack_hello_req(self.SIGNATURE, capabilities=0x7)
    sig, caps = P.unpack_hello_req(payload)
    self.assertEqual((sig, caps), (self.SIGNATURE, 0x7))

  def test_hello_req_bad_signature_rejected(self):
    with self.assertRaises(P.ProtocolError):
      P.pack_hello_req('xyz')
    payload = struct.pack('!H', 64) + b'Z' * 64 + struct.pack('!I', 0)
    with self.assertRaises(P.ProtocolError):
      P.unpack_hello_req(payload)

  def test_hello_req_length_mismatch(self):
    payload = P.pack_hello_req(self.SIGNATURE) + b'\x00'
    with self.assertRaisesRegex(P.ProtocolError, 'length mismatch'):
      P.unpack_hello_req(payload)

  def test_hello_resp_roundtrip(self):
    status, caps, msg = P.unpack_hello_resp(P.pack_hello_resp(0, 0x3, 'ready'))
    self.assertEqual((status, caps, msg), (0, 0x3, 'ready'))

  def test_hello_resp_length_mismatch(self):
    with self.assertRaisesRegex(P.ProtocolError, 'length mismatch'):
      P.unpack_hello_resp(P.pack_hello_resp(0, 0, 'ok') + b'\x00')

  def test_error_resp_roundtrip(self):
    code, msg = P.unpack_error_resp(P.pack_error_resp(7, 'boom'))
    self.assertEqual((code, msg), (7, 'boom'))
    with self.assertRaisesRegex(P.ProtocolError, 'length mismatch'):
      P.unpack_error_resp(P.pack_error_resp(7, 'boom') + b'\x00')


class TensorDictTests(unittest.TestCase):
  def test_roundtrip_all_dtypes_and_scalar(self):
    tensors = {
      'warped': np.arange(2 * 6 * 3 * 4, dtype=np.uint8).reshape(2, 6, 3, 4),
      'half': np.linspace(0.0, 1.0, 5, dtype=np.float16),
      'f32': np.arange(6, dtype=np.float32),
      'scalar': np.array(3.5, dtype=np.float32),
    }
    out = P.unpack_tensor_dict(P.pack_tensor_dict(tensors))
    self.assertEqual(set(out), set(tensors))
    for name, arr in tensors.items():
      np.testing.assert_array_equal(out[name], arr)
      self.assertEqual(out[name].dtype, arr.dtype)

  def test_duplicate_name_rejected(self):
    payload = P.pack_tensor_dict({'a': np.zeros(2, dtype=np.uint8)})
    # 手工拼第二份同名记录
    dup = struct.pack('!H', 2) + payload[2:] + payload[2:]
    with self.assertRaisesRegex(P.ProtocolError, 'Duplicate tensor name'):
      P.unpack_tensor_dict(dup)

  def test_trailing_bytes_rejected(self):
    with self.assertRaisesRegex(P.ProtocolError, 'trailing bytes'):
      P.unpack_tensor_dict(P.pack_tensor_dict({'a': np.zeros(2, dtype=np.uint8)}) + b'\x00')

  def test_unsupported_dtype_rejected(self):
    with self.assertRaisesRegex(P.ProtocolError, 'Unsupported dtype'):
      P.pack_tensor_dict({'a': np.zeros(2, dtype=np.float64)})
    with self.assertRaisesRegex(P.ProtocolError, 'Unsupported dtype'):
      P.pack_tensor_dict({'a': np.zeros(2, dtype=np.int32)})

  def test_allowlist_enforced(self):
    payload = P.pack_tensor_dict({'warped': np.zeros(2, dtype=np.uint8)})
    with self.assertRaisesRegex(P.ProtocolError, 'not in allowlist'):
      P.unpack_tensor_dict(payload, allowlist={'other'})

  def test_byte_length_mismatch_rejected(self):
    # 手工构造：1 个 tensor，uint8，shape(2,)，但声明 999 字节
    body = struct.pack('!H', 1)
    body += struct.pack('!H', 1) + b'a' + struct.pack('!B B', P.DTYPE_UINT8, 1)
    body += struct.pack('!1I', 2) + struct.pack('!Q', 999) + b'\x00\x00'
    with self.assertRaisesRegex(P.ProtocolError, 'byte length mismatch'):
      P.unpack_tensor_dict(body)

  def test_truncated_record_rejected(self):
    with self.assertRaises(P.ProtocolError):
      P.unpack_tensor_dict(struct.pack('!H', 1) + b'\x00')

  def test_too_many_tensors_rejected(self):
    with self.assertRaises(P.ProtocolError):
      P.pack_tensor_dict({f't{i}': np.zeros(1, dtype=np.uint8) for i in range(P.MAX_TENSORS + 1)})


class TensorCompressionTests(unittest.TestCase):
  def _big_compressible(self) -> np.ndarray:
    return np.tile(np.arange(256, dtype=np.uint8), 1536).reshape(2, 6, 128, 256)

  def test_legacy_layout_is_byte_identical_by_default(self):
    tensors = {'warped': np.arange(2 * 6 * 3 * 4, dtype=np.uint8).reshape(2, 6, 3, 4)}
    self.assertEqual(P.pack_tensor_dict(tensors), P.pack_tensor_dict(tensors, compress=False))
    self.assertEqual(set(P.unpack_tensor_dict(P.pack_tensor_dict(tensors))), set(tensors))

  def test_compressed_roundtrip_shrinks_and_preserves_values(self):
    big = self._big_compressible()
    tensors = {'warped': big, 'desire': np.arange(8, dtype=np.float32)}
    raw = P.pack_tensor_dict(tensors, compress=False)
    packed = P.pack_tensor_dict(tensors, compress=True)
    self.assertLess(len(packed), len(raw), 'compressible warped 载荷应更小')

    out = P.unpack_tensor_dict(packed, compressed=True)
    self.assertEqual(set(out), set(tensors))
    for name, arr in tensors.items():
      np.testing.assert_array_equal(out[name], arr)
      self.assertEqual(out[name].dtype, arr.dtype)

  def test_small_tensor_not_compressed_in_compressed_layout(self):
    small = np.arange(2 * 6 * 3 * 4, dtype=np.uint8)
    packed = P.pack_tensor_dict({'warped': small}, compress=True)
    # 记录里 encoding 字节必须仍是 RAW(0)
    encoding_off = 2 + 2 + len(b'warped') + 2 + 4 * small.ndim
    self.assertEqual(packed[encoding_off], P._ENCODING_RAW)
    out = P.unpack_tensor_dict(packed, compressed=True)
    np.testing.assert_array_equal(out['warped'], small)

  def test_truncated_compressed_payload_fails_closed(self):
    packed = P.pack_tensor_dict({'warped': self._big_compressible()}, compress=True)
    with self.assertRaises(P.ProtocolError):
      P.unpack_tensor_dict(packed[:-5], compressed=True)

  def test_decompress_exact_rejects_wrong_length(self):
    stored = P.zlib.compress(b'x' * 10)
    with self.assertRaisesRegex(P.ProtocolError, 'length mismatch'):
      P._decompress_exact(stored, 11)
    self.assertEqual(P._decompress_exact(stored, 10), b'x' * 10)

  def test_decompress_exact_rejects_corrupt_stream(self):
    with self.assertRaises(P.ProtocolError):
      P._decompress_exact(b'not-a-zlib-stream', 4)


class FingerprintTests(unittest.TestCase):
  META = {
    'input_shapes': {'warped': (2, 6, 128, 256)},
    'output_slices': {'plan': slice(0, 10, 1)},
    'vision': {'input_shapes': {'img': (1, 3, 4, 5)}, 'output_slices': {}},
  }

  def test_stable_and_shape_sensitive(self):
    first = P.compute_contract_fingerprint(dict(self.META))
    self.assertEqual(first, P.compute_contract_fingerprint(dict(self.META)))
    changed = dict(self.META)
    changed['input_shapes'] = {'warped': (2, 6, 128, 257)}
    self.assertNotEqual(first, P.compute_contract_fingerprint(changed))
    self.assertEqual(len(first), 64)


if __name__ == '__main__':
  unittest.main()
