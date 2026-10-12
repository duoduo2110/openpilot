#!/usr/bin/env python3
"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

外置大模型加速器主线的**可回滚部署工具**（stdlib only，本机执行）。

设计原则
--------
* 文件清单**显式声明**：只部署 accelerator 主线涉及的路径，绝不把 tests/、
  tools/、日志、临时文件带上设备。
* **先备份再覆盖**：部署前把设备上将被覆盖的文件原样打进
  `/data/tmp/accelerator-backup-<UTC时间戳>.tar.gz`，并记录 absent 清单与
  `git status --porcelain` 快照。
* **原子、非交互、有超时**：所有 ssh/scp 都带 timeout 与 BatchMode。
* **绝不** `rm -rf /data/openpilot`、绝不 `git clean`、绝不触碰 `prebuilt`。

命令
----
    deploy --dry-run          仅列出将变更的文件与大小（只读设备）
    deploy                    备份 → 上传 → 逐文件 sha256 校验
    persist                   安装仓外权威副本 /data/accelerator + 开机恢复钩子
    restore                   在设备上手工执行一次恢复并校验
    unpersist                 移除开机恢复钩子（保留 /data/accelerator 备份）
    rollback --backup <tar>   按备份恢复原文件、删除本次新增文件、复核 git status
    verify                    本地与设备逐文件 sha256 对比
    config --enable/--disable 原子写入 /data/models/accelerator.json
                          （含 --transport tcp|udp|bulk）

开机默认传输 = vendor bulk 的机制：
    persist 会把 /data/continue.sh 顶层的 `/data/c3-ncm-persist.sh` 换成
    `/data/accelerator/c3-bulk-gadget.sh`（只建 ffs，不建/不删 c3ncm），并在
    块内导出 BULK_GADGET/BULK_MOUNT/BULK_OWN_GADGET=0。**不做运行期 NCM↔bulk
    切换** —— c3ncm 的 f_ncm 有内核 BUG，只能靠重启换传输。回滚见仓内
    openpilot/sunnypilot/accelerators/bulk_gadget/bulk_rollback.sh。

用法示例：
    python3 tools/deploy_accelerator.py deploy --dry-run
    python3 tools/deploy_accelerator.py deploy
    python3 tools/deploy_accelerator.py persist
    python3 tools/deploy_accelerator.py rollback --backup /data/tmp/accelerator-backup-20260929T000000Z.tar.gz
    python3 tools/deploy_accelerator.py verify
    python3 tools/deploy_accelerator.py config --enable --address 192.168.43.2:5202
    python3 tools/deploy_accelerator.py config --transport bulk
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 显式文件清单：accelerator 主线 + 四个 UI/modeld 接缝文件
# ---------------------------------------------------------------------------
ACCELERATOR_DIR = 'openpilot/sunnypilot/accelerators'
ACCELERATOR_TARGETS = (
  # 递归收集，含 accelerators/remote/bulk_transport.py（bulk 传输本体）与
  # accelerators/bulk_gadget/（开机 bulk gadget 构建脚本 + root keeper +
  # 切换/回滚脚本）。放在仓内是为了让它们一并进 deploy 清单、仓外权威副本
  # /data/accelerator/files 与 sha256.txt/manifest.txt，`verify` 逐文件核对。
  ACCELERATOR_DIR,
  'openpilot/sunnypilot/modeld_v2/modeld.py',
  'openpilot/selfdrive/ui/sunnypilot/ui_state.py',
  'openpilot/selfdrive/ui/sunnypilot/layouts/settings/models.py',
  'openpilot/selfdrive/ui/sunnypilot/accelerator_link.py',
  'openpilot/selfdrive/ui/sunnypilot/accelerator_link_data.py',
  # 行车界面（onroad）外置模型数值面板
  'openpilot/selfdrive/ui/sunnypilot/onroad/accelerator_panel.py',
  'openpilot/selfdrive/ui/sunnypilot/onroad/hud_renderer.py',
  # Param 强登记：仅源码级清单，当前 prebuilt 的 libparams_c.so 未重编时该键不生效，
  # 运行期开关仍以 /data/models/accelerator.json 的 debug_log 为准。
  'openpilot/common/params_keys.h',
)
# 这些目录/后缀永远不上设备
EXCLUDE_DIRS = frozenset({'__pycache__', 'tests', '.git', '.sse'})
EXCLUDE_SUFFIXES = ('.pyc', '.pyo', '.log', '.tmp')

DEFAULT_CONFIG = {
  'enabled': False,
  'address': '192.168.43.2:5202',
  'warp_path': '/data/models/remote_warp.pkl',
  'deadline_ms': 60,
  'pipeline_depth': 1,
  'debug_log': False,
  # 下口传输：'tcp'（默认）/ 'udp'（大帧分片）/ 'bulk'（vendor FunctionFS）。
  # 置成 'bulk' 的前提是 C3 开机就建 ffs gadget（见 c3-bulk-gadget.sh）——
  # NCM 与 ffs 抢同一 UDC，运行期互切会踩 f_ncm 内核 BUG，只能靠重启换。
  'transport': 'tcp',
}
REMOTE_CONFIG_PATH = '/data/models/accelerator.json'
# 设备上可能的契约文件（按顺序探测第一份存在的）：signature 取其 fingerprint 字段
REMOTE_CONTRACT_CANDIDATES = (
  '/data/models/contract_metadata.json',
  '/data/tmp/warp_build/contract_metadata.json',
)


class DeviceError(RuntimeError):
  """ssh/scp 层错误（超时、非零退出、设备不可达）。"""


# ---------------------------------------------------------------------------
# 设备连接
# ---------------------------------------------------------------------------

class Device:
  def __init__(self, host: str, key: str | None, repo: str, connect_timeout: int = 8):
    self.host = host
    self.key = key
    self.repo = repo.rstrip('/')
    self.connect_timeout = connect_timeout

  def _opts(self) -> list[str]:
    opts = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null', '-o', f'ConnectTimeout={self.connect_timeout}',
            '-o', 'LogLevel=ERROR']
    if self.key:
      opts += ['-i', os.path.expanduser(self.key), '-o', 'IdentitiesOnly=yes']
    return opts

  def ssh(self, script: str, *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    argv = ['ssh', *self._opts(), self.host, 'bash -s']
    try:
      proc = subprocess.run(argv, input=script.encode('utf-8'),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
      raise DeviceError(f'ssh 超时({timeout}s): {exc}') from exc
    except OSError as exc:
      raise DeviceError(f'ssh 启动失败: {exc}') from exc
    if check and proc.returncode != 0:
      raise DeviceError(f'ssh 退出码 {proc.returncode}: {proc.stderr.decode("utf-8", "replace").strip()}')
    return proc

  def scp(self, local: str, remote: str, *, timeout: int = 120) -> None:
    argv = ['scp', *self._opts(), local, f'{self.host}:{remote}']
    try:
      proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
      raise DeviceError(f'scp 超时({timeout}s): {exc}') from exc
    if proc.returncode != 0:
      raise DeviceError(f'scp 失败: {proc.stderr.decode("utf-8", "replace").strip()}')


# ---------------------------------------------------------------------------
# 本地清单
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
  digest = hashlib.sha256()
  with open(path, 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
      digest.update(chunk)
  return digest.hexdigest()


def collect_manifest() -> list[dict]:
  """展开显式清单 → 排序后的 [{rel, local, size, sha256}]。"""
  entries: list[dict] = []
  for target in ACCELERATOR_TARGETS:
    local = os.path.join(REPO_ROOT, target)
    if os.path.isdir(local):
      for root, dirs, names in os.walk(local):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith('.'))
        for name in sorted(names):
          if name.startswith('.') or name.endswith(EXCLUDE_SUFFIXES):
            continue
          rel = os.path.relpath(os.path.join(root, name), REPO_ROOT)
          entries.append({'rel': rel, 'local': os.path.join(root, name)})
    elif os.path.isfile(local):
      entries.append({'rel': target, 'local': local})
    else:
      raise SystemExit(f'清单文件缺失: {local}')
  entries.sort(key=lambda item: item['rel'])
  for entry in entries:
    entry['size'] = os.path.getsize(entry['local'])
    entry['sha256'] = sha256_file(entry['local'])
  return entries


def _human_size(size: int) -> str:
  if size < 1024:
    return f'{size} B'
  if size < 1024 * 1024:
    return f'{size / 1024:.1f} KiB'
  return f'{size / (1024 * 1024):.2f} MiB'


def utc_stamp() -> str:
  return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------

def cmd_deploy_dry_run(device: Device, manifest: list[dict]) -> None:
  print(f'仓库根: {REPO_ROOT}')
  print(f'设备  : {device.host}:{device.repo}')
  print(f'将部署 {len(manifest)} 个文件：')
  print(f'{"状态":<8}{"大小":>10}  相对路径')
  print('-' * 78)
  # 只读探测：设备上已存在 → OVERWRITE，否则 NEW
  script = 'cd ' + shlex.quote(device.repo) + ' || exit 1\n'
  script += 'while IFS= read -r p; do\n'
  script += '  if [ -e "$p" ]; then echo "E $p"; else echo "A $p"; fi\n'
  script += 'done <<\'PATHS\'\n' + '\n'.join(item['rel'] for item in manifest) + '\nPATHS\n'
  states: dict[str, str] = {}
  try:
    proc = device.ssh(script, timeout=20, check=False)
    for line in proc.stdout.decode('utf-8', 'replace').splitlines():
      tag, _, rel = line.partition(' ')
      if tag in ('E', 'A') and rel:
        states[rel] = 'OVERWRITE' if tag == 'E' else 'NEW'
  except DeviceError as exc:
    print(f'[警告] 设备只读探测失败（{exc}），状态标记为 ?')

  total = 0
  for item in manifest:
    total += item['size']
    print(f'{states.get(item["rel"], "?"):<8}{_human_size(item["size"]):>10}  {item["rel"]}')
  print('-' * 78)
  print(f'合计 {len(manifest)} 个文件 / {_human_size(total)}')
  print('dry-run：未写入设备。')


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

def _probe_states(device: Device, manifest: list[dict], *, timeout: int = 30) -> dict[str, bool]:
  script = 'cd ' + shlex.quote(device.repo) + ' || exit 1\n'
  script += 'while IFS= read -r p; do\n'
  script += '  if [ -e "$p" ]; then echo "E $p"; else echo "A $p"; fi\n'
  script += 'done <<\'PATHS\'\n' + '\n'.join(item['rel'] for item in manifest) + '\nPATHS\n'
  proc = device.ssh(script, timeout=timeout)
  states: dict[str, bool] = {}
  for line in proc.stdout.decode('utf-8', 'replace').splitlines():
    tag, _, rel = line.partition(' ')
    if tag in ('E', 'A') and rel:
      states[rel] = tag == 'E'
  return states


def _make_backup(device: Device, manifest: list[dict], states: dict[str, bool],
                 stamp: str) -> str:
  """在设备上打包被覆盖文件 + absent 清单 + git 快照，返回 tar.gz 路径。"""
  staging = f'/data/tmp/.accelerator-backup-{stamp}'
  backup = f'/data/tmp/accelerator-backup-{stamp}.tar.gz'
  existing = [item['rel'] for item in manifest if states.get(item['rel'])]
  absent = [item['rel'] for item in manifest if not states.get(item['rel'])]

  script = [
    'set -e',
    f'STAGE={shlex.quote(staging)}',
    'case "$STAGE" in /data/tmp/.accelerator-backup-*) ;; *) echo "拒绝: 非法 staging 路径"; exit 3;; esac',
    'rm -rf "$STAGE"',
    'mkdir -p "$STAGE/files"',
    f'cd {shlex.quote(device.repo)}',
    '# 部署前 git 快照',
    'git status --porcelain > "$STAGE/git-status-before.txt" 2>/dev/null || true',
    'git rev-parse HEAD > "$STAGE/git-head-before.txt" 2>/dev/null || true',
    '# 备份被覆盖文件的原始版本（保留相对路径）',
    'if [ -n "' + ' '.join(existing) + '" ]; then',
    '  while IFS= read -r p; do',
    '    [ -z "$p" ] && continue',
    '    mkdir -p "$STAGE/files/$(dirname "$p")"',
    '    cp -p "$p" "$STAGE/files/$p"',
    '  done <<\'PATHS\'',
    '\n'.join(existing),
    'PATHS',
    'fi',
    '# app 端清单（供 rollback 精确删除新增文件）',
  ]
  script.append('cat > "$STAGE/absent.txt" <<\'ABSENT\'')
  script.append('\n'.join(absent))
  script.append('ABSENT')
  script.append('cat > "$STAGE/manifest.txt" <<\'MANIFEST\'')
  script.append('\n'.join(item['rel'] for item in manifest))
  script.append('MANIFEST')
  script.append('tar czf ' + shlex.quote(backup) + ' -C "$STAGE" .')
  script.append('rm -rf "$STAGE"')
  script.append(f'ls -l {shlex.quote(backup)}')
  device.ssh('\n'.join(script) + '\n', timeout=90)

  print(f'[备份] {backup}')
  print(f'       被覆盖 {len(existing)} 个，新增 {len(absent)} 个')
  return backup


def _upload_archive(device: Device, manifest: list[dict], stamp: str) -> None:
  """把清单文件打成 tar.gz 经 scp 上传并在设备上就地解包（只覆盖清单内文件）。"""
  archive = os.path.join(tempfile.gettempdir(), f'opencode-accelerator-deploy-{stamp}.tar.gz')
  with tarfile_open(archive) as tar:
    for item in manifest:
      tar.add(item['local'], arcname=item['rel'])
  remote_archive = f'/data/tmp/.accelerator-deploy-{stamp}.tar.gz'
  try:
    device.scp(archive, remote_archive, timeout=180)
    script = [
      'set -e',
      f'cd {shlex.quote(device.repo)}',
      f'tar xzf {shlex.quote(remote_archive)}',
      f'rm -f {shlex.quote(remote_archive)}',
    ]
    device.ssh('\n'.join(script) + '\n', timeout=90)
  finally:
    try:
      os.unlink(archive)
    except OSError:
      pass
  print(f'[上传] {len(manifest)} 个文件已解包到 {device.repo}')


class tarfile_open:
  """惰性导入 tarfile，避免顶部导入开销（本工具偶尔运行）。"""

  def __init__(self, path: str):
    import tarfile
    self._tarfile = tarfile
    self._path = path
    self._handle = None

  def __enter__(self):
    self._handle = self._tarfile.open(self._path, 'w:gz')
    return self._handle

  def __exit__(self, *exc):
    if self._handle is not None:
      self._handle.close()


def _remote_sha256(device: Device, manifest: list[dict], *, timeout: int = 60) -> dict[str, str | None]:
  script = 'cd ' + shlex.quote(device.repo) + ' || exit 1\n'
  script += 'while IFS= read -r p; do\n'
  script += '  if [ -f "$p" ]; then printf "%s  %s\\n" "$(sha256sum "$p" | cut -d" " -f1)" "$p"; else echo "MISSING  $p"; fi\n'
  script += 'done <<\'PATHS\'\n' + '\n'.join(item['rel'] for item in manifest) + '\nPATHS\n'
  proc = device.ssh(script, timeout=timeout)
  digests: dict[str, str | None] = {}
  for line in proc.stdout.decode('utf-8', 'replace').splitlines():
    digest, _, rel = line.partition('  ')
    rel = rel.strip()
    if not rel:
      continue
    digests[rel] = None if digest == 'MISSING' else digest.strip()
  return digests


def cmd_deploy(device: Device, manifest: list[dict]) -> int:
  stamp = utc_stamp()
  states = _probe_states(device, manifest)
  _make_backup(device, manifest, states, stamp)
  _upload_archive(device, manifest, stamp)

  print('[校验] 本地 ↔ 设备 sha256')
  remote = _remote_sha256(device, manifest)
  mismatches = 0
  for item in manifest:
    got = remote.get(item['rel'])
    status = 'OK' if got == item['sha256'] else ('缺失' if got is None else '不一致')
    if status != 'OK':
      mismatches += 1
    print(f'  {status:<4} {item["rel"]}')
  if mismatches:
    print(f'[结果] 有 {mismatches} 个文件校验失败，可用 rollback 回滚。')
    return 1
  print(f'[结果] {len(manifest)} 个文件全部一致。备份: /data/tmp/accelerator-backup-{stamp}.tar.gz')
  return 0


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def _remote_latest_backup(device: Device, pattern: str, *, timeout: int = 30) -> str:
  script = f'ls -1t {shlex.quote(pattern)} 2>/dev/null | head -n 1\n'
  proc = device.ssh(script, timeout=timeout, check=False)
  match = proc.stdout.decode('utf-8', 'replace').strip()
  if not match:
    raise SystemExit(f'设备上找不到匹配的备份: {pattern}')
  return match


def cmd_rollback(device: Device, manifest: list[dict], backup_pattern: str, dry_run: bool) -> int:
  backup = _remote_latest_backup(device, backup_pattern)
  print(f'[回滚] 使用备份: {backup}')
  if not backup.startswith('/data/tmp/accelerator-backup-'):
    raise SystemExit(f'拒绝：备份路径必须位于 /data/tmp/accelerator-backup-*（实际 {backup}）')

  stamp = utc_stamp()
  staging = f'/data/tmp/.accelerator-restore-{stamp}'

  if dry_run:
    script = [
      'set -e',
      f'STAGE={shlex.quote(staging)}',
      'rm -rf "$STAGE"',
      'mkdir -p "$STAGE"',
      f'tar xzf {shlex.quote(backup)} -C "$STAGE"',
      'echo "--- absent.txt（将删除的新增文件）---"',
      f'cat "$STAGE/absent.txt" 2>/dev/null || true',
      'echo "--- 将恢复的原始文件 ---"',
      f'( cd "$STAGE/files" 2>/dev/null && find . -type f | sed "s#^\\./##" ) || true',
      'echo "--- git status 快照（部署前）---"',
      f'cat "$STAGE/git-status-before.txt" 2>/dev/null || true',
      f'rm -rf "$STAGE"',
    ]
    device.ssh('\n'.join(script) + '\n', timeout=90)
    print('dry-run：未改动设备。')
    return 0

  # 回滚全过程在设备上一次完成；删除范围严格限定为「备份 absent.txt ∩ 本次清单」
  script = [
    'set -e',
    f'STAGE={shlex.quote(staging)}',
    f'BACKUP={shlex.quote(backup)}',
    f'REPO={shlex.quote(device.repo)}',
    'case "$STAGE" in /data/tmp/.accelerator-restore-*) ;; *) echo "拒绝: 非法 staging"; exit 3;; esac',
    'rm -rf "$STAGE"',
    'mkdir -p "$STAGE"',
    'tar xzf "$BACKUP" -C "$STAGE"',
    '# 本次清单白名单',
    'cat > "$STAGE/allowed.txt" <<\'ALLOWED\'',
    '\n'.join(sorted(item['rel'] for item in manifest)),
    'ALLOWED',
    '# 1) 就地恢复被覆盖的原文件（tar 管道保留权限与相对路径）',
    'if [ -d "$STAGE/files" ]; then',
    '  ( cd "$STAGE/files" && tar cf - . ) | ( cd "$REPO" && tar xf - )',
    'fi',
    '# 2) 删除本次新增文件：必须是白名单内、且路径安全的相对路径',
    'if [ -f "$STAGE/absent.txt" ]; then',
    '  cd "$REPO"',
    '  while IFS= read -r p; do',
    '    [ -z "$p" ] && continue',
    '    grep -qxF "$p" "$STAGE/allowed.txt" || continue',
    '    case "$p" in /*|*..*) echo "拒绝删除非法路径: $p"; continue;; esac',
    '    rm -f "$p"',
    '  done < "$STAGE/absent.txt"',
    'fi',
    '# 3) 复核 git status 与部署前快照',
    'git -C "$REPO" status --porcelain > "$STAGE/git-status-after.txt" 2>/dev/null || true',
    'if diff -u "$STAGE/git-status-before.txt" "$STAGE/git-status-after.txt" > "$STAGE/git-status.diff" 2>&1; then',
    '  echo GIT_STATUS_MATCH',
    'else',
    '  echo GIT_STATUS_DIFF',
    '  cat "$STAGE/git-status.diff"',
    'fi',
    f'rm -rf "$STAGE"',
  ]
  proc = device.ssh('\n'.join(script) + '\n', timeout=120)
  output = proc.stdout.decode('utf-8', 'replace')
  restored, removed = _count_plan(device, backup, manifest)
  if 'GIT_STATUS_MATCH' in output:
    print(f'[结果] 已恢复 {restored} 个文件、删除 {removed} 个新增文件；git status 与部署前一致。')
    return 0
  print(output.strip())
  print(f'[结果] 已恢复 {restored} 个文件、删除 {removed} 个新增文件；但 git status 与部署前不一致，请人工复核。')
  return 1


def _count_plan(device: Device, backup: str, manifest: list[dict]) -> tuple[int, int]:
  """读备份内的 absent.txt 统计新增数（恢复数 = 清单数 - 新增数）。"""
  allowed = {item['rel'] for item in manifest}
  proc = device.ssh(
    "set -e\n"
    f'tmp=$(mktemp -d /data/tmp/.accel-count-XXXXXX)\n'
    f'tar xzf {shlex.quote(backup)} -C "$tmp" ./absent.txt 2>/dev/null || true\n'
    'cat "$tmp/absent.txt" 2>/dev/null || true\n'
    'rm -rf "$tmp"\n',
    timeout=60, check=False,
  )
  absent = [line.strip() for line in proc.stdout.decode('utf-8', 'replace').splitlines() if line.strip()]
  removed = len([rel for rel in absent if rel in allowed])
  return len(allowed) - removed, removed



# ---------------------------------------------------------------------------
# 持久化部署（候选 B/C）：仓外权威副本 + 每次开机幂等恢复
#
# 背景：openpilot 自身的 `updated` 会在 staging overlay 里执行
#   git checkout --force -B c3-dev FETCH_HEAD && git reset --hard && git clean -xdff
# （见设备 /data/openpilot/openpilot/system/updated/updated.py:377-386），
# 然后把 overlay 固化成新树在下次开机时换入。因此留在 /data/openpilot 的
# 「未提交工作区修改」和「untracked 新文件」都会被整片清除；本地 git 提交也会
# 被 `checkout -B ... FETCH_HEAD` + `reset --hard` 抹掉。
#
# 方案：把权威副本放到**仓外** /data/accelerator/（git clean/reset 够不着，
# 也不受开机时 `rm -rf /data/tmp` 影响），并在 /data/continue.sh（AGNOS 每次
# 开机由 /usr/comma/comma.sh 执行、且在 launch_openpilot.sh 之前）插入一段
# 幂等恢复：逐文件比对 sha256，不一致就从权威副本覆盖回去。tracked 的
# modeld.py 用同一机制恢复，因此不受「提交/未提交」影响。
# ---------------------------------------------------------------------------

PERSIST_DIR = '/data/accelerator'
CONTINUE_SH = '/data/continue.sh'
PERSIST_MARKER = 'accelerator-persist'

PERSIST_CONTINUE_BLOCK = """# >>> accelerator-persist >>>
# 由 tools/deploy_accelerator.py persist 生成：开机先把 cpu4-7 上线，再处理
# openpilot 的 finalized 更新换入，然后恢复仓外权威副本里的加速器补丁，接着把
# USB 下口默认成 vendor bulk（FunctionFS）gadget，最后才启动 openpilot。
# 全部幂等、失败不阻塞。
AP_DIR="${OPENPILOT_DIR:-/data/openpilot}"
AP_STAGING="${OPENPILOT_STAGING_ROOT:-/data/safe_staging}"

# 1) cpu4-7 上线：modeld 的 config_realtime_process(7, 54) 对 cpu7 调
#    sched_setaffinity，cpu7 offline 时会抛 OSError [Errno 22] 起不来。
if [ -x /data/accelerator/c3-cpu-online.sh ]; then
  /data/accelerator/c3-cpu-online.sh || true
fi

# 2) 若上一轮 updated 已 finalize 更新，openpilot 的 launch_chffrplus.sh 会在紧接
#    的启动里把 AP_DIR 换成 finalized 树并 exec 重入，从而丢弃我们在换入前恢复的
#    补丁。这里按 openpilot 的同一判据先完成换入，再恢复补丁，保证补丁落在换入后
#    的新树上（等价于 openpilot 自己会做的事，不改变更新语义）。
AP_MODIFIED=0
if [ -f "$AP_DIR/.overlay_init" ] \\
   && find "$AP_DIR/.git" -newer "$AP_DIR/.overlay_init" 2>/dev/null | grep -q .; then
  AP_MODIFIED=1
fi
if [ -f "$AP_DIR/.overlay_init" ] && [ "$AP_MODIFIED" -eq 0 ] \\
   && [ -f "$AP_STAGING/finalized/.overlay_consistent" ] \\
   && [ ! -d "$AP_STAGING/old_openpilot" ]; then
  echo "[continue] installing finalized overlay update"
  mv "$AP_DIR" "$AP_STAGING/old_openpilot" \\
    && mv "$AP_STAGING/finalized" "$AP_DIR" \\
    || echo "[continue] overlay install failed; continuing"
fi

# 3) 恢复加速器补丁：逐文件 sha256 比对，不一致就从权威副本覆盖回去（幂等）。
if [ -x /data/accelerator/restore.sh ]; then
  /data/accelerator/restore.sh || true
fi

# 4) USB 下口默认走 vendor bulk（FunctionFS），并让 modeld 只当端点使用者。
#    c3ncm 的 f_ncm 有内核 BUG（rmdir/symlink 触发 ncm_free_inst 空指针 + configfs
#    死锁），所以既不创建也不删除 c3ncm：开机只有 ffs，运行期没有切换动作。
#    keeper 以 root 占住 ep0 并绑 UDC（唯一需要 root 的东西）；modeld 仍以
#    comma 运行，BULK_OWN_GADGET=0 使其只开 ep1/ep2。这几个环境变量随
#    exec launch_openpilot.sh 继承下去，manager/nativelauncher 都会传给 modeld。
export BULK_GADGET=/sys/kernel/config/usb_gadget/c3bulk
export BULK_MOUNT=/dev/ffs-c3bulk
if [ -z "${BULK_UDC:-}" ] && [ -d /sys/class/udc ]; then
  BULK_UDC="$(ls /sys/class/udc 2>/dev/null | head -n 1)"
  export BULK_UDC
fi
export BULK_OWN_GADGET=0
if [ -x /data/accelerator/c3-bulk-gadget.sh ]; then
  /data/accelerator/c3-bulk-gadget.sh || true
else
  echo "[continue] /data/accelerator/c3-bulk-gadget.sh 缺失，跳过 bulk gadget"
fi
# <<< accelerator-persist <<<
"""

PERSIST_CPU_ONLINE_SH = """#!/usr/bin/env bash
# 启动 openpilot 前把 cpu4-7 上线：modeld 的 config_realtime_process(7, 54) 会对
# cpu7 调 sched_setaffinity，cpu7 offline 时抛 OSError [Errno 22] 导致 modeld 起
# 不来。幂等、失败不阻塞启动。
set -u
LOG=/data/c3-cpu-online.log
if [ "$(id -u)" -eq 0 ]; then SUDO=; else SUDO=sudo; fi

offline=0
for c in 4 5 6 7; do
  p="/sys/devices/system/cpu/cpu$c/online"
  [ -e "$p" ] || continue
  [ "$(cat "$p" 2>/dev/null)" = 1 ] || offline=1
done
[ "$offline" -eq 0 ] && exit 0

for c in 4 5 6 7; do
  p="/sys/devices/system/cpu/cpu$c/online"
  [ -e "$p" ] || continue
  [ "$(cat "$p" 2>/dev/null)" = 1 ] && continue
  echo 1 | $SUDO tee "$p" >/dev/null 2>&1 || true
done
echo "[$(date -Iseconds)] cpu online -> $(cat /sys/devices/system/cpu/online 2>/dev/null)" >> "$LOG"
exit 0
"""

PERSIST_BULK_GADGET_SH = """#!/usr/bin/env bash
# 开机 bulk gadget 入口（薄壳，勿改逻辑）。
# 真正的实现在权威副本 /data/accelerator/files/<REL> —— 那份同时被 restore.sh
# 恢复进 /data/openpilot 仓内，因此本体始终只有一份，不会与仓内版本漂移。
# 之所以还要这层壳：/data/continue.sh 引用的是仓外路径，openpilot 的
# `git clean -xdff` / `reset --hard` 够不着它。
set -u
STORE=/data/accelerator
REL=openpilot/sunnypilot/accelerators/bulk_gadget/c3-bulk-gadget.sh
for c in "$STORE/files/$REL" "/data/openpilot/$REL"; do
  if [ -f "$c" ]; then
    exec bash "$c" "$@"
  fi
done
echo "[c3-bulk-gadget] 找不到 $REL（权威副本与仓内都没有）" >&2
exit 0
"""

PERSIST_RUN_PROBE_SH = """#!/usr/bin/env bash
# offroad 独立外置链路探针包装（不依赖 modeld / 不依赖相机上车）。
# 用法:
#   run_accel_probe.sh                  # 仅 connect+HELLO，持续保活（UI 图标可绿）
#   run_accel_probe.sh --frames 100     # 握手后发 100 帧再保活
#   run_accel_probe.sh --exit-after-frames --frames 50   # 发满 50 帧即退出
#   run_accel_probe.sh stop             # 停止后台探针并复位状态
#   run_accel_probe.sh --help           # 全部参数
# 后台运行示例:
#   setsid nohup /data/accelerator/run_accel_probe.sh --frames 100 \\
#     > /data/accelerator/accel_probe.log 2>&1 < /dev/null &
set -u
export PATH=/usr/comma/shims:/usr/local/venv/bin:$PATH
export PYTHONPATH=/data/openpilot/pydeps:/data/openpilot:/data/openpilot/msgq_repo
cd /data/openpilot || exit 1
# 便捷别名：`run_accel_probe.sh stop` == `run_accel_probe.sh --stop`
if [ "${1-}" = "stop" ]; then
  set -- --stop
fi
exec /usr/local/venv/bin/python3 -m openpilot.sunnypilot.accelerators.remote.probe "$@"
"""

PERSIST_RESTORE_SH = """#!/usr/bin/env bash
# 加速器改动恢复（幂等）。权威副本在 /data/accelerator/files/（仓外，git
# clean/reset 与开机 rm -rf /data/tmp 都不会碰它）。由 /data/continue.sh 在每次
# 开机、openpilot 启动前调用；也可手工执行。
set -u

STORE="$(cd "$(dirname "$0")" && pwd)"
REPO="${ACCELERATOR_REPO:-/data/openpilot}"
LOG="$STORE/restore.log"

say() { printf '%s %s\\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

[ -d "$REPO/.git" ] || { say "skip: no git repo at $REPO"; exit 0; }
[ -f "$STORE/sha256.txt" ] || { say "skip: no sha256.txt"; exit 0; }

changed=0
missing=0
while IFS='  ' read -r want rel; do
  [ -z "${rel:-}" ] && continue
  src="$STORE/files/$rel"
  if [ ! -f "$src" ]; then
    say "canonical missing: $rel"
    missing=$((missing + 1))
    continue
  fi
  dst="$REPO/$rel"
  have="$(sha256sum "$dst" 2>/dev/null | cut -d' ' -f1)"
  if [ "$have" = "$want" ]; then
    continue
  fi
  mkdir -p "$(dirname "$dst")"
  cp -f "$src" "$dst"
  changed=$((changed + 1))
  say "restored $rel"
done < "$STORE/sha256.txt"

# 链路要求足够的 socket 发送缓冲：每帧 384KiB 的请求若因 wmem_max 过小而在
# worker 里阻塞，实测每帧 RTT 会从 ~60ms 抬到 ~83ms。这里幂等地放宽内核上限。
if [ "$(id -u)" -eq 0 ]; then SUDO=; else SUDO=sudo; fi
for kv in "net.core.wmem_max=4194304" "net.core.wmem_default=1048576"; do
  key="${kv%%=*}"; want="${kv##*=}"
  have="$(cat "/proc/sys/$(echo "$key" | tr . /)" 2>/dev/null)"
  if [ "$have" != "$want" ]; then
    $SUDO sysctl -w "$kv" >/dev/null 2>&1 || true
  fi
done

if [ "$changed" -eq 0 ]; then
  say "no restore needed (all files match)"
fi
exit 0
"""


def _build_persist_archive(manifest: list[dict], stamp: str) -> str:
  """打包含 files/<rel> + sha256.txt + manifest.txt + restore.sh 的上传包。"""
  import io
  import tarfile

  archive = os.path.join(tempfile.gettempdir(), f'opencode-accelerator-persist-{stamp}.tar.gz')
  sha_lines = ''.join(f'{item["sha256"]}  {item["rel"]}\n' for item in manifest)
  manifest_lines = ''.join(f'{item["rel"]}\n' for item in manifest)
  blobs = {
    'sha256.txt': (sha_lines.encode('utf-8'), 0o644),
    'manifest.txt': (manifest_lines.encode('utf-8'), 0o644),
    'restore.sh': (PERSIST_RESTORE_SH.encode('utf-8'), 0o755),
    'c3-cpu-online.sh': (PERSIST_CPU_ONLINE_SH.encode('utf-8'), 0o755),
    'c3-bulk-gadget.sh': (PERSIST_BULK_GADGET_SH.encode('utf-8'), 0o755),
    'run_accel_probe.sh': (PERSIST_RUN_PROBE_SH.encode('utf-8'), 0o755),
    'continue_block.txt': (PERSIST_CONTINUE_BLOCK.encode('utf-8'), 0o644),
  }
  with tarfile.open(archive, 'w:gz') as tar:
    for item in manifest:
      tar.add(item['local'], arcname=f'files/{item["rel"]}')
    for name, (payload, mode) in blobs.items():
      info = tarfile.TarInfo(name)
      info.size = len(payload)
      info.mode = mode
      tar.addfile(info, io.BytesIO(payload))
  return archive


def _install_continue_hook(device: Device) -> bool:
  """把恢复片段幂等装进 /data/continue.sh（首次备份原文件；已存在则整段替换为新版）。

  同时把**顶层**的旧传输钩子行（`/data/c3-ncm-persist.sh`、`/data/accelerator/
  c3-bulk-gadget.sh`）删掉：它们的调用现在收在标记块内的第 4 步。行首锚定
  （`^/data/`）保证只删顶层独立行，不会误伤块内缩进后的调用。
  """
  script = [
    'set -e',
    f'C={shlex.quote(CONTINUE_SH)}',
    f'MARK={shlex.quote(PERSIST_MARKER)}',
    f'STORE={shlex.quote(PERSIST_DIR)}',
    'if [ ! -f "$C" ]; then echo "NO_CONTINUE_SH"; exit 0; fi',
    'if [ ! -f "$STORE/continue.sh.bak" ]; then cp -p "$C" "$STORE/continue.sh.bak"; fi',
    'tmp="$(mktemp /data/.continue.XXXXXX)"',
    'body="$(mktemp /data/.continue.body.XXXXXX)"',
    '# 先删除旧的标记块（若有），保留其余内容',
    'sed "/^# >>> accelerator-persist >>>$/,/^# <<< accelerator-persist <<<$/d" "$C" > "$body"',
    '# 再删除顶层独立的下口传输钩子行（调用已收进新块内）',
    'sed -e "/^\\/data\\/c3-ncm-persist\\.sh/d" -e "/^\\/data\\/accelerator\\/c3-bulk-gadget\\.sh/d" \\',
    '    "$body" > "$body.2"',
    'mv -f "$body.2" "$body"',
    '# 在首行(shebang)之后插入最新块',
    'head -n 1 "$body" > "$tmp"',
    'cat "$STORE/continue_block.txt" >> "$tmp"',
    'tail -n +2 "$body" >> "$tmp"',
    'chmod --reference="$C" "$tmp"',
    'mv -f "$tmp" "$C"',
    'rm -f "$body"',
    'echo HOOK_INSTALLED',
  ]
  proc = device.ssh('\n'.join(script) + '\n', timeout=60)
  out = proc.stdout.decode('utf-8', 'replace')
  if 'NO_CONTINUE_SH' in out:
    print('[警告] 未找到 /data/continue.sh：无法安装开机恢复钩子；请手工把 restore.sh 接到启动流程。')
    return False
  print('[开机钩子] /data/continue.sh 已写入最新恢复片段（原文件备份 /data/accelerator/continue.sh.bak）')
  return True


def cmd_persist(device: Device, manifest: list[dict], *, skip_verify: bool = False) -> int:
  """安装/刷新仓外权威副本 + 开机恢复钩子，并立即执行一次恢复。"""
  stamp = utc_stamp()
  archive = _build_persist_archive(manifest, stamp)
  remote_archive = f'{PERSIST_DIR}/.persist-{stamp}.tar.gz'
  try:
    device.ssh(f'mkdir -p {shlex.quote(PERSIST_DIR)}\n', timeout=30)
    device.scp(archive, remote_archive, timeout=180)
    script = [
      'set -e',
      f'STORE={shlex.quote(PERSIST_DIR)}',
      f'ARC={shlex.quote(remote_archive)}',
      'cd "$STORE"',
      'tar xzf "$ARC"',
      'rm -f "$ARC"',
      'chmod +x restore.sh c3-cpu-online.sh c3-bulk-gadget.sh run_accel_probe.sh',
      # 仓内 bulk_gadget/*.sh 也要可执行：权威副本与 openpilot 仓两份都能直接跑。
      'find files/openpilot/sunnypilot/accelerators/bulk_gadget -name \'*.sh\' -exec chmod 755 {} + 2>/dev/null || true',
      'find files/openpilot/sunnypilot/accelerators/bulk_gadget -name \'*.py\' -exec chmod 755 {} + 2>/dev/null || true',
    ]
    device.ssh('\n'.join(script) + '\n', timeout=120)
  finally:
    try:
      os.unlink(archive)
    except OSError:
      pass
  print(f'[权威副本] {len(manifest)} 个文件 → {PERSIST_DIR}/files/')

  hook = _install_continue_hook(device)

  proc = device.ssh(f'{shlex.quote(PERSIST_DIR)}/c3-cpu-online.sh || true\n'
                    f'{shlex.quote(PERSIST_DIR)}/restore.sh\n'
                    f'tail -n 5 {shlex.quote(PERSIST_DIR)}/restore.log 2>/dev/null || true\n', timeout=120)
  print('[立即恢复]')
  print(proc.stdout.decode('utf-8', 'replace').strip() or '(no log)')

  ok = True
  if not skip_verify:
    remote = _remote_sha256(device, manifest)
    bad = 0
    for item in manifest:
      got = remote.get(item['rel'])
      status = 'OK' if got == item['sha256'] else ('缺失' if got is None else '不一致')
      if status != 'OK':
        bad += 1
        print(f'  {status:<4} {item["rel"]}')
    if bad:
      print(f'[结果] 恢复后仍有 {bad} 个文件不一致。')
      ok = False
    else:
      print(f'[结果] 恢复后 {len(manifest)} 个文件与本地权威副本逐一 sha256 一致。')
  return 0 if (hook and ok) else 1


def cmd_restore(device: Device, manifest: list[dict]) -> int:
  """在设备上手工执行一次恢复并校验。"""
  proc = device.ssh(f'{shlex.quote(PERSIST_DIR)}/restore.sh\n'
                    f'tail -n 10 {shlex.quote(PERSIST_DIR)}/restore.log 2>/dev/null || true\n',
                    timeout=120)
  print(proc.stdout.decode('utf-8', 'replace').strip() or '(no log)')
  remote = _remote_sha256(device, manifest)
  bad = sum(1 for item in manifest if remote.get(item['rel']) != item['sha256'])
  print(f'[结果] {len(manifest) - bad}/{len(manifest)} 与本地权威副本一致。')
  return 1 if bad else 0


def cmd_unpersist(device: Device) -> int:
  """移除开机恢复钩子（保留 /data/accelerator 作为备份）。"""
  script = [
    'set -e',
    f'C={shlex.quote(CONTINUE_SH)}',
    f'STORE={shlex.quote(PERSIST_DIR)}',
    'if [ -f "$C" ] && grep -q "accelerator-persist" "$C"; then',
    '  tmp="$(mktemp /data/.continue.XXXXXX)"',
    '  sed "/>>> accelerator-persist >>>/,/<<< accelerator-persist <<</d" "$C" > "$tmp"',
    '  chmod --reference="$C" "$tmp"',
    '  mv -f "$tmp" "$C"',
    '  echo HOOK_REMOVED',
    'else',
    '  echo NO_HOOK',
    'fi',
  ]
  proc = device.ssh('\n'.join(script) + '\n', timeout=60)
  print(proc.stdout.decode('utf-8', 'replace').strip())
  return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(device: Device, manifest: list[dict]) -> int:
  remote = _remote_sha256(device, manifest)
  bad = 0
  print(f'{"结果":<6} {"大小":>10}  相对路径')
  print('-' * 78)
  for item in manifest:
    got = remote.get(item['rel'])
    status = 'OK' if got == item['sha256'] else ('缺失' if got is None else '不一致')
    if status != 'OK':
      bad += 1
    print(f'{status:<6} {_human_size(item["size"]):>10}  {item["rel"]}')
  print('-' * 78)
  print(f'{len(manifest) - bad}/{len(manifest)} 一致。')
  return 1 if bad else 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _read_remote_config(device: Device) -> dict:
  proc = device.ssh(f'cat {shlex.quote(REMOTE_CONFIG_PATH)} 2>/dev/null || true\n', timeout=30, check=False)
  raw = proc.stdout.decode('utf-8', 'replace').strip()
  if not raw:
    return {}
  try:
    value = json.loads(raw)
  except ValueError:
    return {}
  return value if isinstance(value, dict) else {}


def _read_remote_signature(device: Device) -> str | None:
  """从设备契约 JSON 读取 `fingerprint` 作为 HELLO 用 signature。

  按 REMOTE_CONTRACT_CANDIDATES 顺序取第一份存在的文件；读取/解析失败返回 None
  （调用方保留原 signature，绝不因此中断 config 写入）。
  """
  paths = ' '.join(shlex.quote(p) for p in REMOTE_CONTRACT_CANDIDATES)
  script = (
    'set -e\n'
    f'for p in {paths}; do\n'
    '  if [ -f "$p" ]; then cat "$p"; exit 0; fi\n'
    'done\n'
    'exit 1\n'
  )
  try:
    proc = device.ssh(script, timeout=30, check=False)
  except DeviceError:
    return None
  if proc.returncode != 0:
    return None
  try:
    doc = json.loads(proc.stdout.decode('utf-8', 'replace'))
  except ValueError:
    return None
  fingerprint = doc.get('fingerprint') if isinstance(doc, dict) else None
  if isinstance(fingerprint, str) and len(fingerprint) == 64 and all(c in '0123456789abcdef' for c in fingerprint):
    return fingerprint
  return None


def cmd_config(device: Device, args) -> int:
  cfg = dict(DEFAULT_CONFIG)
  remote = _read_remote_config(device)
  if isinstance(remote, dict):
    cfg.update({k: v for k, v in remote.items() if k not in DEFAULT_CONFIG})
    for key in DEFAULT_CONFIG:
      if key in remote:
        cfg[key] = remote[key]
  if args.enable:
    cfg['enabled'] = True
    # signature 必须与设备契约 metadata 的 fingerprint 一致，否则 HELLO 报
    # `status 1: Model signature mismatch`。enable 时自动从设备契约文件读取。
    signature = args.signature if args.signature is not None else _read_remote_signature(device)
    if signature:
      cfg['signature'] = signature
    else:
      print('[警告] 未能从设备 contract_metadata.json 读到 fingerprint；signature 保持原值，'
            'HELLO 可能因签名的差异被拒绝。', file=sys.stderr)
  if args.disable:
    cfg['enabled'] = False
  if args.address is not None:
    cfg['address'] = args.address
  if args.warp_path is not None:
    cfg['warp_path'] = args.warp_path
  if args.deadline_ms is not None:
    cfg['deadline_ms'] = int(args.deadline_ms)
  if args.pipeline_depth is not None:
    cfg['pipeline_depth'] = int(args.pipeline_depth)
  if args.transport is not None:
    transport = args.transport.strip().lower()
    if transport not in ('tcp', 'udp', 'bulk'):
      raise SystemExit(f'--transport 只能是 tcp/udp/bulk（实际 {args.transport!r}）')
    cfg['transport'] = transport
  if args.debug_log:
    cfg['debug_log'] = True
  if args.no_debug_log:
    cfg['debug_log'] = False

  payload = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
  if args.dry_run or args.show:
    print(f'{REMOTE_CONFIG_PATH}:')
    print(payload, end='')
    if args.dry_run:
      print('dry-run：未写入设备。')
    return 0

  stamp = utc_stamp()
  local = os.path.join(tempfile.gettempdir(), f'opencode-accelerator-config-{stamp}.json')
  remote_tmp = f'/data/models/.accelerator.json.tmp-{stamp}'
  with open(local, 'w', encoding='utf-8') as f:
    f.write(payload)
  try:
    device.ssh('mkdir -p /data/models\n', timeout=30)
    device.scp(local, remote_tmp, timeout=60)
    # rename 同目录内原子替换
    device.ssh(f'mv -f {shlex.quote(remote_tmp)} {shlex.quote(REMOTE_CONFIG_PATH)}\n'
               f'chmod 644 {shlex.quote(REMOTE_CONFIG_PATH)}\n', timeout=30)
  finally:
    try:
      os.unlink(local)
    except OSError:
      pass
  print(f'[配置] 已原子写入 {REMOTE_CONFIG_PATH}（enabled={cfg["enabled"]}, address={cfg["address"]}, '
        f'transport={cfg["transport"]}, deadline_ms={cfg.get("deadline_ms")}, '
        f'signature={cfg.get("signature")}）')
  return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description='外置大模型加速器主线可回滚部署工具')
  parser.add_argument('--host', default=os.environ.get('C3_HOST', 'comma@192.168.43.1'),
                      help='设备 ssh 目标（默认 comma@192.168.43.1）')
  parser.add_argument('--key', default=os.environ.get('C3_KEY', '~/.ssh/id_rsa'),
                      help='ssh 私钥路径（默认 ~/.ssh/id_rsa）')
  parser.add_argument('--remote-repo', default=os.environ.get('C3_REPO', '/data/openpilot'),
                      help='设备上 openpilot 检出路径（默认 /data/openpilot）')
  sub = parser.add_subparsers(dest='command', required=True)

  deploy = sub.add_parser('deploy', help='备份 → 上传 → 校验')
  deploy.add_argument('--dry-run', action='store_true', help='仅列出将变更的文件与大小')

  sub.add_parser('persist', help='安装仓外权威副本 + /data/continue.sh 开机恢复钩子')

  sub.add_parser('restore', help='在设备上手工执行一次恢复并校验')

  sub.add_parser('unpersist', help='移除开机恢复钩子（保留 /data/accelerator 备份）')

  rollback = sub.add_parser('rollback', help='按备份回滚')
  rollback.add_argument('--backup', required=True, help='设备上的备份 tar.gz 路径或 glob')
  rollback.add_argument('--dry-run', action='store_true', help='仅展示回滚计划')

  sub.add_parser('verify', help='本地与设备逐文件 sha256 对比')

  config = sub.add_parser('config', help='写入 /data/models/accelerator.json')
  config.add_argument('--enable', action='store_true')
  config.add_argument('--disable', action='store_true')
  config.add_argument('--address')
  config.add_argument('--warp-path')
  config.add_argument('--deadline-ms', type=int)
  config.add_argument('--pipeline-depth', type=int)
  config.add_argument('--transport', choices=('tcp', 'udp', 'bulk'),
                      help="下口传输：tcp（默认）/ udp（分片）/ bulk（vendor FunctionFS）")
  config.add_argument('--signature', help='显式指定 HELLO 签名（默认从设备 contract_metadata.json 读取）')
  config.add_argument('--debug-log', action='store_true', help='开启远程模型调试日志（debug_log=true）')
  config.add_argument('--no-debug-log', action='store_true', help='关闭远程模型调试日志（debug_log=false）')
  config.add_argument('--show', action='store_true')
  config.add_argument('--dry-run', action='store_true')

  return parser


def main(argv: list[str] | None = None) -> int:
  args = build_parser().parse_args(argv)
  manifest = collect_manifest()
  device = Device(args.host, args.key, args.remote_repo)

  try:
    if args.command == 'deploy':
      if args.dry_run:
        cmd_deploy_dry_run(device, manifest)
        return 0
      return cmd_deploy(device, manifest)
    if args.command == 'rollback':
      return cmd_rollback(device, manifest, args.backup, args.dry_run)
    if args.command == 'persist':
      return cmd_persist(device, manifest)
    if args.command == 'restore':
      return cmd_restore(device, manifest)
    if args.command == 'unpersist':
      return cmd_unpersist(device)
    if args.command == 'verify':
      return cmd_verify(device, manifest)
    if args.command == 'config':
      return cmd_config(device, args)
  except DeviceError as exc:
    print(f'[错误] {exc}', file=sys.stderr)
    return 2
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
