from collections.abc import Iterable

from openpilot.sunnypilot.hardware.profile import HardwareProfile


# Single source of truth for branches that include the isolated C3XL hardware,
# Panda, Tesla, eGPU, and boot-chain compatibility seams.
C3XL_COMPATIBLE_BRANCHES = ("dev-sp-egpu", "dev-sp-egpu-nva", "dev-sp-egpu-prebuild", "navassist-track-p0")

# 真实 Comma 3（STANDARD）分支：同样的硬件 seam，但在官方 C3 + 内置单路 SPI DOS Panda 上验证。
C3_STANDARD_BRANCHES = ("c3-dev-sp-egpu", "c3-dev-sp-egpu-prebuild")

# 所有携带兼容 seam 的分支（用于构建 channel 归类）。
COMPATIBLE_BRANCHES = C3XL_COMPATIBLE_BRANCHES + C3_STANDARD_BRANCHES


def is_prebuild_branch(branch: str) -> bool:
  return branch.endswith(("-prebuild", "-prebuilt"))


def selectable_tici_branches(branches: Iterable[str], profile: HardwareProfile) -> list[str]:
  """Return update targets that are safe for the effective TICI hardware profile."""
  available = set(branches)
  if profile == HardwareProfile.C3XL:
    return [branch for branch in C3XL_COMPATIBLE_BRANCHES if branch in available]
  return [branch for branch in branches if branch.endswith("-tici") or branch in C3_STANDARD_BRANCHES]
