"""Unit tests for the human wording of the plans (pure). No HA.

The planners think in 15-minute buckets counted from now; plan_text turns a plan
into "what, how much, when, at what price, why". The calibration scenarios below
are the live ones from 2026-10-17: three batteries 5.6 days past full, a grid
top-up waiting for the cheapest slots at noon.

Run: python3 tests/test_plan_text.py  |  pytest tests/test_plan_text.py
"""
import importlib.util
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_base = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"


def _load(name, package=False):
    qual = f"wattsmith.{name}" if package else name
    spec = importlib.util.spec_from_file_location(qual, _base / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    if package:
        m.__package__ = "wattsmith"
    sys.modules[qual] = m
    spec.loader.exec_module(m)
    return m


pt = _load("plan_text")
sd = _load("soc_drift")
sys.modules.setdefault("wattsmith", type(sys)("wattsmith"))   # arbitrage uses a relative import
_load("economics", package=True)
arb = _load("arbitrage", package=True)

BERLIN = ZoneInfo("Europe/Berlin")
DE = pt.Locale(",", "Ø")
EN = pt.Locale(".", "avg")

# Saturday 2026-10-17 06:49 local; bucket 0 is the 06:45 slot, so bucket 22 is 12:15
NOW = datetime(2026, 10, 17, 6, 49, tzinfo=BERLIN).timestamp()
BASE = int(NOW // 900) * 900


def ctx(loc=DE, now=NOW):
    return pt.Ctx(base_ts=int(now // 900) * 900, now_ts=now,
                  local=lambda ts: datetime.fromtimestamp(ts, BERLIN), loc=loc)


# ---- locale and numbers -----------------------------------------------------------------

def test_locale_follows_the_country_before_the_language():
    # the live system: English UI, country DE
    assert pt.locale_for("DE", "en") == pt.Locale(",", "Ø")
    assert pt.locale_for("de", "en-GB") == pt.Locale(",", "Ø")      # case-insensitive
    assert pt.locale_for("US", "de") == pt.Locale(".", "avg")       # a country is set: it wins
    assert pt.locale_for(None, "de-DE") == pt.Locale(",", "Ø")      # no country: the language
    assert pt.locale_for(None, "en") == pt.Locale(".", "avg")
    assert pt.locale_for(None, None) == pt.Locale(".", "avg")
    assert pt.locale_for("CH", "de") == pt.Locale(".", "Ø")         # Swiss: dot decimal


def test_numbers_use_the_locale_decimal_and_sensible_precision():
    assert pt.kwh_text(14800, DE) == "~15 kWh"          # whole kWh from 10 up
    assert pt.kwh_text(3240, DE) == "~3,2 kWh"
    assert pt.kwh_text(3240, EN) == "~3.2 kWh"
    assert pt.ct_text(28.7, DE) == "28,7 ct"
    assert pt.ct_text(28.7, EN) == "28.7 ct"


def test_price_is_plain_when_every_slot_costs_the_same_else_the_average():
    assert pt.price_text([28.7, 28.7, 28.71], DE) == "28,7 ct"          # same at the shown precision
    assert pt.price_text([28.0, 30.0], DE) == "Ø 29,0 ct"
    assert pt.price_text([28.0, 30.0], EN) == "avg 29.0 ct"
    # weighted by the energy bought in each slot
    assert pt.price_text([10.0, 20.0], DE, weights=[3000.0, 1000.0]) == "Ø 12,5 ct"
    assert pt.price_text([], DE) == ""


# ---- windows ----------------------------------------------------------------------------

def test_consecutive_buckets_merge_into_one_window():
    assert pt.merge_slots([4, 5, 6, 9, 10]) == [(4, 6), (9, 10)]
    assert pt.merge_slots([9, 4, 5, 4]) == [(4, 5), (9, 9)]
    assert pt.merge_slots([]) == []


def test_windows_are_exact_clock_times():
    c = ctx()
    assert pt.clock(BASE + 22 * 900, c) == "12:15"
    assert pt.windows_text(range(22, 31), c) == "at 12:15–14:30"
    assert pt.windows_text([22, 23, 26, 27], c) == "at 12:15–12:45 + 13:15–13:45"


def test_a_window_that_has_started_reads_until():
    c = ctx()
    assert pt.windows_text([0, 1, 2], c) == "until 07:30"
    assert pt.windows_text([0, 1, 2], c, approx_end=True) == "until ~07:30"
    assert pt.windows_text([0, 1, 6, 7], c) == "until 07:15 + 08:15–08:45"
    assert pt.windows_text([], c) == ""


def test_windows_on_another_day_say_so():
    late = datetime(2026, 10, 17, 22, 40, tzinfo=BERLIN).timestamp()     # bucket 0 = 22:30
    c = ctx(now=late)
    assert pt.windows_text([2, 3], c) == "at 23:00–23:30"
    assert pt.windows_text([6, 7], c) == "at tomorrow 00:00–00:30"
    assert pt.windows_text([2, 3, 10, 11, 12, 13], c) == "at 23:00–23:30 + tomorrow 01:00–02:00"
    assert pt.windows_text([0, 1, 2, 3], c) == "until 23:30"
    assert pt.windows_text([0, 1, 2, 3, 4, 5, 6], c) == "until tomorrow 00:15"
    # a window that runs across midnight is not "tomorrow": it starts today
    assert pt.windows_text([3, 4, 5, 6, 7], c) == "at 23:15–00:30"


def test_summary_carries_the_same_facts_as_data():
    s = pt.plan_summary([22, 23, 26], [28.0, 30.0, 29.0], 14800.0, ctx())
    assert s["windows"] == [{"start": "2026-10-17T12:15:00+02:00", "end": "2026-10-17T12:45:00+02:00"},
                            {"start": "2026-10-17T13:15:00+02:00", "end": "2026-10-17T13:30:00+02:00"}]
    assert s["energy_kwh"] == 14.8
    assert (s["price_ct"], s["price_ct_min"], s["price_ct_max"]) == (29.0, 28.0, 30.0)
    assert pt.plan_summary([], [], None, ctx())["windows"] == []


def test_clip_keeps_a_sensor_state_under_the_255_char_limit():
    assert pt.clip("short") == "short"
    long = "x" * 400
    assert len(pt.clip(long)) <= 250 and pt.clip(long).endswith("…")


# ---- calibration ------------------------------------------------------------------------

CAL_NOW = 100 * 86400.0


def _batteries():
    """The three live batteries: 5.6 d since full, predicted drift 15.0 / 17.2 / 14.6 pts."""
    return {b: sd.BatteryDrift(b, CAL_NOW - 5.6 * 86400.0, 12000.0, pts)
            for b, pts in (("QBBR", 15.0), ("J3T1", 17.2), ("0K0N", 14.6))}


def _calib(prices, drift=None, **kw):
    args = dict(now_ts=CAL_NOW, enabled=True, threshold_pts=8.0, max_days=7.0, grid_enabled=True,
                grid_extra_pts=4.0, prices_ct=list(prices), need_wh=14800.0, per_bucket_wh=1875.0)
    args.update(kw)
    return sd.plan_calibration(drift or _batteries(), **args)


def _prices(base=40.0, **spans):
    """96 buckets at `base`, with ranges overridden: _prices(a=(22, 31, 28.7)) -> 22..30 at 28.7."""
    p = [base] * 96
    for lo, hi, v in spans.values():
        p[lo:hi] = [v] * (hi - lo)
    return p


def test_waiting_top_up_says_what_how_much_when_and_why():
    # the screenshot: "overdue — grid top-up waits for bucket 22 @ 28.7 ct"
    plan = _calib(_prices(a=(22, 31, 28.7)))
    assert plan.status == "grid_waiting" and plan.slots == tuple(range(22, 31))
    assert pt.calibration_text(plan, ctx()) == (
        "Calibration top-up: ~15 kWh at 12:15–14:30 (@ 28,7 ct). "
        "Overdue (drift 17,2 pts, 5,6 d since full).")
    assert "28.7 ct" in pt.calibration_text(plan, ctx(EN))       # the same, in the other number format


def test_waiting_top_up_with_unequal_prices_marks_the_average():
    plan = _calib(_prices(a=(22, 26, 28.0), b=(26, 31, 30.0)))
    assert pt.calibration_text(plan, ctx()).startswith(
        "Calibration top-up: ~15 kWh at 12:15–14:30 (@ Ø 29,1 ct).")


def test_waiting_top_up_in_separate_windows_lists_each():
    plan = _calib(_prices(a=(22, 24, 28.7), b=(26, 33, 28.7)))
    assert pt.calibration_text(plan, ctx()).startswith(
        "Calibration top-up: ~15 kWh at 12:15–12:45 + 13:15–15:00 (@ 28,7 ct).")


def test_top_up_that_starts_now_reads_until():
    plan = _calib(_prices(a=(0, 9, 18.0)))
    assert plan.status == "grid_charging" and plan.detail == "start" and plan.slots == tuple(range(9))
    assert pt.calibration_text(plan, ctx()).startswith(
        "Calibration top-up running: ~15 kWh until 09:00 (@ 18,0 ct).")


def test_running_top_up_says_why_it_is_not_waiting_for_a_cheaper_window():
    # buying at 14.3 ct; a window 1.2 ct cheaper at 16:45 is not worth stopping for
    margin = _calib(_prices(base=14.3, a=(40, 49, 13.1)), in_progress=True)
    assert margin.status == "grid_charging" and margin.detail == "margin"
    assert pt.calibration_text(margin, ctx()) == (
        "Calibration top-up running: ~15 kWh until ~08:45 (@ 14,3 ct). "
        "Not waiting for 16:45: only 1,2 ct cheaper. Overdue (drift 17,2 pts, 5,6 d since full).")
    # a much cheaper window, but a 39 ct stretch lies before it: what is bought now earns its keep there
    pays = _calib(_prices(base=14.3, a=(20, 31, 39.0), b=(40, 49, 10.0)), in_progress=True)
    assert pays.detail == "pays"
    assert "Not waiting for 16:45: prices up to 39,0 ct come first." in pt.calibration_text(pays, ctx())


def test_due_calibration_says_it_waits_for_pv_and_why_not_the_grid():
    drift = {"a": sd.BatteryDrift("a", CAL_NOW - 2.0 * 86400.0, 6000.0, 9.1)}
    due = _calib(_prices(), drift)
    assert due.status == "due" and due.detail == "not_overdue"
    assert pt.calibration_text(due, ctx()) == (
        "Calibration due (drift 9,1 pts, 2,0 d since full). "
        "Charging to 100 % from PV; grid top-up from 12 pts or 7 d.")
    off = _calib(_prices(), drift, grid_enabled=False)
    assert off.detail == "grid_off" and pt.calibration_text(off, ctx()).endswith("grid top-up is off.")
    none = _calib([], _batteries())
    assert none.detail == "no_prices" and "no prices available" in pt.calibration_text(none, ctx())


def test_no_calibration_needed_and_off():
    ok = _calib(_prices(), {"a": sd.BatteryDrift("a", CAL_NOW - 3600.0, 1000.0, 7.2)})
    assert ok.status == "ok"
    assert pt.calibration_text(ok, ctx()) == "No calibration needed: worst drift 7,2 of 8 pts"
    assert pt.calibration_text(_calib(_prices(), enabled=False), ctx()) == "Calibration is off"


def test_a_machine_reason_is_still_there_for_the_history_db():
    plan = _calib(_prices(a=(22, 31, 28.7)))
    assert plan.reason == "overdue — grid top-up waits for bucket 22 @ 28.7 ct"


# ---- arbitrage --------------------------------------------------------------------------

Bucket, BatteryModel, Econ, plan_arbitrage = arb.Bucket, arb.BatteryModel, arb.Econ, arb.plan_arbitrage
ECON = Econ(eta=0.78, wear_ct=3.26, min_margin_ct=1.5)
EMPTY = BatteryModel(soc_pct=11.0, capacity_wh=15360.0, min_soc=11.0, max_soc=100.0,
                     charge_power_w=7500.0)


def _plan(**kw):
    return arb.ArbitragePlan(0.0, 50.0, 11.0, 0.0, "machine", **kw)


def test_planned_purchase_names_the_slots_energy_price_and_the_need_it_serves():
    p = _plan(kind="waiting", buy_slots=(28, 29, 30), buy_slot_wh=(1875.0, 1875.0, 1000.0),
              buy_slot_prices_ct=(27.9, 27.9, 28.1), next_need_idx=53, next_need_price_ct=39.1)
    assert pt.arbitrage_text(p, ctx()) == (
        "Arbitrage top-up planned: ~4,8 kWh at 13:45–14:30 (@ Ø 27,9 ct) "
        "to avoid buying at 39,1 ct from 20:00.")


def test_running_purchase_reads_until_and_flags_a_lifted_cap():
    p = _plan(kind="charging", buy_slots=(0, 1, 2), buy_slot_wh=(1875.0,) * 3,
              buy_slot_prices_ct=(14.3, 14.3, 14.3), next_need_idx=53, next_need_price_ct=39.1)
    assert pt.arbitrage_text(p, ctx()) == (
        "Arbitrage top-up running: ~5,6 kWh until 07:30 (@ 14,3 ct) "
        "to avoid buying at 39,1 ct from 20:00.")
    assert pt.arbitrage_text(p, ctx(), lift_gain_ct=4.3).endswith(
        " Above the usual charge cap: saves ~4,3 ct more.")


def test_no_purchase_cases():
    assert pt.arbitrage_text(_plan(kind="idle"), ctx()) == "No plan: prices or forecast missing"
    assert pt.arbitrage_text(_plan(kind="no_deficit"), ctx()).startswith("No top-up needed")
    assert pt.arbitrage_text(_plan(kind="no_window", breakeven_ct=18.04), ctx()) == (
        "No top-up worth it: no shortfall ahead is dearer than 18,0 ct, the break-even for the cheapest slot")


def test_the_real_planner_fills_the_fields_the_wording_reads():
    # André's midday trough: buy in the cheap noon buckets for the night
    buckets = ([Bucket(30, 0, 400)] + [Bucket(14, 1200, 400)] * 4 + [Bucket(38, 0, 2500)] * 6)
    waiting = plan_arbitrage(buckets, EMPTY, ECON)
    assert waiting.kind == "waiting" and waiting.buy_slots and set(waiting.buy_slots) <= {1, 2, 3, 4}
    assert len(waiting.buy_slots) == len(waiting.buy_slot_wh) == len(waiting.buy_slot_prices_ct)
    assert waiting.buy_slot_prices_ct == (14.0,) * len(waiting.buy_slots)
    text = pt.arbitrage_text(waiting, ctx())
    assert text.startswith("Arbitrage top-up planned: ~") and "(@ 14,0 ct)" in text
    assert waiting.next_need_idx >= 5
    assert f"to avoid buying at 38,0 ct from {pt.clock(BASE + waiting.next_need_idx * 900, ctx())}" in text

    # cheap now, a big dear peak later: buying now
    charging = plan_arbitrage([Bucket(12, 0, 2000)] + [Bucket(35, 0, 2000)] * 8, EMPTY, ECON)
    assert charging.kind == "charging" and charging.buy_slots[0] == 0
    assert sum(charging.buy_slot_wh) >= charging.grid_charge_now_wh > 0
    assert pt.arbitrage_text(charging, ctx()).startswith("Arbitrage top-up running: ~")


def test_the_real_planner_tags_the_no_purchase_cases():
    assert plan_arbitrage([Bucket(12, 2000, 1000)] * 8, EMPTY, ECON).kind == "no_deficit"
    flat = plan_arbitrage([Bucket(12, 0, 0)] + [Bucket(16, 0, 2000)] * 8, EMPTY, ECON)
    assert flat.kind == "no_window" and flat.breakeven_ct is not None
    assert plan_arbitrage([], EMPTY, ECON).kind == "idle"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} plan text tests passed ✓")
