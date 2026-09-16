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
echo "== 关键进程 =="
for pat in "manager.py" "selfdrive.selfdrived.selfdrived" "selfdrive.car.card" "pandad"; do
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
