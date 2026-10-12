"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

vendor bulk（FunctionFS）传输：给 OPRP 协议层一个「伪 socket」。

协议语义完全不变——`send_message`/`recv_message` 仍然只调用 `sendall(bytes)` 与
`recv(n)`；这里只把这两个原语落到 ffs 端点上，因此 `transport='bulk'` 与既有
tcp/udp 走的是同一份编解码代码。

实测（PoC，见 docs/低延迟外置模型运行方案-C3-Low-Latency-Remote-Model-Plan.md）：
- 384 KiB 单帧 p50 2.16 ms / p95 3.72 ms，200 帧 0 丢帧；
- FFS 读块 128 KiB 最佳；256 KiB 必现 order-6 分配失败，故 64 KiB 作稳健回退；
- FunctionFS 会在 bind 时重编号端点，主机侧必须运行时发现（0x82 -> 0x81）；
- SS companion `bMaxBurst=15`（16 KB burst）；
- dwc3 TX FIFO flush bug：设备侧 TX（本端写 ffs IN 端点）每条消息按 16 KiB 对齐，
  接收端按同一规则丢弃确定性填充（见 TX_ALIGN）；
- gadget 不绑 UDC 由脚本负责；**由打开 ep0 的进程写完 descriptors 后再绑**。
"""
from __future__ import annotations

import errno
import os
import select
import struct
import threading
import time

# -- FunctionFS ABI（与 jetlink/transport/ffs.py 一致） ------------------------

FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_STRINGS_MAGIC = 2

FLAG_HAS_FS = 1 << 0
FLAG_HAS_HS = 1 << 1
FLAG_HAS_SS = 1 << 2

USB_DT_INTERFACE = 0x04
USB_DT_ENDPOINT = 0x05
USB_DT_SS_ENDPOINT_COMP = 0x30

USB_ENDPOINT_XFER_BULK = 0x02

EP_OUT = 0x01   # host -> device（设备侧读）
EP_IN = 0x82    # device -> host（设备侧写）
SS_MAX_PACKET = 1024

# dwc3 TX FIFO flush workaround：设备侧每写一条消息都补到 16 KiB 的整数倍；
# 接收端（PC 侧）由 header.payload_len 反推同一填充长度并丢弃。
TX_ALIGN = 16 * 1024

# 读块：128 KiB 实测最优；256 KiB 必现 order-6 失败（禁用）；64 KiB 稳健回退。
DEFAULT_READ_CHUNK = 128 * 1024
FALLBACK_READ_CHUNK = 64 * 1024
# 写块：FunctionFS 的每次 writev 提交为一个 USB request；主机必须用同样大小的读缓冲
# 才能在该 request 完成时收下它。实测主机读 256 KiB、设备写 128 KiB 时两者都卡到
# 超时（主机 URB 等不到满缓冲/短包），因此发送端把整条消息补齐到 write_chunk 的
# 整数倍，保证每次 writev 都是满 write_chunk；接收端按同一 chunk 读并丢弃该填充。
DEFAULT_WRITE_CHUNK = 64 * 1024
FALLBACK_WRITE_CHUNK = 64 * 1024

# gadget 名 / functionfs 挂载点。默认值必须与开机脚本 c3-bulk-gadget.sh 建的
# 那一份**完全一致**（仓内 openpilot/sunnypilot/accelerators/bulk_gadget/），
# 否则任何没带环境变量的临时调用（ssh 里手跑 bench / probe）都会去开一个
# 不存在的挂载点。环境变量仍可覆盖，仅用于历史 PoC 路径。
GADGET = os.environ.get('BULK_GADGET', '/sys/kernel/config/usb_gadget/c3bulk')
MOUNT = os.environ.get('BULK_MOUNT', '/dev/ffs-c3bulk')
UDC_ENV = os.environ.get('BULK_UDC', '')
UDC_SYSFS = '/sys/class/udc'
# 主机 claim 接口前端点返回 EIO；给足宽限期（与 jetlink EP_READY_TIMEOUT 同量级）。
EP_READY_TIMEOUT = float(os.environ.get('BULK_EP_READY_TIMEOUT', '10.0'))
_NOT_READY = (errno.EIO, errno.ENODEV, errno.ESHUTDOWN)

# ffs IO 的**有界性**上限（秒）。见 BulkEndpoint.sendall/recv 的注释：
# 对端消失时 ffs 的 writev/readv 会永久阻塞，线程回不来且 ep 无法重开（EBUSY），
# 整条链路就再也恢复不了。这里给写/读各一个上限 + 非阻塞 + select，把那种
# 「永久阻塞」降级为「有界超时 → 判链路已死 → 正常重连」。
# 真机实测单帧 384KiB 写入 ~1ms、20Hz 稳定，因此这两个上限远在健康区间之外。
BULK_WRITE_TIMEOUT_S = float(os.environ.get('BULK_WRITE_TIMEOUT_S', '2.0'))
BULK_READ_POLL_S = float(os.environ.get('BULK_READ_POLL_S', '0.2'))


def _interface_desc(n_endpoints: int = 2, i_interface: int = 1) -> bytes:
    return struct.pack('<BBBBBBBBB', 9, USB_DT_INTERFACE, 0, 0, n_endpoints,
                       0xFF, 0xFF, 0xFF, i_interface)


def _endpoint_desc(addr: int, max_packet: int) -> bytes:
    return struct.pack('<BBBBHB', 7, USB_DT_ENDPOINT, addr,
                       USB_ENDPOINT_XFER_BULK, max_packet, 0)


def _ss_companion(max_burst: int = 15) -> bytes:
    """SuperSpeed endpoint companion；bMaxBurst 为「额外」包数，15 即 16 KB burst。"""
    return struct.pack('<BBBBH', 6, USB_DT_SS_ENDPOINT_COMP, max_burst, 0, 0)


def build_descriptors() -> bytes:
    """fs/hs/ss 描述符块；EP_OUT 在前 → 设备侧 ep1=OUT、ep2=IN。"""
    fs = _interface_desc() + _endpoint_desc(EP_OUT, 64) + _endpoint_desc(EP_IN, 64)
    hs = _interface_desc() + _endpoint_desc(EP_OUT, 512) + _endpoint_desc(EP_IN, 512)
    ss = (_interface_desc()
          + _endpoint_desc(EP_OUT, SS_MAX_PACKET) + _ss_companion()
          + _endpoint_desc(EP_IN, SS_MAX_PACKET) + _ss_companion())
    body = struct.pack('<III', 3, 3, 5) + fs + hs + ss
    flags = FLAG_HAS_FS | FLAG_HAS_HS | FLAG_HAS_SS
    return struct.pack('<III', FUNCTIONFS_DESCRIPTORS_MAGIC_V2, 12 + len(body), flags) + body


def build_strings(name: str = 'c3bulk') -> bytes:
    s = name.encode() + b'\0'
    return (struct.pack('<IIII', FUNCTIONFS_STRINGS_MAGIC, 16 + 2 + len(s), 1, 1)
            + struct.pack('<H', 0x0409) + s)


def alignment_pad(n: int) -> int:
    """把 n 字节补到 TX_ALIGN 整数倍所需的填充字节数。"""
    return (-int(n)) % TX_ALIGN


def pad_to_align(data: bytes) -> bytes:
    pad = alignment_pad(len(data))
    return data + b'\x00' * pad if pad else data


def _resolve_read_chunk(value=None) -> int:
    if value is None:
        value = os.environ.get('BULK_READ_CHUNK')
    try:
        chunk = int(value) if value is not None else DEFAULT_READ_CHUNK
    except (TypeError, ValueError):
        chunk = DEFAULT_READ_CHUNK
    if chunk < 16 * SS_MAX_PACKET or chunk % SS_MAX_PACKET != 0:
        chunk = DEFAULT_READ_CHUNK
    if chunk > 128 * 1024:   # 256 KiB 必现 order-6 失败，硬性封顶
        chunk = DEFAULT_READ_CHUNK
    return chunk


def _resolve_write_chunk(value=None) -> int:
    if value is None:
        value = os.environ.get('BULK_WRITE_CHUNK')
    try:
        chunk = int(value) if value is not None else DEFAULT_WRITE_CHUNK
    except (TypeError, ValueError):
        chunk = DEFAULT_WRITE_CHUNK
    if chunk < 16 * SS_MAX_PACKET or chunk % TX_ALIGN != 0:
        chunk = DEFAULT_WRITE_CHUNK
    if chunk > 128 * 1024:   # 保持 order-5 以内，256 KiB 有 order-6 风险
        chunk = DEFAULT_WRITE_CHUNK
    return chunk


def _first_udc() -> str:
    try:
        names = sorted(os.listdir(UDC_SYSFS))
    except OSError:
        names = []
    return names[0] if names else ''


def _read_bound_udc(gadget: str) -> str:
    try:
        with open(os.path.join(gadget, 'UDC')) as f:
            return f.read().strip()
    except OSError:
        return ''


def _udc_state(udc: str) -> str:
    if not udc:
        return ''
    try:
        with open(os.path.join(UDC_SYSFS, udc, 'state')) as f:
            return f.read().strip()
    except OSError:
        return ''


def _bind_udc_if_needed(gadget: str, udc: str) -> str:
    existing = _read_bound_udc(gadget)
    if existing:
        return existing
    udc = udc or _first_udc()
    if not udc:
        raise OSError('no USB device controller found under %s' % UDC_SYSFS)
    with open(os.path.join(gadget, 'UDC'), 'w') as f:
        f.write(udc + '\n')
    return udc


def _wait_configured(udc: str, timeout: float) -> None:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while True:
        state = _udc_state(udc)
        if state == 'configured':
            return
        if time.monotonic() >= deadline:
            raise OSError('bulk gadget never configured (udc=%s state=%r)' % (udc, state))
        time.sleep(0.02)


class _OsIo:
    """默认底层 IO：os.readv / os.writev（测试可注入替身）。"""

    def read(self, fd: int, buf: bytearray) -> bytes:
        n = os.readv(fd, [buf])
        return bytes(memoryview(buf)[:n]) if n > 0 else b''

    def write(self, fd: int, mv: memoryview) -> int:
        return os.writev(fd, [mv])


def _close_quietly(fd: int) -> None:
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class BulkEndpoint:
    """OpenPilotRemote 要的最小 socket 接口：sendall / recv / settimeout / close。

    只实现 `RemotePipelineClient` 实际用到的部分：
    - `sendall(data)`：写 ffs EP IN（设备→主机），自动 16 KiB 对齐 + 切块；
    - `recv(n)`：阻塞读 ffs EP OUT（主机→设备），内部缓存多余字节；
    - `settimeout`：只用于「等待主机 claim」的宽限期，不能中断内核阻塞读；
    - `shutdown`/`close`：幂等；拥有 gadget 时先 unbound 唤醒阻塞读再关 fd。
    """

    def __init__(self, ep_in: int, ep_out: int, ep0: int = -1, gadget: str | None = None,
                 bound_udc: str | None = None, read_chunk: int | None = None,
                 write_chunk: int | None = None, io=None, pollable: bool = False):
        self._ep_in = int(ep_in)
        self._ep_out = int(ep_out)
        self._ep0 = int(ep0)
        self._gadget = gadget
        self._bound_udc = bound_udc
        # `_pollable=True` 只由 `open_bulk_endpoint`（真实 ffs fd）置位：真实 fd 是
        # 非阻塞的、可以 select，于是读写有界、close() 后线程必然退出。注入的假 fd
        # （单测）不可 select，保持旧语义不变。
        self._pollable = bool(pollable)
        self.read_chunk = _resolve_read_chunk(read_chunk)
        self.write_chunk = _resolve_write_chunk(write_chunk)
        self._io = io or _OsIo()
        self._timeout: float | None = None
        self._closed = False
        self._ready_deadline: float | None = None
        self._rxbuf = bytearray()
        self._rdbuf = bytearray(self.read_chunk)

    # -- socket-like API ----------------------------------------------------

    def settimeout(self, value) -> None:
        self._timeout = None if value is None else max(0.0, float(value))

    def gettimeout(self):
        return self._timeout

    def fileno(self) -> int:
        return self._ep_out

    def sendall(self, data) -> int:
        """把一条完整线上消息写到 ffs EP IN；返回实际写入字节数（含对齐填充）。

        消息先补齐到 write_chunk 的整数倍，于是每次 writev 都是满 write_chunk，
        主机侧用同尺寸读缓冲即可逐个 request 收下（见 DEFAULT_WRITE_CHUNK）。

        **两个 chunk 之间是有界的**：对端（PC 侧进程）消失时 ffs 写队列只增不减，
        `os.writev` 会阻塞在 `ffs_epfile_io` 里，既不返回也不报错。此时整条消息
        在那个 chunk 上停住，**只有主机恢复取数才会继续**（`O_NONBLOCK` 对 ffs
        的满队列无效，`close()` 也唤不醒）。所以：
          * 帧与帧之间由调用方的 deadline + client 的无进展看门狗兜底
            （见 client.STALL_TIMEOUT_S），不会静默挂死；
          * 端点本身**不可重建**（阻塞中的 read/write 持有 ep，`open` 必然 EBUSY），
            因此断链后只能「保留 ep + 重新 HELLO」，见 client._bulk_session_resettable。
        这里的 `BULK_WRITE_TIMEOUT_S` 约束的是「已 select 判定可写、却反复
        EAGAIN/短写」这类无进展，保证它不会无限自旋。
        """
        if self._closed:
            raise OSError(errno.EBADF, 'bulk endpoint closed')
        data = bytes(data)
        pad = (-len(data)) % self.write_chunk
        if pad:
            data += b'\x00' * pad
        mv = memoryview(data)
        total = mv.nbytes
        deadline = time.monotonic() + BULK_WRITE_TIMEOUT_S
        off = 0
        while off < total:
            end = min(off + self.write_chunk, total)
            chunk = mv[off:end]
            while chunk.nbytes:
                if self._closed:
                    raise EOFError('bulk endpoint closed')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(errno.EAGAIN,
                                     'bulk write made no progress for %.1fs'
                                     % BULK_WRITE_TIMEOUT_S)
                if self._pollable and self._ep_in >= 0:
                    _, writable, _ = select.select([], [self._ep_in], [], min(remaining, 0.25))
                    if not writable:
                        continue
                try:
                    n = self._io.write(self._ep_in, chunk)
                except BlockingIOError:
                    continue                      # 非阻塞 + 队列满：回去等 select
                except OSError as e:
                    if self._closed:
                        raise
                    if e.errno in _NOT_READY and self._retry_ready():
                        continue
                    raise
                if n <= 0:
                    raise OSError(errno.EIO, 'bulk write returned %d' % n)
                off += n
                chunk = chunk[n:]
        return total

    def recv(self, n: int) -> bytes:
        """最多返回 n 字节；不足则读取，直到凑齐、链路出错或本端被关闭。

        读同样**必须有界可打断**：ep_out 设为非阻塞，每次读前 select 一个短窗口，
        于是 `close()` 置 `_closed` 后最多一个窗口就能退出。旧实现是纯阻塞
        `os.readv`，PC 侧消失时线程永久卡在 `ffs_epfile_io`（同样让 ep 无法重开）。
        """
        if n <= 0:
            return b''
        while len(self._rxbuf) < n:
            if self._closed:
                raise EOFError('bulk endpoint closed')
            if self._pollable and self._ep_out >= 0:
                readable, _, _ = select.select([self._ep_out], [], [], BULK_READ_POLL_S)
                if not readable:
                    continue                      # 醒来先看 _closed，再决定是否重试
            try:
                got = self._io.read(self._ep_out, self._rdbuf)
            except BlockingIOError:
                continue
            except OSError as e:
                if self._closed:
                    raise EOFError('bulk endpoint closed') from e
                if e.errno in _NOT_READY and self._retry_ready():
                    continue
                raise
            if not got:
                raise EOFError('bulk endpoint EOF (host disconnected)')
            self._rxbuf.extend(got)
        out = bytes(self._rxbuf[:n])
        del self._rxbuf[:n]
        return out

    def shutdown(self, how=None) -> None:
        """幂等；unbound 拥有者的 gadget 以唤醒任何阻塞中的 ffs 读。"""
        self._closed = True
        self._unbind()

    def close(self) -> None:
        self._closed = True
        self._unbind()
        ep_in, self._ep_in = self._ep_in, -1
        ep_out, self._ep_out = self._ep_out, -1
        ep0, self._ep0 = self._ep0, -1
        for fd in (ep_in, ep0):
            _close_quietly(fd)
        if ep_out >= 0:
            # 端点上一个未完成的 read 会持有 struct file；本线程直接 close 可能卡住，
            # 放到守护线程里关（Linux 上 close 立即返回，但读可能不返回）。
            threading.Thread(target=_close_quietly, args=(ep_out,), daemon=True).start()

    # -- internals ----------------------------------------------------------

    def _retry_ready(self) -> bool:
        now = time.monotonic()
        if self._ready_deadline is None:
            grace = self._timeout if self._timeout is not None else 0.0
            self._ready_deadline = now + max(grace, EP_READY_TIMEOUT)
        if now < self._ready_deadline:
            time.sleep(0.005)
            return True
        return False

    def _unbind(self) -> None:
        gadget = self._gadget
        if gadget is None or not self._bound_udc:
            return
        try:
            with open(os.path.join(gadget, 'UDC'), 'w') as f:
                f.write('\n')
        except OSError:
            pass
        self._bound_udc = None


def open_bulk_endpoint(timeout: float = 10.0, own_gadget: bool | None = None,
                       gadget: str = GADGET, mount: str = MOUNT, udc: str = UDC_ENV,
                       read_chunk: int | None = None, write_chunk: int | None = None) -> BulkEndpoint:
    """打开 bulk 端点并返回伪 socket。

    own_gadget=True（默认）：本进程打开 ep0、写 descriptors、绑定 UDC，再等主机
    完成 SET_CONFIGURATION 后打开 ep1/ep2——即「gadget 由服务进程写完 descriptors
    再绑」。own_gadget=False：假设已有 keeper 占着 ep0 并已绑 UDC，只打开端点。
    """
    if own_gadget is None:
        own_gadget = os.environ.get('BULK_OWN_GADGET', '1') != '0'

    ep0 = -1
    bound_udc = ''
    if own_gadget:
        ep0 = os.open(os.path.join(mount, 'ep0'), os.O_RDWR)
        try:
            os.write(ep0, build_descriptors())
            os.write(ep0, build_strings())
            bound_udc = _bind_udc_if_needed(gadget, udc)
        except BaseException:
            _close_quietly(ep0)
            raise
    else:
        bound_udc = _read_bound_udc(gadget) or _first_udc()

    try:
        _wait_configured(bound_udc, timeout)
        ep_out = os.open(os.path.join(mount, 'ep1'), os.O_RDWR)   # 先声明的 EP_OUT
        ep_in = os.open(os.path.join(mount, 'ep2'), os.O_RDWR)    # 后声明的 EP_IN
    except BaseException:
        _close_quietly(ep0)
        if own_gadget and bound_udc:
            try:
                with open(os.path.join(gadget, 'UDC'), 'w') as f:
                    f.write('\n')
            except OSError:
                pass
        raise

    # 两个数据端点都设为**非阻塞**：读写前先 select 一个有界窗口，于是
    # 「对端消失 → 队列满/无数据」表现为 EAGAIN 而不是永久阻塞在 ffs_epfile_io。
    # 这是链路能被判定为「已死并重连」的前提（见 sendall/recv 的注释）。
    for fd in (ep_in, ep_out):
        try:
            os.set_blocking(fd, False)
        except OSError:
            pass

    return BulkEndpoint(ep_in=ep_in, ep_out=ep_out, ep0=ep0,
                        gadget=gadget if own_gadget else None,
                        bound_udc=bound_udc, read_chunk=read_chunk, write_chunk=write_chunk,
                        pollable=True)
