import unittest

from opendbc.sunnypilot.car.tesla.values import TeslaFlagsSP
from openpilot.cereal import log
from openpilot.sunnypilot.selfdrive.car.tesla.control_runtime import (
  MAX_STATE_AGE_NS,
  TeslaControlRuntime,
  TeslaLongitudinalOwner,
  state_is_fresh,
)


EventName = log.OnroadEvent.EventName


def _flag(name: str) -> int:
  """Split-control bits only exist in newer opendbc revisions."""
  return int(getattr(TeslaFlagsSP, name, 0))


STOCK_LONGITUDINAL_ACTIVE = _flag("STOCK_LONGITUDINAL_ACTIVE")
MANUAL_STOCK_ACTIVE = _flag("MANUAL_STOCK_ACTIVE")
AP_HYBRID_ACTIVE = _flag("AP_HYBRID_ACTIVE")
AP_HYBRID_EXIT_RECOVERY_ACTIVE = _flag("AP_HYBRID_EXIT_RECOVERY_ACTIVE")
SPLIT_CONTROL_AVAILABLE = all((STOCK_LONGITUDINAL_ACTIVE, MANUAL_STOCK_ACTIVE,
                               AP_HYBRID_ACTIVE, AP_HYBRID_EXIT_RECOVERY_ACTIVE))


def _require_split_control():
  if not SPLIT_CONTROL_AVAILABLE:
    raise unittest.SkipTest("pinned opendbc has no Tesla split-control flags")


class FakeEvents:
  def __init__(self, *events):
    self.events = set(events)

  def has(self, event_name):
    return event_name in self.events

  def remove(self, event_name):
    self.events.remove(event_name)


def test_state_freshness_is_bounded_and_ordered():
  assert state_is_fresh(1_000, 1_000)
  assert state_is_fresh(1_000 + MAX_STATE_AGE_NS, 1_000)
  assert not state_is_fresh(999, 1_000)
  assert not state_is_fresh(1_001 + MAX_STATE_AGE_NS, 1_000)
  assert not state_is_fresh(1_000, 0)


def test_stale_flags_fail_closed_to_sp_owner():
  runtime = TeslaControlRuntime(enabled=True)
  state = runtime.update(STOCK_LONGITUDINAL_ACTIVE, 100 + MAX_STATE_AGE_NS + 1, 100)
  assert state.longitudinal_owner == TeslaLongitudinalOwner.sp
  assert not runtime.split_control_transition


def test_unknown_flags_fail_closed_to_sp_owner():
  # A bit this build does not define must never be read as OEM takeover.
  runtime = TeslaControlRuntime(enabled=True)
  state = runtime.update(1 << 30, 100, 100)
  assert state.longitudinal_owner == TeslaLongitudinalOwner.sp
  assert not runtime.split_control_transition


def test_split_control_filters_only_transition_events():
  _require_split_control()
  runtime = TeslaControlRuntime(enabled=True)
  flags = STOCK_LONGITUDINAL_ACTIVE | MANUAL_STOCK_ACTIVE
  runtime.update(flags, 100, 100)
  events = FakeEvents(EventName.buttonCancel, EventName.pcmDisable, EventName.pedalPressed)

  runtime.filter_transition_events(events)

  assert events.events == {EventName.pedalPressed}
  assert runtime.current.longitudinal_owner == TeslaLongitudinalOwner.manual_stock


def test_previous_owner_protects_first_exit_cycle():
  _require_split_control()
  runtime = TeslaControlRuntime(enabled=True)
  runtime.update(AP_HYBRID_ACTIVE, 100, 100)
  runtime.commit_cycle()
  runtime.update(0, 200, 200)
  events = FakeEvents(EventName.pcmDisable)

  runtime.filter_transition_events(events)

  assert not events.events


def test_exit_recovery_keeps_acc_fault_visible():
  _require_split_control()
  runtime = TeslaControlRuntime(enabled=True)
  runtime.update(AP_HYBRID_EXIT_RECOVERY_ACTIVE, 100, 100)
  events = FakeEvents(EventName.accFaulted, EventName.pcmDisable)

  runtime.filter_transition_events(events)

  assert events.events == {EventName.accFaulted}
