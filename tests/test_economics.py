"""Unit tests for battery economics (pure). No HA.

Run: python3 tests/test_economics.py   |   pytest tests/test_economics.py
"""
import importlib.util
import sys
from pathlib import Path

_p = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "economics.py"
_spec = importlib.util.spec_from_file_location("economics", _p)
e = importlib.util.module_from_spec(_spec)
sys.modules["economics"] = e  # dataclasses resolves cls.__module__ here
_spec.loader.exec_module(e)


def test_wear_cost_matches_hand_figure():
    # €1000 / (6000 × 5.12 kWh) ≈ 3.26 ct/kWh
    w = e.wear_cost_ct_per_kwh(1000.0, 6000, 5120.0)
    assert round(w, 2) == 3.26


def test_wear_cost_none_on_bad_input():
    assert e.wear_cost_ct_per_kwh(0, 6000, 5120) is None
    assert e.wear_cost_ct_per_kwh(1000, 0, 5120) is None


def test_effective_cost_and_break_even():
    # 12 ct @ η=0.78 + 3.26 wear = 18.64 ct delivered
    c = e.effective_cost_ct(12.0, 0.78, 3.26)
    assert round(c, 1) == 18.6


def test_is_profitable_gate():
    # charge 12, η 0.78, wear 3.26 -> need > 18.6
    assert e.is_profitable(35.0, 12.0, 0.78, 3.26) is True
    assert e.is_profitable(20.0, 12.0, 0.78, 3.26) is True      # +1.4
    assert e.is_profitable(18.0, 12.0, 0.78, 3.26) is False     # -0.6
    # min-margin filters the marginal win
    assert e.is_profitable(20.0, 12.0, 0.78, 3.26, min_margin_ct=2.0) is False


def test_efc_and_soh():
    assert abs(e.equivalent_full_cycles(52443, 5120) - 52443 / 5120) < 1e-9
    assert round(e.equivalent_full_cycles(52443, 5120), 1) == 10.2
    assert e.equivalent_full_cycles(0, 5120, offset=100.0) == 100.0
    assert e.state_of_health_pct(0, 6000) == 100.0
    assert e.state_of_health_pct(6000, 6000) == 80.0
    assert e.state_of_health_pct(3000, 6000) == 90.0
    assert e.remaining_cycles(10, 6000) == 5990


def test_eta_measured_from_segments():
    # charge: 55 Wh/% ; discharge: 43 Wh/%  -> η ≈ 0.78 (the June measurement)
    charge = [(55.0 * 20, 20.0), (55.0 * 15, 15.0)]
    disch = [(43.0 * 30, 30.0), (43.0 * 10, 10.0)]
    r = e.estimate_round_trip_eta(charge, disch, seed=0.85)
    assert r.measured is True
    assert round(r.eta, 3) == round(43.0 / 55.0, 3)


def test_eta_falls_back_to_seed_when_thin():
    # tiny SOC swings -> not enough to trust -> seed
    r = e.estimate_round_trip_eta([(100, 2)], [(80, 2)], seed=0.78, min_total_dsoc=8)
    assert r.measured is False and r.eta == 0.78


def test_fleet_wear_weighted():
    # identical batteries -> same per-kWh
    w = e.fleet_wear_cost_ct([(1000, 6000, 5120)] * 3)
    assert round(w, 2) == 3.26
    # a pricier battery raises the blended cost
    w2 = e.fleet_wear_cost_ct([(1000, 6000, 5120), (2000, 6000, 5120)])
    assert w2 > 3.26


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} economics tests passed ✓")
