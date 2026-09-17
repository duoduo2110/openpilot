#!/usr/bin/env bash
# C3（Comma Three + 内置单 DOS/F4 Panda）部署与自检
#
# 只面向：普通 C3 + 内置单 Panda（DOS/F4，走 USB），不考虑 C3XL/外置 USB/多 Panda。
#
# 用法（在设备上、仓库根目录执行）：
#   bash scripts/c3_deploy_verify.sh              # 自检 + 构建（不重启服务）
#   bash scripts/c3_deploy_verify.sh --restart     # 自检 + 构建 + 重启 comma.service
#   SKIP_BUILD=1 bash scripts/c3_deploy_verify.sh  # 只自检，不重新构建
#
# 退出码：0 全部通过；1 有未通过项（未通过前不要上车）。
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

RESTART=0
[[ "${1:-}" == "--restart" ]] && RESTART=1

# 设备上的 scons / python 在 /usr/local/venv
[[ -x /usr/local/venv/bin/scons ]] && export PATH="/usr/local/venv/bin:$PATH"

FAIL=()
ok()   { echo "  [OK] $*"; }
bad()  { echo "  [!!] $*"; FAIL+=("$*"); }
info() { echo; echo "== $*"; }

info "0) 环境"
echo "  仓库: $DIR"
if [[ -f /AGNOS ]]; then echo "  设备: comma three (AGNOS)"; else echo "  注意: 未检测到 /AGNOS（本脚本面向设备）"; fi
echo "  scons: $(command -v scons || echo '未找到')"

info "1) 顶层包软链（设备上 PYTHONPATH 只有仓库根，缺一即可能 capnp 中止）"
for pair in "msgq:msgq_repo/msgq" \
            "opendbc:opendbc_repo/opendbc" \
            "rednose:rednose_repo/rednose" \
            "teleoprtc:teleoprtc_repo/teleoprtc" \
            "tinygrad:tinygrad_repo/tinygrad" \
            "cereal:openpilot/cereal"; do
  name="${pair%%:*}"; target="${pair#*:}"
  if [[ -e "$name" || -L "$name" ]]; then
    ok "$name 已存在"
  else
    if ln -sfn "$target" "$name"; then ok "$name -> $target 已创建"; else bad "$name 软链创建失败"; fi
  fi
done

info "2) 导入自检（任一失败都会在设备上以 kj 'Duplicate ID' 中止进程/构建）"
PY="${PY:-/usr/local/venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"
check_import() {  # $1=code $2=label
  if PYTHONPATH="$DIR" timeout 90 "$PY" -c "$1" >/dev/null 2>&1; then ok "$2"; else bad "$2"; fi
}
check_import "import opendbc.car.structs; from openpilot.cereal import log" "顺序 A: opendbc 先、cereal 后"
check_import "from openpilot.cereal import log; import opendbc.car.structs" "顺序 B: cereal 先、opendbc 后"
check_import "import cereal, openpilot.cereal, opendbc.car.structs"         "顺序 C: 两个名字同时使用"
check_import "import cereal; from cereal import car"                         "顺序 D: from cereal import car"
for name in modeld selfdrived controlsd card paramsd pandad trafficcontrold; do
  mod=$(grep -oE "PythonProcess\(\"$name\", \"[a-z_.0-9]+\"" openpilot/system/manager/process_config.py 2>/dev/null | sed 's/.*, "//;s/"//')
  [[ -z "$mod" ]] && continue
  check_import "import $mod" "进程模块可导入: $name"
done

info "3) 构建（scons -j$(nproc)）"
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  # 设备使用共享 scons 缓存（/data/scons_cache）。launch_chffrplus.sh 也会先清掉
  # 这个锁；遗留的 config.lock 会让构建一直卡住，所以这里照做。
  [[ -d /data/scons_cache ]] && rm -f /data/scons_cache/config.lock
  # 与设备既有构建配方一致（线程限制等），缺失时跳过。
  # shellcheck disable=SC1091
  [[ -f "$DIR/launch_env.sh" ]] && source "$DIR/launch_env.sh"
  build_log=/data/c3_build.log
  if scons -j"$(nproc)" > "$build_log" 2>&1; then
    ok "scons 成功（日志 $build_log）"
  else
    bad "scons 失败（日志 $build_log 尾部）"
    tail -n 15 "$build_log" | sed 's/^/      /'
  fi
else
  echo "  跳过构建（SKIP_BUILD=1）"
fi

info "4) 关键产物（缺失即不要上车）"
for f in panda/board/obj/panda.bin.signed \
         panda/board/obj/bootstub.panda.bin \
         openpilot/selfdrive/pandad/pandad; do
  if [[ -e "$f" ]]; then ok "$f"; else bad "缺失: $f"; fi
done

# 模型产物：设备（comma_arm64）只为它自己那颗摄像头编译 dm_warp，因此按“类”判断，
# 而不是要求两个分辨率都在。缺了模型 modeld 起不来 → 校准会一直停在 0%。
M="openpilot/selfdrive/modeld/models"
chk_count() {  # $1=glob $2=描述
  local n
  n=$(ls "$M"/$1 2>/dev/null | wc -l)
  if [[ "$n" -gt 0 ]]; then ok "$2 ($n 个)"; else bad "缺失: $2（modeld 无法启动，校准会停在 0%）"; fi
}
chk_count "driving_tinygrad.pkl*" "driving_tinygrad.pkl*"
chk_count "dmonitoring_model_tinygrad.pkl*" "dmonitoring_model_tinygrad.pkl*"
chk_count "dm_warp_*_tinygrad.pkl" "dm_warp_*_tinygrad.pkl"

info "5) 结果"
if [[ ${#FAIL[@]} -eq 0 ]]; then
  echo "  全部通过。"
  if [[ "$RESTART" == "1" ]]; then
    echo "  重启 comma.service …"
    if sudo systemctl restart comma.service; then echo "  已重启"; else echo "  重启失败，请手动检查"; fi
  fi
  echo "  提醒：Panda 固件刷写有独立流程（pandad.py 自动刷写；失败时用 GPIO 时序 + PandaDFU），本脚本不刷固件。"
else
  echo "  有 ${#FAIL[@]} 项未通过："
  printf '   - %s\n' "${FAIL[@]}"
  echo "  未通过前不要上车。"
  exit 1
fi
