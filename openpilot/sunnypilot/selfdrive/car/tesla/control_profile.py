"""Configuration adapter between openpilot Params and the Tesla opendbc module.

Keep the generic car interface unaware of individual Tesla feature switches.  A
single snapshot is taken during CarParams initialization; dynamic switches that
are explicitly supported by opendbc are read there at runtime.
"""

from collections.abc import Mapping
from enum import IntEnum
from typing import Protocol


class ParamReader(Protocol):
  def get(self, key: str, block: bool = False, encoding: str | None = None,
          return_default: bool = False) -> bytes | str | None: ...


# This is the complete initialization Interface consumed by
# opendbc.sunnypilot.car.interfaces.  Adding a Tesla setting should change this
# Module, the Params declaration, and its owning opendbc test together.
#
# The pinned opendbc revision (d67ac4a3) only consumes TeslaCoopSteering and
# TeslaMadsScreenButton from this snapshot.  The remaining switches back
# capabilities that revision does not implement (external ARS408 radar,
# DynamicAutoStock, dynamic AP hybrid, automatic speed limit, turn-signal and
# speed-button validation); opendbc ignores them, so those features are treated
# as unavailable rather than emulated in this tree.
INITIALIZATION_KEYS = (
  "TeslaCoopSteering",
  "TeslaMadsScreenButton",
  "TeslaARS408Radar",
  "DynamicAutoStock",
  "DynamicAutoStockSpeedKph",
  "DynamicAutoStockSpeedLowKph",
  "DynamicAutoStockBlinkerToSP",
  "DynamicAutoStockCurveToSP",
  "TeslaApHybrid",
  "TeslaDynamicApLongitudinal",
  "TeslaSpeedButtonValidation",
  "TeslaTurnSignalValidation",
)


class TeslaRadarBackend(IntEnum):
  OEM = 0
  ARS408 = 1
  DISABLED = 2


def normalize_mads_screen_button(raw: object) -> int:
  """Map stored MADS screen-button values onto the pinned opendbc encoding.

  The pinned opendbc revision (d67ac4a3) numbers the screen buttons
  Off=0, 3-finger=1, 4-finger=2, 5-finger=3, while the retired/intermediate UI
  stored a 5-finger selection as 2.  Both the retired UI value (3) and the
  intermediate value (2) mean 5-finger, so they migrate to 3.  Anything unknown
  fails closed to Off.  The retired 4-finger selection is no longer offered.
  """
  try:
    value = int(raw)
  except (TypeError, ValueError):
    return 0
  if value in (0, 1):
    return value
  if value in (2, 3):
    return 3
  return 0


def initialization_snapshot(params: ParamReader) -> list[dict[str, bytes | str | int | None]]:
  """Return the stable Params payload passed across the opendbc Seam."""
  snapshot: list[dict[str, bytes | str | int | None]] = [{key: params.get(key, return_default=True)} for key in INITIALIZATION_KEYS]
  # The pinned opendbc consumes its own screen-button encoding, and the UI may
  # have stored a UI-numbered value if the settings adapter hasn't run yet.
  # Normalize at the seam so opendbc always sees its encoding; the mapping is
  # idempotent for values already in that encoding.
  for entry in snapshot:
    if "TeslaMadsScreenButton" in entry:
      entry["TeslaMadsScreenButton"] = normalize_mads_screen_button(entry["TeslaMadsScreenButton"])
  return snapshot


def snapshot_as_dict(params: ParamReader) -> Mapping[str, bytes | str | None]:
  """Dictionary form used by diagnostics and tests."""
  return {key: params.get(key, return_default=True) for key in INITIALIZATION_KEYS}
