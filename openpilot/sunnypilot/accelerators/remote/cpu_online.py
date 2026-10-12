"""
Copyright (c) 2026-, sunnypilot contributors.

本文件是 Part of sunnypilot，基于 MIT License 发布。

保证 modeld 实时线程绑定的 cpu4-7 在 `config_realtime_process(7, 54)` 之前必然在线。

为什么需要
----------
C3（tici）在 offroad、无点火且屏幕熄灭时，`hardwared` 会调用
`HARDWARE.set_power_save(True)` → `common/hardware/comma/hardware.py` 把大核
cpu4-7 用 `echo 0 > .../online` 下线省电。此后任何进程只要在 cpu4-7 仍 offline
时调用 `os.sched_setaffinity`（`config_realtime_process`）就会抛
`OSError: [Errno 22] Invalid argument`，modeld 在 `main()` 开头直接崩溃、起不来。

开机钩子（`/data/continue.sh` → `/data/accelerator/c3-cpu-online.sh`）在启动早期
执行，那时内核刚把 8 个核都拉起来、`hardwared` 还没跑省电路径，因此它是 no-op；
等 hardwared 执行 set_power_save(True) 后大核才下线。而 modeld 由 manager 在
「点火 / 换入」瞬间拉起，与 hardwared 的「上线大核」存在竞态。因此在 modeld
自身启动路径上幂等上线大核，是最接近根因、且与 openpilot 更新无关的兜底。

约束
----
* 只改 cpu 在线状态，**不碰**任何安全门禁（AEB/angle/accel/relay/panda），
  不动 Chestnut，不做任何模型/控制决策。
* 幂等、失败不阻塞：任何异常都被吞掉，最坏情况与改动前一致（modeld 仍可能
  因 Errno 22 起不来，但绝不会因此产生新的失败模式）。
* 请求核确实不可用且无法上线时，行为可预期：本函数返回 False 并保持现状，
  由调用方照常继续（modeld 仍会按原逻辑抛错/退出），不做静默降级。
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

# 大核（big cluster）编号，与 modeld 的 `config_realtime_process(7, 54)` 及
# `common/hardware/comma/hardware.py:set_power_save` 的 `range(4, 8)` 一致。
BIG_CORES = (4, 5, 6, 7)

# 上线后回读校验的总等待上限（秒）；online_store 通常是同步的，留少量余量。
VERIFY_TIMEOUT_S = 2.0
VERIFY_POLL_S = 0.05


def _online_path(cpu: int) -> str:
  return f'/sys/devices/system/cpu/cpu{cpu}/online'


def _read_online(cpu: int) -> str | None:
  """读 cpu 在线状态；路径不存在/读取失败返回 None（视为不可判定）。"""
  try:
    with open(_online_path(cpu)) as f:
      return f.read().strip()
  except OSError:
    return None


def offline_big_cores() -> list[int]:
  """当前明确处于 offline 的大核列表（不可判定的核不列入）。"""
  return [cpu for cpu in BIG_CORES if _read_online(cpu) == '0']


def _write_online(cpu: int, value: int) -> bool:
  """把 cpu 在线状态写成 value；直接写失败时退化用 sudo（comma 有 NOPASSWD）。"""
  path = _online_path(cpu)
  try:
    with open(path, 'w') as f:
      f.write(str(value))
    return True
  except PermissionError:
    pass
  except OSError:
    return False
  try:
    subprocess.run(['sudo', '-n', 'sh', '-c', f'echo {value} > {shlex.quote(path)}'],
                   timeout=3.0, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  except Exception:
    return False
  return True


def ensure_big_cores_online(timeout_s: float = VERIFY_TIMEOUT_S) -> bool:
  """幂等把 cpu4-7 上线；返回调用结束时是否已无大核处于 offline。

  绝不抛异常。等待上限内轮询回读，避免内核异步拉起时的抖动。
  """
  try:
    pending = offline_big_cores()
    if not pending:
      return True
    for cpu in pending:
      _write_online(cpu, 1)

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
      if not offline_big_cores():
        return True
      if time.monotonic() >= deadline:
        return False
      time.sleep(VERIFY_POLL_S)
  except Exception:
    return False


def big_cores_online() -> bool:
  """cpu4-7 是否全部在线（供日志/自检用）。"""
  try:
    return not offline_big_cores()
  except Exception:
    return False
