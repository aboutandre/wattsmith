"""Autumn-to-spring synthetic seasons for the arbitrage planner (hel-130).

The single-day scenarios in test_arbitrage_scenarios.py hand the planner a PERFECT
forecast, so they measure the algorithm. This module measures the planner the way
it actually runs in production: across multi-day episodes where it only ever sees
FORECASTS (learned load profile, Solcast-style PV estimate, day-ahead prices that
publish at 13:00) and the bill is settled on what really happened.

Everything is seeded and deterministic. The distributions are calibrated on the
78 full days of the Wattsmith history DB (2026-07-04 .. 2026-09-21):

  load   hourly means below; day factor log-sd 0.155 (lag-1 corr 0.47);
         within-day hourly log-sd 0.296 (hour-to-hour corr 0.40)
  PV     intraday Solcast log-sd 0.10 (measured); day-ahead widened for winter
  price  Tibber gross = 23.2 + 1.19 x spot (ct) fits the observed 27-102 ct range;
         winter shape + spreads from hel-125 (median daily spread 5-7 ct Dec-Feb)

Run the full sweep:  python3 tests/seasonal_sim.py [episodes_per_month] [seed0] [knob=value ...]
Stress knobs: load_drift (actual load vs learned), load_noise, pv_da_sd — see STRESS.

FINDINGS (2026-09-22, 48 episodes x 7 days, eta 0.72, margin 15%):
  - algorithm: perfect forecast lands +0.2% on the clairvoyant optimum;
  - production (mean load, pessimistic PV): +1.0% — forecast error is worth ~EUR 3/winter;
  - a CAUTIOUS LOAD FORECAST DOES NOT PAY. mean+10/20%, q60/q75/q90 per-slot quantiles
    and a pad applied only to needs >= 50/60 ct all cost more than the plain mean,
    on every seed set and stress case (noisier load, +12% drift, day-ahead PV sd 0.55).
    They cut spike-price imports but over-buy on every other day. Do not retry
    without new evidence (e.g. a real winter's history DB showing a load bias);
  - PV blend vs pessimistic: blend ~EUR 0.5-1 cheaper over the sweep — noise-level;
  - imports at >= 50 ct with an empty fleet are ~all in Dunkelflaute weeks, where a
    perfect forecast imports just as much (nothing cheaper exists to buy from);
  - eta MISMATCH (planner on the 0.80 seed, battery really 0.72): +EUR 8.77 / +7.23 on
    the two seed sets (~EUR 4-5/winter), 43% more energy bought. Bigger than the whole
    forecast-error gap — set eta_override / wire the measured eta (hel-121).
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_arbitrage_scenarios as sc  # noqa: E402  (loads the pure planner modules)

Bucket, BatteryModel, Econ, plan_arbitrage = sc.Bucket, sc.BatteryModel, sc.Econ, sc.plan_arbitrage
CAP, MIN_SOC, MAX_SOC, PER_BUCKET = sc.CAP, sc.MIN_SOC, sc.MAX_SOC, sc.PER_BUCKET

# Production economics with the MEASURED round-trip efficiency (hel-121: ~0.72),
# used both by the planner and by the simulated battery physics.
ECON = Econ(eta=0.72, wear_ct=3.26, min_margin_ct=1.5, forecast_margin_frac=0.15)

DAY = 96                      # 15-min buckets per day
PUBLISH_HOUR = 13             # Tibber publishes tomorrow's curve ~13:00
HORIZON = 144                 # ARBITRAGE_HORIZON_H = 36 h
SPIKE_CT = 50.0               # "spike" price for the exposure metric

# Measured mean house load by local hour (W, EV excluded), history DB 07-04..09-21.
LOAD_W = [279, 277, 284, 282, 306, 472, 432, 597, 678, 646, 609, 714,
          807, 718, 695, 665, 618, 647, 603, 550, 429, 400, 309, 285]
LOAD_DAY_SD, LOAD_DAY_RHO = 0.155, 0.47
LOAD_HOUR_SD, LOAD_HOUR_RHO = 0.296, 0.40

# month: (mean PV kWh/day, day length h, solar noon local h, extra dark-hour lighting W)
# PV = measured July ~24 kWh/day x typical north-German monthly yield fractions.
MONTHS = {
    10: (10.8, 10.6, 13.3, 20),
    11: (5.3, 8.9, 12.4, 45),
    12: (3.1, 7.8, 12.3, 60),
    1: (4.1, 8.3, 12.5, 55),
    2: (7.9, 9.9, 12.6, 35),
    3: (13.9, 11.9, 12.9, 15),
}
# weather regimes: (name, clearness multiplier on the monthly mean, intraday cloud sd)
WEATHER = [("clear", 1.9, 0.08), ("mixed", 1.0, 0.45), ("overcast", 0.3, 0.25), ("dull", 0.08, 0.2)]
WEATHER_P = {10: [.25, .40, .25, .10], 11: [.15, .35, .35, .15], 12: [.12, .30, .38, .20],
             1: [.15, .30, .37, .18], 2: [.22, .38, .30, .10], 3: [.28, .40, .24, .08]}
PV_DAY_AHEAD_SD = 0.35        # winter day-ahead Solcast error (log), vs 0.10 measured intraday
PV_INTRADAY_SD = 0.10

# Stress knobs for sensitivity sweeps (1.0 = calibrated). LOAD_DRIFT scales the ACTUAL
# load but not the learner's history — a winter house using more than the profile it
# learned from; LOAD_NOISE scales both day and hour variance.
STRESS = {"load_drift": 1.0, "load_noise": 1.0, "pv_da_sd": PV_DAY_AHEAD_SD}

# price regimes per day; winter spot shape (multiplier by hour on the day level)
SPOT_SHAPE = [.92, .88, .86, .85, .87, .93, 1.05, 1.18, 1.20, 1.12, 1.05, 1.00,
              .97, .97, 1.00, 1.06, 1.15, 1.25, 1.25, 1.15, 1.05, 1.00, .97, .93]
PRICE_REGIMES = ("normal", "windy", "spike_eve", "spike_morn", "dunkelflaute")
PRICE_P = {10: [.70, .20, .05, .03, .02], 11: [.60, .18, .09, .05, .08], 12: [.58, .20, .09, .05, .08],
           1: [.60, .18, .09, .05, .08], 2: [.66, .18, .08, .04, .04], 3: [.72, .20, .04, .02, .02]}
MIDDAY_SOLAR_DIP = {10: 0.80, 11: 0.92, 12: 0.97, 1: 0.95, 2: 0.85, 3: 0.65}


def gross_ct(spot_ct: float) -> float:
    return 23.2 + 1.19 * spot_ct


@dataclass
class Episode:
    name: str
    month: int
    days: int                         # scored days; arrays carry 2 extra for the horizon
    start_soc: float
    price: list[float]                # ct, actual = published (day-ahead is exact)
    pv: list[float]                   # Wh actual
    pv_da: list[float]                # Wh day-ahead p50 forecast
    pv_id: list[float]                # Wh intraday p50 forecast
    load: list[float]                 # Wh actual
    load_history: list[list[float]]   # prior days x 24 hourly Wh (what the learner has seen)
    regimes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- generator --
def _pick(rng, weights, prev=None, stick=0.0):
    if prev is not None and rng.random() < stick:
        return prev
    return rng.choices(range(len(weights)), weights)[0]


def _load_days(rng, n, month, day_factor=0.0, scale=1.0):
    """n days of hourly Wh with persistent day factors and AR(1) hourly noise."""
    light = MONTHS[month][3]
    day_sd, hour_sd = LOAD_DAY_SD * STRESS["load_noise"], LOAD_HOUR_SD * STRESS["load_noise"]
    out = []
    for _ in range(n):
        day_factor = LOAD_DAY_RHO * day_factor + math.sqrt(1 - LOAD_DAY_RHO ** 2) * rng.gauss(0, day_sd)
        r, hours = 0.0, []
        for h in range(24):
            r = LOAD_HOUR_RHO * r + math.sqrt(1 - LOAD_HOUR_RHO ** 2) * rng.gauss(0, hour_sd)
            base = LOAD_W[h] + (light if (6 <= h < 8 or 16 <= h < 22) else 0)
            # lognormal, mean-preserving
            f = math.exp(day_factor + r - (day_sd ** 2 + hour_sd ** 2) / 2)
            hours.append(base * f * scale)
        out.append(hours)
    return out, day_factor


def _hours_to_buckets(rng, hours):
    out = []
    for wh in hours:
        q = [math.exp(rng.gauss(0, 0.15)) for _ in range(4)]
        s = sum(q)
        out += [wh * x / s for x in q]
    return out


def _pv_day(rng, month, clear_mult, cloud_sd):
    kwh, day_len, noon, _ = MONTHS[month]
    energy = kwh * 1000 * clear_mult
    rise, width = noon - day_len / 2, day_len
    shape = []
    for i in range(DAY):
        h = (i + 0.5) / 4
        x = (h - rise) / width
        shape.append(math.sin(math.pi * x) ** 1.3 if 0 < x < 1 else 0.0)
    tot = sum(shape) or 1.0
    smooth = [energy * s / tot for s in shape]
    c, actual = 0.0, []
    for v in smooth:
        c = 0.7 * c + math.sqrt(1 - 0.49) * rng.gauss(0, cloud_sd)
        actual.append(v * math.exp(c - cloud_sd ** 2 / 2))
    return smooth, actual


def _price_day(rng, month, regime):
    level = {"normal": rng.uniform(8, 13), "windy": rng.uniform(2, 6),
             "spike_eve": rng.uniform(9, 14), "spike_morn": rng.uniform(9, 14),
             "dunkelflaute": rng.uniform(15, 22)}[regime]
    amp = 1.8 if regime == "dunkelflaute" else 1.0
    spot = []
    for i in range(DAY):
        h = i // 4
        m = 1 + amp * (SPOT_SHAPE[h] - 1)
        if 10 <= h < 16:
            m *= MIDDAY_SOLAR_DIP[month]
        v = level * m
        if regime == "windy" and (h >= 22 or h < 6):
            v -= rng.uniform(1, 6)
        spot.append(v + rng.gauss(0, 0.4))
    if regime in ("spike_eve", "spike_morn"):
        start = rng.uniform(17, 19) if regime == "spike_eve" else rng.uniform(6.5, 8)
        dur = rng.uniform(1.0, 3.0)
        peak = rng.uniform(35, 65) if regime == "spike_eve" else rng.uniform(25, 50)
        for i in range(DAY):
            h = (i + 0.5) / 4
            if start - 1 <= h <= start + dur + 1:     # 1 h shoulders either side
                x = 1 - max(0.0, start - h, h - start - dur)
                spot[i] = max(spot[i], level + (peak - level) * x)
    return [gross_ct(s) for s in spot]


def make_episode(seed: int, month: int, days: int = 7, history_days: int = 42) -> Episode:
    rng = random.Random(seed)
    n = days + 2                                      # +2 so the horizon never runs dry
    hist, df = _load_days(rng, history_days, month)
    load_h, _ = _load_days(rng, n, month, df, scale=STRESS["load_drift"])
    load = [x for day in load_h for x in _hours_to_buckets(rng, day)]
    pv, pv_da, pv_id, price, regimes = [], [], [], [], []
    w = p = None
    for _ in range(n):
        w = _pick(rng, WEATHER_P[month], w, stick=0.45)
        smooth, actual = _pv_day(rng, month, WEATHER[w][1], WEATHER[w][2])
        e_act = sum(actual)
        # forecasts: smooth arc scaled to a noisy estimate of the day's actual energy
        s_tot = sum(smooth) or 1.0
        da = e_act * math.exp(rng.gauss(0, STRESS["pv_da_sd"]))
        idf = e_act * math.exp(rng.gauss(0, PV_INTRADAY_SD))
        pv += actual
        pv_da += [v * da / s_tot for v in smooth]
        pv_id += [v * idf / s_tot for v in smooth]
        p = _pick(rng, PRICE_P[month], p, stick=0.35)
        regimes.append(PRICE_REGIMES[p])
        price += _price_day(rng, month, PRICE_REGIMES[p])
    return Episode(f"m{month:02d}-s{seed}", month, days, rng.uniform(13, 60),
                   price, pv, pv_da, pv_id, load, hist, regimes)


def season(episodes_per_month: int = 8, days: int = 7, seed0: int = 1000) -> list[Episode]:
    out = []
    for month in (10, 11, 12, 1, 2, 3):
        for k in range(episodes_per_month):
            out.append(make_episode(seed0 + month * 100 + k, month, days))
    return out


# -------------------------------------------------- load-forecast variants --
def _quantile(vals, q):
    s = sorted(vals)
    if not s:
        return 0.0
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def load_forecast(history: list[list[float]], method: str) -> list[float]:
    """24 hourly Wh from the learner's history. 'mean', 'mean+15', 'q75', ..."""
    cols = list(zip(*history))
    if method == "mean":
        return [sum(c) / len(c) for c in cols]
    if method.startswith("mean+"):
        f = 1 + float(method[5:]) / 100
        return [f * sum(c) / len(c) for c in cols]
    if method.startswith("q"):
        return [_quantile(c, float(method[1:]) / 100) for c in cols]
    raise ValueError(method)


# ------------------------------------------------------------- simulation --
@dataclass
class Result:
    cost_ct: float
    bought_wh: float
    spike_import_wh: float     # imported at >= SPIKE_CT while the fleet was empty
    spike_cost_ct: float


def _pv_forecast(ep: Episode, t: int, k: int, confidence: str) -> float:
    """What Solcast shows at time t for bucket k: intraday for today, day-ahead after."""
    same_day = (k // DAY) == (t // DAY)
    p50 = ep.pv_id[k] if same_day else ep.pv_da[k]
    sd = PV_INTRADAY_SD if same_day else STRESS["pv_da_sd"]
    p10 = p50 * math.exp(-1.2816 * sd)
    if confidence == "pessimistic":
        return p10
    if confidence == "blend":
        return (p50 + p10) / 2
    return p50


def simulate(ep: Episode, load_method: str = "mean", pv_confidence: str = "pessimistic",
             oracle: bool = False, econ: Econ = ECON, true_eta: float | None = None) -> Result:
    """Rolling planner over the episode; decisions on forecasts, bill on actuals.

    `econ` is what the planner BELIEVES; `true_eta` (default: econ.eta) is what the
    simulated battery actually delivers — e.g. planning on the 0.80 seed while the
    real round trip is 0.72.
    """
    eta = econ.eta if true_eta is None else true_eta
    usable_cap = CAP * (MAX_SOC - MIN_SOC) / 100.0
    hourly = None if oracle else load_forecast(ep.load_history, load_method)
    e = CAP * (ep.start_soc - MIN_SOC) / 100.0
    cost = bought = spike_wh = spike_ct = 0.0
    n = ep.days * DAY
    for t in range(n):
        day, hour = divmod(t, DAY)
        visible_end = (day + (2 if hour // 4 >= PUBLISH_HOUR else 1)) * DAY
        end = min(visible_end, t + HORIZON, len(ep.price))
        if oracle:
            buckets = [Bucket(ep.price[k], ep.pv[k], ep.load[k]) for k in range(t, end)]
        else:
            buckets = [Bucket(ep.price[k], _pv_forecast(ep, t, k, pv_confidence),
                              hourly[(k % DAY) // 4] / 4.0) for k in range(t, end)]
        soc = MIN_SOC + 100.0 * e / CAP
        plan = plan_arbitrage(buckets, BatteryModel(soc, CAP, MIN_SOC, MAX_SOC, sc.CHARGE_W), econ)
        net = ep.load[t] - ep.pv[t]
        if net < 0:
            e = min(usable_cap, e + min(-net, PER_BUCKET))
        buy = max(0.0, min(plan.grid_charge_now_wh, PER_BUCKET, (usable_cap - e) / eta))
        if buy > 0:
            cost += buy / 1000.0 * ep.price[t] + buy * eta / 1000.0 * econ.wear_ct
            e = min(usable_cap, e + buy * eta)
            bought += buy
        if net > 0:
            hold_e = max(0.0, CAP * (plan.hold_floor_soc - MIN_SOC) / 100.0)
            take = min(net, max(0.0, e - hold_e), PER_BUCKET)
            empty = e - take < 1.0
            e -= take
            imp = net - take
            cost += imp / 1000.0 * ep.price[t]
            if imp > 1.0 and empty and ep.price[t] >= SPIKE_CT:
                spike_wh += imp
                spike_ct += imp / 1000.0 * ep.price[t]
    cost -= e / 1000.0 * terminal_value_ct(ep)
    return Result(cost, bought, spike_wh, spike_ct)


def terminal_value_ct(ep: Episode) -> float:
    """Leftover stored energy is credited at the median price of the next day — what it
    will displace — so a variant that ends fuller isn't charged for energy it still holds."""
    n = ep.days * DAY
    nxt = sorted(ep.price[n:n + DAY])
    return nxt[len(nxt) // 2]


def dp_optimum(ep: Episode, levels: int = 2049, buy_steps: int = 25) -> float:
    """Clairvoyant achievable optimum on the ACTUALS (floor rounding), same credit."""
    import numpy as np
    n = ep.days * DAY
    usable_cap = CAP * (MAX_SOC - MIN_SOC) / 100.0
    step = usable_cap / (levels - 1)
    e = np.arange(levels) * step
    cost = np.full(levels, np.inf)
    cost[min(levels - 1, int(round(CAP * (ep.start_soc - MIN_SOC) / 100.0 / step)))] = 0.0
    eta = ECON.eta
    buys = [PER_BUCKET * k / buy_steps for k in range(buy_steps + 1)]
    for t in range(n):
        p, net = ep.price[t], ep.load[t] - ep.pv[t]
        surplus, deficit = max(0.0, -net), max(0.0, net)
        e_pv = np.minimum(usable_cap, e + min(surplus, PER_BUCKET))
        nxt = np.full(levels, np.inf)
        for buy in buys:
            gain = np.clip(np.minimum(buy * eta, usable_cap - e_pv), 0.0, None)
            stored = e_pv + gain
            take = np.minimum(np.minimum(deficit, stored), PER_BUCKET)
            c = cost + gain / eta / 1000.0 * p + gain / 1000.0 * ECON.wear_ct + (deficit - take) / 1000.0 * p
            ns = np.clip(np.floor((stored - take) / step - 1e-12).astype(int), 0, levels - 1)
            np.minimum.at(nxt, ns, c)
        cost = nxt
    final = cost - e / 1000.0 * terminal_value_ct(ep)
    return float(np.min(final[np.isfinite(final)]))


# ------------------------------------------------------------------ sweep --
VARIANTS = [
    # (label, load method, pv confidence, oracle)
    ("perfect forecast", "mean", "pessimistic", True),
    ("mean  / pv blend", "mean", "blend", False),
    ("mean", "mean", "pessimistic", False),
    ("mean+10%", "mean+10", "pessimistic", False),
    ("mean+20%", "mean+20", "pessimistic", False),
    ("q60", "q60", "pessimistic", False),
    ("q75", "q75", "pessimistic", False),
    ("q90", "q90", "pessimistic", False),
]


def _run_one(ep: Episode):
    row = {"ep": ep.name, "month": ep.month, "regimes": ep.regimes[:ep.days], "opt": dp_optimum(ep)}
    for label, method, conf, oracle in VARIANTS:
        row[label] = simulate(ep, method, conf, oracle)
    return row


def run_sweep(episodes: list[Episode], procs: int = 8):
    import multiprocessing as mp
    with mp.get_context("fork").Pool(procs) as pool:
        return pool.map(_run_one, episodes)


def report(rows) -> None:
    labels = [v[0] for v in VARIANTS]
    months = (10, 11, 12, 1, 2, 3)
    opt_all = sum(r["opt"] for r in rows)
    print(f"\n{len(rows)} episodes x 7 days — costs in EUR, regret vs clairvoyant optimum on actuals")
    print(f"{'variant':<18}" + "".join(f"{m:>8}" for m in ("Oct", "Nov", "Dec", "Jan", "Feb", "Mar"))
          + f"{'total':>9}{'regret':>8}{'worst€':>7}{'kWh buy':>8}{'spike€':>8}")
    print(f"{'optimum':<18}" + "".join(
        f"{sum(r['opt'] for r in rows if r['month'] == m) / 100:8.2f}" for m in months)
        + f"{opt_all / 100:9.2f}")
    for lab in labels:
        tot = sum(r[lab].cost_ct for r in rows)
        # absolute: a sunny March week can cost ~0, so a ratio explodes
        worst = max(r[lab].cost_ct - r["opt"] for r in rows) / 100
        print(f"{lab:<18}" + "".join(
            f"{sum(r[lab].cost_ct for r in rows if r['month'] == m) / 100:8.2f}" for m in months)
            + f"{tot / 100:9.2f}{(tot - opt_all) / opt_all * 100:7.1f}%{worst:7.2f}"
            + f"{sum(r[lab].bought_wh for r in rows) / 1000:8.0f}"
            + f"{sum(r[lab].spike_cost_ct for r in rows) / 100:8.2f}")
    # spike-day slice: episodes containing a spike or Dunkelflaute day
    spiky = [r for r in rows if any(x in ("spike_eve", "spike_morn", "dunkelflaute") for x in r["regimes"])]
    if spiky:
        o = sum(r["opt"] for r in spiky)
        print(f"\nepisodes with spike/Dunkelflaute days: {len(spiky)}  (optimum {o / 100:.2f} EUR)")
        for lab in labels:
            t = sum(r[lab].cost_ct for r in spiky)
            print(f"  {lab:<18}{t / 100:8.2f}  regret {(t - o) / o * 100:5.1f}%")


if __name__ == "__main__":
    import time
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    seed0 = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    for kv in sys.argv[3:]:                      # e.g. load_drift=1.1 pv_da_sd=0.5
        key, val = kv.split("=")
        STRESS[key] = float(val)
    print("stress:", STRESS, "seed0:", seed0)
    t0 = time.time()
    rows = run_sweep(season(k, seed0=seed0))
    report(rows)
    print(f"\n({time.time() - t0:.0f} s)")
