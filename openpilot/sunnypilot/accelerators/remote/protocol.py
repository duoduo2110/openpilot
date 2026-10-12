"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

OPRP（OpenPilot Remote Protocol）线上帧格式，自包含移植版。

数据语义与 C3 侧已验证实现保持一致（源：pc-remote-model/pc_remote_model/
remote_protocol.py），但不依赖 pc_remote_model 包路径，可被 openpilot 直接导入。

线格式：header(44B, 含 header CRC32) + payload + payload CRC32(4B)。
header 为 `!I H H I Q Q Q I I`：magic/version/msg_type/flags/req_id/frame_id/
timestamp_eof/payload_len/header_crc32。req_id 与 frame_id 供流水线按请求配对。

约定：C3 只上传 warped uint8 帧与标量 packed；history/prev_feat 由服务端持有，
禁止上行（见 remote_server 的 FORBIDDEN_NETWORK_INPUTS，客户端侧在 client.py 复核）。
"""
from __future__ import annotations

import io
import select
import socket
import struct
import time
import zlib
import hashlib
import numpy as np

MAGIC = 0x4F505250  # "OPRP" ASCII
PROTOCOL_VERSION = 1

# Message types
MSG_HELLO_REQ = 0x0001
MSG_HELLO_RESP = 0x0002
MSG_INFERENCE_REQ = 0x0003
MSG_INFERENCE_RESP = 0x0004
MSG_ERROR_RESP = 0xFFFF

# MSG_ERROR_RESP 的服务端错误码（与 pc_remote_model.remote_server 一一对应）。
# 客户端按码分级处理：**只有 4 是链路级**（服务端已不认这条连接：PC 侧进程重启、
# 会话重建或链路被中间设备重置），必须整条链路重新握手；其余是单帧/单请求级，
# 只丢当前帧，链路保留（否则一次偶发的推理异常就会把整条链路打成退避）。
ERR_INVALID_HELLO = 3
ERR_INFERENCE_BEFORE_HELLO = 4
ERR_INFERENCE_EXECUTION = 5
ERR_UNKNOWN_MSG_TYPE = 6
# 需要「彻底拆链 + 重新 HELLO」的服务端错误码
LINK_FATAL_ERROR_CODES = frozenset({ERR_INFERENCE_BEFORE_HELLO})

# Flags
FLAG_RESET_TEMPORAL = 0x0001
# tensor 载荷采用「按张量可压缩」记录布局。仅在 HELLO capabilities 协商成功后由
# 发送方置位；老端不认识该位，也就永远不会收到（未协商成功不压缩）。
FLAG_ZLIB_TENSOR = 0x0002
VALID_FLAGS = FLAG_RESET_TEMPORAL | FLAG_ZLIB_TENSOR

# HELLO capabilities 协商位：置位表示本端支持「按张量 zlib 压缩」。
CAP_ZLIB_TENSOR = 0x00000001

# 压缩策略：只压图像类大张量（例如 warped），标量小张量保持原样。
COMPRESS_MIN_BYTES = 64 * 1024   # 小于该字节数不尝试压缩
COMPRESS_LEVEL = 1               # zlib level（1 最快；带宽瓶颈下优先省时延）
_ENCODING_RAW = 0                # 压缩布局内的编码标记：原始字节
_ENCODING_ZLIB = 1               # 压缩布局内的编码标记：zlib 流

# Limits
MAX_REQUEST_PAYLOAD_SIZE = 2 * 1024 * 1024   # 2 MiB
MAX_RESPONSE_PAYLOAD_SIZE = 8 * 1024 * 1024  # 8 MiB
MAX_TENSORS = 32
MAX_NDIM = 8
MAX_DIM_SIZE = 1048576
MAX_TOTAL_ELEMENTS = 32 * 1024 * 1024

# Header format: magic (4B), version (2B), msg_type (2B), flags (4B),
# request_id (8B), frame_id (8B), timestamp_eof (8B), payload_len (4B), header_crc32 (4B)
HEADER_FORMAT = '!I H H I Q Q Q I I'
HEADER_CRC_OFFSET = struct.calcsize('!I H H I Q Q Q I')  # 40 bytes

HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 44 bytes（header + header CRC32）

# -- UDP 分片传输（可选，默认不走；与 PC 侧 pc_remote_model/remote_protocol.py 镜像）--
# 每个 UDP datagram 的载荷 = CHUNK_HDR(16B) + 本片数据(<= chunk_data)。
# 字段：msg_id(u32, 对应 Header.request_id)、chunk_idx(u16)、chunk_total(u16)、
#       msg_len(u32, 重组后完整消息长度)、msg_crc(u32, 完整消息 payload 的 CRC32)。
# 完整消息 = Header.pack(payload_len) + payload + struct.pack('!I', crc32(payload))，
# 与 TCP 的 send_message() 写出的字节流**逐字节一致**。
CHUNK_HDR_FORMAT = '!IHHII'
CHUNK_HDR_SIZE = 16
CHUNK_DATA_DEFAULT = 16384

# 分片总数上限（chunk_total 为 u16）
_MAX_CHUNK_TOTAL = 0xFFFF
# 重组缓冲的 msg_id 上限：防止异常/恶意对端堆积未完成消息占用内存
_MAX_REASSEMBLY_BUFFERS = 64
# 单个 UDP datagram 的最大接收长度（远大于 16 + 16384）
MAX_DATAGRAM_SIZE = 65535

# Dtype codes
DTYPE_UINT8 = 1
DTYPE_FLOAT16 = 2
DTYPE_FLOAT32 = 3

DTYPE_CODE_TO_NUMPY = {
  DTYPE_UINT8: np.dtype(np.uint8),
  DTYPE_FLOAT16: np.dtype(np.float16),
  DTYPE_FLOAT32: np.dtype(np.float32),
}

NUMPY_TO_DTYPE_CODE = {
  np.dtype(np.uint8): DTYPE_UINT8,
  np.dtype(np.float16): DTYPE_FLOAT16,
  np.dtype(np.float32): DTYPE_FLOAT32,
}


class ProtocolError(Exception):
  """Exception raised for remote protocol errors."""
  pass


class Header:
  def __init__(self, msg_type: int, request_id: int = 0, frame_id: int = 0,
               timestamp_eof: int = 0, flags: int = 0, version: int = PROTOCOL_VERSION):
    self.magic = MAGIC
    self.version = version
    self.msg_type = msg_type
    self.flags = flags
    self.request_id = request_id
    self.frame_id = frame_id
    self.timestamp_eof = timestamp_eof
    self.payload_len = 0

  def pack(self, payload_len: int) -> bytes:
    if payload_len < 0 or payload_len > 0xFFFFFFFF:
      raise ProtocolError(f"Invalid payload length: {payload_len}")
    self.payload_len = payload_len
    header_without_crc = struct.pack(
      '!I H H I Q Q Q I',
      self.magic,
      self.version,
      self.msg_type,
      self.flags,
      self.request_id,
      self.frame_id,
      self.timestamp_eof,
      self.payload_len,
    )
    crc = zlib.crc32(header_without_crc) & 0xFFFFFFFF
    return header_without_crc + struct.pack('!I', crc)

  @classmethod
  def unpack(cls, data: bytes) -> 'Header':
    expected_header_size = struct.calcsize(HEADER_FORMAT)
    if len(data) != expected_header_size:
      raise ProtocolError(f"Header length mismatch: expected {expected_header_size}, got {len(data)}")

    header_without_crc = data[:HEADER_CRC_OFFSET]
    expected_crc = zlib.crc32(header_without_crc) & 0xFFFFFFFF
    actual_crc, = struct.unpack('!I', data[HEADER_CRC_OFFSET:expected_header_size])
    if actual_crc != expected_crc:
      raise ProtocolError(f"Header CRC mismatch: expected 0x{expected_crc:08x}, got 0x{actual_crc:08x}")

    magic, version, msg_type, flags, req_id, frame_id, ts_eof, payload_len = struct.unpack(
      '!I H H I Q Q Q I', header_without_crc
    )

    if magic != MAGIC:
      raise ProtocolError(f"Invalid magic: 0x{magic:08x} != 0x{MAGIC:08x}")
    if version != PROTOCOL_VERSION:
      raise ProtocolError(f"Unsupported protocol version: {version} != {PROTOCOL_VERSION}")
    if flags & ~VALID_FLAGS != 0:
      raise ProtocolError(f"Unknown header flags: 0x{flags:08x}")

    hdr = cls(msg_type, req_id, frame_id, ts_eof, flags, version)
    hdr.payload_len = payload_len
    return hdr


def recv_exact(sock, n: int) -> bytes:
  """Receive exactly n bytes from socket, handling fragmentation and EOF."""
  buf = bytearray()
  while len(buf) < n:
    chunk = sock.recv(n - len(buf))
    if not chunk:
      raise EOFError(f"Connection closed while waiting for {n - len(buf)} bytes")
    buf.extend(chunk)
  return bytes(buf)


def send_message(sock, header: Header, payload: bytes) -> None:
  """Send header, payload, and payload CRC32 over socket."""
  payload_len = len(payload)
  header_bytes = header.pack(payload_len)
  payload_crc = zlib.crc32(payload) & 0xFFFFFFFF
  sock.sendall(header_bytes + payload + struct.pack('!I', payload_crc))


def recv_message(sock, max_payload_size: int = MAX_REQUEST_PAYLOAD_SIZE) -> tuple[Header, bytes]:
  """Receive and validate message header, payload, and payload CRC32."""
  header_size = struct.calcsize(HEADER_FORMAT)
  header_bytes = recv_exact(sock, header_size)
  header = Header.unpack(header_bytes)

  if header.payload_len > max_payload_size:
    raise ProtocolError(f"Payload size {header.payload_len} exceeds limit {max_payload_size}")

  payload = recv_exact(sock, header.payload_len)
  crc_bytes = recv_exact(sock, 4)
  actual_payload_crc, = struct.unpack('!I', crc_bytes)
  expected_payload_crc = zlib.crc32(payload) & 0xFFFFFFFF
  if actual_payload_crc != expected_payload_crc:
    raise ProtocolError(f"Payload CRC mismatch: expected 0x{expected_payload_crc:08x}, got 0x{actual_payload_crc:08x}")

  return header, payload


# -- UDP 分片传输原语（可选，默认不走；见 protocol.py 顶部 CHUNK_* 说明）---------------

def build_wire_message(header: Header, payload: bytes) -> bytes:
  """组出与 TCP send_message() 完全一致的完整消息字节流。

  = Header.pack(payload_len) + payload + struct.pack('!I', crc32(payload))
  """
  payload_crc = zlib.crc32(payload) & 0xFFFFFFFF
  return header.pack(len(payload)) + payload + struct.pack('!I', payload_crc)


def _timeout_error(message: str) -> Exception:
  """UDP deadline 超时复用客户端既有 RemoteTimeout；独立使用时回落到 ProtocolError。

  延迟导入 .client 以避免 protocol <-> client 的循环导入（调用发生在模块加载完成后）。
  """
  try:
    from .client import RemoteTimeout  # noqa: PLC0415
    return RemoteTimeout(message)
  except Exception:
    return ProtocolError(message)


def _addr_matches(src, expect) -> bool:
  """来源地址是否匹配 expect_addr；无法解析时从宽放行（不误杀）。"""
  if src is None or expect is None:
    return True
  try:
    src_host, src_port = str(src[0]), int(src[1])
    exp_host, exp_port = str(expect[0]), int(expect[1])
  except (TypeError, IndexError, ValueError):
    return True
  if src_port != exp_port:
    return False
  if not exp_host or src_host == exp_host:
    return True
  try:
    resolved = {info[4][0] for info in
                socket.getaddrinfo(exp_host, exp_port, type=socket.SOCK_DGRAM)}
  except OSError:
    resolved = set()
  return src_host in resolved


def send_message_udp(sock, addr, header: Header, payload: bytes,
                     chunk_data: int = CHUNK_DATA_DEFAULT) -> None:
  """把一条完整消息按 chunk_data 切片后逐片 sendto/发送（不重传、不保证到达）。

  addr=None 时用已 connect() 的 socket 直接 send()；否则 sendto(datagram, addr)。
  """
  if chunk_data <= 0:
    raise ProtocolError(f'chunk_data must be positive, got {chunk_data}')
  message = build_wire_message(header, payload)
  msg_len = len(message)
  msg_crc = zlib.crc32(payload) & 0xFFFFFFFF
  msg_id = header.request_id & 0xFFFFFFFF
  chunk_total = (msg_len + chunk_data - 1) // chunk_data
  if chunk_total > _MAX_CHUNK_TOTAL:
    raise ProtocolError(f'message needs {chunk_total} chunks, exceeds {_MAX_CHUNK_TOTAL}')
  for idx in range(chunk_total):
    data = message[idx * chunk_data:(idx + 1) * chunk_data]
    datagram = struct.pack(CHUNK_HDR_FORMAT, msg_id, idx, chunk_total, msg_len, msg_crc) + data
    if addr is None:
      sock.send(datagram)
    else:
      sock.sendto(datagram, addr)


def recv_message_udp(sock, deadline: float | None = None,
                     max_payload_size: int = MAX_REQUEST_PAYLOAD_SIZE,
                     expect_addr=None,
                     chunk_data: int = CHUNK_DATA_DEFAULT) -> tuple[Header, bytes]:
  """接收并按 msg_id 重组一条完整 UDP 分片消息，返回 (Header, payload)。

  - 乱序：按 chunk_idx 落位，收齐 chunk_total 片才交付；
  - 重复片：重复的 chunk_idx 直接丢弃；
  - 校验：msg_len/chunk_total 自洽、完整消息长度、payload CRC32、Header.unpack；
    任何校验失败只丢弃该消息并继续等待，不抛异常（UDP 下单片损坏不应拖垮链路）；
  - 超时：超过 deadline 抛既有 RemoteTimeout（独立使用回落 ProtocolError）；
  - expect_addr 非空时丢弃来源不符的包；
  - 单条消息 msg_len > HEADER_SIZE + max_payload_size + 4 直接丢弃。

  deadline=None 时阻塞等待（直到收到一条完整消息）。select 只读就绪，不改动
  共享 socket 的超时，避免影响其它线程。
  """
  if chunk_data <= 0:
    raise ProtocolError(f'chunk_data must be positive, got {chunk_data}')
  max_message_len = HEADER_SIZE + max_payload_size + 4
  buffers: dict[int, dict] = {}
  order: list[int] = []

  while True:
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
      raise _timeout_error('UDP reassembly deadline exceeded')
    try:
      readable, _, _ = select.select([sock], [], [], remaining)
    except (OSError, ValueError) as e:
      raise ProtocolError(f'UDP select failed: {e}') from e
    if not readable:
      raise _timeout_error('UDP reassembly deadline exceeded')
    try:
      datagram, src = sock.recvfrom(MAX_DATAGRAM_SIZE)
    except OSError as e:
      raise ProtocolError(f'UDP recv failed: {e}') from e

    if not _addr_matches(src, expect_addr):
      continue
    if len(datagram) < CHUNK_HDR_SIZE:
      continue  # 不足以解析分片头
    msg_id, chunk_idx, chunk_total, msg_len, msg_crc = struct.unpack(
      CHUNK_HDR_FORMAT, datagram[:CHUNK_HDR_SIZE])
    if chunk_total == 0 or chunk_idx >= chunk_total:
      continue  # 非法分片头
    if msg_len > max_message_len or msg_len < CHUNK_HDR_SIZE:
      continue  # 超限/不可能的长度，直接丢弃
    # chunk_total 必须与 msg_len/chunk_data 自洽（两侧 chunk_data 不一致时据此发现）
    expected_total = (msg_len + chunk_data - 1) // chunk_data
    if chunk_total != expected_total:
      continue
    data = datagram[CHUNK_HDR_SIZE:]
    expected_this = min(chunk_data, msg_len - chunk_idx * chunk_data)
    if len(data) != expected_this:
      continue  # 分片长度不符（截断/脏片）

    buf = buffers.get(msg_id)
    if buf is None:
      buf = {'parts': {}, 'chunk_total': chunk_total, 'msg_len': msg_len,
             'msg_crc': msg_crc, 'count': 0}
      buffers[msg_id] = buf
      order.append(msg_id)
      if len(order) > _MAX_REASSEMBLY_BUFFERS:
        evicted = order.pop(0)
        buffers.pop(evicted, None)
    elif (buf['chunk_total'] != chunk_total or buf['msg_len'] != msg_len
          or buf['msg_crc'] != msg_crc):
      continue  # 同一 msg_id 的分片头不一致，视为脏数据丢弃
    if chunk_idx in buf['parts']:
      continue  # 重复片丢弃
    buf['parts'][chunk_idx] = data
    buf['count'] += 1
    if buf['count'] < buf['chunk_total']:
      continue

    # 收齐：组装完整消息并校验
    del buffers[msg_id]
    if msg_id in order:
      order.remove(msg_id)
    message = b''.join(buf['parts'][i] for i in range(buf['chunk_total']))
    if len(message) != msg_len:
      continue
    header_bytes = message[:HEADER_SIZE]
    payload = message[HEADER_SIZE:msg_len - 4]
    actual_crc, = struct.unpack('!I', message[msg_len - 4:])
    if actual_crc != (zlib.crc32(payload) & 0xFFFFFFFF) or actual_crc != msg_crc:
      continue  # CRC 损坏：丢弃该消息，继续等待
    if len(payload) > max_payload_size:
      continue
    try:
      header = Header.unpack(header_bytes)
    except ProtocolError:
      continue
    if header.payload_len != len(payload):
      continue
    return header, payload


def _normalize_slice_tuple(val) -> tuple[int, int, int]:
  if isinstance(val, slice):
    return (
      0 if val.start is None else int(val.start),
      0 if val.stop is None else int(val.stop),
      1 if val.step is None else int(val.step),
    )
  elif isinstance(val, (list, tuple)):
    if len(val) == 3:
      return (int(val[0]), int(val[1]), int(val[2]))
    elif len(val) == 2:
      return (int(val[0]), int(val[1]), 1)
    elif len(val) == 1:
      return (int(val[0]), int(val[0]) + 1, 1)
  elif isinstance(val, (int, np.integer)):
    return (int(val), int(val) + 1, 1)
  raise TypeError(f"Unsupported slice type in metadata: {type(val)}")


def compute_contract_fingerprint(metadata: dict | None = None, bundle_ref: str = "") -> str:
  """
  Generate canonical contract fingerprint for model metadata.
  Ignores runtime/device specific fields and bundle_ref.
  Depends ONLY on static model contract (input_shapes, output_slices, policy keys).
  """
  hasher = hashlib.sha256()

  if not isinstance(metadata, dict):
    meta_bytes = str(metadata).encode('utf-8')
    hasher.update(struct.pack('!I', len(meta_bytes)) + meta_bytes)
    return hasher.hexdigest()

  # 1. Vision input_shapes
  vision_input_shapes = {}
  if 'vision' in metadata and isinstance(metadata['vision'], dict):
    vision_input_shapes = metadata['vision'].get('input_shapes', {})

  for name in sorted(vision_input_shapes.keys() if isinstance(vision_input_shapes, dict) else []):
    shape = tuple(int(x) for x in vision_input_shapes[name])
    n_bytes = name.encode('utf-8')
    hasher.update(b"v_in:" + struct.pack('!I', len(n_bytes)) + n_bytes)
    hasher.update(struct.pack(f'!I {len(shape)}I', len(shape), *shape))

  # 2. Vision output_slices
  vision_output_slices = {}
  if 'vision' in metadata and isinstance(metadata['vision'], dict):
    vision_output_slices = metadata['vision'].get('output_slices', {})

  for name in sorted(vision_output_slices.keys() if isinstance(vision_output_slices, dict) else []):
    sl_tuple = _normalize_slice_tuple(vision_output_slices[name])
    n_bytes = name.encode('utf-8')
    hasher.update(b"v_out:" + struct.pack('!I', len(n_bytes)) + n_bytes)
    hasher.update(struct.pack('!3i', *sl_tuple))

  # 3. Direct/Root input_shapes
  root_input_shapes = metadata.get('input_shapes', {})
  for name in sorted(root_input_shapes.keys() if isinstance(root_input_shapes, dict) else []):
    shape = tuple(int(x) for x in root_input_shapes[name])
    n_bytes = name.encode('utf-8')
    hasher.update(b"p_in:" + struct.pack('!I', len(n_bytes)) + n_bytes)
    hasher.update(struct.pack(f'!I {len(shape)}I', len(shape), *shape))

  # 4. Direct/Root output_slices
  root_output_slices = metadata.get('output_slices', {})
  for name in sorted(root_output_slices.keys() if isinstance(root_output_slices, dict) else []):
    sl_tuple = _normalize_slice_tuple(root_output_slices[name])
    n_bytes = name.encode('utf-8')
    hasher.update(b"p_out:" + struct.pack('!I', len(n_bytes)) + n_bytes)
    hasher.update(struct.pack('!3i', *sl_tuple))

  # 5. Policy keys (excluding runtime keys)
  runtime_keys = {'vision', 'warp_dev', 'DEV', 'QUEUE_DEV', 'input_queues', 'numpy_inputs'}
  policy_keys = sorted([k for k in metadata if k not in runtime_keys])
  for key in policy_keys:
    k_bytes = key.encode('utf-8')
    hasher.update(b"pk:" + struct.pack('!I', len(k_bytes)) + k_bytes)
    policy_meta = metadata[key]
    if isinstance(policy_meta, dict):
      p_shapes = policy_meta.get('input_shapes', {})
      for name in sorted(p_shapes.keys() if isinstance(p_shapes, dict) else []):
        shape = tuple(int(x) for x in p_shapes[name])
        n_bytes = name.encode('utf-8')
        hasher.update(b"pk_in:" + struct.pack('!I', len(n_bytes)) + n_bytes)
        hasher.update(struct.pack(f'!I {len(shape)}I', len(shape), *shape))

      p_slices = policy_meta.get('output_slices', {})
      for name in sorted(p_slices.keys() if isinstance(p_slices, dict) else []):
        sl_tuple = _normalize_slice_tuple(p_slices[name])
        n_bytes = name.encode('utf-8')
        hasher.update(b"pk_out:" + struct.pack('!I', len(n_bytes)) + n_bytes)
        hasher.update(struct.pack('!3i', *sl_tuple))

  return hasher.hexdigest()


def pack_hello_req(model_signature: str, capabilities: int = 0) -> bytes:
  """Pack HELLO request payload."""
  if len(model_signature) != 64 or not all(c in '0123456789abcdef' for c in model_signature):
    raise ProtocolError("HELLO signature must be a 64-character lowercase hex string")
  sig_bytes = model_signature.encode('ascii')
  return struct.pack('!H', len(sig_bytes)) + sig_bytes + struct.pack('!I', capabilities)


def unpack_hello_req(payload: bytes) -> tuple[str, int]:
  """Unpack HELLO request payload with exact length verification."""
  if len(payload) < 6:
    raise ProtocolError("HELLO request payload too short")
  sig_len, = struct.unpack('!H', payload[:2])
  expected_len = 2 + sig_len + 4
  if len(payload) != expected_len:
    raise ProtocolError(f"HELLO request payload length mismatch: expected {expected_len}, got {len(payload)}")

  sig_bytes = payload[2:2 + sig_len]
  caps, = struct.unpack('!I', payload[2 + sig_len:expected_len])
  try:
    sig = sig_bytes.decode('ascii')
  except UnicodeDecodeError as e:
    raise ProtocolError(f"Signature must be ASCII: {e}")

  if len(sig) != 64 or not all(c in '0123456789abcdef' for c in sig):
    raise ProtocolError("HELLO signature must be a 64-character lowercase hex string")

  return sig, caps


def pack_hello_resp(status: int, capabilities: int = 0, message: str = "OK") -> bytes:
  """Pack HELLO response payload."""
  msg_bytes = message.encode('ascii', errors='replace')
  if len(msg_bytes) > 65535:
    msg_bytes = msg_bytes[:65535]
  return struct.pack('!H I H', status, capabilities, len(msg_bytes)) + msg_bytes


def unpack_hello_resp(payload: bytes) -> tuple[int, int, str]:
  """Unpack HELLO response payload with exact length verification."""
  if len(payload) < 8:
    raise ProtocolError("HELLO response payload too short")
  status, caps, msg_len = struct.unpack('!H I H', payload[:8])
  expected_len = 8 + msg_len
  if len(payload) != expected_len:
    raise ProtocolError(f"HELLO response payload length mismatch: expected {expected_len}, got {len(payload)}")

  msg = payload[8:expected_len].decode('ascii', errors='replace')
  return status, caps, msg


def pack_error_resp(error_code: int, message: str) -> bytes:
  """Pack ERROR response payload."""
  msg_bytes = message.encode('ascii', errors='replace')
  if len(msg_bytes) > 65535:
    msg_bytes = msg_bytes[:65535]
  return struct.pack('!H H', error_code, len(msg_bytes)) + msg_bytes


def unpack_error_resp(payload: bytes) -> tuple[int, str]:
  """Unpack ERROR response payload with exact length verification."""
  if len(payload) < 4:
    raise ProtocolError("ERROR response payload too short")
  err_code, msg_len = struct.unpack('!H H', payload[:4])
  expected_len = 4 + msg_len
  if len(payload) != expected_len:
    raise ProtocolError(f"ERROR response payload length mismatch: expected {expected_len}, got {len(payload)}")

  msg = payload[4:expected_len].decode('ascii', errors='replace')
  return err_code, msg


def _maybe_compress_tensor(raw_bytes: bytes, min_bytes: int, level: int) -> tuple[int, bytes]:
  """按需压缩单个张量。

  小于阈值、或压缩后并不更小（随机字节）时回落原始编码，绝不把载荷压大。
  """
  if len(raw_bytes) < min_bytes:
    return _ENCODING_RAW, raw_bytes
  packed = zlib.compress(raw_bytes, level)
  if len(packed) >= len(raw_bytes):
    return _ENCODING_RAW, raw_bytes
  return _ENCODING_ZLIB, packed


def _decompress_exact(stored: bytes, expected_len: int) -> bytes:
  """精确解压：解压后长度必须严格等于 expected_len，否则 fail-closed。

  用有界 decompressobj 防止解压炸弹；任何 zlib 错误/截断/长度不符都抛 ProtocolError。
  """
  try:
    decomp = zlib.decompressobj()
    out = decomp.decompress(stored, expected_len + 1)
    if len(out) < expected_len + 1 and not decomp.eof:
      raise ProtocolError("Compressed tensor stream truncated")
    if len(out) != expected_len:
      raise ProtocolError(
        f"Compressed tensor length mismatch: got {len(out)}, expected {expected_len}")
    return out
  except zlib.error as e:
    raise ProtocolError(f"Compressed tensor inflate failed: {e}") from e


def pack_tensor_dict(tensors: dict[str, np.ndarray],
                     compress: bool = False,
                     compress_min_bytes: int = COMPRESS_MIN_BYTES,
                     compress_level: int = COMPRESS_LEVEL) -> bytes:
  """Pack dictionary of ndarrays into pure binary ndarray records with pre-checks.

  compress=False（默认）时线格式与历史版本逐字节一致，保证老端互通。
  compress=True 时每个 tensor 记录多出 encoding(1) + raw_len(8) + stored_len(8)，
  可对图像类大张量单独 zlib 压缩；必须与接收端 compressed=True 成对使用。
  """
  if len(tensors) > MAX_TENSORS:
    raise ProtocolError(f"Tensor count {len(tensors)} exceeds limit {MAX_TENSORS}")

  buf = bytearray()
  buf.extend(struct.pack('!H', len(tensors)))

  total_packed_bytes = 2

  for name, arr in tensors.items():
    if not isinstance(arr, np.ndarray):
      arr = np.asarray(arr)

    if arr.dtype.hasobject or arr.dtype.type not in (np.uint8, np.float16, np.float32):
      raise ProtocolError(f"Unsupported dtype '{arr.dtype}' for tensor '{name}'. Only uint8, float16, float32 allowed.")

    if arr.ndim > MAX_NDIM or arr.ndim < 0:
      raise ProtocolError(f"Tensor '{name}' dimension {arr.ndim} invalid or exceeds MAX_NDIM {MAX_NDIM}")

    dtype_code = NUMPY_TO_DTYPE_CODE[np.dtype(arr.dtype.type)]

    if arr.ndim > 0:
      arr_c = np.ascontiguousarray(arr)
    else:
      arr_c = arr

    prod_shape = 1
    for dim in arr_c.shape:
      if dim > MAX_DIM_SIZE:
        raise ProtocolError(f"Tensor '{name}' dimension size {dim} exceeds limit {MAX_DIM_SIZE}")
      prod_shape *= dim
      if prod_shape > MAX_TOTAL_ELEMENTS:
        raise ProtocolError(f"Tensor '{name}' total element count exceeds limit {MAX_TOTAL_ELEMENTS}")

    name_bytes = name.encode('utf-8')
    name_len = len(name_bytes)
    if name_len > 65535:
      raise ProtocolError(f"Tensor name '{name}' too long")

    raw_bytes = arr_c.tobytes()
    byte_len = len(raw_bytes)

    if compress:
      encoding, stored = _maybe_compress_tensor(raw_bytes, compress_min_bytes, compress_level)
    else:
      encoding, stored = _ENCODING_RAW, raw_bytes
    stored_len = len(stored)

    if compress:
      record_len = 2 + name_len + 2 + (4 * arr_c.ndim) + 1 + 8 + 8 + stored_len
    else:
      record_len = 2 + name_len + 2 + (4 * arr_c.ndim) + 8 + byte_len
    total_packed_bytes += record_len
    if total_packed_bytes > MAX_RESPONSE_PAYLOAD_SIZE:
      raise ProtocolError(f"Total payload size exceeds MAX_RESPONSE_PAYLOAD_SIZE ({MAX_RESPONSE_PAYLOAD_SIZE})")

    buf.extend(struct.pack('!H', name_len))
    buf.extend(name_bytes)
    buf.extend(struct.pack('!B B', dtype_code, arr_c.ndim))
    if arr_c.ndim > 0:
      buf.extend(struct.pack(f'!{arr_c.ndim}I', *arr_c.shape))
    if compress:
      buf.extend(struct.pack('!B Q Q', encoding, byte_len, stored_len))
      buf.extend(stored)
    else:
      buf.extend(struct.pack('!Q', byte_len))
      buf.extend(raw_bytes)

  return bytes(buf)


def unpack_tensor_dict(payload: bytes, allowlist: set[str] | None = None,
                       max_tensor_size: int = MAX_REQUEST_PAYLOAD_SIZE,
                       compressed: bool = False) -> dict[str, np.ndarray]:
  """Unpack binary ndarray records with duplicate name rejection, max_tensor_size limit, and exact stream reading.

  compressed=True 时按 FLAG_ZLIB_TENSOR 记录布局解析（每个 tensor 带 encoding/raw_len/
  stored_len）；解压前后长度都必须精确匹配，否则 fail-closed 抛 ProtocolError。
  """
  stream = io.BytesIO(payload)
  if len(payload) < 2:
    raise ProtocolError("Payload too short for tensor count")

  num_tensors, = struct.unpack('!H', stream.read(2))
  if num_tensors > MAX_TENSORS:
    raise ProtocolError(f"Tensor count {num_tensors} exceeds limit {MAX_TENSORS}")

  seen_names = set()
  result = {}

  for _ in range(num_tensors):
    name_len_buf = stream.read(2)
    if len(name_len_buf) < 2:
      raise ProtocolError("Truncated tensor record header")
    name_len, = struct.unpack('!H', name_len_buf)

    name_bytes = stream.read(name_len)
    if len(name_bytes) < name_len:
      raise ProtocolError("Truncated tensor name")

    try:
      name = name_bytes.decode('utf-8')
    except UnicodeDecodeError as e:
      raise ProtocolError(f"Invalid UTF-8 tensor name: {e}")

    if name in seen_names:
      raise ProtocolError(f"Duplicate tensor name '{name}' in request")
    seen_names.add(name)

    if allowlist is not None and name not in allowlist:
      raise ProtocolError(f"Tensor name '{name}' not in allowlist")

    dtype_ndim_buf = stream.read(2)
    if len(dtype_ndim_buf) < 2:
      raise ProtocolError("Truncated tensor dtype/ndim header")
    dtype_code, ndim = struct.unpack('!B B', dtype_ndim_buf)

    if dtype_code not in DTYPE_CODE_TO_NUMPY:
      raise ProtocolError(f"Unknown dtype code: {dtype_code}")
    if ndim > MAX_NDIM:
      raise ProtocolError(f"Tensor ndim {ndim} exceeds MAX_NDIM {MAX_NDIM}")

    shape = ()
    if ndim > 0:
      shape_buf = stream.read(4 * ndim)
      if len(shape_buf) < 4 * ndim:
        raise ProtocolError("Truncated tensor shape")
      shape = struct.unpack(f'!{ndim}I', shape_buf)

    encoding = _ENCODING_RAW
    if compressed:
      enc_buf = stream.read(1)
      if len(enc_buf) < 1:
        raise ProtocolError("Truncated tensor encoding")
      encoding, = struct.unpack('!B', enc_buf)
      if encoding not in (_ENCODING_RAW, _ENCODING_ZLIB):
        raise ProtocolError(f"Unknown tensor encoding: {encoding}")

    byte_len_buf = stream.read(8)
    if len(byte_len_buf) < 8:
      raise ProtocolError("Truncated tensor byte length")
    byte_len, = struct.unpack('!Q', byte_len_buf)

    if compressed:
      stored_len_buf = stream.read(8)
      if len(stored_len_buf) < 8:
        raise ProtocolError("Truncated tensor stored length")
      stored_len, = struct.unpack('!Q', stored_len_buf)
    else:
      stored_len = byte_len

    np_dtype = DTYPE_CODE_TO_NUMPY[dtype_code]
    prod_shape = 1
    for dim in shape:
      if dim > MAX_DIM_SIZE:
        raise ProtocolError(f"Tensor dimension size {dim} exceeds limit {MAX_DIM_SIZE}")
      prod_shape *= dim
      if prod_shape > MAX_TOTAL_ELEMENTS:
        raise ProtocolError(f"Total element count {prod_shape} exceeds limit {MAX_TOTAL_ELEMENTS}")

    expected_byte_len = prod_shape * np_dtype.itemsize
    if byte_len != expected_byte_len:
      raise ProtocolError(f"Tensor '{name}' byte length mismatch: got {byte_len}, expected {expected_byte_len}")

    if byte_len > max_tensor_size:
      raise ProtocolError(f"Tensor '{name}' byte length {byte_len} exceeds max size limit {max_tensor_size}")
    if stored_len > max_tensor_size:
      raise ProtocolError(f"Tensor '{name}' stored length {stored_len} exceeds max size limit {max_tensor_size}")

    if compressed:
      stored = stream.read(stored_len)
      if len(stored) < stored_len:
        raise ProtocolError(f"Truncated raw bytes for tensor '{name}'")
      if encoding == _ENCODING_ZLIB:
        raw_bytes = _decompress_exact(stored, byte_len)
      else:
        if stored_len != byte_len:
          raise ProtocolError(
            f"Tensor '{name}' raw stored length mismatch: got {stored_len}, expected {byte_len}")
        raw_bytes = stored
    else:
      raw_bytes = stream.read(byte_len)
      if len(raw_bytes) < byte_len:
        raise ProtocolError(f"Truncated raw bytes for tensor '{name}'")

    arr = np.frombuffer(raw_bytes, dtype=np_dtype).reshape(shape)
    if arr.ndim > 0 and not arr.flags['C_CONTIGUOUS']:
      arr = np.ascontiguousarray(arr)

    result[name] = arr

  # Verify payload stream is fully consumed
  if stream.tell() != len(payload):
    raise ProtocolError(f"Payload contains unconsumed trailing bytes: read {stream.tell()}, total {len(payload)}")

  return result
