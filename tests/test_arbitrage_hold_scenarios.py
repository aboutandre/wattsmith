"""Discharge-hold scenarios: does the fleet keep its energy for the dearest hours?

The case that started this (2026-09-24): the fleet cannot cover everything until
the next refill, and the price curve has a dear evening, a cheaper night shoulder
(~33 ct) and a dearer morning peak (~45 ct). Buying at 33 ct to cover 45 ct does
not clear price/eta + wear, but SPENDING energy that is already stored at 33 ct
instead of 45 ct is still a loss, so the planner must hold through the shoulder.

Every scenario is a 15-minute price curve (each bucket its own price, as Tibber
settles it) run as a rolling simulation, exactly as test_arbitrage_scenarios does,
and scored against the clairvoyant DP bracket. On top of the bill, the stored
energy is booked as LOTS so the report can say what each purchase paid off:

  - every grid charge is its own lot: the bucket it was bought in, that bucket's
    price, the energy that landed in the cells and what it cost (price on the AC
    energy + wear on what was stored — the same bill the simulator charges);
  - PV surplus is a lot at 0 ct;
  - the energy in the fleet at the start is a lot at the price the scenario says
    it was charged at;
  - discharge draws lots first-in-first-out, recording the price it displaced.

Energy is fungible, so which lot a kWh "came from" is a bookkeeping convention
(FIFO here); the bill is the same under any convention. What the lots answer is
whether each purchase was paid back by the price it displaced. They do NOT feed
the decision: what a stored kWh cost is sunk — keeping or spending it only
depends on the prices still to come.

Run: python3 tests/test_arbitrage_hold_scenarios.py  |  pytest tests/test_arbitrage_hold_scenarios.py
"""
from __future__ import annotations

import math
import random
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_arbitrage_scenarios as sc  # noqa: E402  (loads the pure planner modules)

Bucket, BatteryModel, plan_arbitrage = sc.Bucket, sc.BatteryModel, sc.plan_arbitrage
CAP, MIN_SOC, MAX_SOC, PER_BUCKET, ECON = sc.CAP, sc.MIN_SOC, sc.MAX_SOC, sc.PER_BUCKET, sc.ECON
USABLE = CAP * (MAX_SOC - MIN_SOC) / 100.0

# Measured mean house load by local hour (W, EV excluded) — seasonal_sim.LOAD_W.
LOAD_W = [279, 277, 284, 282, 306, 472, 432, 597, 678, 646, 609, 714,
          807, 718, 695, 665, 618, 647, 603, 550, 429, 400, 309, 285]

# Hourly anchors read off the 2026-09-24/25 Tibber chart (ct/kWh, gross).
TODAY = [28, 29, 28, 27, 26.5, 27, 29, 33, 35, 33, 28, 22,
         17, 15.3, 15, 17.5, 24, 28, 39, 43, 46, 41, 38, 35]
TOMORROW = [33.5, 33, 33, 34, 34.5, 34, 38, 43, 45, 44, 36, 30,
            24, 18, 17, 21, 28, 34, 40, 46, 49, 44, 39, 36]


# ------------------------------------------------------------ curve helpers --
def quarter_hours(hourly: list[float], seed: int, jitter: float = 0.8) -> list[float]:
    """Hourly anchors -> a 15-min curve: linear between anchors plus seeded
    jitter, so every bucket carries its own price (as the spot market does)."""
    rng = random.Random(seed)
    out = []
    for h, v in enumerate(hourly):
        nxt = hourly[h + 1] if h + 1 < len(hourly) else v
        for q in range(4):
            out.append(round(v + (nxt - v) * q / 4 + rng.uniform(-jitter, jitter), 2))
    return out


def load_curve(n_days: int, scale: float = 1.0) -> list[float]:
    return [LOAD_W[(k // 4) % 24] * scale * sc.BUCKET_H for k in range(96 * n_days)]


def pv_curve(peaks_w: list[float], sunrise: float = 7.5, sunset: float = 19.0) -> list[float]:
    """One sine arc per day (Wh per bucket); `peaks_w` gives each day's peak."""
    out = []
    for peak in peaks_w:
        for k in range(96):
            h = (k + 0.5) / 4
            v = peak * math.sin(math.pi * (h - sunrise) / (sunset - sunrise)) if sunrise < h < sunset else 0.0
            out.append(max(0.0, v) * sc.BUCKET_H)
    return out


def window(series: list[float], start_h: float) -> list[float]:
    """From start_h on day 1 to the end of day 2 — the horizon Tibber publishes."""
    return series[int(start_h * 4):]


def _hold_scenarios():
    """(name, prices, pv, load, start_soc, start_lot_ct). All start mid-afternoon
    of day 1 unless the name says otherwise and run to the end of day 2."""
    S = []
    base = TODAY + TOMORROW
    autumn_pv = [900, 900]                          # weak, as in the chart

    def add(name, hourly, *, start_h=15.5, soc=40.0, lot_ct=0.0, pv=autumn_pv,
            load=1.0, seed=None):
        seed = len(S) + 1 if seed is None else seed
        S.append((name,
                  window(quarter_hours(hourly, seed), start_h),
                  window(pv_curve(pv), start_h),
                  window(load_curve(2, load), start_h),
                  soc, lot_ct))

    # --- the chart itself, fleet too small to reach tomorrow's trough ----------
    add("H01 chart, 40% at 15:30", base, soc=40.0)
    add("H02 chart, 25% at 15:30", base, soc=25.0)
    add("H03 chart, 60% at 15:30 (enough?)", base, soc=60.0)
    add("H04 chart, heat-pump load x1.6", base, soc=45.0, load=1.6)
    add("H05 chart, tomorrow overcast", base, soc=40.0, pv=[900, 150])
    add("H06 chart, tomorrow sunny", base, soc=40.0, pv=[900, 4200])
    add("H07 chart, start 12:00 in the trough", base, start_h=12.0, soc=20.0)
    add("H08 chart, start 22:00, 35% left", base, start_h=22.0, soc=35.0)

    # --- how deep the night shoulder sits under the morning peak ---------------
    for n, shoulder in enumerate((40, 36, 30, 24), start=9):
        tom = list(TOMORROW)
        tom[0:6] = [shoulder] * 6
        add(f"H{n:02d} night shoulder {shoulder} ct vs 45 ct morning",
            TODAY + tom, start_h=20.0, soc=35.0)

    # --- which peak is the dearer one -------------------------------------------
    tom = list(TOMORROW)
    tom[6:10] = [48, 55, 57, 50]
    add("H13 morning peak 57 ct > evening", TODAY + tom, start_h=17.0, soc=40.0)
    today = list(TODAY)
    today[18:22] = [45, 52, 56, 48]
    tom = list(TOMORROW)
    tom[6:10] = [35, 37, 38, 36]
    add("H14 evening peak 56 ct > morning", today + tom, start_h=17.0, soc=40.0)

    # --- what the stored energy cost (sunk: must not change the decision) -----
    add("H15 fleet filled in the 15 ct trough", base, start_h=15.0, soc=55.0, lot_ct=15.0 / 0.8 + 3.26, seed=15)
    add("H16 fleet filled at 30 ct (a bad buy)", base, start_h=15.0, soc=55.0, lot_ct=30.0 / 0.8 + 3.26, seed=15)
    add("H17 fleet filled from PV (0 ct)", base, start_h=15.0, soc=55.0, lot_ct=0.0, seed=15)

    # --- winter: narrow spread, little or no sun --------------------------------
    narrow = [29, 28, 28, 27, 27, 28, 31, 33, 34, 32, 30, 28,
              26, 25, 25, 26, 28, 31, 34, 35, 34, 32, 30, 29]
    add("H18 winter narrow spread, no PV", narrow * 2, start_h=16.0, soc=35.0, pv=[0, 0], load=1.4)
    add("H19 winter narrow spread, token PV", narrow * 2, start_h=16.0, soc=25.0, pv=[300, 300], load=1.4)

    # --- troughs that refill (holding would be wrong) ---------------------------
    tom = list(TOMORROW)
    tom[2:5] = [18, 16, 17]
    add("H20 night trough 16 ct refills before the morning", TODAY + tom, start_h=17.0, soc=40.0)
    add("H21 Dunkelflaute flat ~40 ct", [40 + 3 * math.sin(h / 3) for h in range(48)],
        start_h=15.0, soc=45.0, pv=[100, 100], load=1.2)
    tom = list(TOMORROW)
    tom[7:9] = [88, 92]
    add("H22 90 ct spike at 07-09", TODAY + tom, start_h=18.0, soc=45.0)
    add("H23 noisy 15-min prices around 35 ct", [35] * 48, start_h=15.0, soc=45.0, seed=99)
    S[-1] = (S[-1][0], [p + random.Random(7 + i).uniform(-5, 5) for i, p in enumerate(S[-1][1])],
             *S[-1][2:])
    tom = list(TOMORROW)
    tom[0:6] = [4, 3, 2.5, 2.5, 3, 5]
    add("H24 windy night 3 ct (below wear) then 45 ct", TODAY + tom, start_h=20.0, soc=35.0)
    add("H25 big fleet, low load: nothing to protect", base, start_h=15.5, soc=95.0, load=0.6)
    return S


# ------------------------------------------------------------ lot simulator --
def simulate_lots(prices, pv, load, start_soc, start_lot_ct=0.0, planner=None):
    """sc.simulate's physics, bucket for bucket, with the stored energy booked as
    lots. Returns a dict: cost, lots, and per-bucket traces for the report."""
    planner = planner or plan_arbitrage
    n = len(prices)
    e = CAP * (start_soc - MIN_SOC) / 100.0
    lots = deque()
    ledger = []
    if e > 0:
        lot = dict(kind="start", t=None, price=None, stored=e, left=e,
                   cost=e / 1000.0 * start_lot_ct, used=[])
        lots.append(lot)
        ledger.append(lot)
    total = 0.0
    trace = []
    for t in range(n):
        buckets = [Bucket(price_ct=prices[k], pv_wh=pv[k], load_wh=load[k]) for k in range(t, n)]
        bat = BatteryModel(soc_pct=MIN_SOC + 100.0 * e / CAP, capacity_wh=CAP, min_soc=MIN_SOC,
                           max_soc=MAX_SOC, charge_power_w=sc.CHARGE_W)
        plan = planner(buckets, bat, ECON)
        net = load[t] - pv[t]
        # 1) free PV charging
        if net < 0:
            add = min(USABLE, e + min(-net, PER_BUCKET)) - e
            if add > 0:
                lot = dict(kind="pv", t=t, price=0.0, stored=add, left=add, cost=0.0, used=[])
                lots.append(lot)
                ledger.append(lot)
                e += add
        # 2) the planner's grid purchase: its own lot at this bucket's price
        buy = max(0.0, min(plan.grid_charge_now_wh, PER_BUCKET, USABLE - e))
        if buy > 0:
            stored = buy * ECON.eta
            cost = buy / 1000.0 * prices[t] + stored / 1000.0 * ECON.wear_ct
            total += cost
            lot = dict(kind="grid", t=t, price=prices[t], stored=stored, left=stored, cost=cost, used=[])
            lots.append(lot)
            ledger.append(lot)
            e = min(USABLE, e + stored)
        # 3) serve the deficit, respecting the hold; FIFO through the lots
        take = 0.0
        if net > 0:
            hold_e = max(0.0, CAP * (plan.hold_floor_soc - MIN_SOC) / 100.0)
            spendable = max(0.0, e - hold_e) if t + 1 < n else e
            take = min(net, spendable, PER_BUCKET)
            e -= take
            total += (net - take) / 1000.0 * prices[t]
            need = take
            while need > 1e-9 and lots:
                lot = lots[0]
                d = min(need, lot["left"])
                lot["left"] -= d
                lot["used"].append((t, d, prices[t]))
                need -= d
                if lot["left"] <= 1e-9:
                    lots.popleft()
        trace.append(dict(t=t, price=prices[t], net=net, buy=buy, take=take, e=e,
                          hold=plan.hold_floor_soc))
    return dict(cost=total, ledger=ledger, trace=trace, stranded=e)


def lot_summary(ledger):
    """Payoff per kind: energy stored, energy delivered, the average price it
    displaced, what it cost, and how many grid lots displaced less than they cost."""
    out = {}
    for kind in ("start", "pv", "grid"):
        ls = [lot for lot in ledger if lot["kind"] == kind]
        stored = sum(lot["stored"] for lot in ls)
        delivered = sum(d for lot in ls for _t, d, _p in lot["used"])
        value = sum(d * p for lot in ls for _t, d, p in lot["used"]) / 1000.0
        cost = sum(lot["cost"] for lot in ls)
        losers = sum(1 for lot in ls if kind == "grid"
                     and sum(d * p for _t, d, p in lot["used"]) / 1000.0 < lot["cost"] - 1e-6)
        out[kind] = dict(lots=len(ls), stored=stored, delivered=delivered,
                         avoided_ct=(value / delivered * 1000.0) if delivered else None,
                         value=value, cost=cost, payoff=value - cost, losers=losers)
    return out


def no_battery_cost(prices, pv, load):
    return sum(max(0.0, load[t] - pv[t]) / 1000.0 * prices[t] for t in range(len(prices)))


# ------------------------------------------------------------------- tests --
def test_hold_scenarios_near_optimal():
    rows = []
    for name, prices, pv, load, soc, lot_ct in _hold_scenarios():
        lower = sc.dp_bound(prices, pv, load, soc, mode="ceil")
        upper = sc.dp_bound(prices, pv, load, soc, mode="floor")
        got = simulate_lots(prices, pv, load, soc, lot_ct)["cost"]
        assert got >= lower * 0.99, f"{name}: beat the DP lower bound by >1% — simulator bug"
        rows.append((name, got, lower, upper))
    print(f"\n  {'scenario':<52}{'planner':>9}{'DP lo':>9}{'DP hi':>9}")
    for name, got, lo, hi in rows:
        print(f"  {name:<52}{got:9.2f}{lo:9.2f}{hi:9.2f}")
    got = sum(r[1] for r in rows)
    hi = sum(r[3] for r in rows)
    print(f"  {'TOTAL':<52}{got:9.1f}{sum(r[2] for r in rows):9.1f}{hi:9.1f}")
    assert got <= hi * 1.01, f"aggregate {got:.1f} ct vs achievable {hi:.1f} ct"


def test_holds_through_the_night_shoulder_for_the_morning_peak():
    """H10: 20:00, 35% left, no cheap window before the 43-45 ct morning, and a
    36 ct night shoulder in between. Buying at 36 for 45 does not clear eta +
    wear, but the energy already stored must go to the morning, not the night.
    (The previous hold spent 1.43 kWh overnight and imported 1.56 kWh at the peak.)"""
    S = {s[0][:3]: s for s in _hold_scenarios()}
    _name, prices, pv, load, soc, lot_ct = S["H10"]
    tr = simulate_lots(prices, pv, load, soc, lot_ct)["trace"]
    start = 80                                                          # 20:00

    def in_window(r, a, b):
        return a <= r["t"] + start < b
    shoulder = sum(r["take"] for r in tr if in_window(r, 96, 116))     # 00:00-05:00
    morning = sum(r["take"] for r in tr if in_window(r, 120, 136))     # 06:00-10:00
    morning_import = sum(max(0.0, r["net"]) - r["take"] for r in tr if in_window(r, 120, 136))
    assert morning > 2 * shoulder, (shoulder, morning)
    assert morning_import < 400.0, morning_import


def test_grid_lots_pay_back():
    """Every kWh bought should displace more than it cost, in aggregate."""
    for name, prices, pv, load, soc, lot_ct in _hold_scenarios():
        s = lot_summary(simulate_lots(prices, pv, load, soc, lot_ct)["ledger"])["grid"]
        assert s["payoff"] >= -0.5, f"{name}: grid lots lost {s['payoff']:.2f} ct"


def test_what_stored_energy_cost_does_not_change_the_decision():
    """H15-H17 are the same day with the fleet charged at 15 ct, 30 ct or from PV.
    The price paid is sunk; the plan and the bill must not depend on it."""
    S = {s[0][:3]: s for s in _hold_scenarios()}
    bills = {k: simulate_lots(*S[k][1:])["cost"] for k in ("H15", "H16", "H17")}
    assert max(bills.values()) - min(bills.values()) < 1e-9, bills


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} hold-scenario tests passed ✓")
