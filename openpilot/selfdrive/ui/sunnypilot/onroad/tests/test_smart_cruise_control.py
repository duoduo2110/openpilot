import unittest

from opendbc.sunnypilot.car.tesla.values import TeslaFlagsSP
from openpilot.selfdrive.ui.sunnypilot.onroad.smart_cruise_control import tesla_longitudinal_label


def _flag(name: str) -> int:
  """Split-control bits only exist in newer opendbc revisions."""
  return int(getattr(TeslaFlagsSP, name, 0))


STOCK_LONGITUDINAL_ACTIVE = _flag("STOCK_LONGITUDINAL_ACTIVE")
MANUAL_STOCK_ACTIVE = _flag("MANUAL_STOCK_ACTIVE")
AP_HYBRID_ACTIVE = _flag("AP_HYBRID_ACTIVE")
AP_HYBRID_STOCK_LATERAL_ACTIVE = _flag("AP_HYBRID_STOCK_LATERAL_ACTIVE")
DYNAMIC_STOCK_ACTIVE = _flag("DYNAMIC_STOCK_ACTIVE")
SPLIT_CONTROL_AVAILABLE = all((STOCK_LONGITUDINAL_ACTIVE, MANUAL_STOCK_ACTIVE,
                               AP_HYBRID_ACTIVE, AP_HYBRID_STOCK_LATERAL_ACTIVE, DYNAMIC_STOCK_ACTIVE))


def _require_split_control():
  if not SPLIT_CONTROL_AVAILABLE:
    raise unittest.SkipTest("pinned opendbc has no Tesla split-control flags")


def test_dynamic_stock_owner_uses_acc_label():
  _require_split_control()
  flags = STOCK_LONGITUDINAL_ACTIVE | DYNAMIC_STOCK_ACTIVE
  assert tesla_longitudinal_label(flags, is_tesla=True) == ("ACC", False)


def test_ap_hybrid_stock_owner_uses_ap_label_and_lateral_state():
  _require_split_control()
  flags = STOCK_LONGITUDINAL_ACTIVE | AP_HYBRID_ACTIVE | AP_HYBRID_STOCK_LATERAL_ACTIVE
  assert tesla_longitudinal_label(flags, is_tesla=True) == ("AP", True)


def test_sp_owned_longitudinal_keeps_scc_v_label():
  assert tesla_longitudinal_label(0, is_tesla=True) == ("SCC-V", False)
  assert tesla_longitudinal_label(AP_HYBRID_ACTIVE, is_tesla=True) == ("SCC-V", False)
  assert tesla_longitudinal_label(STOCK_LONGITUDINAL_ACTIVE, is_tesla=False) == ("SCC-V", False)


def test_unknown_flags_keep_scc_v_label():
  # Unknown/unavailable ownership bits must not be rendered as OEM takeover.
  assert tesla_longitudinal_label(1 << 30, is_tesla=True) == ("SCC-V", False)


def test_manual_stock_owner_uses_acc_label():
  _require_split_control()
  flags = STOCK_LONGITUDINAL_ACTIVE | MANUAL_STOCK_ACTIVE
  assert tesla_longitudinal_label(flags, is_tesla=True) == ("ACC", False)
