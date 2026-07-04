"""Unit tests for reconcile_goe() — the per-tick go-e drift correction.

Covers the bug where the go-e reverted to frc=0 (charge-by-default) while the planner
said "off", and the coordinator never re-asserted frc=1 because it only wrote on plan
*change*. reconcile_goe compares the plan against the charger's ACTUAL frc/amp/psm and
returns the /api/set params to send (or None if already in sync).

Pure logic — no Home Assistant needed. The HA imports in ev_coordinator are stubbed so
the module loads; only reconcile_goe (which touches no HA objects) is exercised.

Run directly:   python3 tests/test_ev_reconcile.py
Or with pytest: pytest tests/test_ev_reconcile.py
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# ── stub the homeassistant + wattsmith deps ev_coordinator imports at load ───
sys.modules["aiohttp"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
_helpers = MagicMock()
sys.modules["homeassistant.helpers"] = _helpers
sys.modules["homeassistant.helpers.aiohttp_client"] = MagicMock()

# DataUpdateCoordinator is subclassed at class-definition time, so it must be a real
# class (a MagicMock instance cannot be used as a base class).
_uc = MagicMock()
_uc.DataUpdateCoordinator = type("DataUpdateCoordinator", (), {})
sys.modules["homeassistant.helpers.update_coordinator"] = _uc

# wattsmith package + relative deps (const/settings/ev_planner/battery_bridge). MagicMock
# modules satisfy the `from .x import Y` names; none are evaluated at import time.
sys.modules.setdefault("wattsmith", MagicMock())
sys.modules["wattsmith.const"] = MagicMock()
sys.modules["wattsmith.settings"] = MagicMock()
sys.modules["wattsmith.ev_planner"] = MagicMock()
sys.modules["wattsmith.battery_bridge"] = MagicMock()

# ── load ev_coordinator ──────────────────────────────────────────────────────
_path = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "wattsmith"
    / "ev_coordinator.py"
)
_spec = importlib.util.spec_from_file_location("wattsmith.ev_coordinator", _path)
_mod = importlib.util.module_from_spec(_spec)
_mod.__package__ = "wattsmith"
sys.modules["wattsmith.ev_coordinator"] = _mod
_spec.loader.exec_module(_mod)

reconcile_goe = _mod.reconcile_goe


def _plan(charge, amp=6, phases=1):
    """Minimal stand-in for EvPlan — reconcile_goe only reads these three attrs."""
    return SimpleNamespace(charge=charge, amp=amp, phases=phases)


# ── OFF plan (charge=False) ──────────────────────────────────────────────────

def test_off_and_goe_already_off_no_write():
    # plan=off, go-e already frc=1 → nothing to do
    assert reconcile_goe(_plan(False), {"frc": 1, "amp": 6, "psm": 1}) is None


def test_off_but_goe_neutral_reasserts_frc1():
    # THE BUG: go-e reverted to frc=0 (charge-by-default) while plan says off.
    assert reconcile_goe(_plan(False), {"frc": 0, "amp": 6, "psm": 1}) == {"frc": "1"}


def test_off_but_goe_forced_on_reasserts_frc1():
    assert reconcile_goe(_plan(False), {"frc": 2, "amp": 16, "psm": 1}) == {"frc": "1"}


def test_off_never_sends_amp_or_psm():
    # an "off" write must not carry amp/psm — only frc=1
    params = reconcile_goe(_plan(False), {"frc": 0})
    assert params == {"frc": "1"}


# ── CHARGE plan (charge=True) ────────────────────────────────────────────────

def test_charge_in_sync_no_write():
    assert reconcile_goe(_plan(True, amp=6, phases=1), {"frc": 2, "amp": 6, "psm": 1}) is None


def test_charge_3phase_in_sync_no_write():
    assert reconcile_goe(_plan(True, amp=10, phases=3), {"frc": 2, "amp": 10, "psm": 2}) is None


def test_charge_but_goe_off_reasserts_full_params():
    assert reconcile_goe(_plan(True, amp=8, phases=1), {"frc": 1, "amp": 8, "psm": 1}) == {
        "frc": "2", "amp": "8", "psm": "1",
    }


def test_charge_but_goe_neutral_reasserts():
    assert reconcile_goe(_plan(True, amp=6, phases=1), {"frc": 0, "amp": 6, "psm": 1}) == {
        "frc": "2", "amp": "6", "psm": "1",
    }


def test_charge_amp_mismatch_rewrites():
    # correct frc/psm but wrong current → re-send
    assert reconcile_goe(_plan(True, amp=12, phases=1), {"frc": 2, "amp": 6, "psm": 1}) == {
        "frc": "2", "amp": "12", "psm": "1",
    }


def test_charge_phase_mismatch_rewrites():
    # want 3-phase (psm=2) but go-e on 1-phase (psm=1)
    assert reconcile_goe(_plan(True, amp=10, phases=3), {"frc": 2, "amp": 10, "psm": 1}) == {
        "frc": "2", "amp": "10", "psm": "2",
    }


# ── unreadable go-e (actual=None) ────────────────────────────────────────────

def test_unreadable_goe_asserts_off():
    # cannot verify → assert the plan anyway
    assert reconcile_goe(_plan(False), None) == {"frc": "1"}


def test_unreadable_goe_asserts_charge():
    assert reconcile_goe(_plan(True, amp=6, phases=1), None) == {
        "frc": "2", "amp": "6", "psm": "1",
    }


def test_missing_keys_in_actual_rewrites():
    # go-e returned a partial/garbled status (no frc key) → treat as mismatch, re-send
    assert reconcile_goe(_plan(False), {"amp": 6}) == {"frc": "1"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} EV reconcile tests passed ✓")
