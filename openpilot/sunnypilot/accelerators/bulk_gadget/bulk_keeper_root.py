#!/usr/bin/env python3
# Part of sunnypilot, MIT licensed. 见同目录 LICENSE。
"""Root keeper for the C3 bulk (FunctionFS) downlink: owns ep0 and holds the UDC bound.

Nothing else. This is the only process in the bulk path that runs as root, because
opening FunctionFS ``ep0`` (the descriptor writer / bind owner) needs CAP_ADMIN.
``modeld`` keeps running as ``comma`` with ``BULK_OWN_GADGET=0`` and only opens
``ep1``/``ep2``, which is unprivileged once the endpoint nodes are chowned.

Start it through ``c3-bulk-gadget.sh`` (boot hook) — it builds the configfs gadget,
mounts functionfs and then launches this keeper detached.

Two deliberate deviations from the PoC keeper:

* the UDC is bound even when no host is plugged in. Binding is what makes the
  gadget enumerate as soon as the PC attaches; waiting for ``configured`` first
  would wedge the keeper on an unplugged cable at boot.
* ``configured`` is logged, never waited on: a missing host must not kill the
  keeper, otherwise the next plug-in would find ep0 closed and the gadget dead.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pwd
import signal
import sys
import time

# 与 c3-bulk-gadget.sh 里的 gadget/挂载点保持一致；可用环境变量覆盖。
DEFAULT_GADGET = os.environ.get('BULK_GADGET', '/sys/kernel/config/usb_gadget/c3bulk')
DEFAULT_MOUNT = os.environ.get('BULK_MOUNT', '/dev/ffs-c3bulk')
ENDPOINT_OWNER = os.environ.get('BULK_EP_OWNER', 'comma')
REPO = os.environ.get('OPENPILOT_DIR', '/data/openpilot')
STORE = os.environ.get('ACCELERATOR_STORE', '/data/accelerator')
REL_TRANSPORT = 'openpilot/sunnypilot/accelerators/remote/bulk_transport.py'
TRANSPORT_CANDIDATES = (
  os.path.join(REPO, REL_TRANSPORT),
  os.path.join(STORE, 'files', REL_TRANSPORT),
)

_STOP = False


def _on_signal(signum, _frame):
  global _STOP
  _STOP = True
  print(f'keeper: signal {signum}, releasing', flush=True)


def load_bulk_transport(candidates=TRANSPORT_CANDIDATES):
  """按路径直接加载 bulk_transport 模块。

  刻意**不**走 `import openpilot...`：那会拖进整个 openpilot 包的 __init__ 链，
  而开机时包可能正处在 overlay 换入的中间态。这里只要那一个纯 stdlib 模块
  （errno/os/struct/threading/time），descriptor 是唯一真相来源。
  """
  for path in candidates:
    if not os.path.isfile(path):
      continue
    spec = importlib.util.spec_from_file_location('c3_bulk_transport', path)
    if spec is None or spec.loader is None:
      continue
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path
  raise SystemExit('bulk_transport.py not found; tried: ' + ', '.join(candidates))


def chown_endpoints(mount: str, owner: str = ENDPOINT_OWNER) -> list[str]:
  """把 ep1/ep2 交给 modeld 的运行用户（comma），ep0 保持 root 独占。"""
  try:
    ent = pwd.getpwnam(owner)
  except KeyError:
    print(f'keeper: 用户 {owner} 不存在，端点保持 root', flush=True)
    return []
  done = []
  for name in ('ep1', 'ep2'):
    node = os.path.join(mount, name)
    if not os.path.exists(node):
      continue
    try:
      os.chown(node, ent.pw_uid, ent.pw_gid)
      done.append(name)
    except OSError as exc:
      print(f'keeper: chown {node} 失败: {exc}', flush=True)
  return done


def main() -> int:
  parser = argparse.ArgumentParser(description='C3 bulk gadget root keeper (ep0 + UDC)')
  parser.add_argument('--gadget', default=DEFAULT_GADGET)
  parser.add_argument('--mount', default=DEFAULT_MOUNT)
  args = parser.parse_args()

  signal.signal(signal.SIGTERM, _on_signal)
  signal.signal(signal.SIGINT, _on_signal)
  try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # ssh/终端断开不影响保活
  except (OSError, ValueError):
    pass

  bt, src = load_bulk_transport()
  print(f'keeper: bulk_transport={src}', flush=True)
  mount, gadget = args.mount, args.gadget
  if os.environ.get('BULK_MOUNT'):
    mount = os.environ['BULK_MOUNT']
  if os.environ.get('BULK_GADGET'):
    gadget = os.environ['BULK_GADGET']

  ep0 = os.open(os.path.join(mount, 'ep0'), os.O_RDWR)
  os.write(ep0, bt.build_descriptors())
  os.write(ep0, bt.build_strings())
  print(f'keeper: descriptors 已写入 ep0 (gadget={gadget})', flush=True)
  # ep1/ep2 是写完 descriptors 才出现的，chown 必须排在写完之后。
  print(f'keeper: chown -> {chown_endpoints(mount)} (owner={ENDPOINT_OWNER})', flush=True)

  udc = ''
  bound_logged = False
  while not _STOP:
    if not udc:
      try:
        udc = bt._bind_udc_if_needed(gadget, os.environ.get('BULK_UDC', ''))
      except OSError as exc:
        print(f'keeper: 绑 UDC 失败（稍后重试）: {exc}', flush=True)
        time.sleep(2.0)
        continue
    state = bt._udc_state(udc)
    if not bound_logged:
      print(f'keeper: ready udc={udc} state={state or "<none>"} mount={mount}', flush=True)
      bound_logged = True
    if state == 'configured' and not getattr(main, '_configured_logged', False):
      main._configured_logged = True
      print(f'keeper: 主机已 SET_CONFIGURATION udc={udc}', flush=True)
    time.sleep(1.0)

  # 退出前主动解绑，让主机侧看到干净的断开而不是悬挂接口。
  if udc:
    try:
      with open(os.path.join(gadget, 'UDC'), 'w') as f:
        f.write('\n')
      print(f'keeper: 已解绑 udc={udc}', flush=True)
    except OSError as exc:
      print(f'keeper: 解绑失败（忽略）: {exc}', flush=True)
  os.close(ep0)
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
