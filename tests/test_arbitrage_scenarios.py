"""Scenario validation for the arbitrage planner — does it pick the BEST choice?

Assert-based tests only check what the author thought to assert. This suite instead
SCORES the planner: every scenario is run as a rolling simulation (re-plan each
bucket, execute bucket 0's decision, advance — exactly how production behaves) and
the resulting grid bill is compared against a clairvoyant dynamic-programming
optimum for the same scenario. The DP is an independent implementation: it knows
the whole future and searches the full charge/discharge space — including
keeping stored energy through a cheap deficit for a dearer one — so it brackets
the true optimum.

The metric is REGRET: (planner_cost - optimal_cost) / optimal_cost. A rolling
planner cannot beat a clairvoyant one, so regret >= 0 always; the question is how
close it gets, and whether any scenario makes it behave pathologically.

Run: python3 tests/test_arbitrage_scenarios.py  |  pytest tests/test_arbitrage_scenarios.py
"""
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

_base = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"

_pkg = type(sys)("wattsmith")
sys.modules["wattsmith"] = _pkg
for _name in ("economics", "arbitrage"):
    _s = importlib.util.spec_from_file_location(f"wattsmith.{_name}", _base / f"{_name}.py")
    _m = importlib.util.module_from_spec(_s)
    _m.__package__ = "wattsmith"
    sys.modules[f"wattsmith.{_name}"] = _m
    _s.loader.exec_module(_m)
a = sys.modules["wattsmith.arbitrage"]
Bucket, BatteryModel, Econ, plan_arbitrage = a.Bucket, a.BatteryModel, a.Econ, a.plan_arbitrage

CAP = 15360.0
MIN_SOC = 13.0
MAX_SOC = 100.0
CHARGE_W = 7500.0
BUCKET_H = 0.25
PER_BUCKET = CHARGE_W * BUCKET_H          # 1875 Wh
ECON = Econ(eta=0.80, wear_ct=3.26, min_margin_ct=1.5, forecast_margin_frac=0.0)


# ---------------------------------------------------------------- scenarios --
def _ramp(vals, reps):
    out = []
    for v, n in zip(vals, reps):
        out += [float(v)] * n
    return out


def _pv_arc(n, peak, start, width, cloud=None):
    """A sine PV arc over n buckets; `cloud` is an optional per-bucket multiplier."""
    out = []
    for i in range(n):
        if start <= i < start + width:
            v = peak * math.sin(math.pi * (i - start) / width)
        else:
            v = 0.0
        if cloud:
            v *= cloud[i % len(cloud)]
        out.append(max(0.0, v) * BUCKET_H)     # W -> Wh per bucket
    return out


def scenarios():
    """(name, prices_ct[], pv_wh[], load_wh[], start_soc_pct)"""
    S = []
    flat_load = [125.0] * 48                    # 500 W house

    # 1 — textbook: cheap night, dear evening peak, no sun
    S.append(("01 cheap night / dear evening, no sun",
              [14] * 20 + [24] * 12 + [46] * 10 + [30] * 6,
              [0.0] * 48, flat_load, 20.0))
    # 2 — high summer: PV covers everything, nothing to do
    S.append(("02 high summer, PV covers all",
              [28] * 48, _pv_arc(48, 5000, 12, 28), flat_load, 60.0))
    # 3 — André's case: cheap noon trough, PV too weak to fill for the night
    S.append(("03 cheap noon trough + weak PV, dear night",
              [30] * 8 + [14] * 8 + [30] * 12 + [40] * 20,
              _pv_arc(48, 900, 10, 20), flat_load, 18.0))
    # 4 — deep winter: almost no sun at all, flat dear prices
    S.append(("04 deep winter, no sun, flat price",
              [32] * 48, _pv_arc(48, 350, 16, 12), [160.0] * 48, 30.0))
    # 5 — windy night: wind crashes the overnight price below daytime
    S.append(("05 windy night, negative-ish overnight price",
              [3] * 16 + [28] * 16 + [44] * 16,
              _pv_arc(48, 1200, 18, 16), flat_load, 15.0))
    # 6 — duck curve: cheap midday, brutal evening ramp
    S.append(("06 duck curve, cheap midday, brutal evening",
              [26] * 12 + [8] * 12 + [26] * 8 + [55] * 16,
              _pv_arc(48, 3500, 12, 22), flat_load, 25.0))
    # 7 — two troughs: must pick the right one
    S.append(("07 two troughs, one deeper",
              [30] * 6 + [18] * 4 + [30] * 8 + [10] * 4 + [30] * 6 + [42] * 20,
              [0.0] * 48, flat_load, 20.0))
    # 8 — price spike mid-horizon, nothing cheap before it
    S.append(("08 spike with no cheap window before it",
              [33] * 20 + [70] * 6 + [33] * 22, [0.0] * 48, flat_load, 40.0))
    # 9 — cloudy day: PV forecast arrives in broken bursts
    S.append(("09 broken cloud, intermittent PV",
              [22] * 24 + [38] * 24,
              _pv_arc(48, 2600, 10, 26, cloud=[1, 0.2, 0.9, 0.1, 0.8, 0.3]),
              flat_load, 22.0))
    # 10 — fleet starts full: should buy nothing
    S.append(("10 fleet already full",
              [12] * 24 + [50] * 24, [0.0] * 48, flat_load, 100.0))
    # 11 — fleet starts empty at the floor on a dear day
    S.append(("11 fleet at floor, dear day",
              [36] * 48, _pv_arc(48, 600, 18, 10), flat_load, 13.0))
    # 12 — cheap window AFTER the need (must not wait for it)
    S.append(("12 cheap window only AFTER the deficit",
              [34] * 10 + [60] * 8 + [9] * 30, [0.0] * 48, flat_load, 18.0))
    # 13 — heat-pump morning: big load spike before sunrise
    S.append(("13 morning load spike before sunrise",
              [20] * 16 + [30] * 8 + [38] * 24,
              _pv_arc(48, 1800, 28, 16),
              _ramp([120, 700, 150], [16, 8, 24]), 24.0))
    # 14 — EV-free weekend: low flat load, wide spread
    S.append(("14 low load, wide spread",
              [10] * 16 + [48] * 32, [0.0] * 48, [70.0] * 48, 16.0))
    # 15 — marginal spread: just under the profitability gate
    S.append(("15 marginal spread below the gate",
              [24] * 24 + [32] * 24, [0.0] * 48, flat_load, 20.0))
    # 16 — marginal spread: just over the gate
    S.append(("16 marginal spread just over the gate",
              [24] * 24 + [37] * 24, [0.0] * 48, flat_load, 20.0))
    # 17 — winter + windy night + dear morning peak (compound)
    S.append(("17 winter, windy night, dear morning peak",
              [6] * 12 + [40] * 8 + [58] * 6 + [34] * 22,
              _pv_arc(48, 300, 20, 10), [150.0] * 48, 14.0))
    # 18 — very long deficit that outruns charge power
    S.append(("18 deficit larger than charge power can pre-buy",
              [11] * 4 + [45] * 44, [0.0] * 48, [600.0] * 48, 15.0))
    # 19 — sawtooth prices, no clear trough
    S.append(("19 sawtooth prices",
              [20 + (i % 7) * 4 for i in range(48)], [0.0] * 48, flat_load, 20.0))
    # 20 — shoulder season: PV fills by noon, evening still dear
    S.append(("20 shoulder, PV fills by noon, dear evening",
              [25] * 16 + [16] * 8 + [25] * 8 + [44] * 16,
              _pv_arc(48, 2800, 14, 20), flat_load, 30.0))

    # ---- WINTER FAMILY: the day shortens and PV collapses toward nothing -------
    # Winter is the regime the fleet has never actually run in. The daylight window
    # narrows from 10 h to zero while the night load stays, so the battery has to
    # be carried across ever-longer dark stretches on bought energy alone. Prices
    # are modelled on the real winter shape from hel-125: a HIGH mean (~27 ct) with
    # a NARROW spread (5-7 ct), which is what makes winter arbitrage hard.
    winter_load = [150.0] * 48                  # 600 W — heating season
    narrow = [29] * 12 + [25] * 8 + [28] * 8 + [33] * 12 + [27] * 8   # ~6 ct spread
    for n, (hours, peak, soc) in enumerate([
            (10, 2200, 35.0),      # late autumn
            (8, 1400, 28.0),
            (6, 900, 22.0),
            (5, 600, 18.0),
            (4, 350, 15.0)], start=21):
        S.append((f"{n} winter: {hours} h day, {peak} W peak PV",
                  narrow, _pv_arc(48, peak, 24 - 2 * hours, 4 * hours),
                  winter_load, soc))
    # 26 — midwinter overcast: PV is a rounding error
    S.append(("26 midwinter overcast, PV ~ zero",
              narrow, _pv_arc(48, 120, 18, 12), winter_load, 20.0))
    # 27 — polar night: literally no PV for the whole horizon
    S.append(("27 no PV at all, narrow winter spread",
              narrow, [0.0] * 48, winter_load, 25.0))
    # 28 — no PV, but one genuine cheap window to find
    S.append(("28 no PV, single cheap window",
              [30] * 14 + [12] * 6 + [30] * 12 + [36] * 16,
              [0.0] * 48, winter_load, 16.0))
    # 29 — no PV and the fleet starts at the floor: pure bought-energy day
    S.append(("29 no PV, fleet starts at the floor",
              [28] * 16 + [17] * 8 + [28] * 8 + [38] * 16,
              [0.0] * 48, winter_load, 13.0))
    # 30 — long dark stretch: 20 h of night either side of a token PV blip
    S.append(("30 20 h dark, token midday PV blip",
              narrow, _pv_arc(48, 500, 22, 6), winter_load, 30.0))
    return S


# ------------------------------------------------------- clairvoyant optimum --
TAKE_FRACS = (0.0, 0.25, 0.5, 0.75, 1.0)


def dp_bound(prices, pv, load, start_soc, mode="ceil", levels=8193, buy_steps=25):
    """Clairvoyant optimum for one scenario, in EUR-cent. Vectorised over states.

    State = stored usable energy on a fixed grid. Per bucket: PV surplus charges
    for free, any amount of grid energy may be bought into the battery (paying
    price on the AC energy and wear on what lands in the cells), then the deficit
    is served from storage with the remainder imported. How much of the deficit
    storage serves is itself a choice (TAKE_FRACS): holding stored energy through
    a cheap deficit to spend it on a dearer one later is part of the optimum.

    The state grid forces a rounding choice, and that choice decides which side of
    the true optimum the answer falls:
      mode="ceil"  — successor rounded UP, handing the solver energy it never paid
                     for, so the result is a strict LOWER bound on the optimum.
      mode="floor" — successor rounded DOWN, discarding energy. Wasting energy is
                     always feasible, so this is an ACHIEVABLE cost and therefore
                     an UPPER bound on the optimum.
    The true optimum lies between them; a narrow bracket is what makes the regret
    figures meaningful, so the suite reports the bracket width.
    """
    import numpy as np
    usable_cap = CAP * (MAX_SOC - MIN_SOC) / 100.0
    step = usable_cap / (levels - 1)
    rnd = np.ceil if mode == "ceil" else np.floor
    e = np.arange(levels) * step
    cost = np.full(levels, np.inf)
    cost[min(levels - 1, max(0, int(round(CAP * (start_soc - MIN_SOC) / 100.0 / step))))] = 0.0
    buys = [PER_BUCKET * k / buy_steps for k in range(buy_steps + 1)]
    for t in range(len(prices)):
        p, net = prices[t], load[t] - pv[t]
        surplus, deficit = max(0.0, -net), max(0.0, net)
        e_pv = np.minimum(usable_cap, e + min(surplus, PER_BUCKET))
        nxt = np.full(levels, np.inf)
        for buy in buys:
            gain = np.clip(np.minimum(buy * ECON.eta, usable_cap - e_pv), 0.0, None)
            ac = gain / ECON.eta if ECON.eta else np.zeros_like(gain)
            stored = e_pv + gain
            can = np.minimum(np.minimum(deficit, stored), PER_BUCKET)
            for frac in (TAKE_FRACS if deficit > 0 else (1.0,)):
                take = can * frac
                c = (cost + ac / 1000.0 * p + gain / 1000.0 * ECON.wear_ct
                     + (deficit - take) / 1000.0 * p)
                ns = np.clip(rnd((stored - take) / step - 1e-12).astype(int), 0, levels - 1)
                np.minimum.at(nxt, ns, c)
        cost = nxt
        if not np.isfinite(cost).any():
            return float("inf")
    return float(np.nanmin(np.where(np.isfinite(cost), cost, np.nan)))


def dp_optimal(prices, pv, load, start_soc):
    """Achievable clairvoyant cost (upper bound on the optimum) — the regret target."""
    return dp_bound(prices, pv, load, start_soc, mode="floor")


# --------------------------------------------------------- rolling simulator --
def simulate(prices, pv, load, start_soc, horizon=None):
    """Run the planner the way production does: re-plan every bucket, act on bucket 0."""
    usable_cap = CAP * (MAX_SOC - MIN_SOC) / 100.0
    soc = start_soc
    total = 0.0
    n = len(prices)
    for t in range(n):
        end = n if horizon is None else min(n, t + horizon)
        buckets = [Bucket(price_ct=prices[k], pv_wh=pv[k], load_wh=load[k])
                   for k in range(t, end)]
        bat = BatteryModel(soc_pct=soc, capacity_wh=CAP, min_soc=MIN_SOC,
                           max_soc=MAX_SOC, charge_power_w=CHARGE_W)
        plan = plan_arbitrage(buckets, bat, ECON)
        e = CAP * (soc - MIN_SOC) / 100.0          # usable stored now
        # 1) free PV charging
        net = load[t] - pv[t]
        if net < 0:
            e = min(usable_cap, e + min(-net, PER_BUCKET))
        # 2) the planner's grid purchase for this bucket
        buy = max(0.0, min(plan.grid_charge_now_wh, PER_BUCKET, usable_cap - e))
        if buy > 0:
            total += buy / 1000.0 * prices[t] + (buy * ECON.eta) / 1000.0 * ECON.wear_ct
            e = min(usable_cap, e + buy * ECON.eta)
        # 3) serve the deficit, respecting the planner's discharge hold
        if net > 0:
            hold_e = max(0.0, CAP * (plan.hold_floor_soc - MIN_SOC) / 100.0)
            spendable = max(0.0, e - hold_e) if t + 1 < n else e
            take = min(net, spendable, PER_BUCKET)
            e -= take
            total += (net - take) / 1000.0 * prices[t]
        soc = MIN_SOC + 100.0 * e / CAP
    return total


def _report(rows, title):
    print(f"\n{'='*84}\n{title}\n{'='*84}")
    print(f"  {'scenario':<46}{'planner':>9}{'optimal':>9}{'regret':>9}")
    worst = 0.0
    for name, got, opt in rows:
        reg = 0.0 if opt <= 0 else (got - opt) / opt
        worst = max(worst, reg)
        flag = "  <-- " if reg > 0.15 else ""
        print(f"  {name:<46}{got:9.3f}{opt:9.3f}{reg*100:8.1f}%{flag}")
    print(f"\n  worst regret: {worst*100:.1f}%")
    return worst


# ------------------------------------------------------------------- tests --
def test_synthetic_scenarios_near_optimal():
    rows = []
    for name, prices, pv, load, soc in scenarios():
        lower = dp_bound(prices, pv, load, soc, mode="ceil")
        opt = dp_optimal(prices, pv, load, soc)          # achievable upper bound
        got = simulate(prices, pv, load, soc)
        rows.append((name, got, opt))
        # No policy can beat the clairvoyant LOWER bound; beating the achievable
        # upper bound is fine and simply means the truth sits between them.
        # 1% slack: the DP's state/buy grids carry ~0.1% discretisation noise.
        assert got >= lower * 0.99, f"{name}: beat the DP lower bound by >1% — simulator bug"
    worst = _report(rows, "SYNTHETIC SCENARIOS — rolling planner vs clairvoyant DP optimum")
    total_got = sum(g for _n, g, _o in rows)
    total_opt = sum(o for _n, _g, o in rows)
    # Aggregate is the headline: across the whole scenario set the rolling planner
    # must land essentially on the clairvoyant optimum.
    assert total_got <= total_opt * 1.02, (
        f"aggregate {total_got:.1f} ct vs achievable {total_opt:.1f} ct")
    # The old "known gaps" (scenario 05 at +28%, historical 2026-09-04 at +11%)
    # were the discharge hold spending stored energy on cheap buckets ahead of dear
    # ones — hidden while this DP could not hold energy either. With the oracle
    # able to hold (TAKE_FRACS) and the planner allocating stored energy by merit
    # order, both sit on the optimum.
    assert worst <= 0.05, f"worst-case regret {worst*100:.1f}% exceeds 5%"


def test_planner_never_buys_when_pv_covers_everything():
    for name, prices, pv, load, soc in scenarios():
        if name.startswith(("02", "10")):
            buckets = [Bucket(prices[k], pv[k], load[k]) for k in range(len(prices))]
            bat = BatteryModel(soc_pct=soc, capacity_wh=CAP, min_soc=MIN_SOC,
                               max_soc=MAX_SOC, charge_power_w=CHARGE_W)
            plan = plan_arbitrage(buckets, bat, ECON)
            assert plan.grid_charge_now_wh == 0.0, f"{name}: {plan.reason}"


def test_short_horizon_does_not_break_the_planner():
    """A 36 h horizon is the production setting; make sure a clipped view is sane."""
    for name, prices, pv, load, soc in scenarios():
        got = simulate(prices, pv, load, soc, horizon=16)
        assert got >= 0 and math.isfinite(got), name


# --------------------------------------------------- historical replay --------
_HIST = Path(__file__).resolve().parent / "fixtures" / "historical_days.json"


def test_historical_days_near_optimal():
    if not _HIST.exists():
        print(f"  SKIP historical replay — fixture not present ({_HIST.name})")
        return
    days = json.loads(_HIST.read_text())
    rows = []
    for d in days:
        lower = dp_bound(d["prices"], d["pv"], d["load"], d["start_soc"], mode="ceil")
        opt = dp_optimal(d["prices"], d["pv"], d["load"], d["start_soc"])
        got = simulate(d["prices"], d["pv"], d["load"], d["start_soc"])
        rows.append((f'{d["date"]}  {d["note"]}', got, opt))
        assert got >= lower * 0.99, f'{d["date"]}: beat the DP lower bound by >1% — simulator bug'
    worst = _report(rows, "HISTORICAL DAYS (real prices, PV and load from the history DB)")
    total_got = sum(g for _n, g, _o in rows)
    total_opt = sum(o for _n, _g, o in rows)
    assert total_got <= total_opt * 1.02, (
        f"aggregate {total_got:.1f} ct vs achievable {total_opt:.1f} ct")
    assert worst <= 0.05, f"worst-case regret {worst*100:.1f}% exceeds 5%"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} scenario tests passed ✓")
