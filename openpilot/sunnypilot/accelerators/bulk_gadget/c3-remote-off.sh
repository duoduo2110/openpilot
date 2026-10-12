#!/usr/bin/env bash
# C3 侧「一键停用远端外置大模型」—— 车上救命用的一条命令。
#
# 做的事只有一件：把 /data/models/accelerator.json 的 enabled 置 false（原子写）。
#
# 生效方式（很重要，决定你什么时候算停下来）：
#   * **不需要重启 modeld / 不需要重启 C3。** JoinedModel 每帧最多每 1.0s
#     重读一次 accelerator.json 的 enabled（remote/joined.py:52 ENABLED_REFRESH_SECONDS），
#     所以最坏 1 秒后本帧就走回本地小模型。
#   * 开关切到本地后，链路是**单向 latch**：本点火周期内不会再自动接回远端。
#     想要远端必须人工把 enabled 改回 true（重新跑本脚本 on，或用 UI 切换）。
#   * 如果此刻 modeld 根本没跑（openpilot 没起），改配置同样有效——下次起来就是本地。
#
# 用法:
#   c3-remote-off.sh          # 停用（enabled=false）
#   c3-remote-off.sh on       # 重新启用（enabled=true）
#   c3-remote-off.sh status   # 只看当前开关/传输/调试日志，不改
set -u

CFG="${ACCELERATOR_CONFIG_PATH:-/data/models/accelerator.json}"
OP="${1:-off}"
PY=/usr/local/venv/bin/python3
[ -x "$PY" ] || PY="$(command -v python3 || true)"

if [ -z "$PY" ]; then echo "FATAL: 找不到 python3" >&2; exit 1; fi

"$PY" - "$CFG" "$OP" <<'PYEOF'
import json
import os
import sys

cfg_path, op = sys.argv[1], sys.argv[2]
sys.path.insert(0, '/data/openpilot')
try:
  from openpilot.sunnypilot.accelerators.remote import config as C
except Exception as e:  # noqa: BLE001
  print(f'FATAL: 无法导入 accelerator config 模块: {e!r}', file=sys.stderr)
  raise SystemExit(2)

if op == 'status':
  cfg = C.load(cfg_path)
  print(json.dumps({
    'path': cfg_path, 'enabled': cfg['enabled'], 'transport': cfg['transport'],
    'debug_log': cfg['debug_log'], 'deadline_ms': cfg['deadline_ms'],
    'address': cfg['address'], 'pipeline_depth': cfg['pipeline_depth'],
  }, ensure_ascii=False, indent=2, sort_keys=True))
  raise SystemExit(0)

if op not in ('on', 'off'):
  print(f'用法: {sys.argv[0]} [on|off|status]', file=sys.stderr)
  raise SystemExit(1)

cfg = C.load(cfg_path)
before = cfg['enabled']
cfg['enabled'] = (op == 'on')
if not C.save(cfg, cfg_path):
  print(f'FATAL: 写 {cfg_path} 失败（权限/只读/磁盘满）', file=sys.stderr)
  raise SystemExit(3)

after = C.enabled(cfg_path)
print(f'{cfg_path}: enabled {before} -> {after}  (transport={C.transport(cfg_path)} '
      f'debug_log={C.debug_log(cfg_path)})')
if op == 'off':
  print('已停用远端：modeld 最迟 1s 内回落到本地小模型；本点火周期不会再自动接回。')
else:
  print('已启用远端：modeld 会在停车且未接管时按退避重试接入（可能需要等几秒到一分钟）。')
raise SystemExit(0 if after == cfg['enabled'] else 4)
PYEOF
