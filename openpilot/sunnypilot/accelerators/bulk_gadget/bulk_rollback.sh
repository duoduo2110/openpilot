#!/usr/bin/env bash
# 回滚：把 C3 下口从 vendor bulk 切回 NCM（c3ncm）。
#
# ★ 关键约束：只有在 **c3ncm 不存在** 的前提下才能建它。
#   c3ncm 一旦存在，原 NCM 脚本会 rmdir 掉旧的 ncm 实例再重建，而这条路径会触发
#   f_ncm 内核 BUG（`ncm_free_inst` 空指针解引用 + configfs 死锁）。所以本脚本
#   在 `$G` 已存在时**直接拒绝**，不做任何 rmdir/symlink —— 这正是「切回 NCM 必须在
#   无 c3ncm 时进行」的实现。
#
# 正常回滚流程（本脚本负责第 1 步，其余由 tools/deploy_accelerator.py 负责）：
#   1) bash /data/openpilot/openpilot/sunnypilot/accelerators/bulk_gadget/bulk_rollback.sh
#      （脚本自己会 sudo 提权）→ 建 c3ncm + 绑 UDC + 配 usb0 192.168.43.1/24，
#        并把 NCM 开机钩子写回 /data/continue.sh（幂等）
#   2) python3 tools/deploy_accelerator.py --host comma@192.168.31.161 unpersist
#      → 移除 accelerator-persist 块（含 bulk gadget 开机调用）
#   3) python3 tools/deploy_accelerator.py --host comma@192.168.31.161 \
#        config --transport tcp
#   4) sudo reboot
set -u

G=/sys/kernel/config/usb_gadget/c3ncm
UDC="${BULK_UDC:-a600000.dwc3}"
CIDR=192.168.43.1/24
LOG=/data/c3-ncm.log
CONTINUE=/data/continue.sh
NCM_HOOK='/data/c3-ncm-persist.sh || true'
SELF="$(readlink -f "$0")"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
say_log() { say "$*" | tee -a "$LOG" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  if sudo -n true 2>/dev/null; then
    say_log "re-exec as root: $SELF"
    exec sudo -n -E bash "$SELF" "$@"
  fi
  say_log "FATAL: 需要 root"
  exit 1
fi

mkdir -p /sys/kernel/config 2>/dev/null || true
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config || true

# ★ 安全闸：c3ncm 已存在 → 拒绝，绝不走 rmdir 重建路径。
if [ -d "$G" ]; then
  say_log "拒绝：$G 已存在。重建 c3ncm 会 rmdir 旧 ncm 实例并触发 f_ncm 内核 BUG。"
  say_log "      若只是要恢复链路，请直接 reboot（开机脚本会重建它）。"
  exit 2
fi

# 让 dwc3 进 peripheral 模式（NCM 需要）
echo peripheral > /sys/devices/platform/soc/a600000.ssusb/mode 2>/dev/null || true

# 释放 UDC：只解绑我们自己建的 ffs gadget，以及任何别的已绑 gadget（不 rmdir）
for g in /sys/kernel/config/usb_gadget/*; do
  [ -d "$g" ] || continue
  if [ -n "$(cat "$g/UDC" 2>/dev/null)" ]; then
    say_log "unbind $g"
    echo '' > "$g/UDC" 2>/dev/null || true
  fi
done
pkill -f '[b]ulk_keeper_root' 2>/dev/null || true
umount -l /dev/ffs-c3bulk 2>/dev/null || true
rmdir /dev/ffs-c3bulk 2>/dev/null || true

# 全新建 c3ncm（目录不存在，零 rmdir / 零 symlink 复用）
mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409" || { say_log "FATAL: mkdir 失败"; exit 1; }
echo 0x1d6b | tee "$G/idVendor" >/dev/null
echo 0x0104 | tee "$G/idProduct" >/dev/null
echo 0x0100 | tee "$G/bcdDevice" >/dev/null
echo 0x0310 | tee "$G/bcdUSB" >/dev/null
printf '%s\n' "$(cut -c1-16 < /proc/sys/kernel/random/boot_id)" | tee "$G/strings/0x409/serialnumber" >/dev/null
echo comma  | tee "$G/strings/0x409/manufacturer" >/dev/null
echo C3-NCM | tee "$G/strings/0x409/product" >/dev/null
echo NCM    | tee "$G/configs/c.1/strings/0x409/configuration" >/dev/null
echo 250    | tee "$G/configs/c.1/MaxPower" >/dev/null
echo 0x80   | tee "$G/configs/c.1/bmAttributes" >/dev/null
mkdir -p "$G/functions/ncm.usb0"
ln -sf "$G/functions/ncm.usb0" "$G/configs/c.1/ncm.usb0"
echo "$UDC" | tee "$G/UDC" >/dev/null

for _ in $(seq 1 30); do [ -e /sys/class/net/usb0 ] && break; sleep 0.2; done
if [ ! -e /sys/class/net/usb0 ]; then
  say_log "FATAL: usb0 未出现"
  exit 1
fi
ip addr flush dev usb0 2>/dev/null || true
ip addr add "$CIDR" dev usb0
ip link set usb0 up
say_log "OK: c3ncm 已建并绑定 udc=$UDC，usb0=$CIDR state=$(cat "/sys/class/udc/$UDC/state" 2>/dev/null)"

# 把 NCM 开机钩子写回 /data/continue.sh（幂等；必须在 accelerator-persist 块之外）
if [ -f "$CONTINUE" ]; then
  if grep -qF "$NCM_HOOK" "$CONTINUE"; then
    say_log "continue.sh 已有 NCM 钩子，跳过"
  else
    cp -p "$CONTINUE" "$CONTINUE.bak.bulkrollback"
    tmp="$(mktemp /data/.continue.rb.XXXXXX)"
    if grep -q '^# <<< accelerator-persist <<<$' "$CONTINUE"; then
      sed '/^# <<< accelerator-persist <<<$/a '"$NCM_HOOK" "$CONTINUE" > "$tmp"
    else
      sed '/^exec /i '"$NCM_HOOK" "$CONTINUE" > "$tmp"
    fi
    chmod --reference="$CONTINUE" "$tmp"
    mv -f "$tmp" "$CONTINUE"
    say_log "continue.sh 已写回 NCM 钩子（备份 $CONTINUE.bak.bulkrollback）"
  fi
else
  say_log "WARN: $CONTINUE 不存在，跳过钩子写回"
fi
exit 0
