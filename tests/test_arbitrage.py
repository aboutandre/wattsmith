"""Unit tests for the advisory arbitrage planner (pure). No HA.

Run: python3 tests/test_arbitrage.py  |  pytest tests/test_arbitrage.py
"""
import importlib.util
import sys
from pathlib import Path

_base = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _base / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


# arbitrage imports "from .economics import ..." -> make economics importable as a package sibling
_pkg = type(sys)("wattsmith")
sys.modules["wattsmith"] = _pkg
econ_spec = importlib.util.spec_from_file_location("wattsmith.economics", _base / "economics.py")
econ_mod = importlib.util.module_from_spec(econ_spec)
econ_mod.__package__ = "wattsmith"
sys.modules["wattsmith.economics"] = econ_mod
econ_spec.loader.exec_module(econ_mod)
arb_spec = importlib.util.spec_from_file_location("wattsmith.arbitrage", _base / "arbitrage.py")
a = importlib.util.module_from_spec(arb_spec)
a.__package__ = "wattsmith"
sys.modules["wattsmith.arbitrage"] = a
arb_spec.loader.exec_module(a)

Bucket, BatteryModel, Econ, plan_arbitrage = a.Bucket, a.BatteryModel, a.Econ, a.plan_arbitrage

# fleet: ~15 kWh, currently low, room to charge; 7.5 kW charge power
BAT = BatteryModel(soc_pct=20.0, capacity_wh=15360.0, min_soc=11.0, max_soc=80.0,
                   charge_power_w=7500.0)
ECON = Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5)


def _flat(prices, load=1000.0, pv=0.0):
    return [Bucket(price_ct=p, pv_wh=pv, load_wh=load) for p in prices]


def test_no_deficit_no_charge():
    # PV covers load every bucket -> nothing to arbitrage
    buckets = [Bucket(price_ct=12, pv_wh=2000, load_wh=1000) for _ in range(8)]
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert plan.grid_charge_now_wh == 0.0
    assert "no profitable" in plan.reason


def test_big_future_peak_triggers_charge():
    # cheap now (12), big deficits later at 35 ct that PV won't cover
    buckets = _flat([12] + [35] * 8, load=2000.0, pv=0.0)
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert plan.grid_charge_now_wh > 0
    # bounded by this window's charge energy (7.5kW × 0.25h = 1875 Wh)
    assert plan.grid_charge_now_wh <= 1875.0 + 1e-6
    assert plan.target_soc > BAT.soc_pct


def test_moderate_peak_not_profitable():
    # future deficits only 16 ct < effective cost (~18.6) -> skip
    buckets = _flat([12] + [16] * 8, load=2000.0)
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert plan.grid_charge_now_wh == 0.0
    assert "no profitable" in plan.reason


def test_defers_when_cheaper_window_ahead():
    # cheaper window (8 ct) sits before the 35-ct deficit -> wait, don't charge now
    buckets = [Bucket(12, 0, 2000), Bucket(8, 0, 2000), Bucket(35, 0, 3000),
               Bucket(35, 0, 3000), Bucket(35, 0, 3000)]
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert plan.grid_charge_now_wh == 0.0
    assert "cheaper window ahead" in plan.reason


def test_import_cap_limits_charge():
    buckets = _flat([12] + [35] * 8, load=3000.0, pv=0.0)
    capped = Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5, import_cap_w=2000.0)
    plan = plan_arbitrage(buckets, BAT, capped)
    # 2000 W × 0.25 h = 500 Wh ceiling on this window
    assert plan.grid_charge_now_wh <= 500.0 + 1e-6


def test_headroom_limits_charge_near_full():
    nearly_full = BatteryModel(soc_pct=79.0, capacity_wh=15360.0, min_soc=11.0,
                               max_soc=80.0, charge_power_w=7500.0)
    buckets = _flat([12] + [35] * 8, load=3000.0)
    plan = plan_arbitrage(buckets, nearly_full, ECON)
    headroom = 15360.0 * (80.0 - 79.0) / 100.0   # ~153 Wh
    assert plan.grid_charge_now_wh <= headroom + 1e-6


def test_hold_floor_protects_earmarked_energy():
    # already holding some usable energy + a future 35-ct deficit -> hold floor above min
    charged = BatteryModel(soc_pct=60.0, capacity_wh=15360.0, min_soc=11.0,
                           max_soc=80.0, charge_power_w=7500.0)
    buckets = _flat([12] + [35] * 6, load=2000.0)
    plan = plan_arbitrage(buckets, charged, ECON)
    assert plan.hold_floor_soc > charged.min_soc


def test_forecast_margin_tops_up_even_when_raw_deficit_exactly_covered():
    # usable_now (1382.4 Wh) exactly matches the raw forecast deficit -> with zero
    # margin that reads as "already hold enough"; the default margin (15%) should
    # still top up, since a forecast that's exactly right leaves no buffer for a miss.
    buckets = [Bucket(12, 0, 0), Bucket(35, 0, 2764.8)]
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert abs(plan.profitable_deficit_wh - 1382.4) < 1.0
    assert plan.grid_charge_now_wh > 0

    zero_margin = Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5, forecast_margin_frac=0.0)
    plan0 = plan_arbitrage(buckets, BAT, zero_margin)
    assert plan0.grid_charge_now_wh == 0.0
    assert "already hold enough" in plan0.reason


def test_forecast_margin_raises_hold_floor():
    charged = BatteryModel(soc_pct=60.0, capacity_wh=15360.0, min_soc=11.0,
                           max_soc=80.0, charge_power_w=7500.0)
    buckets = _flat([12] + [35] * 6, load=2000.0)
    padded = plan_arbitrage(buckets, charged, ECON)  # default 15% margin

    zero_margin = Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5, forecast_margin_frac=0.0)
    unpadded = plan_arbitrage(buckets, charged, zero_margin)

    assert padded.hold_floor_soc > unpadded.hold_floor_soc
    # padding only affects the reservation, not the reported raw forecast deficit
    assert padded.profitable_deficit_wh == unpadded.profitable_deficit_wh


def test_build_buckets_aligns_and_bounds_horizon():
    now = 900.0  # bucket-aligned
    prices = [(0.0, 0.10), (3600.0, 0.30)]   # hour 0 = 10 ct, hour 1 = 30 ct
    pv = {900: 500.0}
    load = [400.0] * 24
    buckets = a.build_buckets(now, prices, pv, load, horizon_h=2.0)
    # prices known through the end of hour 1 (last start 3600 + 1h grace = 7200):
    # slots 900,1800,...,6300 = 7 slots
    assert len(buckets) == 7
    assert buckets[0].price_ct == 10.0 and buckets[0].pv_wh == 500.0
    assert buckets[0].load_wh == 400.0
    assert buckets[-1].price_ct == 30.0   # carried forward into hour 1's slots


def test_build_buckets_stops_past_confirmed_prices():
    prices = [(0.0, 0.10)]            # only hour 0 published
    buckets = a.build_buckets(0.0, prices, {}, None, horizon_h=6.0)
    # emits hour-0 slots then stops once >1h past the last published start
    assert 0 < len(buckets) <= 8


def test_build_buckets_empty_without_prices():
    assert a.build_buckets(0.0, [], {}, None) == []


def test_pv_slots_from_detailed_splits_30min():
    periods = [{"period_start": "1970-01-01T00:00:00+00:00", "pv_estimate": 2.0}]
    slots = a.pv_slots_from_detailed(periods)
    # 2 kW × 0.25 h × 1000 = 500 Wh in each of the two 15-min sub-slots
    assert slots[0] == 500.0 and slots[900] == 500.0


def test_pv_slots_skips_bad_periods():
    assert a.pv_slots_from_detailed([{"pv_estimate": 1.0}]) == {}   # no start
    assert a.pv_slots_from_detailed([{"period_start": "nope", "pv_estimate": 1}]) == {}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} arbitrage tests passed ✓")
