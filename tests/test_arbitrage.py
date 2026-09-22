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
    # PV covers load every bucket -> nothing to arbitrage. This is now reported
    # distinctly from "deficits exist but no buy price beats them", which the old
    # single "no profitable window" string collapsed together (6106 of 7571 live
    # buckets carried it, hiding which case was actually occurring).
    buckets = [Bucket(price_ct=12, pv_wh=2000, load_wh=1000) for _ in range(8)]
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert plan.grid_charge_now_wh == 0.0
    assert "no deficit" in plan.reason


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


def test_defers_when_a_cheaper_window_can_cover_the_whole_need():
    # An 8-ct bucket sits before a 35-ct deficit small enough for one charge
    # bucket to cover -> wait for it and name it, don't buy at 12 ct now.
    buckets = [Bucket(12, 0, 500), Bucket(8, 0, 500), Bucket(35, 0, 1200)]
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert plan.grid_charge_now_wh == 0.0
    assert plan.buy_idx == 1 and plan.next_need_idx == 2
    assert "waiting" in plan.reason


def test_buys_now_too_when_one_cheap_bucket_cannot_cover_the_need():
    # Same shape, but the deficit is far bigger than a single 1875 Wh charge
    # bucket. The 8-ct bucket alone is not enough, and 12 ct still beats 35 ct,
    # so buying now as well is correct — the old all-or-nothing defer was wrong.
    buckets = [Bucket(12, 0, 2000), Bucket(8, 0, 2000), Bucket(35, 0, 3000),
               Bucket(35, 0, 3000), Bucket(35, 0, 3000)]
    plan = plan_arbitrage(buckets, BAT, ECON)
    assert plan.grid_charge_now_wh > 0
    assert plan.next_need_idx == 2 and plan.next_need_price_ct == 35


def test_cheap_midday_trough_charges_for_the_night():
    # André's case: PV is still producing but will not fill the fleet, and a short
    # cheap window sits at noon. The night deficit must pull from that trough.
    buckets = ([Bucket(30, 0, 400)]                 # now, dear
               + [Bucket(14, 1200, 400)] * 4        # the cheap midday trough, PV on
               + [Bucket(38, 0, 2500)] * 6)         # the night it cannot cover
    empty = BatteryModel(soc_pct=11.0, capacity_wh=15360.0, min_soc=11.0,
                         max_soc=100.0, charge_power_w=7500.0)
    plan = plan_arbitrage(buckets, empty, ECON)
    assert plan.buy_idx is not None and 1 <= plan.buy_idx <= 4, plan.reason
    assert plan.next_need_idx >= 5
    assert plan.grid_charge_now_wh == 0.0        # not now at 30 ct — at the trough


def test_dearest_need_wins_the_scarce_cheap_bucket():
    # One cheap bucket, two competing deficits -> the dearer one is served first.
    buckets = [Bucket(10, 0, 0), Bucket(30, 0, 2000), Bucket(60, 0, 2000)]
    empty = BatteryModel(soc_pct=11.0, capacity_wh=15360.0, min_soc=11.0,
                         max_soc=100.0, charge_power_w=7500.0)
    plan = plan_arbitrage(buckets, empty, ECON)
    assert plan.next_need_price_ct == 60      # the 60-ct need, not the 30-ct one


def test_no_match_when_no_buy_price_beats_the_need():
    # Deficit is real but every buyable bucket is too dear to be worth storing.
    buckets = [Bucket(30, 0, 0), Bucket(31, 0, 3000), Bucket(31, 0, 3000)]
    empty = BatteryModel(soc_pct=11.0, capacity_wh=15360.0, min_soc=11.0,
                         max_soc=100.0, charge_power_w=7500.0)
    plan = plan_arbitrage(buckets, empty, ECON)
    assert plan.grid_charge_now_wh == 0.0
    assert "no profitable window" in plan.reason


def test_import_cap_does_not_throttle_pv_charging():
    # An import cap limits GRID purchases, never sunshine: with a big PV surplus
    # the fleet must still fill from PV and leave no deficit to buy for.
    buckets = [Bucket(12, 8000, 500)] * 6 + [Bucket(40, 0, 500)] * 4
    capped = Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5, import_cap_w=500.0)
    low = BatteryModel(soc_pct=12.0, capacity_wh=15360.0, min_soc=11.0,
                       max_soc=100.0, charge_power_w=7500.0)
    plan = plan_arbitrage(buckets, low, capped)
    assert plan.grid_charge_now_wh == 0.0


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


def test_deficit_is_already_net_of_stored_energy_so_it_is_not_subtracted_twice():
    # REGRESSION. The forward sim reports the deficit REMAINING AFTER the battery
    # has discharged into it. The old planner then subtracted usable_now from that
    # deficit a second time, so with 1382.4 Wh stored against a 2764.8 Wh load it
    # concluded "already hold enough" and bought nothing — importing the shortfall
    # at 35 ct instead of pre-buying it at 12 ct. It under-bought by exactly the
    # amount the fleet was holding.
    buckets = [Bucket(12, 0, 0), Bucket(35, 0, 2764.8)]
    zero_margin = Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5, forecast_margin_frac=0.0)
    plan0 = plan_arbitrage(buckets, BAT, zero_margin)
    assert abs(plan0.profitable_deficit_wh - 1382.4) < 1.0   # battery covers the other half
    assert plan0.grid_charge_now_wh > 1300.0, plan0.reason   # and we buy the rest cheap

    padded = plan_arbitrage(buckets, BAT, ECON)              # 15% margin buys a little more
    assert padded.grid_charge_now_wh >= plan0.grid_charge_now_wh


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


# ---- load profile is keyed by LOCAL hour (must match BaselineLearner) ----
def test_load_profile_indexed_by_local_not_utc_hour():
    # The learner keys its samples by datetime.fromtimestamp(...).hour. Indexing the
    # profile by the UTC hour rotates it by the UTC offset: in CEST that put the
    # morning ramp two hours late, so the sim under-bought for it (2026-09-21).
    import os
    import time as _time
    from datetime import datetime

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    _time.tzset()
    try:
        ts = 1789966800                      # 2026-09-21T05:00Z == 07:00 CEST
        assert datetime.fromtimestamp(ts).hour == 7, "fixture is not 07:00 local"
        load = [0.0] * 24
        load[7] = 800.0                      # the breakfast ramp, at LOCAL 07:00
        buckets = a.build_buckets(ts, [(ts, 0.30)], {}, load, horizon_h=0.25)
        assert buckets[0].load_wh == 800.0   # would be load[5] == 0.0 under UTC
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()


# ---- PV confidence level ------------------------------------------------
def test_pv_confidence_selects_central_pessimistic_or_blend():
    periods = [{"period_start": "1970-01-01T00:00:00+00:00",
                "pv_estimate": 2.0, "pv_estimate10": 1.0}]
    assert a.pv_slots_from_detailed(periods, "central")[0] == 500.0       # 2.0 kW
    assert a.pv_slots_from_detailed(periods, "pessimistic")[0] == 250.0   # 1.0 kW
    assert a.pv_slots_from_detailed(periods, "blend")[0] == 375.0         # mean


def test_pv_confidence_falls_back_to_central_without_p10():
    periods = [{"period_start": "1970-01-01T00:00:00+00:00", "pv_estimate": 2.0}]
    for level in a.PV_CONFIDENCE_LEVELS:
        assert a.pv_slots_from_detailed(periods, level)[0] == 500.0


def test_pessimistic_pv_buys_more_than_central():
    # PV forecast to land inside the EXPENSIVE window: against p10 it no longer
    # covers that load, so the unmet deficit — and the earmark — grows.
    periods = [{"period_start": "1970-01-01T01:00:00+00:00",   # the 40 ct hour
                "pv_estimate": 4.0, "pv_estimate10": 0.4}]
    prices = [(0.0, 0.12), (3600.0, 0.40)]
    bat = BatteryModel(soc_pct=11.0, capacity_wh=15360.0, min_soc=11.0,
                       max_soc=100.0, charge_power_w=7500.0)
    plans = {}
    for level in ("central", "pessimistic"):
        buckets = a.build_buckets(
            0.0, prices, a.pv_slots_from_detailed(periods, level),
            [2000.0] * 24, horizon_h=2.0)
        plans[level] = plan_arbitrage(buckets, bat, ECON)
    assert (plans["pessimistic"].profitable_deficit_wh
            > plans["central"].profitable_deficit_wh)


# ---- forecast margin ----------------------------------------------------
def test_bigger_forecast_margin_earmarks_more():
    buckets = _flat([12] + [35] * 4, load=2000.0, pv=0.0)
    bat = BatteryModel(soc_pct=20.0, capacity_wh=15360.0, min_soc=11.0,
                       max_soc=100.0, charge_power_w=7500.0)
    lean = plan_arbitrage(buckets, bat, Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5,
                                             forecast_margin_frac=0.0))
    padded = plan_arbitrage(buckets, bat, Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5,
                                               forecast_margin_frac=0.5))
    assert padded.hold_floor_soc >= lean.hold_floor_soc
    assert padded.grid_charge_now_wh >= lean.grid_charge_now_wh



# ── hel-131: tomorrow's PV lives in a separate Solcast entity ────────────────
def test_solcast_forecast_entities_adds_tomorrow_sibling():
    assert a.solcast_forecast_entities("sensor.solcast_pv_forecast_forecast_today") == [
        "sensor.solcast_pv_forecast_forecast_today",
        "sensor.solcast_pv_forecast_forecast_tomorrow",
    ]
    assert a.solcast_forecast_entities("sensor.my_pv_forecast") == ["sensor.my_pv_forecast"]
    assert a.solcast_forecast_entities("") == []


def test_today_only_forecast_invents_a_need_tomorrow_afternoon():
    """The live bug: at 15:00 the priced horizon reaches tomorrow, but reading only
    the today-sensor leaves tomorrow's slots at 0 W — so a sunny afternoon shows up
    as a deficit. With tomorrow's periods merged in, the need disappears."""
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=2))
    now = datetime(2026, 9, 22, 15, 0, tzinfo=tz)
    prices = [((now + timedelta(minutes=15 * i)).timestamp(), 0.30) for i in range(33 * 4)]
    for i in range(33 * 4):                    # dear tomorrow 14:00-18:00, cheap tonight
        h = (now + timedelta(minutes=15 * i)).hour
        day = (now + timedelta(minutes=15 * i)).day
        if day == 23 and 14 <= h < 18:
            prices[i] = (prices[i][0], 0.60)
        elif h < 5:
            prices[i] = (prices[i][0], 0.20)

    def periods(day, kw):
        base = datetime(2026, 9, day, 0, 0, tzinfo=tz)
        return [{"period_start": (base + timedelta(minutes=30 * j)).isoformat(),
                 "pv_estimate": kw if 16 <= j < 36 else 0.0} for j in range(48)]

    today, tomorrow = periods(22, 2.0), periods(23, 2.0)       # 2 kW 08:00-18:00
    load = [150.0] * 24                                         # 600 W flat
    bat = BatteryModel(soc_pct=15.0, capacity_wh=15360.0, min_soc=13.0, max_soc=100.0,
                       charge_power_w=7500.0)
    econ = Econ(eta=0.75, wear_ct=3.26, min_margin_ct=1.5, forecast_margin_frac=0.0)

    only_today = a.build_buckets(now.timestamp(), prices, a.pv_slots_from_detailed(today), load)
    both = a.build_buckets(now.timestamp(), prices, a.pv_slots_from_detailed(today + tomorrow), load)
    bad, good = plan_arbitrage(only_today, bat, econ), plan_arbitrage(both, bat, econ)
    # tomorrow 14:00-18:00 is sunny: with the forecast there is nothing to buy for it
    assert bad.profitable_deficit_wh > 0 and bad.next_need_price_ct == 60.0
    assert good.next_need_price_ct != 60.0


# ── surplus gate for the zero-grid pulse hold (hel-136) ─────────────────────
def test_forecast_deficit_zero_when_storage_covers_the_horizon():
    full = BatteryModel(soc_pct=90.0, capacity_wh=15360.0, min_soc=13.0, max_soc=100.0,
                        charge_power_w=7500.0)
    buckets = [Bucket(30.0, 0.0, 150.0)] * 40          # 10 h at 600 W = 6 kWh < ~11.8 kWh stored
    assert a.forecast_deficit_wh(buckets, full) == 0.0


def test_forecast_deficit_counts_the_shortfall():
    low = BatteryModel(soc_pct=20.0, capacity_wh=15360.0, min_soc=13.0, max_soc=100.0,
                       charge_power_w=7500.0)
    buckets = [Bucket(30.0, 0.0, 150.0)] * 40
    d = a.forecast_deficit_wh(buckets, low)
    assert abs(d - (6000.0 - 15360.0 * 0.07)) < 5.0
    assert a.forecast_deficit_wh([], low) is None

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} arbitrage tests passed ✓")
