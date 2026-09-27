"""The fleet ran empty at 06:00 with 14 ct energy on offer the day before (hel-139).

WHAT HAPPENED (2026-09-26/27, real data in fixtures/2026-09-26_empty_by_morning.json)

  12:17  An overdue SOC calibration started a grid top-up to 100% at 14-15 ct.
  13:00  Tibber published tomorrow's prices. Tomorrow's noon was 13.4-13.8 ct.
  13:12  The top-up re-ran its "cheapest buckets in the next 24 h" search, found
         tomorrow slightly cheaper, and stopped at 61%. In the same tick the
         arbitrage planner saw "no deficit in the forecast horizon" and bought
         nothing either.
  16:00  PV had filled the fleet to its 80% cap. The evening (37-42 ct) and the
         night (33-37 ct) were covered from the batteries, as they should be.
  06:00  Empty at the 13% floor. The house then imported ~1.2 kWh at 32-34 ct
         until the sun came back.

WHY THE PLANNER THOUGHT THERE WAS ENOUGH: it overestimated the stored energy
and underestimated the drain, in two independent ways.

  1. It turned displayed SOC into energy at the rated capacity (154 Wh per % for
     3 x 5.12 kWh). The fleet actually delivers ~127 Wh per displayed % at the
     socket (factor 0.82-0.84: discharge losses plus the BMS SOC drift, measured
     the same on every battery). -> BatteryModel.delivery_factor,
     soc_drift.fit_delivery_factor.
  2. It forecast the house load, but while the batteries cover the house the
     zero-grid loop also exports ~85 W: the -60 W grid target plus the overshoot
     each time a load switches off. That was 1.20 kWh that night.
     -> arbitrage.with_discharge_overhead / fit_discharge_overhead_w.

  Together they are about the ~2 kWh the fleet came up short.

WHY THE TOP-UP GAVE UP: its window search jumps when tomorrow's prices appear.
Stopping a running top-up to save 0.8 ct/kWh tomorrow gave up ~2 kWh of cheap
energy for a 33-42 ct night. -> plan_calibration(in_progress=...).

The replay below re-plans every 15 minutes like production, against a simulated
fleet built to match the measured one (0.83 delivery, eta 0.817 in, 85 W export
while discharging). With the old planner it runs empty at 06:15, as reality did
at 06:00.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_arbitrage_scenarios as sc  # noqa: E402  (loads economics + arbitrage)
import test_soc_drift as tsd  # noqa: E402  (loads soc_drift)

a = sys.modules["wattsmith.arbitrage"]
sd = tsd.sd

FX = json.loads((Path(__file__).resolve().parent / "fixtures" /
                 "2026-09-26_empty_by_morning.json").read_text())
R = FX["replay"]
CAP, MIN_SOC, ETA, WEAR = R["capacity_wh"], R["min_soc"], R["eta_measured"], R["wear_ct"]
TRUE_DELIVERY = 0.83          # what the real fleet delivers per displayed Wh (measured)
TRUE_EXPORT_W = 85.0          # what really goes to the grid while discharging (measured)
ECON = a.Econ(eta=ETA, wear_ct=WEAR, min_margin_ct=1.5, forecast_margin_frac=0.15)


def _buckets(start=0, overhead_w=0.0):
    raw = [a.Bucket(p, pv, ld) for p, pv, ld in
           zip(R["prices_ct"][start:], R["pv_wh"][start:], R["load_wh"][start:])]
    return a.with_discharge_overhead(raw, overhead_w)


def _fleet(soc, delivery):
    # max 100: the overdue calibration had opened the ceiling at 13:12
    return a.BatteryModel(soc_pct=soc, capacity_wh=CAP, min_soc=MIN_SOC, max_soc=100.0,
                          charge_power_w=7500.0, delivery_factor=delivery)


# ── 1. the measurements, on the real history ──────────────────────────────────
def test_delivery_factor_measured_on_the_real_fleet():
    caps = {r["battery_id"]: 5120.0 for r in FX["battery_bucket_3d"]}
    fit = sd.fit_delivery_factor(FX["battery_bucket_3d"], caps)
    assert fit is not None and 0.80 <= fit.factor <= 0.87, fit
    assert all(0.78 <= f <= 0.88 for f in fit.per_battery.values()), fit.per_battery


def test_discharge_overhead_measured_on_the_real_fleet():
    w = a.fit_discharge_overhead_w(FX["bucket_7d"])
    assert w is not None and 70.0 <= w <= 100.0, w


# ── 2. the estimators' edge cases ─────────────────────────────────────────────
def _row(i, soc0, soc1, dis=150.0, chg=0.0, bid="a"):
    return {"ts_start": i * 900, "battery_id": bid, "charge_wh": chg,
            "discharge_wh": dis, "soc_start": soc0, "soc_end": soc1}


def test_whole_percent_rounding_cancels_over_a_run():
    # Each bucket really drains half a point (25.6 Wh of a 5120 Wh battery) and
    # delivers 0.8 of that. Logged in whole %, single buckets show 0 or 1 point
    # (80, 80, 79, 79, ...), so bucket by bucket the ratio is 0 or 1.6. Over the
    # run the drops telescope to the true 10 points and the factor comes out exact.
    rows = []
    for i in range(20):
        soc0, soc1 = 80 - (i + 1) // 2, 80 - (i + 2) // 2
        rows.append(_row(i, soc0, soc1, dis=0.8 * 25.6))
    fit = sd.fit_delivery_factor(rows, {"a": 5120.0}, min_points=5)
    assert fit.points == 10.0
    assert fit.factor == pytest.approx(0.8)


def test_a_bms_reset_or_charging_ends_the_run():
    rows = [_row(0, 50, 49), _row(1, 49, 48), _row(2, 48, 60),      # reset: jump up
            _row(3, 60, 59, chg=300.0),                              # charging
            _row(4, 59, 58), _row(5, 58, 57), _row(6, 57, 56)]
    fit = sd.fit_delivery_factor(rows, {"a": 5120.0}, min_points=1, min_run_pts=1)
    # only the two clean runs count (50->48 and 59->56); the 12-point jump does not
    assert fit.points == 5.0


def test_too_little_discharge_keeps_the_fallback():
    rows = [_row(i, 50 - i, 49 - i) for i in range(10)]            # 10 points only
    assert sd.fit_delivery_factor(rows, {"a": 5120.0}) is None


def test_overhead_is_only_added_where_the_fleet_covers_the_house():
    b = a.with_discharge_overhead([a.Bucket(30, 0, 200), a.Bucket(20, 900, 300)], 80.0)
    assert b[0].load_wh == 220.0          # night: the fleet covers the house + export
    assert b[1].load_wh == 300.0          # PV surplus: the export is PV, not stored


def test_delivery_factor_scales_every_soc_conversion():
    # the same stored % is worth less, and the hold floor it hands back is in the
    # same displayed-% units the manager applies
    full, real = _fleet(50.0, 1.0), _fleet(50.0, 0.8)
    assert real.wh_per_pct == pytest.approx(0.8 * full.wh_per_pct)
    buckets = [a.Bucket(40, 0, 1000)] * 8
    hold_full = a.plan_arbitrage([a.Bucket(20, 0, 200)] + buckets, full, ECON).hold_floor_soc
    hold_real = a.plan_arbitrage([a.Bucket(20, 0, 200)] + buckets, real, ECON).hold_floor_soc
    assert hold_real > hold_full          # fewer Wh per % -> more % kept for the peak


# ── 3. the 13:12 decision ─────────────────────────────────────────────────────
def test_at_1312_nominal_capacity_saw_almost_no_need():
    plan = a.plan_arbitrage(_buckets(), _fleet(R["soc_at_1312"], 1.0), ECON)
    assert plan.grid_charge_now_wh == 0.0
    assert plan.next_need_idx is None or plan.profitable_deficit_wh < 400.0, plan.reason


def test_at_1312_the_measured_fleet_sees_the_night_short_and_waits_for_1330():
    plan = a.plan_arbitrage(_buckets(overhead_w=TRUE_EXPORT_W),
                            _fleet(R["soc_at_1312"], TRUE_DELIVERY), ECON)
    assert plan.next_need_idx is not None and plan.next_need_price_ct >= 30.0, plan.reason
    assert plan.buy_idx == 1 and plan.buy_price_ct < 15.0, plan.reason   # 13:30 @ 14.1 ct


# ── 4. rolling replay of the whole 24 h ───────────────────────────────────────
def _replay(delivery, overhead_w):
    """Production loop: re-plan each bucket, act on bucket 0. The simulated fleet is
    the MEASURED one whatever the planner believes: 0.83 delivered per displayed
    Wh, eta 0.817 on everything stored (grid and PV alike), 85 W exported while
    it covers the house. Returns (bill ct, kWh imported at >= 30 ct, first empty
    bucket or None, energy left at the end)."""
    wpp = CAP * TRUE_DELIVERY / 100.0
    e, cap_e = (R["soc_at_1312"] - MIN_SOC) * wpp, (100.0 - MIN_SOC) * wpp
    bill = dear_import = 0.0
    empty_at = None
    n = len(R["prices_ct"])
    for i in range(n):
        price, pv, load = R["prices_ct"][i], R["pv_wh"][i], R["load_wh"][i]
        plan = a.plan_arbitrage(_buckets(i, overhead_w),
                                _fleet(MIN_SOC + e / wpp, delivery), ECON)
        net = load - pv
        if net < 0:
            e = min(cap_e, e - net * ETA)
        buy = max(0.0, min(plan.grid_charge_now_wh, 1875.0, (cap_e - e) / ETA))
        bill += buy / 1000 * price + buy * ETA / 1000 * WEAR
        e += buy * ETA
        if net > 0:
            hold = max(0.0, (plan.hold_floor_soc - MIN_SOC) * wpp)
            take = min(net + TRUE_EXPORT_W * 0.25, max(0.0, e - hold), 1875.0)
            e -= take
            short = max(0.0, net - take)
            bill += short / 1000 * price
            if short > 1.0 and price >= 30.0:
                dear_import += short
            if e < 1.0 and empty_at is None and i < R["actuals_until_bucket"]:
                empty_at = i
    return bill, dear_import / 1000, empty_at, e


def test_replay_old_planner_runs_empty_before_the_sun_like_reality():
    _bill, dear_kwh, empty_at, _left = _replay(1.0, 0.0)
    assert empty_at is not None and 64 <= empty_at <= 72, empty_at   # 05:15-07:15
    assert dear_kwh > 1.0, dear_kwh                                  # reality: ~1.2 kWh @ 32-34 ct


def test_replay_measured_planner_buys_at_14_ct_and_never_runs_dry():
    old_bill, _, _, old_left = _replay(1.0, 0.0)
    bill, dear_kwh, empty_at, left = _replay(TRUE_DELIVERY, TRUE_EXPORT_W)
    assert empty_at is None and dear_kwh == 0.0
    # compare fairly: energy left at 13:15 is worth what it costs to buy back in
    # today's 13.5 ct trough, delivered (price / eta + wear)
    refill = 13.5 / ETA + WEAR
    assert bill - left / 1000 * refill < old_bill - old_left / 1000 * refill


def test_replay_seed_values_already_prevent_it():
    # before the history holds a night of discharge: sqrt(eta) and the grid target
    _bill, dear_kwh, empty_at, _left = _replay(ETA ** 0.5, 60.0)
    assert empty_at is None and dear_kwh == 0.0


# ── 4b. the 80% cap as a SOFT cap (hel-140) ───────────────────────────────────
# With the calibration NOT open, the ceiling is the 80% Max Charge SOC. On this
# afternoon the sun alone fills the fleet to 80% before the cheap window ends, so
# even the corrected planner found "no profitable window": no room to carry 14 ct
# energy into the night. plan_with_soft_cap lifts the cap only when that pays.
def _soft(lifted_before=False, lift=5.0, keep=1.0):
    from dataclasses import replace
    bat = replace(_fleet(R["soc_at_1312"], TRUE_DELIVERY), max_soc=80.0)
    return a.plan_with_soft_cap(_buckets(overhead_w=TRUE_EXPORT_W), bat, ECON,
                                hard_max_soc=100.0, lift_gain_ct=lift, keep_gain_ct=keep,
                                lifted_before=lifted_before)


def test_at_80_percent_the_night_cannot_be_bought_for():
    from dataclasses import replace
    bat = replace(_fleet(R["soc_at_1312"], TRUE_DELIVERY), max_soc=80.0)
    plan = a.plan_arbitrage(_buckets(overhead_w=TRUE_EXPORT_W), bat, ECON)
    assert plan.saving_ct == 0.0 and "no profitable window" in plan.reason


def test_soft_cap_lifts_to_carry_14_ct_energy_into_the_night():
    plan, lifted, gain = _soft()
    assert lifted and gain >= 5.0, (gain, plan.reason)
    assert plan.buy_idx == 1 and plan.buy_price_ct < 15.0, plan.reason


def test_soft_cap_hysteresis():
    _plan, _lifted, gain = _soft()
    # a gain between the two thresholds keeps an existing lift but does not start one
    _p, lifted_new, _g = _soft(lifted_before=False, lift=gain + 1.0, keep=gain - 1.0)
    _p, lifted_kept, _g = _soft(lifted_before=True, lift=gain + 1.0, keep=gain - 1.0)
    assert not lifted_new and lifted_kept


# ── 5. the calibration top-up that gave up ────────────────────────────────────
def _calibration(prices, in_progress):
    drift = {"a": tsd._drift(17.4, 4.6)}                  # overdue, as that afternoon
    return tsd._plan(drift, prices=prices, need=6600.0, lookahead=96,
                     in_progress=in_progress, commit_margin_ct=3.0, eta=ETA, wear_ct=WEAR)


def test_calibration_that_had_not_started_still_picks_the_cheapest_window():
    p = _calibration(R["prices_ct"], in_progress=False)
    assert p.status == "grid_waiting", p.reason          # unchanged behaviour: 13.4 ct tomorrow


def test_calibration_already_running_finishes_instead_of_waiting_a_day():
    p = _calibration(R["prices_ct"], in_progress=True)
    assert p.status == "grid_charging" and p.grid_charge_now, p.reason   # 14.2 vs 13.4 ct


def test_a_running_calibration_still_moves_for_a_real_saving_with_nothing_dear_between():
    prices = [22.0] * 4 + [21.0] * 8 + [10.0] * 12 + [22.0] * 72
    p = _calibration(prices, in_progress=True)
    assert p.status == "grid_waiting", p.reason          # 12 ct cheaper, nothing dear before it


def test_a_running_calibration_keeps_buying_when_it_pays_before_the_cheaper_window():
    prices = [18.0] * 4 + [40.0] * 12 + [10.0] * 12 + [22.0] * 68
    p = _calibration(prices, in_progress=True)
    # 8 ct cheaper later, but the 40 ct evening comes first: 18/0.817 + 3.3 = 25 ct
    assert p.status == "grid_charging", p.reason


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} tests passed")
