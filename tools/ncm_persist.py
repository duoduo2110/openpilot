#!/usr/bin/env python3
"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

C3 NCM 自启持久化脚本（本机维护工具，设备侧落地）。

设备现状
--------
* `/data/c3-ncm-gadget.sh`：已验证可配置 NCM gadget，但每次都会 `sudo` 拆掉重建；
* `/data/continue.sh`：`cd /data/openpilot; exec ./launch_openpilot.sh`，
  由启动流程在 openpilot 之前执行（该场景通常已有 root）。

本工具做两件事
--------------
1. 生成**幂等**的 `/data/c3-ncm-persist.sh`：先判断 `usb0`/UDC 是否已配置，
   已配置直接 `exit 0`；未配置才重建。continue.sh 场景已是 root，因此不再重复
   叠加 `sudo`（仅在非 root 时才退化使用 sudo）。
2. 用**原子写**更新 `/data/continue.sh`：先备份为 `/data/continue.sh.bak-<时间戳>`，
   再「临时文件 → rename」替换，新内容形如：

       #!/usr/bin/env bash
       /data/c3-ncm-persist.sh || true
       cd /data/openpilot
       exec ./launch_openpilot.sh

`--dry-run` 只打印将写入的内容与差异，不落盘；本工具不重启设备。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import sys
import tempfile

from deploy_accelerator import Device, DeviceError, utc_stamp

DEFAULT_HOST = os.environ.get('C3_HOST', 'comma@192.168.43.1')
DEFAULT_KEY = os.environ.get('C3_KEY', '~/.ssh/id_rsa')
PERSIST_PATH = '/data/c3-ncm-persist.sh'
CONTINUE_PATH = '/data/continue.sh'

# ---------------------------------------------------------------------------
# 待落地的文件内容（单一事实来源，dry-run 与实际写入完全一致）
# ---------------------------------------------------------------------------

PERSIST_SCRIPT = r'''#!/usr/bin/env bash
# C3 NCM gadget 幂等恢复片段（由 tools/ncm_persist.py 生成，请勿手改）
# 由 /data/continue.sh 在 openpilot 启动前调用；已配置或配置成功均返回 0。
set -eu

G=/sys/kernel/config/usb_gadget/c3ncm
UDC=a600000.dwc3
CIDR=192.168.43.1/24
LOG=/data/c3-ncm.log

# continue.sh 场景通常已是 root；仅在非 root 时才退化使用 sudo，避免重复提权
if [ "$(id -u)" -eq 0 ]; then
  SUDO=
else
  SUDO=sudo
fi

say() { echo "[$(date -Iseconds)] $*"; }

# 幂等：Gadget 已绑 UDC + usb0 已起且已配 IP，则无事可做
if [ -d "$G" ] \
   && [ -e /sys/class/net/usb0 ] \
   && [ "$(cat "$G/UDC" 2>/dev/null || true)" = "$UDC" ] \
   && ip -br addr show dev usb0 2>/dev/null | grep -q "UP.*$CIDR"; then
  exit 0
fi

exec >>"$LOG" 2>&1
say "configure C3 NCM v2 (persist)"

$SUDO mkdir -p /sys/devices/platform/soc 2>/dev/null || true
$SUDO sh -c 'echo peripheral > /sys/devices/platform/soc/a600000.ssusb/mode' 2>/dev/null || true

if [ -d "$G" ]; then
  if [ -f "$G/UDC" ]; then
    $SUDO sh -c "echo '' > '$G/UDC'" 2>/dev/null || true
  fi
  $SUDO rm -f "$G/configs/c.1/ncm.usb0" 2>/dev/null || true
  $SUDO rmdir "$G/functions/ncm.usb0" "$G/configs/c.1/strings/0x409" "$G/configs/c.1/strings" \
    "$G/configs/c.1" "$G/configs" "$G/strings/0x409" "$G/strings" "$G" 2>/dev/null || true
fi

$SUDO mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409"
echo 0x1d6b | $SUDO tee "$G/idVendor" >/dev/null
echo 0x0104 | $SUDO tee "$G/idProduct" >/dev/null
echo 0x0100 | $SUDO tee "$G/bcdDevice" >/dev/null
echo 0x0310 | $SUDO tee "$G/bcdUSB" >/dev/null
echo "$(cat /proc/sys/kernel/random/boot_id | cut -c1-16)" | $SUDO tee "$G/strings/0x409/serialnumber" >/dev/null
echo comma | $SUDO tee "$G/strings/0x409/manufacturer" >/dev/null
echo C3-NCM | $SUDO tee "$G/strings/0x409/product" >/dev/null
echo NCM | $SUDO tee "$G/configs/c.1/strings/0x409/configuration" >/dev/null
echo 250 | $SUDO tee "$G/configs/c.1/MaxPower" >/dev/null
echo 0x80 | $SUDO tee "$G/configs/c.1/bmAttributes" >/dev/null
$SUDO mkdir -p "$G/functions/ncm.usb0"
$SUDO ln -sf "$G/functions/ncm.usb0" "$G/configs/c.1/ncm.usb0"
echo "$UDC" | $SUDO tee "$G/UDC" >/dev/null

for _ in $(seq 1 30); do [ -e /sys/class/net/usb0 ] && break; sleep 0.2; done
if [ ! -e /sys/class/net/usb0 ]; then
  say "usb0 did not appear"
  exit 1
fi
$SUDO ip addr flush dev usb0 2>/dev/null || true
$SUDO ip addr add "$CIDR" dev usb0
$SUDO ip link set usb0 up
say "state=$(cat /sys/class/udc/$UDC/state 2>/dev/null || echo unknown)"
exit 0
'''

CONTINUE_SCRIPT = '''#!/usr/bin/env bash
/data/c3-ncm-persist.sh || true
cd /data/openpilot
exec ./launch_openpilot.sh
'''


def _sha256(text: str) -> str:
  return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _read_remote(device: Device, path: str) -> str | None:
  proc = device.ssh(f'cat {path!r} 2>/dev/null || true\n', timeout=30, check=False)
  text = proc.stdout.decode('utf-8', 'replace')
  return text if text.strip() else None


def _write_remote_atomic(device: Device, path: str, content: str, mode: str, stamp: str) -> None:
  """本机生成临时文件 → scp 到设备同目录隐藏名 → chmod → rename 原子替换。"""
  directory = os.path.dirname(path) or '/data'
  base = os.path.basename(path)
  remote_tmp = f'{directory}/.{base}.tmp-{stamp}'
  local_tmp = os.path.join(tempfile.gettempdir(), f'opencode-{base}-{stamp}')
  with open(local_tmp, 'w', encoding='utf-8') as f:
    f.write(content)
  try:
    device.scp(local_tmp, remote_tmp, timeout=60)
    script = [
      'set -e',
      f'test -f {remote_tmp!r}',
      f'chmod {mode} {remote_tmp!r}',
      f'mv -f {remote_tmp!r} {path!r}',
    ]
    device.ssh('\n'.join(script) + '\n', timeout=60)
  finally:
    try:
      os.unlink(local_tmp)
    except OSError:
      pass


def _print_diff(before: str | None, after: str, label: str) -> None:
  print(f'--- {label} 差异 ---')
  if before is None:
    print(f'（设备上不存在 {label}，将新建）')
    return
  diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                   fromfile=f'{label} (当前)', tofile=f'{label} (将写入)', lineterm=''))
  if not diff:
    print('（内容一致，无差异）')
  else:
    print('\n'.join(diff))


def cmd_dry_run(device: Device, persist_path: str, continue_path: str) -> int:
  stamp = utc_stamp()
  backup = f'{continue_path}.bak-{stamp}'
  try:
    current_persist = _read_remote(device, persist_path)
    current_continue = _read_remote(device, continue_path)
    reachable = True
  except DeviceError as exc:
    current_persist = current_continue = None
    reachable = False
    print(f'[警告] 设备不可达（{exc}），仅展示将写入的内容', file=sys.stderr)

  print(f'设备: {device.host}')
  print(f'将写入: {persist_path}  (mode 755, sha256={_sha256(PERSIST_SCRIPT)[:12]}…)')
  print(f'将写入: {continue_path} (mode 755, sha256={_sha256(CONTINUE_SCRIPT)[:12]}…)')
  print(f'continue.sh 备份: {backup}')
  print()
  print('==================== /data/c3-ncm-persist.sh ====================')
  print(PERSIST_SCRIPT, end='')
  print('==================== /data/continue.sh ====================')
  print(CONTINUE_SCRIPT, end='')
  print()
  if reachable:
    _print_diff(current_continue, CONTINUE_SCRIPT, continue_path)
    existing = '存在' if current_persist is not None else '不存在'
    print(f'{persist_path} 当前状态: {existing}')
  print('dry-run：未写入设备、未重启。')
  return 0


def cmd_apply(device: Device, persist_path: str, continue_path: str) -> int:
  stamp = utc_stamp()
  backup = f'{continue_path}.bak-{stamp}'
  # 1) 先备份 continue.sh（保留权限与时间戳）
  device.ssh(
    'set -e\n'
    f'if [ -f {continue_path!r} ]; then cp -p {continue_path!r} {backup!r}; echo BACKUP_OK; '
    'else echo BACKUP_ABSENT; fi\n',
    timeout=30,
  )
  print(f'[备份] {backup}')

  # 2) 原子写入 persist 脚本与 continue.sh
  _write_remote_atomic(device, persist_path, PERSIST_SCRIPT, '755', stamp)
  _write_remote_atomic(device, continue_path, CONTINUE_SCRIPT, '755', stamp)

  # 3) 复核 sha256 与权限
  script = (
    f'echo "{_sha256(PERSIST_SCRIPT)}  {persist_path}"; sha256sum {persist_path!r} | cut -d" " -f1\n'
    f'echo "{_sha256(CONTINUE_SCRIPT)}  {continue_path}"; sha256sum {continue_path!r} | cut -d" " -f1\n'
    f'stat -c "%a %n" {persist_path!r} {continue_path!r}\n'
  )
  print(device.ssh(script, timeout=30).stdout.decode('utf-8', 'replace').strip())
  print('[结果] continue.sh 已原子更新；本工具未重启设备。')
  return 0


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description='C3 NCM 自启持久化（幂等 + 原子）')
  parser.add_argument('--host', default=DEFAULT_HOST)
  parser.add_argument('--key', default=DEFAULT_KEY)
  parser.add_argument('--persist-path', default=PERSIST_PATH)
  parser.add_argument('--continue-path', default=CONTINUE_PATH)
  parser.add_argument('--dry-run', action='store_true', help='只打印将写入的内容与差异，不落盘')
  return parser


def main(argv: list[str] | None = None) -> int:
  args = build_parser().parse_args(argv)
  device = Device(args.host, args.key, '/data')
  try:
    if args.dry_run:
      return cmd_dry_run(device, args.persist_path, args.continue_path)
    return cmd_apply(device, args.persist_path, args.continue_path)
  except DeviceError as exc:
    print(f'[错误] {exc}', file=sys.stderr)
    return 2


if __name__ == '__main__':
  raise SystemExit(main())
