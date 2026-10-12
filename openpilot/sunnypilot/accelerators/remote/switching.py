"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

换入/重连状态机的骨架：常量 + 纯函数。

语义照搬 jetlink 的 joining.py：
- 连接失败指数退避：5s 起、翻倍、上限 60s；稳定运行 60s 后退避复位为 1s 快速重试；
- 只在 selfdriveState/carState/carControl 全部 disengaged 且快照新鲜时才换入/重连。

本 lane 只交付纯函数与接口，不做任何 cereal 订阅、不建线程（实际订阅在后续 lane）。
纯函数的好处是离线可单测，且换入判定在任何设备上都可在导入期被验证。
"""
from __future__ import annotations

from dataclasses import dataclass

# 一次失败的后续尝试间隔（秒），与 jetlink 同名同义
REJOIN_DELAY = 5.0
REJOIN_DELAY_MAX = 60.0
REJOIN_DELAY_QUICK = 1.0
# 一次「持续住」的连接判定为 stable，之后失败按 failure=1 处理（快速重试）
STABLE_SECONDS = 60.0
# 快照超过这个年龄即视为过期，禁止换入（无信息时宁可不换）
ENGAGEMENT_MAX_AGE = 0.25

# 换入/换出需要同时看的三个消息名，供后续 lane 建 SubMaster
ENGAGEMENT_SOURCES = ('selfdriveState', 'carState', 'carControl')

# big_model_state 的取值，与 modelDataV2SP.acceleratorState / jetlink 一致
BIG_MODEL_STATES = ('unavailable', 'running', 'ready', 'retrying', 'joining')


def engagement_from(enabled: bool, lat_active: bool, long_active: bool, valid: bool) -> bool:
  """由三路消息推出「是否 engaged」。

  快照无效（valid=False）时保守判为 engaged，这样在没有任何信息时也不会换入。
  """
  if not valid:
    return True
  return bool(enabled or lat_active or long_active)


def snapshot_fresh(age: float, max_age: float = ENGAGEMENT_MAX_AGE) -> bool:
  """快照是否新鲜：age 需落在 [0, max_age) 内。"""
  return 0.0 <= age < max_age


def window_open(engaged: bool, age: float, max_age: float = ENGAGEMENT_MAX_AGE) -> bool:
  """是否存在可换入窗口：快照新鲜且未 engaged。

  standstill 不足以开窗——纵向控制可能仍压着刹车，必须三者全 disengaged。
  """
  return snapshot_fresh(age, max_age) and not engaged


def next_backoff(failures: int, held_seconds: float | None,
                 base: float = REJOIN_DELAY, cap: float = REJOIN_DELAY_MAX,
                 quick: float = REJOIN_DELAY_QUICK,
                 stable_seconds: float = STABLE_SECONDS) -> tuple[int, float]:
  """按一次失败推进退避状态，返回 (新的 failure 计数, 下次尝试间隔秒)。

  held_seconds 为上一次连接持续时长；None 表示从未连上。
  连续失败每次翻倍并封顶；一旦稳定运行超过 stable_seconds，退避复位为 quick。
  """
  stable = held_seconds is not None and held_seconds > stable_seconds
  failures = 1 if stable else max(0, int(failures)) + 1
  delay = quick if stable else min(base * (2 ** (failures - 1)), cap)
  return failures, delay


def big_model_state(stopped: bool, active_is_big: bool, available: bool, failures: int) -> str:
  """modelDataV2SP.acceleratorState 的骨架取值（纯函数，无副作用）。"""
  if stopped:
    return 'unavailable'
  if active_is_big:
    return 'running'
  if available:
    return 'ready'
  return 'retrying' if failures else 'joining'


@dataclass
class Backoff:
  """退避状态持有者：不做 IO、不起线程，只给 join loop（后续 lane）用。"""

  failures: int = 0
  delay: float = 0.0

  def reset(self) -> None:
    self.failures = 0
    self.delay = 0.0

  def on_failure(self, held_seconds: float | None) -> float:
    self.failures, self.delay = next_backoff(self.failures, held_seconds)
    return self.delay
