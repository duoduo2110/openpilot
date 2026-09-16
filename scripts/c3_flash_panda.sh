#!/usr/bin/env bash
# C3 内置 Panda（DOS / STM32F4）ROM bootloader 诊断与 bootstub 重刷
#
# 只面向：Comma Three + 内置单 Panda（DOS/F4，hw_type 0x06），不考虑 C3XL/外置/多 Panda。
#
# 背景：内置 Panda 的 bootloader 经 SPI 访问，且进入 ROM bootloader 需厂商验证过的
#       GPIO 时序（RST_N=1, BOOT0=1, 0.2s, RST_N=0, 1s）。常规 0xd1 进 bootstub 在
#       某些固件下不响应，因此设备变砖/固件损坏时必须走这条路。
#
# 来源：按可用的 C3 参考分支里的厂商脚本 flash_bootstub.sh 的做法重建，并把目标
#       改为 F4（bootstub 0x08000000 / bootstub.panda.bin）；刷写调用 panda 库自带
#       的 PandaDFU.recover()（会自动按 MCU 选择 bootstub 文件与地址）。
# ⚠️ 本脚本是重建版：尚未在设备上验证。先跑 --check（只读）确认能进 ROM bootloader，
#    确认无误再用 --flash。刷写前请先确认 /data/openpilot 已完成构建（有 bootstub 产物）。
#
# 用法（设备上、仓库根目录，需要 sudo 写 GPIO）：
#   bash scripts/c3_flash_panda.sh --check    # 只读：进 ROM bootloader 读 MCU 类型，然后恢复
#   bash scripts/c3_flash_panda.sh --flash    # 写 F4 bootstub，之后需给 Panda 完整断电重上电
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

RST_PIN=124   # STM_RST_N
BOOT0_PIN=134 # STM_BOOT0

MODE="${1:---check}"
[[ "$MODE" == "--check" || "$MODE" == "--flash" ]] || { echo "用法: $0 [--check|--flash]"; exit 2; }

PY="${PY:-/usr/local/venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"

gpio_path() { echo "/sys/class/gpio/gpio$1/value"; }

gpio_prepare() {
  local pin
  for pin in "$RST_PIN" "$BOOT0_PIN"; do
    if [[ ! -e "$(gpio_path "$pin")" ]]; then
      echo "  gpio$pin 未导出，尝试导出 …"
      echo "$pin" | sudo tee "/sys/class/gpio/export" >/dev/null 2>&1 || true
      sleep 0.3
      echo out | sudo tee "/sys/class/gpio/gpio$pin/direction" >/dev/null 2>&1 || true
    fi
    [[ -e "$(gpio_path "$pin")" ]] || { echo "  [!!] gpio$pin 不可用（GPIO sysfs 未提供）"; return 1; }
  done
  return 0
}

gpio_write() { echo "$2" | sudo tee "$(gpio_path "$1")" >/dev/null; }
gpio_read()  { cat "$(gpio_path "$1")" 2>/dev/null; }

enter_rom_bootloader() {
  echo ">> 进入 ROM bootloader（BOOT0 保持高）…"
  gpio_write "$RST_PIN" 1
  gpio_write "$BOOT0_PIN" 1
  sleep 0.2
  gpio_write "$RST_PIN" 0
  sleep 1
}

exit_rom_bootloader() {
  echo ">> 恢复正常启动（BOOT0=0 + 复位）…"
  gpio_write "$BOOT0_PIN" 0
  gpio_write "$RST_PIN" 1
  sleep 0.3
  gpio_write "$RST_PIN" 0
  sleep 0.5
}

echo "== C3 Panda ROM bootloader 工具（$MODE）=="
echo "  仓库: $DIR"
gpio_prepare || { echo "GPIO 不可用，终止（不尝试刷写）"; exit 1; }
echo "  初始电平: RST_N=$(gpio_read "$RST_PIN")  BOOT0=$(gpio_read "$BOOT0_PIN")"
echo "  固件产物: $(ls -l panda/board/obj/bootstub.panda.bin 2>/dev/null | awk '{print $5" bytes"}' || echo '缺失（需先 scons）')"

enter_rom_bootloader

echo ">> 探测 STM32 ROM bootloader（SPI/USB 自动）…"
PYTHONPATH="$DIR" timeout 90 "$PY" - <<'PYEOF'
import sys
from panda.python.dfu import PandaDFU

try:
  dfu = PandaDFU(None)
except Exception as e:
  print(f"  [!!] 连接 ROM bootloader 失败: {type(e).__name__}: {e}")
  print("       说明：时序不对 / Panda 未供电 / 已处于其它状态（如停在应用态或损坏）")
  sys.exit(3)

try:
  print(f"  [OK] 已连上 ROM bootloader: mcu_type={dfu.get_mcu_type()}")
except Exception as e:
  print(f"  [!!] 连接后读取 MCU 类型失败: {type(e).__name__}: {e}")
  sys.exit(4)
finally:
  try:
    dfu.close()
  except Exception:
    pass
PYEOF
probe_rc=$?

if [[ "$MODE" == "--check" ]]; then
  exit_rom_bootloader
  echo
  if [[ $probe_rc -eq 0 ]]; then
    echo "== 结论：ROM bootloader 可达（Panda 硬件与 SPI 链路正常）=="
    echo "   若此时系统里仍「panda 否」，问题更可能在应用固件/枚举，而不是硬件链路。"
  else
    echo "== 结论：ROM bootloader 不可达（退出码 $probe_rc）=="
    echo "   依次检查：Panda 供电、GPIO 时序、SPI 设备（ls -l /dev/spidev*）、是否已损坏。"
  fi
  exit $probe_rc
fi

# --flash：用 panda 库自带的 recover()（按 MCU 选 bootstub 文件 + 擦 sector 0/1 + 写入 + 跳转）
echo ">> 写入 F4 bootstub（PandaDFU.recover）…"
PYTHONPATH="$DIR" timeout 180 "$PY" - <<'PYEOF'
import sys
from panda.python.dfu import PandaDFU

try:
  dfu = PandaDFU(None)
except Exception as e:
  print(f"  [!!] 无法连接 ROM bootloader: {type(e).__name__}: {e}")
  sys.exit(3)

try:
  mcu = dfu.get_mcu_type()
  if mcu.name != "F4":
    print(f"  [!!] 检测到 {mcu.name}，本脚本面向内置 DOS/F4，已中止（避免写错固件）")
    sys.exit(5)
  dfu.recover()
  print("  [OK] bootstub 已写入并跳转（panda 现在应运行 bootstub 的 soft flasher）")
except Exception as e:
  print(f"  [!!] 写入失败: {type(e).__name__}: {e}")
  sys.exit(6)
finally:
  try:
    dfu.close()
  except Exception:
    pass
PYEOF
flash_rc=$?

exit_rom_bootloader
echo
if [[ $flash_rc -eq 0 ]]; then
  echo "== bootstub 写入完成 =="
  echo "   下一步（必须）：给 Panda 完整断电再上电（断供电 2~3 秒），"
  echo "   上电后 bootstub 的 soft flasher 会闪烁绿灯，pandad 启动时会自动刷 F4 应用固件。"
  echo "   然后运行：bash scripts/c3_deploy_verify.sh  或直接重启 comma.service 后看 pandaStates。"
else
  echo "== 写入未成功（退出码 $flash_rc）=="
fi
exit $flash_rc
