"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

本地回环假 OPRP 服务端：线程 + socket，模拟 HELLO / INFERENCE 响应。

支持的行为（通过 behavior 回调按请求编程）：
- ok            正常回包（raw_outputs_0=frame_id，raw_outputs_1=flags）
- drop          不回包（测 deadline 超时）
- delay         延迟 N 秒再回包
- error         回 MSG_ERROR_RESP
- wrong_req_id  回包带错误 req_id（测配对失败）
- bad_payload_crc 故意写坏 payload CRC（测帧校验）
- bad_msg_type  回包 msg_type 非法
- mismatch_frame 回包 frame_id 不匹配

reverse_batch=N：积累 N 个 INFERENCE 请求后**逆序**回包，用于证明客户端确实
按 req_id 配对，而不是退化成严格串行的一问一答。
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import numpy as np

from openpilot.sunnypilot.accelerators.remote import protocol as P


class InferenceRecord:
  def __init__(self, req_id: int, frame_id: int, ts_eof: int, flags: int, tensor_names: list[str],
               tensor_arrays: dict | None = None):
    self.req_id = req_id
    self.frame_id = frame_id
    self.ts_eof = ts_eof
    self.flags = flags
    self.tensor_names = tensor_names
    self.tensor_arrays = tensor_arrays or {}

  @property
  def reset(self) -> bool:
    return bool(self.flags & P.FLAG_RESET_TEMPORAL)


def _ok_record(frame_id: int, ts_eof: int, flags: int) -> dict[str, np.ndarray]:
  return {
    'raw_outputs_0': np.full((4,), float(frame_id), dtype=np.float32),
    'raw_outputs_1': np.array(float(flags), dtype=np.float32),
  }


class FakeRemoteServer(threading.Thread):
  def __init__(self, signature: str = '0' * 64, hello_status: int = 0,
               hello_caps: int = 0, hello_message: str = 'OK',
               reverse_batch: int = 1, behavior=None, hello_msg_type: int | None = None,
               close_after_hello: bool = False):
    super().__init__(daemon=True, name='fake-oprp-server')
    self.signature = signature
    self.hello_status = hello_status
    self.hello_caps = hello_caps
    self.hello_message = hello_message
    self.hello_msg_type = hello_msg_type
    self.close_after_hello = close_after_hello
    self.reverse_batch = max(1, int(reverse_batch))
    self._behavior = behavior or (lambda rec: ('ok',))

    self.received: list[InferenceRecord] = []
    self.hellos: list[tuple[str, int]] = []
    self.sent_order: list[int] = []
    self.error: Exception | None = None
    self._stop_evt = threading.Event()
    self._conn: socket.socket | None = None
    self._pending: list[tuple[InferenceRecord, tuple]] = []

    self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self._listener.bind(('127.0.0.1', 0))
    self._listener.listen(1)
    self._listener.settimeout(0.2)
    self.host, self.port = self._listener.getsockname()

  @property
  def address(self) -> str:
    return f'{self.host}:{self.port}'

  # -- 线程主体 ---------------------------------------------------------------

  def run(self) -> None:
    try:
      # 顺序服务多个客户端连接（与真实 remote_server 的 serve_client 循环一致）：
      # 断链后客户端会在同一端口重建+重新 HELLO，服务端必须能接住第二次连接。
      while not self._stop_evt.is_set():
        try:
          conn, _ = self._listener.accept()
        except socket.timeout:
          continue
        except OSError:
          break
        self._conn = conn
        conn.settimeout(0.2)
        while not self._stop_evt.is_set():
          try:
            header, payload = P.recv_message(conn, max_payload_size=P.MAX_RESPONSE_PAYLOAD_SIZE)
          except socket.timeout:
            continue
          except (EOFError, OSError, P.ProtocolError):
            break
          self._handle(header, payload)
        self._close_conn()
    except Exception as e:  # 记录到测试里断言，避免线程静默死掉
      self.error = e
    finally:
      self._close_conn()

  def _handle(self, header: P.Header, payload: bytes) -> None:
    if header.msg_type == P.MSG_HELLO_REQ:
      sig, caps = P.unpack_hello_req(payload)
      self.hellos.append((sig, caps))
      if self.hello_msg_type is not None:
        resp = P.Header(msg_type=self.hello_msg_type, request_id=header.request_id)
        P.send_message(self._conn, resp, P.pack_hello_resp(0, 0, 'OK'))
      else:
        resp = P.Header(msg_type=P.MSG_HELLO_RESP, request_id=header.request_id)
        P.send_message(self._conn, resp, P.pack_hello_resp(self.hello_status, self.hello_caps,
                                                           self.hello_message))
      if self.close_after_hello:
        self._close_conn()
        self._stop_evt.set()
      return

    if header.msg_type != P.MSG_INFERENCE_REQ:
      return

    tensors = P.unpack_tensor_dict(payload, max_tensor_size=P.MAX_RESPONSE_PAYLOAD_SIZE,
                                   compressed=bool(header.flags & P.FLAG_ZLIB_TENSOR))
    record = InferenceRecord(header.request_id, header.frame_id, header.timestamp_eof,
                             header.flags, sorted(tensors), tensors)
    self.received.append(record)
    self._pending.append((record, self._behavior(record)))
    if len(self._pending) >= self.reverse_batch:
      batch, self._pending = self._pending, []
      for rec, action in reversed(batch):
        self._respond(rec, action)
        if self._stop_evt.is_set():
          return

  def _respond(self, rec: InferenceRecord, action: tuple) -> None:
    kind = action[0]
    if kind == 'drop':
      return
    if kind == 'delay':
      if self._stop_evt.wait(float(action[1])):
        return
    if kind == 'error':
      hdr = P.Header(msg_type=P.MSG_ERROR_RESP, request_id=rec.req_id,
                     frame_id=rec.frame_id, timestamp_eof=rec.ts_eof)
      P.send_message(self._conn, hdr, P.pack_error_resp(int(action[1]), str(action[2])))
      self.sent_order.append(rec.frame_id)
      return

    req_id = rec.req_id + int(action[1]) if kind == 'wrong_req_id' else rec.req_id
    frame_id = rec.frame_id + 1000 if kind == 'mismatch_frame' else rec.frame_id
    msg_type = 0x00FF if kind == 'bad_msg_type' else P.MSG_INFERENCE_RESP
    hdr = P.Header(msg_type=msg_type, request_id=req_id, frame_id=frame_id, timestamp_eof=rec.ts_eof)
    payload = P.pack_tensor_dict(_ok_record(rec.frame_id, rec.ts_eof, rec.flags))
    if kind == 'bad_payload_crc':
      header_bytes = hdr.pack(len(payload))
      self._conn.sendall(header_bytes + payload + struct.pack('!I', 0xDEADBEEF))
    else:
      P.send_message(self._conn, hdr, payload)
    self.sent_order.append(rec.frame_id)

  def _close_conn(self) -> None:
    conn, self._conn = self._conn, None
    if conn is not None:
      try:
        conn.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass
      try:
        conn.close()
      except OSError:
        pass

  def close(self) -> None:
    self._stop_evt.set()
    self._close_conn()
    try:
      self._listener.close()
    except OSError:
      pass
    if self.is_alive():
      self.join(timeout=1.0)

  def wait_for_frames(self, count: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if len(self.received) >= count:
        return True
      time.sleep(0.005)
    return len(self.received) >= count
