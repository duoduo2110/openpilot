#!/usr/bin/env bash
# C3 开机默认传输 = vendor bulk（FunctionFS, ffs）。
#
# 为什么是「开机就 bulk」而不是运行时在 NCM/bulk 之间切：
#   c3ncm 那条 f_ncm 内核实现有 BUG —— 任何 rmdir / symlink 触碰 c3ncm 的 ncm 实例
#   都会触发 `ncm_free_inst` 空指针解引用，并在 configfs 上死锁。所以这里
#   **只建 ffs、既不创建也不删除 c3ncm**：开机时唯一的 gadget 就是 ffs，
#   运行时根本不存在「切换」这个动作，从而绕开该 BUG。
#
# 幂等：已绑定 UDC 且 keeper 在跑 → 直接返回；只清理自己建的 c3bulk 目录，
# 对任何其它 gadget 最多解绑 UDC（PoC 验证过的路径），绝不 rmdir/symlink 它。
#
# 失败一律不阻塞开机（恒返回 0），只写日志到 /data/accelerator/bulk-gadget.log。
# 由 /data/continue.sh 在 openpilot 启动前调用（deploy_accelerator.py persist 装钩子）。
set -u

STORE="${ACCELERATOR_STORE:-/data/accelerator}"
G=/sys/kernel/config/usb_gadget/c3bulk
FFS_NAME=c3bulk
FFS_MOUNT=/dev/ffs-c3bulk
LOG="$STORE/bulk-gadget.log"
KEEPER_WAIT="${BULK_KEEPER_WAIT:-40}"
SELF="$(readlink -f "$0")"
REL_KEEPER=openpilot/sunnypilot/accelerators/bulk_gadget/bulk_keeper_root.py
# keeper 的 systemd 单元（独立于 openpilot 的 cgroup，见下面第 6 步的说明）
REL_UNIT=openpilot/sunnypilot/accelerators/bulk_gadget/c3-bulk-keeper.service

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
say_log() { say "$*" | tee -a "$LOG" >&2; }

mkdir -p "$STORE" 2>/dev/null || true

# configfs 写入与 functionfs 挂载都要 root；continue.sh 场景一般是 comma。
if [ "$(id -u)" -ne 0 ]; then
  if sudo -n true 2>/dev/null; then
    say_log "re-exec as root: $SELF"
    exec sudo -n -E bash "$SELF" "$@"
  fi
  say_log "FATAL: 需要 root（configfs + functionfs），放弃建 bulk gadget"
  exit 0
fi

# gadget 名/挂载点必须与 bulk_transport.py 读到的一致；modeld 侧由
# /data/continue.sh 导出同名环境变量（BULK_GADGET / BULK_MOUNT / BULK_UDC）。
export BULK_GADGET="$G"
export BULK_MOUNT="$FFS_MOUNT"
UDC="${BULK_UDC:-$(ls /sys/class/udc 2>/dev/null | head -n 1)}"
export BULK_UDC="$UDC"
say_log "start gadget=$G mount=$FFS_MOUNT udc=${UDC:-<none>}"

mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config || true

# 幂等短路：已绑定 + keeper 在跑。
if [ -n "$(cat "$G/UDC" 2>/dev/null)" ] && pgrep -f '[b]ulk_keeper_root' >/dev/null 2>&1; then
  say_log "already up: udc=$(cat "$G/UDC") state=$(cat "/sys/class/udc/${UDC:-none}/state" 2>/dev/null)"
  exit 0
fi

# 1) 停掉上一轮的 keeper（只杀自己那一个）
pkill -f '[b]ulk_keeper_root' 2>/dev/null || true

# 2) 清掉自己上一轮建的 c3bulk（幂等；不碰 c3ncm）
if [ -d "$G" ]; then
  echo '' > "$G/UDC" 2>/dev/null || true
  rm -f "$G/configs/c.1/ffs.$FFS_NAME" 2>/dev/null || true
  rmdir "$G/configs/c.1/strings/0x409" "$G/configs/c.1/strings" "$G/configs/c.1" "$G/configs" \
        "$G/functions/ffs.$FFS_NAME" "$G/functions" "$G/strings/0x409" "$G/strings" "$G" 2>/dev/null || true
fi
umount -l "$FFS_MOUNT" 2>/dev/null || true
rmdir "$FFS_MOUNT" 2>/dev/null || true

# 3) UDC 被别的 gadget 占着 → 只解绑（写空 UDC），绝不 rmdir/symlink 它的目录
BUSY=""
for g in /sys/kernel/config/usb_gadget/*; do
  [ -d "$g" ] || continue
  [ "$g" = "$G" ] && continue
  if [ -n "$(cat "$g/UDC" 2>/dev/null)" ]; then BUSY="$g"; break; fi
done
if [ -n "$BUSY" ]; then
  say_log "WARN: UDC 被 $BUSY 占用 -> 只解绑 UDC，不 rmdir/symlink（f_ncm BUG 规避）"
  echo '' > "$BUSY/UDC" 2>/dev/null || say_log "WARN: 解绑 $BUSY 失败"
fi

# 4) 建 ffs gadget（只有 ffs，无 ncm）
mkdir -p "$G" || { say_log "FATAL: mkdir $G 失败"; exit 0; }
cd "$G" || exit 0
printf '0x1209\n' > idVendor            # pid.codes
printf '0x0001\n' > idProduct
printf '0x0320\n' > bcdUSB              # 3.2：宣告 SuperSpeed
printf '0x0100\n' > bcdDevice
printf '0x00\n'   > bDeviceClass
printf '0x00\n'   > bDeviceSubClass
printf '0x00\n'   > bDeviceProtocol
mkdir -p strings/0x409
printf 'comma\n' > strings/0x409/manufacturer
printf 'c3bulk\n' > strings/0x409/product
printf '%s\n' "$(cut -c1-16 < /proc/sys/kernel/random/boot_id)" > strings/0x409/serialnumber
mkdir -p configs/c.1/strings/0x409
printf 'c3bulk link\n' > configs/c.1/strings/0x409/configuration
printf '0xC0\n' > configs/c.1/bmAttributes
printf '8\n'    > configs/c.1/MaxPower
mkdir -p "functions/ffs.$FFS_NAME"
ln -s "$G/functions/ffs.$FFS_NAME" "configs/c.1/ffs.$FFS_NAME"

# 5) 挂 functionfs。UDC 故意不绑：descriptors 必须由打开 ep0 的进程写完再绑，
#    否则 FunctionFS 会在未写 descriptors 时枚举出一个空接口。
mkdir -p "$FFS_MOUNT" || { say_log "FATAL: mkdir $FFS_MOUNT 失败"; exit 0; }
mountpoint -q "$FFS_MOUNT" || mount -t functionfs "$FFS_NAME" "$FFS_MOUNT" \
  || { say_log "FATAL: mount -t functionfs 失败"; exit 0; }
[ -e "$FFS_MOUNT/ep0" ] || { say_log "FATAL: $FFS_MOUNT/ep0 未出现"; exit 0; }
chmod 0755 "$FFS_MOUNT" 2>/dev/null || true

# 6) 起 root keeper：写 descriptors + 绑 UDC + 一直占着 ep0
#    （开 ep0 需要 CAP_ADMIN，所以 keeper 是本方案里唯一跑 root 的东西；
#      modeld 仍以 comma 跑，只开 ep1/ep2。）
#
#    走 **systemd 单元** 而不是 setsid/nohup：keeper 一旦挂在 openpilot 的 cgroup
#    下面，`systemctl restart comma.service`（或任何 openpilot 重启）会连带 SIGTERM
#    掉它；ep0 一放开，UDC 就被解绑、主机侧 1209:0001 消失，而没有任何东西会把它
#    拉回来 —— 实测踩过：重启 comma.service 后 bulk 链路静默永久失效，只能重插线
#    或重启 C3。独立单元 + Restart=always 才是「开机自建 + 掉了自己回来」的正解。
#    单元装在 /run/systemd/system（这台机器 / 是 ro，只有 /run 可写），每次开机
#    由本脚本重装一次；「开机自启」的语义由 continue.sh 这个钩子保证。
PY=/usr/local/venv/bin/python3
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { say_log "FATAL: 找不到 python3"; exit 0; }
KEEPER=""
for c in "$STORE/files/$REL_KEEPER" "/data/openpilot/$REL_KEEPER"; do
  if [ -f "$c" ]; then KEEPER="$c"; break; fi
done
[ -n "$KEEPER" ] || { say_log "FATAL: 找不到 bulk_keeper_root.py"; exit 0; }

KEEPER_UNIT=c3-bulk-keeper.service
KEEPER_UNIT_SRC=""
for c in "$STORE/files/$REL_UNIT" "/data/openpilot/$REL_UNIT"; do
  if [ -f "$c" ]; then KEEPER_UNIT_SRC="$c"; break; fi
done

keeper_started=0
# 这台 C3 的 / 是 ext4 **ro**（/etc/systemd/system、/usr/lib/systemd/system 都写不进去），
# 所以单元装到 /run/systemd/system（tmpfs，systemd 的运行时单元目录）。它不跨重启，
# 但「开机自建」本来就由本脚本在 /data/continue.sh 里跑一遍负责 —— 每次开机都会
# 重新装 + 重启 keeper。enable 在 ro 镜像上必然失败，失败也无所谓（有开机钩子），
# 但在 / 可写的镜像上 enable 能额外多一层保险，所以照做、只容忍失败。
KEEPER_UNIT_DIR=/run/systemd/system
if [ -n "$KEEPER_UNIT_SRC" ] \
   && mkdir -p "$KEEPER_UNIT_DIR" \
   && install -m 0644 "$KEEPER_UNIT_SRC" "$KEEPER_UNIT_DIR/$KEEPER_UNIT" 2>/dev/null \
   && systemctl daemon-reload >/dev/null 2>&1 \
   && { systemctl enable "$KEEPER_UNIT" >/dev/null 2>&1 || true; } \
   && systemctl restart "$KEEPER_UNIT" >/dev/null 2>&1 \
   && systemctl is-active --quiet "$KEEPER_UNIT"; then
  say_log "keeper 已由 systemd 托管: $KEEPER_UNIT (unit=$KEEPER_UNIT_DIR/$KEEPER_UNIT, ExecStart=$PY $KEEPER)"
  keeper_started=1
fi
if [ "$keeper_started" != "1" ]; then
  say_log "WARN: systemd 托管 keeper 不可用，回退 setsid/nohup（无自动重启，openpilot 重启会连带杀掉它）"
  setsid nohup "$PY" "$KEEPER" >>"$LOG" 2>&1 </dev/null &
  say_log "keeper 已启动: $PY $KEEPER (pid=$!)"
fi

# 7) 等 keeper 起来并把 UDC 绑上（绑上≠主机已配置；没插线也要绑成功，
#    keeper 会一直等 configured，插上主机才枚举）
BOUND=""
i=0
while [ "$i" -lt "$((KEEPER_WAIT * 2))" ]; do
  pgrep -f '[b]ulk_keeper_root' >/dev/null 2>&1 || { say_log "FATAL: keeper 进程不存在"; break; }
  BOUND="$(cat "$G/UDC" 2>/dev/null)"
  [ -n "$BOUND" ] && break
  sleep 0.5
  i=$((i + 1))
done
if [ -n "$BOUND" ]; then
  say_log "OK: udc=$BOUND state=$(cat "/sys/class/udc/$BOUND/state" 2>/dev/null) (host configured 与否见 keeper 日志)"
else
  say_log "WARN: ${KEEPER_WAIT}s 内 UDC 未绑定；openpilot 仍照常启动，bulk 链路会重试"
fi
exit 0
