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



# ── η from history buckets: contiguous runs, not ticked buckets (hel-121) ────
_WH_PER_PCT = 51.2   # 5.12 kWh battery


def _simulate_battery(bid, t0, legs, power_w=400.0, eta_c=0.93, eta_d=0.80, soc0=50.0):
    """Integer-SOC battery_bucket rows for a sequence of ('c'|'d', n_buckets) legs."""
    rows, cells, ts = [], soc0 * _WH_PER_PCT, t0
    for mode, n in legs:
        for _ in range(n):
            ac = power_w * 0.25
            before = int(cells / _WH_PER_PCT)
            cells += ac * eta_c if mode == "c" else -ac / eta_d
            rows.append({"ts_start": ts, "battery_id": bid,
                         "charge_wh": ac if mode == "c" else 0.0,
                         "discharge_wh": ac if mode == "d" else 0.0,
                         "soc_start": float(before), "soc_end": float(int(cells / _WH_PER_PCT))})
            ts += 900
    return rows


def test_eta_runs_recover_true_round_trip_despite_integer_soc():
    # 100 Wh/bucket moves ~1.8 %SOC charging, so many buckets don't tick at all
    rows = _simulate_battery("a", 0, [("c", 40), ("d", 40)] * 3)
    c, d = e.eta_segments_from_buckets(rows)
    r = e.estimate_round_trip_eta(c, d, seed=0.5)
    assert r.measured
    assert abs(r.eta - 0.93 * 0.80) < 0.02, r.eta


def test_bucket_level_selection_is_biased_which_is_why_runs_exist():
    """Regression for the 0.84-vs-0.75 trap: feeding single buckets and keeping
    only the ones whose SOC ticked over-states η. Runs must not."""
    rows = _simulate_battery("a", 0, [("c", 40), ("d", 40)] * 3, power_w=160.0)
    per_bucket_c = [(r["charge_wh"], r["soc_end"] - r["soc_start"]) for r in rows if r["charge_wh"]]
    per_bucket_d = [(r["discharge_wh"], r["soc_start"] - r["soc_end"]) for r in rows if r["discharge_wh"]]
    biased = e.estimate_round_trip_eta(per_bucket_c, per_bucket_d, seed=0.5).eta
    c, d = e.eta_segments_from_buckets(rows)
    good = e.estimate_round_trip_eta(c, d, seed=0.5).eta
    assert abs(good - 0.744) < 0.02
    assert abs(biased - 0.744) > abs(good - 0.744)


def test_eta_runs_split_on_gaps_mode_changes_and_batteries():
    a = _simulate_battery("a", 0, [("c", 20)])
    b = _simulate_battery("b", 0, [("c", 20)])
    gap = _simulate_battery("a", 900 * 100, [("c", 20)], soc0=80.0)   # not contiguous
    c, d = e.eta_segments_from_buckets(a + b + gap)
    assert len(c) == 3 and d == []
    mixed = dict(a[5], charge_wh=100.0, discharge_wh=50.0)             # breaks the run
    c2, _ = e.eta_segments_from_buckets(a[:5] + [mixed] + a[6:], min_run_dsoc=1.0)
    assert len(c2) == 2


def test_eta_runs_skip_short_swings_and_missing_soc():
    rows = _simulate_battery("a", 0, [("c", 2)])                      # ~3.6 % swing
    assert e.eta_segments_from_buckets(rows, min_run_dsoc=5.0) == ([], [])
    rows = [dict(r, soc_start=None) for r in _simulate_battery("a", 0, [("c", 20)])]
    assert e.eta_segments_from_buckets(rows) == ([], [])


def test_parse_eta_override():
    assert e.parse_eta_override(None) is None
    assert e.parse_eta_override("") is None
    assert e.parse_eta_override(0) is None            # 0 = auto
    assert e.parse_eta_override(0.74) == 0.74
    assert e.parse_eta_override(74) == 0.74           # percent slipped in
    assert e.parse_eta_override("0.8") == 0.8
    assert e.parse_eta_override("abc") is None
    assert e.parse_eta_override(140) is None          # nonsense stays auto

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} economics tests passed ✓")
