"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

OPRP 客户端：支持「1 帧流水线」的同步阻塞式客户端。

与项目已验证的 pc_remote_model.remote_client 的关键区别是**流水线**：
- submit() 只负责把请求写上线并返回一个 handle（含绝对 deadline）；
- collect(handle) 才去等这一帧的响应；
- 因此调用方可以先 submit(N+1)、再 collect(N)，按 header 的 req_id 配对，
  不会退化成严格串行的一问一答（这是达成 20Hz 的关键）。

一个后台 reader 线程独占所有收包，按 req_id 分发到对应 handle，所以服务端
乱序回包也能正确配对；单帧 deadline 自「提交时刻」起算（绝对 deadline）。

线程模型：
- 调用线程：connect()/hello()/submit()/collect()/close()
- reader 线程：hello() 成功后启动，只做 recv + 按 req_id 分发
- 发送用 _send_lock 串行化，保证一条 TCP 流上不会交错半个请求

降级约定：连接/协议层错误抛 RemoteLinkError（链路已死，需重连）；
单帧超时抛 RemoteTimeout（链路保留，迟到响应按 req_id 丢弃）；
服务端 MSG_ERROR_RESP 抛 RemoteServerError（该帧失败，链路保留）。
"""
from __future__ import annotations

import os
import socket
import select
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .protocol import (
  CAP_ZLIB_TENSOR,
  CHUNK_DATA_DEFAULT,
  FLAG_RESET_TEMPORAL,
  FLAG_ZLIB_TENSOR,
  LINK_FATAL_ERROR_CODES,
  MAX_RESPONSE_PAYLOAD_SIZE,
  MSG_ERROR_RESP,
  MSG_HELLO_REQ,
  MSG_HELLO_RESP,
  MSG_INFERENCE_REQ,
  MSG_INFERENCE_RESP,
  Header,
  ProtocolError,
  pack_hello_req,
  pack_tensor_dict,
  recv_message,
  recv_message_udp,
  send_message,
  send_message_udp,
  unpack_error_resp,
  unpack_hello_resp,
  unpack_tensor_dict,
)
from .debug_log import log_event
from .status import RemoteStatus

# 服务端持有 history / prev_feat / 内部队列；这些张量禁止上行，客户端侧再复核一次
FORBIDDEN_INPUTS = frozenset({
  'prev_feat',
  'feat_q',
  'img_q',
  'big_img_q',
  'desire_q',
  'tfm',
  'big_tfm',
  'packed_npy_inputs',
})

DEFAULT_ADDRESS = '192.168.43.2:5202'
DEFAULT_DEADLINE_MS = 60.0
# 传输模式：'tcp'（默认，零回归）、'udp'（可选的大帧低延迟分片传输）或
# 'bulk'（可选的 vendor bulk / FunctionFS 直连，见 bulk_transport.py）。
TRANSPORT_TCP = 'tcp'
TRANSPORT_UDP = 'udp'
TRANSPORT_BULK = 'bulk'
VALID_TRANSPORTS = (TRANSPORT_TCP, TRANSPORT_UDP, TRANSPORT_BULK)
# UDP 模式下的 socket 缓冲：单帧请求可达 ~2MiB，给足内核缓冲避免 sendto 排队/丢包。
UDP_SNDBUF = 8 * 1024 * 1024
UDP_RCVBUF = 8 * 1024 * 1024
# 已超时/已收下的 req_id，其迟到响应直接丢弃，避免串帧
_RETIRED_CAP = 4096

# -- 后台重连监督器（link supervisor）----------------------------------------
# 断链后**在同一个 modeld 进程内**自动重建：1s 起、每失败翻倍、上限 30s。
# 与 JoinedModel 的「换入退避」（5s→60s，语义是换不换大模型）是两件不同的事：
# 这里只管「链路能不能通」，不管「要不要用远端供数」。
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 30.0
# 两次重连尝试之间的最小间隔：即使被反复立即唤醒（如服务端连续 code 4），
# 也不允许把重连打成热循环。
RECONNECT_MIN_INTERVAL_S = 0.5
# 监督器空闲轮询间隔（秒）
RECONNECT_POLL_S = 0.25
# close() 等待重连临界区退出的上限（秒）；超时后仍然硬拆，不无限期阻塞调用方。
RECONNECT_LOCK_TIMEOUT_S = 5.0
# -- 无进展看门狗（bulk/ffs 专用）--------------------------------------------
# bulk 伪 socket 在 PC 侧服务消失时**不会**报错：ffs 端点在内核里仍然是「打开」
# 的，device 侧 write 照样成功、read 阻塞不返回。于是 client 会一直以为链路活着
# （connected=True），既不重连、也把每帧超时当成单帧抖动吞掉 —— 这正是「停 PC
# 服务后 C3 永远不恢复」的真正机制（TCP 侧则表现为 FIN/RST）。
# 因此这里用「已连续提交但长时间收不到任何成功帧」来判定链路已死：
#   * 只在**正在提交帧**时评估 —— 不发帧（没换入 / 本地兜底）时永远不会误判；
#   * 新链路有冷启动宽限（COLL_GRACE_S，真机首帧可达 ~1.1s），宽限内不判定。
STALL_TIMEOUT_S = 1.0
STALL_COLD_GRACE_S = 3.0
# bulk 连续重连失败多少次后，把「端点被卡死、需人工复位」写进 last_error
BULK_WEDGE_AFTER_ATTEMPTS = 3
# bulk 重连尝试卡住多久后判定为「端点被卡死」（会写进 last_error 的可执行提示）。
# 取 25s：健康重连实测 ≤8s 完成（含 TRT 引擎复用），而真卡死会持续数分钟；
# 阈值太小会把「恢复得慢一点」误报成「端点报废」，比不报更糟。
BULK_WEDGE_AFTER_S = 25.0

# modeld 主线程是 SCHED_FIFO 实时线程，并且被 `config_realtime_process(7, 54)`
# 绑在 cpu7；子线程会继承同一亲和性掩码，于是 reader/worker 与实时主线程抢同一个
# 核，真机实测把每帧 RTT 从 ~50ms 抬到 ~59ms（60ms deadline 下几乎必然超时）。
# 这里把网络线程显式绑到非实时核，让 I/O 与实时推理真正并行。
NETWORK_CORES = (0, 1, 2, 3)


def pin_network_thread(cores=NETWORK_CORES) -> None:
  """把当前线程绑到非实时核；不支持/核不存在时静默跳过，绝不因此拖崩链路。"""
  try:
    os.sched_setaffinity(0, {int(c) for c in cores})
  except (AttributeError, OSError, ValueError):
    pass


class RemoteError(Exception):
  """远端链路相关错误基类。"""


class RemoteLinkError(RemoteError):
  """连接/握手/协议/套接字错误；抛出时链路已被关闭，需要重连。

  `link_level=True` 标记「这是链路级故障（不是单帧抖动）」：上层据此把重连
  节奏交给链路层自己的退避监督器，而不是再叠一层语义重复的退避。
  """

  link_level = True


class RemoteHandshakeError(RemoteLinkError):
  """HELLO 握手失败。"""


class RemoteTimeout(RemoteError):
  """单帧在 deadline 内未收到响应；链路保留，迟到响应会被丢弃。"""

  link_level = False


class RemoteServerError(RemoteError):
  """服务端返回 MSG_ERROR_RESP；只该帧失败，链路保留（非 code 4 时）。"""

  link_level = False


def parse_remote_address(addr: str, default_port: int = 5202) -> tuple[str, int]:
  """解析 host:port，兼容 IPv4、主机名与 [IPv6]:port。"""
  if not addr:
    return '192.168.43.2', default_port
  if addr.startswith('['):
    end = addr.find(']')
    if end == -1:
      raise ValueError(f'Invalid IPv6 address format: {addr}')
    host = addr[1:end]
    rest = addr[end + 1:]
    port = int(rest[1:]) if rest.startswith(':') else default_port
    return host, port
  if ':' in addr:
    host, port_str = addr.rsplit(':', 1)
    return host, int(port_str)
  return addr, default_port


@dataclass
class PendingRequest:
  """一次 INFERENCE 请求的 handle；由 submit() 返回，交给 collect() 消费。"""

  req_id: int
  frame_id: int
  timestamp_eof: int
  flags: int
  deadline: float                     # 绝对 time.monotonic() deadline
  submitted_at: float
  _event: threading.Event = field(default_factory=threading.Event, repr=False)
  outputs: list[np.ndarray] | None = None
  error: RemoteError | None = None
  collected: bool = False
  # 握手型请求（bulk 会话级恢复时走 reader 线程的那次 HELLO）：不校验
  # frame_id/timestamp_eof，只把 HELLO_RESP 的 (status, caps, msg) 交给调用方。
  expect_hello: bool = False
  hello_result: tuple[int, int, str] | None = None

  @property
  def remaining(self) -> float:
    return self.deadline - time.monotonic()

  @property
  def done(self) -> bool:
    return self._event.is_set()


class RemotePipelineClient:
  """常驻 TCP 连接 + 1 帧流水的 OPRP 客户端。"""

  def __init__(self, signature: str, address: str = DEFAULT_ADDRESS,
               deadline_ms: float = DEFAULT_DEADLINE_MS, pipeline_depth: int = 1,
               connect_timeout: float = 2.0, status: RemoteStatus | None = None,
               forbid_inputs: frozenset[str] = FORBIDDEN_INPUTS,
               capabilities: int = CAP_ZLIB_TENSOR,
               transport: str = TRANSPORT_TCP,
               udp_chunk_data: int = CHUNK_DATA_DEFAULT):
    self.signature = signature
    self.address = address
    self.host, self.port = parse_remote_address(address)
    # 传输模式：非法/未知取值一律回落 TCP，保证默认行为不变（零回归）。
    mode = str(transport).strip().lower() if transport is not None else TRANSPORT_TCP
    self.transport = mode if mode in VALID_TRANSPORTS else TRANSPORT_TCP
    self.udp_chunk_data = int(udp_chunk_data)
    self._peer: tuple | None = None
    self.deadline_s = max(0.001, float(deadline_ms) / 1000.0)
    # pipeline_depth 表示「可提前多少帧排队」；同时最多挂 pipeline_depth + 1 个
    # 未收取的请求（当前要读的那一帧 + 提前排队的那些）。depth=1 即经典 1 帧流水线。
    self.pipeline_depth = max(1, int(pipeline_depth))
    self.max_in_flight = max(2, self.pipeline_depth + 1)
    self.connect_timeout = connect_timeout
    self.status = status if status is not None else RemoteStatus()
    self.forbid_inputs = forbid_inputs
    # 本端在 HELLO 里声明的能力位；只有对端 HELLO_RESP 也回同一位时才启用压缩。
    self.capabilities = int(capabilities)
    self._compression_enabled = False

    self._sock: socket.socket | None = None
    self._reader: threading.Thread | None = None
    self._stop = threading.Event()
    self._send_lock = threading.Lock()
    self._pending_lock = threading.Lock()
    self._pending: dict[int, PendingRequest] = {}
    self._retired: list[int] = []
    self._retired_set: set[int] = set()
    self._request_counter = 0
    self._hello_done = False
    self._armed_reset = False
    # 连接代际：每次建 socket / 拆 socket 都 +1。reader 线程捕获自己那一代，
    # 醒来后若发现代际已变（说明自己管的是一条已被拆掉的旧连接），只退出、**不**报
    # 链路错误 —— 否则「重连刚成功就被上一个连接的收尾错误打死」会形成死循环。
    self._link_gen = 0

    # -- 后台重连监督器状态 ---------------------------------------------------
    # `_link_lock` 串行化 connect / hello / close 三条会动 `_sock` 的路径；
    # 监督器线程与实时帧线程、close()（下电）都可能并发进入。
    self._link_lock = threading.RLock()
    self._supervisor_lock = threading.Lock()
    self._supervisor_thread: threading.Thread | None = None
    self._supervisor_armed = False
    self._supervisor_stop = threading.Event()
    self._supervisor_wake = threading.Event()
    self._reconnect_backoff_s = 0.0
    self._reconnect_attempts = 0
    self._reconnect_last_attempt = 0.0
    # 本次重连尝试的起始时刻：bulk 的重握手可能**卡在 ffs 写里不返回**（端点被已死
    # 会话卡死），此时不会有「失败」事件，只能源上看出来 —— 用来给出可执行提示。
    self._reconnect_started_at = 0.0
    self._wedge_reported = False
    # 无进展看门狗：`_stall_since` 是「本轮连续提交但还没有任何成功帧」的起点
    self._stall_since: float | None = None
    self._stall_waited_s = 0.0
    # 冷启动宽限截止时刻（monotonic）：新链路的前几帧可能很慢，宽限内不判无进展
    self._cold_until = 0.0
    # 每次成功握手 +1；上层用它发现「链路刚刚恢复过」，从而让下一帧带上
    # FLAG_RESET_TEMPORAL 并放宽冷启动 deadline（不依赖墙钟）。
    self._recovery_epoch = 0
    self._connected_since: float | None = None

  # -- 只读属性 ---------------------------------------------------------------

  @property
  def connected(self) -> bool:
    return (self._sock is not None and self._hello_done
            and self._reader is not None and self._reader.is_alive() and not self._stop.is_set())

  @property
  def reconnect_armed(self) -> bool:
    """后台重连监督器是否已武装（= 配置开关为开）。"""
    return self._supervisor_armed

  @property
  def reconnect_backoff_s(self) -> float:
    """当前重连退避（秒）。>0 表示正在退避等待中；0 表示空闲或正在尝试。"""
    return float(self._reconnect_backoff_s)

  @property
  def reconnect_attempts(self) -> int:
    return int(self._reconnect_attempts)

  @property
  def recovery_epoch(self) -> int:
    """成功握手次数；链路每恢复一次 +1，供上层判定「刚刚恢复」。"""
    return int(self._recovery_epoch)

  @property
  def connected_since(self) -> float | None:
    """本次连接建立时刻（time.monotonic）；未连接为 None。"""
    return self._connected_since

  @property
  def in_flight(self) -> int:
    with self._pending_lock:
      return len(self._pending)

  @property
  def compression_enabled(self) -> bool:
    """HELLO 协商成功后为 True：上行请求按可压缩布局发送（未协商则保持老格式）。"""
    return self._compression_enabled

  @property
  def last_state(self) -> dict:
    """给状态发布方（后续 lane）读的遥测快照。"""
    return self.status.to_dict()

  # -- 连接与握手 -------------------------------------------------------------

  def connect(self) -> None:
    """建立连接（不含握手）；失败抛 RemoteLinkError。

    默认 TCP；transport='udp' 时改用 SOCK_DGRAM + connect() 固定对端，并放大
    SO_SNDBUF/SO_RCVBUF 以承载大帧分片。UDP 无连接握手，真正的可用性由 HELLO 判定。
    transport='bulk' 时打开 vendor bulk（FunctionFS）伪 socket（bulk_transport）。

    **只有真正可用的链路才允许短路**（socket 在 + 已握手 + reader 活着 + 未 stop）。
    「socket 对象还在」不等于「链路还活着」：bulk 伪 socket 与内核 TCP 缓冲都可能在
    对端进程消失后仍然存在、写入照样成功，但服务端已经不再认这条连接。此时若按
    `_sock is not None` 短路，重连就是空操作 —— 表现为「同一 modeld 进程内永远
    不恢复，必须重启 openpilot」。因此这里对残留句柄一律**先彻底拆掉再重建**，
    保证重建后一定走一遍新的 HELLO。
    """
    with self._link_lock:
      if self.connected:
        return
      if self._sock is not None:
        # 残留但不可信的句柄：连带它的 reader 一起拆干净，避免旧 reader
        # 在新链路上收到旧连接的收尾错误而误杀新连接。
        self._hard_close()
        log_event('connect_rebuild', host=self.host, port=self.port, transport=self.transport,
                  reason='stale socket handle discarded')
      self._connect_locked()

  def _connect_locked(self) -> None:
    """真正建 socket（调用方已持有 `_link_lock` 且确认当前无可用链路）。"""
    if self.transport == TRANSPORT_BULK:
      self._connect_bulk()
      return
    family = socket.AF_INET6 if ':' in self.host else socket.AF_INET
    udp = self.transport == TRANSPORT_UDP
    sock = socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    sock.settimeout(self.connect_timeout)
    try:
      if udp:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, UDP_SNDBUF)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RCVBUF)
        sock.connect((self.host, self.port))
      else:
        sock.connect((self.host, self.port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
      # UDP 记录对端地址，供 sendto / expect_addr 过滤使用（getpeername 返回已解析地址）。
      self._peer = sock.getpeername() if udp else None
    except OSError as e:
      try:
        sock.close()
      except OSError:
        pass
      self.status.record_failure(f'connect {self.host}:{self.port}: {e}')
      log_event('connect_fail', host=self.host, port=self.port, transport=self.transport, reason=str(e))
      raise RemoteLinkError(f'Failed to connect to {self.host}:{self.port}: {e}') from e
    self._sock = sock
    self._stop.clear()
    self._hello_done = False
    self._compression_enabled = False
    self._connected_since = None
    self._link_gen += 1
    log_event('connect_ok', host=self.host, port=self.port, transport=self.transport)

  def _connect_bulk(self) -> None:
    """vendor bulk（FunctionFS）连接：打开伪 socket，不涉及 TCP/UDP 地址。

    延迟导入 bulk_transport：仅 bulk 路径才需要 os/ffs，默认 tcp 导入链零变化。
    打开失败（无 gadget/未配置/端点忙）抛 RemoteLinkError，与 TCP connect 一致。
    """
    from .bulk_transport import open_bulk_endpoint  # noqa: PLC0415
    try:
      sock = open_bulk_endpoint(timeout=self.connect_timeout)
    except (OSError, ValueError, RuntimeError) as e:
      self.status.record_failure(f'bulk open failed: {e}')
      log_event('connect_fail', host=self.host, port=self.port, transport=self.transport, reason=str(e))
      raise RemoteLinkError(f'Failed to open bulk endpoint: {e}') from e
    self._sock = sock
    self._stop.clear()
    self._hello_done = False
    self._compression_enabled = False
    self._link_gen += 1
    log_event('connect_ok', host=self.host, port=self.port, transport=self.transport)

  def hello(self, signature: str | None = None) -> None:
    """同步完成 HELLO 握手，成功后才启动 reader 线程。"""
    with self._link_lock:
      if self._sock is None:
        raise RemoteLinkError('connect() must be called before hello()')
      self._hello_locked(signature)

  def _hello_fail(self, keep_socket: bool) -> None:
    """握手失败后的收尾：默认彻底拆链；bulk 会话级恢复时只作废会话、保留端点。"""
    if keep_socket:
      self._hello_done = False
      self._connected_since = None
      self._stall_since = None
      self._cold_until = 0.0
    else:
      self._hard_close()

  def _hello_locked(self, signature: str | None = None, start_reader: bool = True,
                    keep_socket: bool = False) -> None:
    """握手本体（调用方已持有 `_link_lock` 且 `_sock` 非空）。

    * `start_reader=False`：bulk 的会话级恢复 —— reader 线程读的是同一条常驻管道，
      跨会话继续有效（按 req_id 配对，旧 req_id 的响应自然被丢弃），不能再起一个，
      否则两个线程会抢同一条流、把响应分给已经作废的那一侧。因此这条分支的收包
      **必须走 reader 线程**（`_hello_via_reader`），绝不在本线程再读一次同一个 fd。
    * `keep_socket=True`：握手失败也**不拆端点**。bulk 端点上那个阻塞中的 read
      关不掉，拆了之后就再也 `open` 不回来（EBUSY）；这里只作废会话。
    """
    sig = signature if signature is not None else self.signature
    sock = self._sock
    udp = self.transport == TRANSPORT_UDP
    try:
      if not start_reader:
        caps = self._hello_via_reader(sock, sig)
        # 双方都声明支持才启用；老服务端 caps=0 → 不压缩，保持向后兼容。
        self._compression_enabled = (
          bool(caps & CAP_ZLIB_TENSOR) and bool(self.capabilities & CAP_ZLIB_TENSOR))
        return self._hello_succeeded(sock, False)
      req = Header(msg_type=MSG_HELLO_REQ, request_id=self._next_request_id())
      hello_payload = pack_hello_req(sig, capabilities=self.capabilities)
      sock.settimeout(self.connect_timeout)
      if udp:
        # UDP 握手同样走分片收发；丢包/损坏即超时 → 握手失败（由上层按退避重连）。
        send_message_udp(sock, self._peer, req, hello_payload, chunk_data=self.udp_chunk_data)
        resp_hdr, resp_payload = recv_message_udp(
          sock, deadline=time.monotonic() + self.connect_timeout,
          max_payload_size=MAX_RESPONSE_PAYLOAD_SIZE, expect_addr=self._peer,
          chunk_data=self.udp_chunk_data)
      else:
        send_message(sock, req, hello_payload)
        resp_hdr, resp_payload = recv_message(sock, max_payload_size=MAX_RESPONSE_PAYLOAD_SIZE)
      if resp_hdr.msg_type != MSG_HELLO_RESP:
        raise RemoteHandshakeError(f'Unexpected msg_type during HELLO: {resp_hdr.msg_type}')
      status, caps, msg = unpack_hello_resp(resp_payload)
      if status != 0:
        raise RemoteHandshakeError(f'HELLO rejected with status {status}: {msg}')
      # 双方都声明支持才启用；老服务端 caps=0 → 不压缩，保持向后兼容。
      self._compression_enabled = bool(caps & CAP_ZLIB_TENSOR) and bool(self.capabilities & CAP_ZLIB_TENSOR)
    except RemoteHandshakeError as e:
      self._hello_fail(keep_socket)
      self.status.record_failure('handshake failed')
      log_event('hello_fail', transport=self.transport, reason=str(e))
      raise
    except (ProtocolError, OSError, EOFError, RemoteTimeout) as e:
      self._hello_fail(keep_socket)
      self.status.record_failure(f'handshake error: {e}')
      log_event('hello_fail', transport=self.transport, reason=str(e))
      raise RemoteHandshakeError(f'HELLO failed: {e}') from e

    return self._hello_succeeded(sock, start_reader)

  def _hello_succeeded(self, sock, start_reader: bool) -> None:
    """握手成功后的收尾：置连接状态 + 记恢复代际 + （必要时）拉起 reader 线程。

    会话级恢复（`start_reader=False`）走的是**同一个** reader 线程，所以这里必须
    原样复用它 —— 两处逻辑一旦分叉，就会出现「链路标记已连上但没有人在收包」的
    假健康，下一帧立刻被判无进展。
    """
    self._hello_done = True
    self._recovery_epoch += 1
    self._connected_since = time.monotonic()
    # 新链路有冷启动宽限：真机首帧可达 ~1.1s，宽限内不判「无进展」。
    self._cold_until = time.monotonic() + STALL_COLD_GRACE_S
    self._stall_since = None
    self.status.set_connected(True)
    # 「链路刚刚恢复」打一个墙钟戳：UI 用它显示「已恢复·外置运行」。
    # 首次握手（recovery_epoch==1）不算「恢复」，否则每次开机都会闪一下「已恢复」。
    if self._recovery_epoch > 1:
      self.status.mark_recovered(time.time())  # noqa: TID251 （与状态文件同一墙钟域）
    self._sync_reconnect_status()
    log_event('hello_ok', transport=self.transport, compression=self._compression_enabled,
              caps=self.capabilities, recovery_epoch=self._recovery_epoch)
    # reader 线程独占收包：它必须拿到一个「阻塞、无超时」的 socket，否则发送侧
    # 为 deadline 设置的超时会串到 reader 的 recv 上，把正常等待误判成读超时。
    sock.settimeout(None)
    if not start_reader and self._reader is not None and self._reader.is_alive():
      return          # bulk 会话级恢复：复用常驻 reader（管道跨会话仍然有效）
    # 带上连接代际：旧连接的 reader 若此时才醒，不会误杀这条新连接。
    self._link_gen += 1
    generation = self._link_gen
    self._reader = threading.Thread(target=self._reader_main, args=(sock, generation),
                                    name='oprp-reader', daemon=True)
    self._reader.start()

  def _hello_via_reader(self, sock, signature: str) -> int:
    """会话级恢复用的 HELLO：**只写不读**，收包交给常驻 reader 线程按 req_id 配对。

    为什么不能像首次握手那样在本线程 `send_message` + `recv_message`：bulk 端点上
    常驻着一条 reader 线程，它正阻塞在同一个 fd 的 `ffs_epfile_io` 上等主机回包。
    本线程再去读同一个 fd 就是**两个消费者抢一条流**，谁抢到算谁的 —— 于是
    「HELLO 响应被 reader 当成未知 req_id 丢弃、握手方在不可中断的 ffs 读里永久
    等下去」。这正是「PC 服务重启后 C3 永远重连不上、只能重启 openpilot」的真因：
    表面上像内核 ffs 的限制，其实是这次握手的收包方式错了。

    改成把 HELLO 当成一个普通 pending 请求登记进 `_pending`，由 reader 按 req_id
    分发回本线程的事件上。等待用的是 `Event.wait`（纯用户态、可超时、可中断），
    所以对端已经死掉时是**干净的超时失败**并推进退避，而不是卡死在 ffs 读里。
    返回协商到的 caps。
    """
    req_id = self._next_request_id()
    handle = PendingRequest(
      req_id=req_id, frame_id=-1, timestamp_eof=0, flags=0,
      deadline=time.monotonic() + self.connect_timeout,
      submitted_at=time.monotonic(), expect_hello=True,
    )
    with self._pending_lock:
      self._pending[req_id] = handle
    try:
      with self._send_lock:
        send_message(sock, Header(msg_type=MSG_HELLO_REQ, request_id=req_id),
                     pack_hello_req(signature, capabilities=self.capabilities))
      if not handle._event.wait(timeout=self.connect_timeout):
        raise RemoteTimeout('no HELLO response from server')
      if handle.error is not None:
        raise handle.error
    finally:
      self._retire(handle)
    if handle.hello_result is None:
      raise RemoteHandshakeError('HELLO response carried no payload')
    status, caps, msg = handle.hello_result
    if status != 0:
      raise RemoteHandshakeError(f'HELLO rejected with status {status}: {msg}')
    return caps

  def connect_and_hello(self, signature: str | None = None) -> None:
    """建连接 + 握手。链路已健康时**幂等短路**（不重复握手、不多起 reader 线程）。

    链路不健康但 `_sock` 还在（握手未完成 / reader 已死 / 已被判死）时，**先彻底
    拆掉残留句柄再重建 + 重新 HELLO** —— 绝不因为「句柄还在」而空转。
    要强制重建（链路看起来还健康也要拆）请用 `reconnect()`。
    全程持 `_link_lock`，不与重连监督器交错。
    """
    with self._link_lock:
      if self.connected:
        return
      if self._bulk_session_resettable():
        # bulk 会话级恢复：端点不可重建，只能重跑 HELLO（见该方法说明）
        self._hello_done = False
        self._hello_locked(signature, start_reader=False, keep_socket=True)
        return
      if self._sock is not None:
        self._hard_close()
        log_event('connect_rebuild', host=self.host, port=self.port, transport=self.transport,
                  reason='stale socket handle discarded')
      self._connect_locked()
      self._hello_locked(signature)

  def reconnect(self, signature: str | None = None) -> None:
    """重建/重握手，把链路恢复到可用；失败时抛出。

    * **bulk** 且端点仍可会话级恢复（`_bulk_session_resettable`）：保留 ffs 端点，
      只重跑一次 HELLO。见该方法的说明 —— 端点上的阻塞 read 关不掉，重开必然 EBUSY。
    * 其余（tcp/udp，或 bulk 端点已不可用）：**先彻底拆掉旧链路**再 connect +
      重新 HELLO，绝不因为「socket 句柄还在」而空转。

    全程持 `_link_lock`，因此不会与帧线程的 `connect_and_hello()` 交错。
    """
    with self._link_lock:
      if self._bulk_session_resettable():
        self._hello_done = False
        self._hello_locked(signature, start_reader=False, keep_socket=True)
        return
      self._hard_close()
      self._connect_locked()
      self._hello_locked(signature)

  # -- 帧路径 -----------------------------------------------------------------

  def _check_stall(self) -> None:
    """无进展看门狗（bulk/ffs 必需）：连续提交却长时间无任何成功帧 → 判链路已死。

    * 首次进入「本轮提交」时记起点；每收到一个成功帧就把起点清空；
    * 冷启动宽限内不判定（新链路首帧在真机上可达 ~1.1s）；
    * 判定成立时抛 `RemoteLinkError` —— 调用方（worker）会照常把它冒泡给主线程，
      主线程同帧回退本地小模型，**不会出现控制空白**。

    注意：这只能覆盖「submit 能返回」的传输。bulk 的 `sendall` 在对端消失时会
    **永久阻塞**在 ffs 写队列上（内核既不返回也不报错），submit 根本回不来 ——
    那条路径由监督器线程里的 `_check_stall_supervised()` 兜底（见 STALL_* 注释）。
    """
    if self._stall_deadline_passed():
      self._kill_stalled_link()

  def _stall_deadline_passed(self) -> bool:
    now = time.monotonic()
    if now < self._cold_until:
      self._stall_since = now
      return False
    if self._stall_since is None:
      self._stall_since = now
      return False
    waited = now - self._stall_since
    if waited < STALL_TIMEOUT_S:
      return False
    self._stall_since = None
    self._stall_waited_s = waited
    return True

  def _bulk_session_resettable(self) -> bool:
    """bulk 端点是否可以只重置**会话**（而不是重建 socket）。

    FunctionFS 的 ep 不是一条「连接」，而是一条常驻管道：PC 侧服务重启时
    device 端的 fd 依然有效（USB 链路没断），作废的只是**会话**（HELLO 协商出来的
    状态）。反过来，ep_out 上那个阻塞中的 read 只有**主机写入数据**才会结束，
    `close()` 唤不醒它 —— 于是「关掉再 open」会永远拿到 `EBUSY`（实测 ep 一直被
    那个线程持有，重连 100% 失败，链路再也回不来）。所以 bulk 走「保留 ep +
    重新 HELLO」的会话级恢复。
    """
    if self.transport != TRANSPORT_BULK or self._sock is None:
      return False
    return self._reader is not None and self._reader.is_alive()

  def _kill_stalled_link(self) -> None:
    waited = float(self._stall_waited_s)
    # 判死：把在途请求标失败、置未连接，并唤醒监督器重建 —— 本帧由上层同帧回退
    # 本地小模型，不会出现控制空白。
    self._on_link_error(f'no successful frame for {waited:.1f}s (link stalled)')
    raise RemoteLinkError(f'link stalled: no successful frame for {waited:.1f}s')

  def _check_stall_supervised(self) -> None:
    """监督器线程侧的看门狗：负责「submit 卡在 ffs 写里回不来」的那条路径。

    `_stall_since` 在 submit **发包之前**就已置位，所以即便 submit 永久阻塞，
    这里依然能看到「已经 X 秒没有任何成功帧」，从而拆链 + 唤醒重建。
    """
    if self._stall_deadline_passed():
      self._kill_stalled_link()

  def _note_frame_ok(self) -> None:
    """收到一个成功帧：清空无进展计时。"""
    self._stall_since = None

  def submit(self, frame_id: int, ts_eof: int, tensors: dict[str, np.ndarray],
             flags: int = 0, deadline: float | None = None, reset: bool = False) -> PendingRequest:
    """把一帧请求写上线并返回 handle；本调用不等待响应。

    deadline 可传绝对 time.monotonic() 值（调用方共享一帧预算）；默认自本次
    提交时刻起算 deadline_s。reset=True 或已 request_reset() 时带 FLAG_RESET_TEMPORAL。
    """
    if not self.connected or self._sock is None:
      # 链路不可用：请求重连（退避中则自动忽略，见 request_reconnect）；
      # 本帧由上层同帧回退本地小模型。
      self.request_reconnect()
      raise RemoteLinkError('client is not connected/handshaken')
    if not tensors:
      raise ValueError('submit() requires at least one input tensor')
    for name in tensors:
      if name in self.forbid_inputs:
        raise ValueError(f"internal state tensor '{name}' must not be sent over the network")

    # 无进展看门狗：bulk 链路上「对端消失」不会产生任何 socket 错误，只能靠
    # 「连续提交却一个成功帧都没有」来判定链路已死。判定后立刻走正常链路错误
    # 路径（关 socket + 唤醒重连），本帧由上层同帧回退本地小模型。
    self._check_stall()

    if reset or self._armed_reset:
      flags |= FLAG_RESET_TEMPORAL
    self._armed_reset = False

    try:
      payload = pack_tensor_dict(tensors, compress=self._compression_enabled)
    except ProtocolError as e:
      raise RemoteLinkError(f'failed to pack request: {e}') from e

    req_id = self._next_request_id()
    handle = PendingRequest(
      req_id=req_id, frame_id=int(frame_id), timestamp_eof=int(ts_eof), flags=flags,
      deadline=float(deadline) if deadline is not None else time.monotonic() + self.deadline_s,
      submitted_at=time.monotonic(),
    )

    with self._pending_lock:
      if len(self._pending) >= self.max_in_flight:
        raise RemoteLinkError(f'more than {self.max_in_flight} requests in flight; collect() first')
      self._pending[req_id] = handle

    # 压缩位只上线路（header.flags），不污染 handle.flags 的语义（reset 等）。
    wire_flags = flags | (FLAG_ZLIB_TENSOR if self._compression_enabled else 0)
    req_hdr = Header(msg_type=MSG_INFERENCE_REQ, request_id=req_id, frame_id=handle.frame_id,
                     timestamp_eof=handle.timestamp_eof, flags=wire_flags)
    try:
      with self._send_lock:
        if self._sock is None:
          raise RemoteLinkError('link closed during submit')
        remaining = handle.deadline - time.monotonic()
        if remaining <= 0:
          raise RemoteTimeout('deadline exceeded before send')
        # 用 select 施加发送侧 deadline，而不是往共享 socket 上设超时：
        # socket 的超时是 fd 级的，改了会同时影响 reader 线程正在进行的 recv。
        # bulk 伪 socket 不是可 select 的 fd，且写入是同步的 ffs writev，直接写。
        if self.transport != TRANSPORT_BULK:
          _, writable, _ = select.select([], [self._sock], [], remaining)
          if not writable:
            raise RemoteTimeout('socket not writable within deadline')
        if self.transport == TRANSPORT_UDP:
          send_message_udp(self._sock, self._peer, req_hdr, payload,
                           chunk_data=self.udp_chunk_data)
        else:
          send_message(self._sock, req_hdr, payload)
    except RemoteTimeout as e:
      self._retire(handle)
      self.status.record_failure('deadline exceeded before send', timeout=True)
      log_event('submit_timeout', frame=handle.frame_id, req=req_id, transport=self.transport, reason=str(e))
      raise
    except (OSError, ProtocolError, EOFError) as e:
      self._retire(handle)
      self._on_link_error(f'send failed: {e}')
      log_event('submit_fail', frame=handle.frame_id, req=req_id, transport=self.transport, reason=str(e))
      raise RemoteLinkError(f'send failed: {e}') from e

    self.status.record_submit(handle.frame_id)
    log_event('submit', frame=handle.frame_id, req=req_id, bytes=len(payload), transport=self.transport,
              depth=self.pipeline_depth)
    return handle

  def collect(self, handle: PendingRequest, timeout: float | None = None) -> list[np.ndarray]:
    """等待并取回 handle 对应的 outputs；超时抛 RemoteTimeout。"""
    if handle.collected:
      raise RemoteError('handle already collected')
    wait_for = handle.deadline - time.monotonic()
    if timeout is not None:
      wait_for = min(wait_for, timeout)
    if not handle._event.wait(max(0.0, wait_for)):
      self._retire(handle)
      self.status.record_failure(f'frame {handle.frame_id} timed out', timeout=True)
      log_event('frame_timeout', frame=handle.frame_id, req=handle.req_id, transport=self.transport,
                waited_ms=wait_for * 1000.0)
      raise RemoteTimeout(f'frame {handle.frame_id} (req {handle.req_id}) exceeded deadline')
    handle.collected = True
    self._retire(handle, from_result=True)
    if handle.error is not None:
      log_event('frame_error', frame=handle.frame_id, req=handle.req_id, transport=self.transport,
                reason=str(handle.error))
      raise handle.error
    latency_ms = (time.monotonic() - handle.submitted_at) * 1000.0
    self.status.record_success(latency_ms, handle.frame_id)
    self._note_frame_ok()
    log_event('frame_ok', frame=handle.frame_id, req=handle.req_id, transport=self.transport,
              total_ms=latency_ms)
    assert handle.outputs is not None
    return handle.outputs

  def infer(self, frame_id: int, ts_eof: int, tensors: dict[str, np.ndarray],
            flags: int = 0, deadline: float | None = None, reset: bool = False) -> list[np.ndarray]:
    """单帧便捷路径：submit 后立即 collect（流水线请直接用 submit/collect）。"""
    return self.collect(self.submit(frame_id, ts_eof, tensors, flags=flags, deadline=deadline, reset=reset))

  def discard(self, handle: PendingRequest) -> None:
    """丢弃一个未收取的 handle：移出在途集合，迟到响应按未知 req_id 丢弃。

    用于流水线重置（例如某帧超时后清空在途请求）；不记录成功/失败，不抛异常。
    """
    try:
      self._retire(handle)
    except Exception:
      pass

  def request_reset(self) -> None:
    """让下一次 submit 带上 FLAG_RESET_TEMPORAL（服务端据此清空 history）。"""
    self._armed_reset = True
    # 重置时序 = 服务端 history 作废，随后的头几帧必然更慢：给一段冷启动宽限，
    # 免得无进展看门狗把「正常的重建后首帧」误判成断链。
    self._cold_until = time.monotonic() + STALL_COLD_GRACE_S
    self._stall_since = None

  def reset_temporal(self) -> None:
    """request_reset 的别名，语义更直白。"""
    self.request_reset()

  # -- 内部 -------------------------------------------------------------------

  def _next_request_id(self) -> int:
    with self._pending_lock:
      self._request_counter = (self._request_counter + 1) & 0xFFFFFFFFFFFFFFFF
      if self._request_counter == 0:
        self._request_counter = 1
      return self._request_counter

  def _retire(self, handle: PendingRequest, from_result: bool = False) -> None:
    with self._pending_lock:
      self._pending.pop(handle.req_id, None)
      if not from_result:
        self._retired_set.add(handle.req_id)
        self._retired.append(handle.req_id)
        if len(self._retired) > _RETIRED_CAP:
          drop = self._retired[:len(self._retired) // 2]
          del self._retired[:len(self._retired) // 2]
          for rid in drop:
            self._retired_set.discard(rid)

  def _reader_main(self, sock=None, generation: int | None = None) -> None:
    pin_network_thread()
    # 代际缺省（老调用方/测试）时视为「当前代」，行为与从前一致
    if generation is None:
      sock, generation = self._sock, self._link_gen
    udp = self.transport == TRANSPORT_UDP
    try:
      while not self._stop.is_set() and sock is not None and self._link_gen == generation:
        sock.settimeout(None)
        if udp:
          # deadline=None：阻塞等待整条消息收齐；单帧 deadline 仍由 collect() 控制。
          header, payload = recv_message_udp(
            sock, deadline=None, max_payload_size=MAX_RESPONSE_PAYLOAD_SIZE,
            expect_addr=self._peer, chunk_data=self.udp_chunk_data)
        else:
          header, payload = recv_message(sock, max_payload_size=MAX_RESPONSE_PAYLOAD_SIZE)
        if self._link_gen != generation:
          return  # 连接已被拆/重建：这条流的响应属于旧连接，直接丢弃
        self._dispatch(header, payload)
    except Exception as e:  # EOFError / OSError / ProtocolError：链路已不可信
      # 代际已变 = 自己管的是一条被拆掉的旧连接：只退出。
      # 误报会让刚建好的新连接立刻被 _on_link_error 打死，形成永不恢复的死循环。
      if self._link_gen != generation:
        log_event('reader_stale_exit', transport=self.transport, reason=str(e))
        return
      if not self._stop.is_set():
        self._on_link_error(f'read failed: {e}')

  def _dispatch(self, header: Header, payload: bytes) -> None:
    with self._pending_lock:
      handle = self._pending.get(header.request_id)
    if handle is None:
      # 已超时/已收取/未知 req_id 的迟到响应：丢弃，不算错误
      return

    if handle.expect_hello:
      # 握手型请求：只取 HELLO_RESP 的内容，不做 frame_id/时间戳校验，也不碰
      # 链路健康状态 —— 握手成败由等待方（重连监督器线程）自己判定并决定拆不拆。
      if header.msg_type == MSG_HELLO_RESP:
        try:
          handle.hello_result = unpack_hello_resp(payload)
        except (ProtocolError, ValueError) as e:
          handle.error = RemoteHandshakeError(f'bad HELLO response: {e}')
      elif header.msg_type == MSG_ERROR_RESP:
        code, msg = unpack_error_resp(payload)
        handle.error = RemoteHandshakeError(f'HELLO rejected: server error {code}: {msg}')
      else:
        handle.error = RemoteHandshakeError(
          f'Unexpected msg_type during HELLO: {header.msg_type}')
      handle._event.set()
      return

    outputs: list[np.ndarray] | None = None
    error: RemoteError | None = None
    fatal = False
    try:
      if header.msg_type == MSG_ERROR_RESP:
        code, msg = unpack_error_resp(payload)
        if code in LINK_FATAL_ERROR_CODES:
          # code 4 = 服务端不认这条连接（PC 侧进程重启/会话重建）。此时继续在旧
          # socket 上发推理只会每帧都拿到同一个错误 —— 必须把这次请求作废并**彻底
          # 重建 + 重新 HELLO**。本帧结果对调用方是失败（上层同帧回退本地小模型），
          # 绝不会拿一个「没跑过推理」的结果去控制。
          error = RemoteLinkError(f'server error {code}: {msg} (link resync required)')
          fatal = True
          log_event('rehandshake_required', frame=header.frame_id, req=header.request_id,
                    transport=self.transport, reason=f'code {code}')
        else:
          error = RemoteServerError(f'server error {code}: {msg}')
          log_event('server_error', frame=header.frame_id, req=header.request_id,
                    transport=self.transport, reason=f'code {code}')
      elif header.msg_type != MSG_INFERENCE_RESP:
        error = RemoteLinkError(f'unexpected response msg_type {header.msg_type}')
        fatal = True
      elif header.frame_id != handle.frame_id or header.timestamp_eof != handle.timestamp_eof:
        error = RemoteLinkError(
          f'response mismatch: expected (frame={handle.frame_id}, ts={handle.timestamp_eof}), '
          f'got (frame={header.frame_id}, ts={header.timestamp_eof})')
        fatal = True
      else:
        outputs = self._validate_outputs(unpack_tensor_dict(
          payload, max_tensor_size=MAX_RESPONSE_PAYLOAD_SIZE,
          compressed=bool(header.flags & FLAG_ZLIB_TENSOR)))
    except (ProtocolError, ValueError) as e:
      error = RemoteLinkError(f'bad response for req {header.request_id}: {e}')
      fatal = True

    if error is not None and isinstance(error, RemoteLinkError):
      fatal = True
    handle.error = error
    handle.outputs = outputs
    handle._event.set()
    if error is not None:
      self.status.record_failure(str(error))
    if fatal:
      self._on_link_error(str(error))

  @staticmethod
  def _validate_outputs(out_dict: dict[str, np.ndarray]) -> list[np.ndarray]:
    if not out_dict:
      raise ValueError('response carries no tensors')
    outputs: list[np.ndarray] = []
    index = 0
    while f'raw_outputs_{index}' in out_dict:
      arr = out_dict[f'raw_outputs_{index}']
      if arr.dtype.type not in (np.float16, np.float32):
        raise ValueError(f'illegal output dtype {arr.dtype} for raw_outputs_{index}')
      if not np.all(np.isfinite(arr)):
        raise ValueError(f'non-finite output in raw_outputs_{index}')
      outputs.append(arr)
      index += 1
    if not outputs:
      raise ValueError('response has no raw_outputs_N tensors')
    return outputs

  def _on_link_error(self, reason: str) -> None:
    """reader/send 侧发现链路失效：置未连接、失败所有未决请求，并让链路可被重建。

    * **bulk**：只作废**会话**（`_hello_done=False`），保留 ffs 端点与 reader ——
      端点上的阻塞 read 只有主机写数据才会结束，关掉再 open 必然 `EBUSY`，
      见 `_bulk_session_resettable()`。
    * **tcp/udp**：**彻底**关闭 socket（无论其句柄是否还在），否则后面的
      `connect()` 会因为「句柄还在」而短路，重连变成空操作。
    """
    if self._stop.is_set():
      return
    # 主动收尾（close / 用户关开关）不是故障，别把正常收尾记成 last_error
    intentional = reason in ('client closed', 'reconnect disarmed')
    if not intentional:
      self.status.record_failure(reason)
      log_event('link_error', transport=self.transport, reason=reason)
    with self._pending_lock:
      handles = list(self._pending.values())
      self._pending.clear()
    error = RemoteLinkError(reason)
    for handle in handles:
      handle.error = error
      handle._event.set()
    self.status.set_connected(False)
    if self._bulk_session_resettable():
      self._hello_done = False
      self._stall_since = None
      self._cold_until = 0.0
      log_event('bulk_session_reset', transport=self.transport, reason=reason)
    else:
      self._hard_close()
    # 链路已死：立刻唤醒后台重连监督器（退避中则由它自己的阶梯控制）
    self._supervisor_wake.set()

  def _hard_close(self) -> None:
    """把 socket、握手态与连接代际一并作废（幂等，可并发调用）。"""
    self._stop.set()
    self._hello_done = False
    self._connected_since = None
    self._stall_since = None
    self._cold_until = 0.0
    # 代际 +1：任何仍在阻塞的旧 reader 醒来后会发现自己已过期，从而只退出而不报
    # 链路错误（否则会误杀刚重建好的新连接）。
    self._link_gen += 1
    sock, self._sock = self._sock, None
    if sock is not None:
      try:
        sock.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass
      try:
        sock.close()
      except OSError:
        pass

  def _teardown(self, reason: str) -> None:
    """主动拆链：失败所有未决请求 + 彻底关闭 socket + 置未连接（幂等）。"""
    with self._link_lock:
      with self._pending_lock:
        handles = list(self._pending.values())
        self._pending.clear()
      error = RemoteLinkError(reason)
      for handle in handles:
        handle.error = error
        handle._event.set()
      self.status.set_connected(False)
      self._hard_close()

  # -- 后台重连监督器 -----------------------------------------------------------

  def arm_reconnect(self, armed: bool = True, *, immediate: bool = False) -> None:
    """武装/解除后台重连监督器（设置页开关落在这里）。

    * `armed=False`：停掉监督器并**断开链路**（用户明确不要外置）；
    * `armed=True` 且 `immediate=True`：设置页 off→on 时立刻重置退避并触发一次
      重连，不等上一次退避窗口（这是「设置页切一下就能恢复」的关键）。

    **本方法由 modeld 的实时主线程调用，绝不能阻塞**：监督器可能正卡在一次无界
    的 ffs 写里并持有 `_link_lock`，而解除武装要拆链就必须拿这把锁。因此解除侧
    只改标志（置未连接后本进程就不再发帧），真正的 join + 拆链交给一条一次性
    守护线程去做。

    监督器只管「链路通不通」，**不碰**换入决策 —— 换入仍由 JoinedModel 的安全窗口
    门禁控制，因此绝不在行车中直接把控制切回大模型。
    """
    armed = bool(armed)
    with self._supervisor_lock:
      was_armed, self._supervisor_armed = self._supervisor_armed, armed
      if not armed:
        self._supervisor_stop.set()
        self._reconnect_backoff_s = 0.0
        self._reconnect_attempts = 0
    if not armed:
      # 先立刻置「未连接」：调用方返回后就不再发帧，拆链晚一步也安全。
      self.status.set_connected(False)
      self._sync_reconnect_status()
      self._supervisor_wake.set()
      threading.Thread(target=self._disarm_teardown, name='oprp-disarm', daemon=True).start()
      log_event('reconnect_disarm', transport=self.transport)
      return
    if immediate or not was_armed:
      # off→on（或首次武装）：退避归零 + 立刻唤醒，不等退避
      with self._supervisor_lock:
        self._reconnect_backoff_s = 0.0
        self._reconnect_attempts = 0
        self._reconnect_last_attempt = 0.0
    self._supervisor_stop.clear()
    self._ensure_reconnect_thread()
    self._supervisor_wake.set()
    self._sync_reconnect_status()
    log_event('reconnect_arm', transport=self.transport, immediate=bool(immediate or not was_armed))

  def _disarm_teardown(self) -> None:
    """解除武装后的收尾（跑在一次性守护线程上，绝不占用实时线程）。

    走 `_on_link_error` 而不是 `_teardown`：对 bulk 而言这只是**作废会话**、保留
    ffs 端点。若在这里把端点关掉，端点上那个阻塞中的 read/write 关不掉、ep 也
    再也 `open` 不回来（EBUSY）—— 于是「关一次外置再打开」就会把链路永久卡死，
    正是本轮要修的那个症状。只有真正的 `close()`（下电/退出）才销毁端点。
    """
    try:
      self._stop_reconnect_thread()
      self._on_link_error('reconnect disarmed')
    except Exception:
      pass

  def request_reconnect(self) -> None:
    """请求一次尽快重连。

    **只在没有退避等待时才生效**：否则每帧「链路不可用，请重连」的无脑请求会
    把退避阶梯彻底架空（实测退避 30s 却变成 2Hz 重试）。要真正立刻重连请用
    `arm_reconnect(True, immediate=True)`（设置页 off→on 走的就是这条）。
    """
    with self._supervisor_lock:
        if self._reconnect_backoff_s > 0.0:
            return
    self._supervisor_wake.set()

  def _sync_reconnect_status(self) -> None:
    """把重连监督器状态推给 status（UI 的「重连中(退避 Ns)」来源）。绝不抛异常。"""
    try:
      self.status.set_reconnect(self._supervisor_armed, self._reconnect_backoff_s,
                                self._reconnect_attempts)
    except Exception:
      pass

  def _ensure_reconnect_thread(self) -> None:
    with self._supervisor_lock:
      thread = self._supervisor_thread
      if thread is not None and thread.is_alive():
        return
      self._supervisor_stop.clear()
      thread = threading.Thread(target=self._reconnect_main, name='oprp-reconnect', daemon=True)
      self._supervisor_thread = thread
      thread.start()

  def _stop_reconnect_thread(self) -> None:
    with self._supervisor_lock:
      thread, self._supervisor_thread = self._supervisor_thread, None
    self._supervisor_stop.set()
    self._supervisor_wake.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
      thread.join(timeout=2.0)

  # bulk 端点被「已死会话」卡死时的可执行提示。FunctionFS 上那个阻塞中的
  # read/write 只有主机恢复取数才会结束，close() 唤不醒；它持有的 ep 又让
  # `open('/dev/ffs-c3bulk/ep1')` 永远返回 EBUSY。复位 ep 只能重绑 UDC（root）或
  # 重启设备侧持有者，所以这里把**该做什么**直接写进 last_error，让设置页/面板
  # 能原样告诉用户，而不是让人对着一句 errno 猜。
  BULK_WEDGE_HINT = ('bulk 端点被已死会话卡死（ffs 阻塞 IO 无法中断，ep 不可重开）；'
                     '请重插 USB 下口，或重启 openpilot 复位端点')

  def _reconnect_attempt(self) -> bool:
    """真正做一次「重建/重握手」；成功返回 True。绝不抛异常。"""
    now = time.monotonic()
    if now - self._reconnect_last_attempt < RECONNECT_MIN_INTERVAL_S:
      return False
    self._reconnect_last_attempt = now
    self._reconnect_started_at = now
    self._wedge_reported = False
    with self._supervisor_lock:
      self._reconnect_attempts += 1
      attempt = self._reconnect_attempts
    self._sync_reconnect_status()
    ok = False
    try:
      self.reconnect()
      ok = True
    except Exception as exc:  # 监督器线程绝不能因任何异常而死掉
      message = f'reconnect failed: {exc}'
      if self.transport == TRANSPORT_BULK and attempt >= BULK_WEDGE_AFTER_ATTEMPTS:
        message += f' | {self.BULK_WEDGE_HINT}'
      self.status.record_failure(message)
      log_event('reconnect_attempt_fail', transport=self.transport, attempt=attempt, reason=str(exc))
    if not ok:
      return False
    # 新链路的时序状态是全新的：让服务端清空 history，并给足冷启动宽限。
    self.request_reset()
    self._wedge_reported = False
    self._reconnect_started_at = 0.0
    log_event('reconnect_ok', transport=self.transport, attempt=attempt,
              recovery_epoch=self._recovery_epoch)
    return True

  def _reconnect_backoff_next(self) -> float:
    """推进退避阶梯：1s → 2s → 4s → … → 30s（封顶），返回本次间隔。"""
    with self._supervisor_lock:
      current = float(self._reconnect_backoff_s)
      nxt = RECONNECT_BASE_S if current <= 0 else min(current * 2.0, RECONNECT_MAX_S)
      self._reconnect_backoff_s = nxt
      return nxt

  def _reconnect_wedge_watchdog(self) -> None:
    """独立看门狗：报告「重连尝试卡死在 ffs 写里」这一**不会自行失败**的故障。

    bulk 的重握手一旦卡在端点上就既不返回也不报错，监督器线程随之停在
    `_reconnect_attempt()` 里 —— 于是既没有退避推进、也没有失败事件，用户只看到
    「重连中」一直不变。这条看门狗按耗时判定并把**该做什么**写进 last_error，
    让设置页/面板能原样告诉用户，而不是让人对着一句 errno 猜。
    """
    if self.transport != TRANSPORT_BULK:
      return
    while not self._supervisor_stop.is_set():
      self._supervisor_wake.wait(1.0)
        # 唤醒后继续：立即唤醒只是让判定更准，不改变 1s 的节流
      self._supervisor_wake.clear()
      if self._supervisor_stop.is_set() or not self._supervisor_armed:
        continue
      if self.connected:
        # 恢复成功：清掉本次尝试的计时与已报状态（下次真卡住还能再报）
        self._reconnect_started_at = 0.0
        self._wedge_reported = False
        continue
      started = self._reconnect_started_at
      if started <= 0.0:
        continue
      waited = time.monotonic() - started
      if waited < BULK_WEDGE_AFTER_S or self._wedge_reported:
        continue
      self._wedge_reported = True
      self.status.record_failure(
        f'bulk 重连卡住 {waited:.0f}s（端点被已死会话卡死，ffs 阻塞 IO 无法中断）| '
        f'{self.BULK_WEDGE_HINT}')
      log_event('reconnect_wedge', transport=self.transport, waited_s=round(waited, 1))

  def _reconnect_main(self) -> None:
    """监督器主循环：链路不可用时按 1s→2s→…→30s 退避重建；通了就立刻回到空闲。"""
    pin_network_thread()
    watchdog = None
    if self.transport == TRANSPORT_BULK:
      # 卡死检测必须独立成线程：监督器自己可能正卡在 ffs 写里回不来
      watchdog = threading.Thread(target=self._reconnect_wedge_watchdog,
                                 name='oprp-reconnect-wd', daemon=True)
      watchdog.start()
    while not self._supervisor_stop.is_set():
      if not self.reconnect_armed:
        break
      if self.connected:
        # 链路「看起来健康」时也要跑无进展看门狗：bulk 上对端消失既不产生 socket
        # 错误、也不让 sendall 返回，唯一的信号就是「发了很多帧却一个成功帧都没有」。
        try:
          self._check_stall_supervised()
        except RemoteLinkError:
          continue   # 已拆链 → 回到循环顶部走重连分支
        with self._supervisor_lock:
          if self._reconnect_backoff_s or self._reconnect_attempts:
            log_event('reconnect_idle', transport=self.transport)
          self._reconnect_backoff_s = 0.0
          self._reconnect_attempts = 0
          self._reconnect_last_attempt = 0.0
        self._sync_reconnect_status()
        self._supervisor_wake.wait(RECONNECT_POLL_S)
        self._supervisor_wake.clear()
        continue
      # 链路不可用：等退避（被 wake 打断则提前重来）
      with self._supervisor_lock:
        delay = max(self._reconnect_backoff_s, RECONNECT_BASE_S)
      woken = self._supervisor_wake.wait(delay)
      self._supervisor_wake.clear()
      if self._supervisor_stop.is_set() or not self.reconnect_armed:
        break
      if woken and self.connected:
        continue
      if self._reconnect_attempt():
        with self._supervisor_lock:
          self._reconnect_backoff_s = 0.0
          self._reconnect_attempts = 0
        self._sync_reconnect_status()
        continue
      # 失败：推进 1s→2s→…→30s 阶梯，并把这个退避显式给 UI
      delay = self._reconnect_backoff_next()
      self._sync_reconnect_status()
      log_event('reconnect_wait', transport=self.transport, backoff_s=delay)
    if watchdog is not None:
      watchdog.join(timeout=0.5)
    log_event('reconnect_supervisor_exit', transport=self.transport)

  def close(self) -> None:
    """幂等关闭：唤醒 reader、失败未决请求、断开连接，并停掉后台重连监督器。"""
    self._supervisor_stop.set()
    self._supervisor_wake.set()
    with self._supervisor_lock:
      self._supervisor_armed = False
      self._reconnect_backoff_s = 0.0
      self._reconnect_attempts = 0
    self._sync_reconnect_status()
    self._stop_reconnect_thread()
    reader = self._reader
    if not self._stop.is_set() and (self._sock is not None or reader is not None):
      log_event('link_closed', transport=self.transport)
    self._teardown('client closed')
    if reader is not None and reader.is_alive() and reader is not threading.current_thread():
      reader.join(timeout=1.0)
    self._reader = None
    self.status.set_connected(False)

  def __enter__(self) -> 'RemotePipelineClient':
    self.connect_and_hello()
    return self

  def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
    self.close()
