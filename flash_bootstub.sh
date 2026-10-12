#!/bin/bash
# ============================================================
# 手动把 bootstub 烧进 SPI panda (STM32H725) 的 flash
# 适用: 新 panda / panda 固件损坏 / 需要重刷 bootstub 时
# 用法: ssh comma@<设备IP> 'bash /home/comma/flash_bootstub.sh'
# 注意: 烧完必须给 panda 完整断电再上电!
# ============================================================
export PATH=/usr/local/venv/bin:$PATH
export PYTHONPATH=/data/openpilot
cd /data/openpilot || { echo "ERROR: /data/openpilot not found"; exit 1; }

if [ ! -f panda/board/obj/bootstub.panda_h7.bin ]; then
  echo "ERROR: bootstub.panda_h7.bin not found (need to build first: scons)"
  exit 1
fi
echo "bootstub file: $(ls -la panda/board/obj/bootstub.panda_h7.bin | awk '{print $5}') bytes"
echo "firmware version: $(cat panda/board/obj/version 2>/dev/null)"

# 1) 进入 ROM bootloader: RST复位 + BOOT0=1 (保持高)
echo ">> entering ROM bootloader (BOOT0=1)..."
echo 1 > /sys/class/gpio/gpio124/value
echo 1 > /sys/class/gpio/gpio134/value
sleep 0.2
echo 0 > /sys/class/gpio/gpio124/value
sleep 1

# 2) 单 handle 分块刷写 bootstub (带重试 + 读回校验)
python3 << 'PYEOF'
import time, sys
from panda.python.spi import STBootloaderSPIHandle
from panda.python.constants import FW_PATH, McuType
import os

BS = 256
fn = os.path.join(FW_PATH, McuType.H7.config.bootstub_fn)
code = open(fn, "rb").read()
nblocks = (len(code) + BS - 1) // BS
print(f"bootstub {len(code)} bytes, {nblocks} blocks")

def w(pin, val):
    with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
        f.write(str(val))

for attempt in range(30):
    # reset with BOOT0 held high (each retry)
    w(124, 1); w(134, 1); time.sleep(0.2); w(124, 0); time.sleep(1)
    try:
        h = STBootloaderSPIHandle()
    except Exception:
        print(f"  attempt {attempt}: connect fail")
        continue
    print(f"  attempt {attempt}: connected, chipid={hex(h.get_chip_id())}")
    for round_ in range(8):
        try:
            h.erase_sector(0); h.erase_sector(1)
        except Exception as e:
            print(f"    erase err {e}")
            break
        ok = True
        for i in range(nblocks):
            block = code[i*BS:(i+1)*BS].ljust(BS, b"\xFF")
            addr = 0x08000000 + i*BS
            success = False
            for r in range(6):
                try:
                    h.program(addr, block)
                    if h.read(addr, BS) == block:
                        success = True
                        break
                except Exception:
                    pass
            if not success:
                ok = False
                print(f"    round {round_}: block {i} failed")
                break
        if ok:
            v = h.read(0x08000000, 32)
            print(f"  *** BOOTSTUB FLASH SUCCESS *** head={v.hex()}")
            sys.exit(0)
    try:
        h.close()
    except Exception:
        pass

print("FLASH FAILED after all retries")
sys.exit(1)
PYEOF

# 3) 恢复正常模式 (BOOT0=0)
echo 0 > /sys/class/gpio/gpio134/value
echo 1 > /sys/class/gpio/gpio124/value
sleep 0.3
echo 0 > /sys/class/gpio/gpio124/value

echo ""
echo "=============================================================="
echo " bootstub 已烧入 flash!"
echo " 现在必须给 panda 完整断电再上电:"
echo "   拔掉 panda 电源 / 关掉供电开关, 等 2~3 秒, 再上电"
echo " 上电后 panda 绿灯会闪烁(soft flasher), pandad 会自动刷 app"
echo "=============================================================="
