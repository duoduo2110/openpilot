#!/usr/bin/env bash
# 「切到 bulk」的运行期入口 —— 实际只做一件事：跑 c3-bulk-gadget.sh。
#
# 历史背景：这里曾经是一个运行期 NCM↔bulk 切换脚本（unbind c3ncm → 建 ffs）。
# 现在**不做运行期切换**：c3ncm 的 f_ncm 有内核 BUG（rmdir/symlink 触碰 ncm 实例
# 会触发 `ncm_free_inst` 空指针 + configfs 死锁），所以唯一安全的切换方式是重启，
# 让开机脚本从头只建 ffs。本脚本保留为「重跑一次建 gadget」的运维入口
# （例如 keeper 意外挂掉后不重启也能拉回来），语义因此与开机路径完全一致。
#
# 幂等；失败恒返回 0。日志：/data/accelerator/bulk-gadget.log
set -u
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$SELF_DIR/c3-bulk-gadget.sh"
if [ ! -x "$TARGET" ]; then
  echo "bulk_switch: 找不到 $TARGET" >&2
  exit 0
fi
exec "$TARGET" "$@"
