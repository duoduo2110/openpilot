#!/usr/bin/env bash
# C3 上车诊断快照（只读，不改变任何状态）
#
# 用途：刷机/上车后一条命令拿到判读所需证据——车型识别、点火状态、Panda 状态、
#       selfdriveState 是否在发布（决定屏幕是否显示「sunnypilot 不可用」）、
#       关键进程是否在跑、最近错误汇总。
#
# 用法（设备上，仓库根目录）：
#   bash scripts/c3_onroad_check.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
PY="${PY:-/usr/local/venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"

echo "== 版本 =="
echo "  时间: $(date '+%F %T')"
echo "  AGNOS: $(cat /AGNOS 2>/dev/null || echo '(无 /AGNOS)')"
echo "  代码: $(git rev-parse --abbrev-ref HEAD 2>/dev/null) @ $(git log -1 --format='%h %s' 2>/dev/null)"

echo
echo "== 运行时快照（最多等 10s 收数）=="
PYTHONPATH="$DIR" timeout 90 "$PY" - <<'PYEOF'
import time
import openpilot.cereal.messaging as messaging

sm = messaging.SubMaster(['pandaStates', 'carParams', 'deviceState', 'selfdriveState'])
deadline = time.monotonic() + 10.0
while time.monotonic() < deadline:
  sm.update(200)
  if sm.valid['pandaStates'] and sm.valid['selfdriveState']:
    break

if sm.valid['deviceState']:
  started = bool(sm['deviceState'].started)
  print(f"  deviceState.started = {started}   (True=已进入 onroad，False=offroad)")
else:
  print("  deviceState: 未收到（manager/hardwared 可能没起来）")

if sm.valid['carParams']:
  cp = sm['carParams']
  print(f"  carParams: fingerprint={cp.carFingerprint}  brand={cp.brand}  notCar={cp.notCar}")
else:
  print("  carParams: 未收到 ← card 没产出（车型识别未完成）")

if sm.valid['pandaStates']:
  for p in sm['pandaStates']:
    print(f"  panda: type={p.pandaType} safety={p.safetyModel} controlsAllowed={p.controlsAllowed} "
          f"heartbeatLost={p.heartbeatLost} faultStatus={p.faultStatus} faults={list(p.faults)}")
else:
  print("  pandaStates: 未收到 ← 内置 Panda 无通信")

if sm.valid['selfdriveState']:
  age = time.monotonic() - sm.recv_time['selfdriveState']
  print(f"  selfdriveState: 已收到（{age:.1f}s 前）enabled={bool(sm['selfdriveState'].enabled)}  ← 正常")
else:
  print("  selfdriveState: 未收到  ← 这就是屏幕显示「sunnypilot 不可用」的直接原因")
PYEOF

echo
echo "== Panda 链路枚举（判断是硬件链路还是应用固件问题）=="
echo "  USB 设备（正常应用态应为 3801:ddcc；bootstub/DFU 是别的 id）:"
if command -v lsusb >/dev/null 2>&1; then
  lsusb 2>/dev/null | grep -iE "3801|stm|dfu|0483" | sed 's/^/    /' || echo "    (未发现 3801:xxxx / ST 相关 USB 设备)"
else
  for d in /sys/bus/usb/devices/*; do
    [[ -f "$d/idVendor" ]] || continue
    v="$(cat "$d/idVendor" 2>/dev/null)"; p="$(cat "$d/idProduct" 2>/dev/null)"
    [[ "$v" == "3801" || "$v" == "0483" ]] && echo "    $v:$p  ($(basename "$d"))"
  done
  echo "    (无 lsusb，已用 /sys/bus/usb 兜底)"
fi
echo "  /dev 节点:"
ls -l /dev/ttyACM* /dev/spidev* 2>/dev/null | sed 's/^/    /' || echo "    (无 ttyACM* / spidev*)"
echo "  GPIO（124=STM_RST_N, 134=STM_BOOT0）:"
for pin in 124 134; do
  if [[ -e "/sys/class/gpio/gpio$pin/value" ]]; then
    echo "    gpio$pin = $(cat "/sys/class/gpio/gpio$pin/value" 2>/dev/null)"
  else
    echo "    gpio$pin 未导出"
  fi
done

echo
echo "== onroad 关键信号（排查校准/车速/模型）=="
PYTHONPATH="$DIR" timeout 90 "$PY" - <<'PYEOF'
import time
import openpilot.cereal.messaging as messaging

sm = messaging.SubMaster(['carState', 'extrinsicsCalibration', 'modelV2', 'selfdriveState', 'deviceState'])
deadline = time.monotonic() + 8.0
while time.monotonic() < deadline:
  sm.update(200)
  if sm.valid['carState'] and sm.valid['extrinsicsCalibration']:
    break

if sm.valid['carState']:
  cs = sm['carState']
  print(f"  车速 vEgo={cs.vEgo:.2f} m/s ({cs.vEgo * 2.23694:.1f} mph)  vEgoRaw={cs.vEgoRaw:.2f} m/s"
        f"  standstill={cs.standstill}  gear={cs.gearShifter}  gasPressed={cs.gasPressed}")
  print("  ← 校准要求 vEgo > 6.71 m/s（15 mph）；若开车时这里长期≈0，说明 CAN 车速没解析出来")
else:
  print("  carState: 未收到（card 未产出车辆状态）")

if sm.valid['extrinsicsCalibration']:
  ec = sm['extrinsicsCalibration']
  print(f"  校准 validBlocks={ec.validBlocks}  status={ec.status}  rpyCalib={[round(float(x), 4) for x in ec.rpyCalib]}")
  print("  ← validBlocks 随有效校准块累积；长期为 0 说明没拿到有效车速/相机位姿")
else:
  print("  extrinsicsCalibration: 未收到（calibrationd 未产出）")

if sm.valid['modelV2']:
  age = time.monotonic() - sm.recv_time['modelV2']
  print(f"  modelV2: 已收到（{age:.1f}s 前）← 模型在跑（校准需要它的相机位姿）")
else:
  print("  modelV2: 未收到 ← modeld 没在产出（会导致校准无法推进）")

if sm.valid['selfdriveState']:
  ss = sm['selfdriveState']
  a1 = getattr(ss, 'alertText1', '') or getattr(ss, 'alertText', '')
  print(f"  selfdriveState: enabled={ss.enabled} state={ss.state} 提示={a1!r}")
PYEOF

echo
echo "== 模型产物（modeld 依赖；缺则校准拿不到位姿）=="
M="openpilot/selfdrive/modeld/models"
drv=$(ls "$M"/driving_tinygrad.pkl* 2>/dev/null | wc -l)
dm=$(ls "$M"/dmonitoring_model_tinygrad.pkl* 2>/dev/null | wc -l)
warp=$(ls "$M"/dm_warp_*_tinygrad.pkl 2>/dev/null | wc -l)
echo "  driving_tinygrad.pkl*        : $drv 个"
echo "  dmonitoring_model_tinygrad*  : $dm 个"
echo "  dm_warp_*_tinygrad.pkl       : $warp 个"
echo "  （设备只编译自己那颗摄像头对应的 dm_warp，1 个即为正常）"
if [[ "$drv" -gt 0 && "$dm" -gt 0 && "$warp" -gt 0 ]]; then
  echo "  [OK] 三类模型产物齐全"
else
  echo "  [!!] 有缺失 → modeld 无法启动，校准会一直停在 0%"
fi
echo "  目录内容："
ls -l "$M" 2>/dev/null | sed 's/^/    /'

echo
echo "== 关键进程 =="
for pat in "manager.py" "selfdrive.selfdrived.selfdrived" "selfdrive.car.card" "selfdrive.locationd.calibrationd" "selfdrive.modeld.modeld" "pandad"; do
  # pgrep -c 无匹配时仍会打印 0，但退出码非零；因此只取 stdout，不要加 || 兜底。
  n="$(pgrep -c -f "$pat" 2>/dev/null | head -1)"
  n="${n:-0}"
  printf '  %-34s %s\n' "$pat" "$([[ "$n" -gt 0 ]] && echo "运行中 ($n)" || echo "未运行")"
done

echo
echo "== 最近错误汇总（最新 swaglog 的 ERROR）=="
LOG="$(ls -t /data/log/swaglog.* 2>/dev/null | head -1)"
if [[ -n "${LOG:-}" ]]; then
  echo "  日志: $LOG"
  sudo grep -a '"level": "ERROR"' "$LOG" 2>/dev/null | timeout 30 "$PY" -c '
import sys, json, collections
c = collections.Counter()
for line in sys.stdin:
    try:
        c[json.loads(line).get("filename", "?")] += 1
    except Exception:
        pass
if not c:
    print("    (无 ERROR)")
for k, v in c.most_common(10):
    print(f"    {v:5d}  {k}")
' || echo "    (读取失败，可忽略)"
  echo "  提示：card 崩溃会显示为 openpilot.selfdrive.car.card 计数偏高。"
else
  echo "  (未找到 /data/log/swaglog.*)"
fi
