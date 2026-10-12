"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

offroad 独立链路探针：不依赖 modeld、不依赖相机/上车，仅用链路客户端层
(`RemotePipelineClient`) 对 PC 侧 5202 做 connect + HELLO，并可选发 N 帧，然后把
**本次真实测量**写入 `/data/models/accelerator_status.json`，让 C3 UI 在停车时就能
显示外置链路是否就绪（图标变绿）。

语义（重要，务必理解）
--------------------
* `connected / ready / frames_sent / frames_ok / 延迟分位` 全部是探针在链路上实测的。
* `big_active` 由 `--link-only` 控制（默认 False，即默认置 `big_active=True`）：
  握手成功且链路未断开时置 True。这是**探针的「链路已就绪」语义**，与 onroad
  modeld 的「远端输出正在被 modeld 使用」语义不同。探针只写 UI 读取的状态文件，
  **不参与**任何控制/安全门禁（AEB/angle/accel/relay/panda 均不读它）。
* 探针与 modeld **互斥**：5202 一次只服务一个客户端，且二者会互相覆盖状态文件。
  因此默认检测到 modeld 在运行时拒绝启动（`--force` 可强制）。
* 探针退出（`--stop` / SIGTERM / SIGINT / `--duration` 到期）会写一份「链路已停」
  快照（`connected=false, big_active=false, state='unavailable'`），避免图标残留绿色。

安全边界
--------
* 只读 `accelerator.json` 取地址/签名/预算，不做任何写配置。
* 只导入轻量链路层（numpy + client/protocol/status/status_writer），**不导入
  tinygrad / cereal / camerad**，因此 offroad、无相机也能跑。
* 任何异常都不冒泡成未捕获崩溃：链路错误只记状态并按退避重连。

用法（在 C3 上）::

    # 推荐包装脚本（已随 persist 部署）
    /data/accelerator/run_accel_probe.sh                 # 仅握手，持续保活（绿灯）
    /data/accelerator/run_accel_probe.sh --frames 100    # 握手后发 100 帧再保活
    /data/accelerator/run_accel_probe.sh stop            # 停止后台探针

    # 或直接跑模块
    cd /data/openpilot
    PYTHONPATH=/data/openpilot/pydeps:/data/openpilot:/data/openpilot/msgq_repo \
      /usr/local/venv/bin/python3 -m openpilot.sunnypilot.accelerators.remote.probe --frames 50

后台运行::

    setsid nohup /data/accelerator/run_accel_probe.sh --frames 100 \
      > /data/accelerator/accel_probe.log 2>&1 < /dev/null &
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time

import numpy as np

from . import config as accel_config
from .client import RemoteError, RemoteLinkError, RemotePipelineClient, RemoteTimeout
from .protocol import CAP_ZLIB_TENSOR
from .status import RemoteStatus
from .status_writer import StatusWriter

# 探针 pid 文件（后台运行/`stop` 用）；放在仓外持久目录
PIDFILE = '/data/accelerator/accel_probe.pid'
# 与 PC 契约一致的输入形状（服务端 required=['action_t','desire','traffic_convention','warped']）
WARPED_SHAPE = (2, 6, 128, 256)
DESIRE_SHAPE = (8,)
SCALAR2_SHAPE = (1, 2)
# 契约 metadata（取 fingerprint 作 signature 兜底）
CONTRACT_PATH = '/data/models/contract_metadata.json'
# 重连退避
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 30.0


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
  try:
    os.kill(pid, 0)
    return True
  except OSError:
    return False


def _read_pidfile() -> int | None:
  try:
    with open(PIDFILE) as f:
      return int(f.read().strip())
  except (OSError, ValueError):
    return None


def _write_pidfile(pid: int) -> None:
  try:
    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    with open(PIDFILE, 'w') as f:
      f.write(str(pid))
  except OSError:
    pass


def _remove_pidfile() -> None:
  try:
    os.unlink(PIDFILE)
  except OSError:
    pass


def _modeld_running() -> bool:
  """是否有 modeld 正在运行（按 /proc cmdline 精确匹配，避免抢 5202 客户端槽）。"""
  markers = (b'modeld_tinygrad', b'selfdrive.modeld.modeld', b'selfdrive/modeld/modeld')
  try:
    for name in os.listdir('/proc'):
      if not name.isdigit():
        continue
      try:
        with open(f'/proc/{name}/cmdline', 'rb') as f:
          cmd = f.read()
      except OSError:
        continue
      if any(m in cmd for m in markers):
        return True
  except OSError:
    pass
  return False


def _read_contract_signature() -> str | None:
  try:
    with open(CONTRACT_PATH) as f:
      doc = json.load(f)
    fp = doc.get('fingerprint') if isinstance(doc, dict) else None
    if isinstance(fp, str) and len(fp) == 64:
      return fp
  except (OSError, ValueError):
    pass
  return None


def _build_tensors(frame_id: int) -> dict[str, np.ndarray]:
  """构造一帧确定性、契约合法的输入；只发外部张量（warped + 三个标量）。"""
  warped = np.zeros(WARPED_SHAPE, dtype=np.uint8)
  # 让每帧略有差异，便于确认服务端确实逐帧执行（不影响链路语义）
  warped[0, 0, frame_id % WARPED_SHAPE[2], frame_id % WARPED_SHAPE[3]] = 1
  desire = np.zeros(DESIRE_SHAPE, dtype=np.float32)
  desire[0] = 1.0
  traffic_convention = np.zeros(SCALAR2_SHAPE, dtype=np.float32)
  traffic_convention[0, 0] = 1.0
  action_t = np.zeros(SCALAR2_SHAPE, dtype=np.float32)
  return {
    'warped': warped,
    'desire': desire,
    'traffic_convention': traffic_convention,
    'action_t': action_t,
  }


def _summary(status: RemoteStatus, frames_target: int, hello_ms: float | None) -> dict:
  snap = status.to_dict()
  return {
    'frames_target': frames_target,
    'frames_sent': snap['frames_sent'],
    'frames_ok': snap['frames_ok'],
    'frames_failed': snap['frames_failed'],
    'timeouts': snap['timeouts'],
    'hello_ms': None if hello_ms is None else round(hello_ms, 3),
    'p50_ms': round(snap['p50_ms'], 3),
    'p95_ms': round(snap['p95_ms'], 3),
    'p99_ms': round(snap['p99_ms'], 3),
    'max_ms': round(snap['max_ms'], 3),
    'last_error': snap['last_error'],
  }


# ---------------------------------------------------------------------------
# 停止既有探针
# ---------------------------------------------------------------------------

def stop_existing(wait_s: float = 10.0) -> int:
  pid = _read_pidfile()
  if pid is None:
    print('未发现运行中的探针（无 pidfile 或内容非法）。')
    return 0
  if not _pid_alive(pid):
    print(f'pidfile 中的 pid={pid} 已不存在，清理 pidfile。')
    _remove_pidfile()
    return 0
  print(f'向探针 pid={pid} 发送 SIGTERM ...')
  try:
    os.kill(pid, signal.SIGTERM)
  except OSError as e:
    print(f'发送 SIGTERM 失败: {e}')
    return 1
  deadline = time.monotonic() + wait_s
  while time.monotonic() < deadline:
    if not _pid_alive(pid):
      print('探针已退出。')
      _remove_pidfile()
      return 0
    time.sleep(0.2)
  print(f'等待 {wait_s:.0f}s 后探针仍在运行（pid={pid}）。')
  return 1


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def _resolve_settings(args) -> dict:
  cfg = accel_config.load()
  address = args.address or str(cfg.get('address') or '192.168.43.2:5202')
  signature = args.signature or str(cfg.get('signature') or '') or (_read_contract_signature() or '')
  if not signature:
    signature = '0' * 64
  return {
    'address': address,
    'signature': signature,
    'deadline_ms': float(args.deadline_ms if args.deadline_ms is not None else cfg.get('deadline_ms', 60)),
    'pipeline_depth': int(args.pipeline_depth if args.pipeline_depth is not None else cfg.get('pipeline_depth', 1)),
    'transport': str(cfg.get('transport') or 'tcp'),
    # 与 backend.load 一致：只有配置开了 payload_compression 才在 HELLO 声明能力位，
    # 否则保持未压缩布局（默认），确保探针与 modeld 的上行线格式一致。
    'capabilities': CAP_ZLIB_TENSOR if cfg.get('payload_compression') else 0,
    'enabled': bool(cfg.get('enabled', False)),
  }


def run(args) -> int:
  if args.stop:
    return stop_existing()

  # 单实例 + 与 modeld 互斥
  existing = _read_pidfile()
  if existing is not None and _pid_alive(existing):
    print(f'已有探针在运行 (pid={existing})；如需重启请先 `--stop`。', file=sys.stderr)
    return 3
  if not args.force and _modeld_running():
    print('检测到 modeld 正在运行：探针会抢占 5202 客户端槽并覆盖状态文件，已拒绝。'
          '（确需运行请加 --force）', file=sys.stderr)
    return 3

  settings = _resolve_settings(args)
  if settings['address'] in ('', ':'):
    print('accelerator.json 未配置 address，无法探测。', file=sys.stderr)
    return 2
  if not settings['enabled']:
    print('[警告] accelerator.json enabled=false；探针仍会写入状态，但 UI 不会按外置显示。',
          file=sys.stderr)

  interval = max(0.05, float(args.status_interval))
  writer = StatusWriter(interval=interval)
  status = RemoteStatus()
  client = RemotePipelineClient(
    signature=settings['signature'],
    address=settings['address'],
    deadline_ms=settings['deadline_ms'],
    pipeline_depth=settings['pipeline_depth'],
    transport=settings['transport'],
    status=status,
  )

  stop_evt = threading.Event()

  def _on_signal(signum, _frame):
    stop_evt.set()

  for sig in (signal.SIGTERM, signal.SIGINT):
    try:
      signal.signal(sig, _on_signal)
    except (ValueError, OSError):
      pass

  _write_pidfile(os.getpid())
  started = time.monotonic()
  hello_ms: float | None = None
  frames_done = 0
  backoff = RECONNECT_MIN_S

  print(f'probe pid={os.getpid()} address={settings["address"]} '
        f'frames_target={args.frames} hz={args.hz} green={not args.link_only} '
        f'contract={settings["signature"][:12]}…')

  try:
    # 连接 + 握手（失败按退避重试，直到停止）
    while not stop_evt.is_set():
      try:
        t0 = time.monotonic()
        client.connect_and_hello()
        hello_ms = (time.monotonic() - t0) * 1000.0
        backoff = RECONNECT_MIN_S
        print(f'connect_ok + hello_ok ({hello_ms:.1f} ms)，开始{"发帧" if args.frames else "保活"}')
        break
      except (RemoteLinkError, RemoteTimeout, OSError) as e:
        status.record_failure(f'probe connect/hello failed: {e}')
        writer.maybe_write(status=status, ready=False, big_active=False, state='retrying')
        print(f'connect/hello 失败（{backoff:.0f}s 后重试）: {e}', file=sys.stderr)
        if stop_evt.wait(backoff):
          break
        backoff = min(backoff * 2.0, RECONNECT_MAX_S)

    # 运行期：发帧或保活
    while not stop_evt.is_set():
      if args.duration and (time.monotonic() - started) >= args.duration:
        print(f'到达 --duration {args.duration}s，退出。')
        break

      if args.frames > 0 and frames_done < args.frames:
        frame_id = frames_done + 1
        ts_eof = int(time.monotonic() * 1e9)
        loop_t0 = time.monotonic()
        try:
          handle = client.submit(frame_id, ts_eof, _build_tensors(frame_id))
          client.collect(handle)
          frames_done += 1
        except RemoteTimeout as e:
          print(f'frame {frame_id} 超时: {e}', file=sys.stderr)
        except RemoteError as e:
          status.record_failure(f'probe frame {frame_id} failed: {e}')
          print(f'frame {frame_id} 失败: {e}；尝试重连', file=sys.stderr)
          try:
            client.close()
            t0 = time.monotonic()
            client.connect_and_hello()
            hello_ms = (time.monotonic() - t0) * 1000.0
          except (RemoteLinkError, RemoteTimeout, OSError) as e2:
            if stop_evt.wait(backoff):
              break
            backoff = min(backoff * 2.0, RECONNECT_MAX_S)
        except OSError as e:
          status.record_failure(f'probe frame {frame_id} io: {e}')
          if stop_evt.wait(backoff):
            break
        else:
          backoff = RECONNECT_MIN_S
        # 限速到 hz
        period = 1.0 / max(0.1, float(args.hz))
        remain = period - (time.monotonic() - loop_t0)
        if remain > 0:
          stop_evt.wait(remain)
        if args.exit_after_frames and frames_done >= args.frames:
          print(f'已发满 {frames_done} 帧，退出（--exit-after-frames）。')
          break
      else:
        # 不发帧 / 已发满：保活并周期写状态，保持图标新鲜
        stop_evt.wait(interval)

      writer.maybe_write(status=status, ready=client.connected,
                         big_active=(not args.link_only) and client.connected)
  finally:
    print('probe 退出中：写「链路已停」快照并清理 ...')
    # 停止快照不走节流（直接 write），避免残留绿色
    try:
      writer.write(writer.snapshot(status=None, connected=False, ready=False,
                                   big_active=False, state='unavailable'))
    except Exception:
      pass
    try:
      client.close()
    except Exception:
      pass
    _remove_pidfile()

  print(json.dumps(_summary(status, args.frames, hello_ms), ensure_ascii=False))
  return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
  p = argparse.ArgumentParser(
    prog='openpilot.sunnypilot.accelerators.remote.probe',
    description='offroad 独立外置链路探针：connect+HELLO（可选发 N 帧）并写 accelerator_status.json')
  p.add_argument('--stop', action='store_true', help='按 pidfile 停止既有后台探针后退出')
  p.add_argument('--force', action='store_true', help='即使检测到 modeld 在运行也强制启动')
  p.add_argument('--frames', type=int, default=0, help='总共发多少帧（0=仅握手不发帧，默认 0）')
  p.add_argument('--hz', type=float, default=20.0, help='发帧速率（默认 20）')
  p.add_argument('--exit-after-frames', action='store_true', help='发满 --frames 后立即退出')
  p.add_argument('--duration', type=float, default=0.0, help='运行多少秒后自动退出（0=不限）')
  p.add_argument('--status-interval', type=float, default=1.0, help='状态文件落盘最小间隔秒（默认 1.0）')
  p.add_argument('--link-only', action='store_true',
                 help='只置 connected/ready，不置 big_active（图标不会变绿）')
  p.add_argument('--address', default=None, help='覆盖 accelerator.json 的 address')
  p.add_argument('--signature', default=None, help='覆盖 accelerator.json 的 signature')
  p.add_argument('--deadline-ms', type=float, default=None, help='覆盖每帧 deadline（毫秒）')
  p.add_argument('--pipeline-depth', type=int, default=None, help='覆盖 pipeline_depth')
  return p


def main(argv=None) -> int:
  args = build_parser().parse_args(argv)
  try:
    return run(args)
  except KeyboardInterrupt:
    return 0


if __name__ == '__main__':
  raise SystemExit(main())
