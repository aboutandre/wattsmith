"""Unit tests for adaptive PV charging (pure logic, no hardware/HA needed).

Run directly:   python3 tests/test_adaptive.py
Or with pytest: pytest tests/test_adaptive.py
"""
import importlib.util
import sys
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "adaptive.py"
_spec = importlib.util.spec_from_file_location("adaptive", _path)
adaptive = importlib.util.module_from_spec(_spec)
sys.modules["adaptive"] = adaptive
_spec.loader.exec_module(adaptive)

AdaptiveConfig = adaptive.AdaptiveConfig
AdaptiveObservation = adaptive.AdaptiveObservation
plan = adaptive.plan_adaptive_ceiling

# Fleet: 3 × 5120 Wh = 15360 Wh; cap 75 → ceiling 100 headroom = 3840 Wh.
FLEET_WH = 3 * 5120
CAP = 75.0


def _obs(**kw):
    base = dict(
        cap_soc=CAP,
        fleet_soc=70.0,
        fleet_capacity_wh=FLEET_WH,
        remaining_pv_wh=4000.0,
        hours_to_sunset=3.0,
    )
    base.update(kw)
    return AdaptiveObservation(**base)


def _cfg(**kw):
    base = dict(enabled=True, ceiling_soc=100.0, baseline_load_w=0.0, forecast_derate=1.0)
    base.update(kw)
    return AdaptiveConfig(**base)


def test_disabled_holds_at_cap():
    r = plan(_obs(), _cfg(enabled=False))
    assert r.effective_max_soc == CAP and r.status == adaptive.STATUS_DISABLED and not r.open


def test_ceiling_not_above_cap_inactive():
    r = plan(_obs(), _cfg(ceiling_soc=75.0))
    assert r.effective_max_soc == CAP and r.status == adaptive.STATUS_INACTIVE


def test_no_forecast_inactive():
    r = plan(_obs(remaining_pv_wh=None), _cfg())
    assert r.status == adaptive.STATUS_INACTIVE and r.effective_max_soc == CAP


def test_no_soc_inactive():
    r = plan(_obs(fleet_soc=None), _cfg())
    assert r.status == adaptive.STATUS_INACTIVE


def test_zero_capacity_inactive():
    r = plan(_obs(fleet_capacity_wh=0.0), _cfg())
    assert r.status == adaptive.STATUS_INACTIVE


def test_sun_down_inactive():
    r = plan(_obs(hours_to_sunset=0.0), _cfg())
    assert r.status == adaptive.STATUS_INACTIVE and r.effective_max_soc == CAP


def test_large_surplus_holds_at_cap():
    # 9000 Wh surplus >> 3840 headroom → hold, export the excess.
    r = plan(_obs(remaining_pv_wh=9000.0, hours_to_sunset=4.0), _cfg())
    assert not r.open and r.effective_max_soc == CAP and r.status == adaptive.STATUS_HOLDING
    assert abs(r.headroom_wh - 3840.0) < 1e-6
    assert abs(r.remaining_surplus_wh - 9000.0) < 1e-6


def test_small_surplus_opens_ceiling():
    # 2600 Wh surplus ≤ 3840 headroom → open to ceiling.
    r = plan(_obs(remaining_pv_wh=2600.0, hours_to_sunset=2.0), _cfg())
    assert r.open and r.effective_max_soc == 100.0 and r.status == adaptive.STATUS_CHARGING


def test_boundary_surplus_equals_headroom_opens():
    # Exactly headroom (3840) → open (<=).
    r = plan(_obs(remaining_pv_wh=3840.0, hours_to_sunset=2.0), _cfg())
    assert r.open and r.effective_max_soc == 100.0


def test_baseline_load_can_flip_to_open():
    # 5000 Wh raw > headroom → would hold; subtract 4h×500W = 2000 → 3000 ≤ 3840 → open.
    held = plan(_obs(remaining_pv_wh=5000.0, hours_to_sunset=4.0), _cfg(baseline_load_w=0.0))
    opened = plan(_obs(remaining_pv_wh=5000.0, hours_to_sunset=4.0), _cfg(baseline_load_w=500.0))
    assert not held.open and opened.open
    assert abs(opened.remaining_surplus_wh - 3000.0) < 1e-6


def test_derate_can_flip_to_open():
    # 4200 raw > 3840 → hold; ×0.9 = 3780 ≤ 3840 → open.
    held = plan(_obs(remaining_pv_wh=4200.0, hours_to_sunset=2.0), _cfg(forecast_derate=1.0))
    opened = plan(_obs(remaining_pv_wh=4200.0, hours_to_sunset=2.0), _cfg(forecast_derate=0.9))
    assert not held.open and opened.open


def test_latch_keeps_open_once_climbing():
    # Already above cap (80 > 75) → stay open even with huge surplus.
    r = plan(_obs(fleet_soc=80.0, remaining_pv_wh=12000.0, hours_to_sunset=5.0), _cfg())
    assert r.open and r.effective_max_soc == 100.0


def test_negative_surplus_clamped_and_opens():
    # Baseline outweighs PV → surplus clamped to 0 ≤ headroom → open.
    r = plan(_obs(remaining_pv_wh=500.0, hours_to_sunset=3.0), _cfg(baseline_load_w=1000.0))
    assert r.remaining_surplus_wh == 0.0 and r.open


def test_fleet_headroom_reported():
    # soc 60 → fleet headroom to 100 = 15360 × 40/100 = 6144 Wh.
    r = plan(_obs(fleet_soc=60.0, remaining_pv_wh=1000.0, hours_to_sunset=2.0), _cfg())
    assert abs(r.fleet_headroom_wh - 6144.0) < 1e-6


def test_never_lowers_cap():
    # Effective max is never below the cap, in any branch.
    for kw in (dict(enabled=False), dict(), dict(remaining_pv_wh=None)):
        r = plan(_obs(), _cfg(**kw)) if "enabled" in kw or "remaining_pv_wh" not in kw else plan(_obs(remaining_pv_wh=None), _cfg())
        assert r.effective_max_soc >= CAP


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} adaptive tests passed ✓")
