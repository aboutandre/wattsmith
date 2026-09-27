"""House-load forecast from the history DB: 15-minute slots by weekday (hel-141).

The planner needs to know how much energy the house will draw in every
15-minute bucket of its horizon, because that is what the batteries have to
cover when the sun does not. It used to take this from BaselineLearner: one
value per (weekday, HOUR), fed by an instantaneous 5-minute snapshot of the
house-consumption sensor, with a configured 500 W for any slot it had not
learned. And ArbitrageCoordinator._load_by_hour applied TODAY's weekday to the
whole 36-hour horizon, so tomorrow was forecast with today's pattern.

This module learns the profile from the history DB instead. `bucket.house_wh` is
the exact energy the house used in each 15-minute bucket, and the DB keeps it
forever. The profile is re-fitted every 6 hours by the arbitrage coordinator,
so it follows the season and the household's routines.

THE MODEL, and why it is this simple. Walk-forward backtest on 2026-07-04..09-26
(85 days; each day forecast only from the days before it):

    model                                   6-hour-window error   night (18-06) error / bias
    fixed 500 W (the old fallback)                  32.4 %               33.2 % / +32 %
    weekday x hour (~ BaselineLearner)              14.4 %                9.5 % / -4.9 %
    all days x 15 min, last 28 days                 12.7 %                8.4 % / -2.7 %
    THIS: weekday x 15 min, 56 days,                12.7 %                8.1 % / -3.4 %
          half-life 14 d, prior 3 days, +-1 slot

  - 15-minute slots, per weekday. With only ~8 weeks of history a weekday's own
    slot is noisy (8 samples), so each weekday profile is SHRUNK toward the
    all-days profile by `prior_days` pseudo-days. A weekday with little data
    therefore follows the all-days profile. Once a weekday really differs (a
    school or office day), its own data outweighs the prior and it takes over.
  - Recency weighting (half-life 14 days over a 56-day window) follows the season:
    the evening load moves as the days shorten.
  - +-1 slot smoothing takes the edge off single appliance events, which recur
    only roughly at the same quarter hour.
  - Mean, not median: the planner sums energy, and the median of a spiky load
    under-counts it.

Tried and REJECTED on the same backtest (do not re-add without new evidence):
  - Same-day scaling ("today runs 20% high, so scale the next hours"): worse in
    every setting. The 6-hour error went from 13.0% to 13.2-17.3%, and the
    longer the look-back or fade, the worse it got. Household load is not
    persistent enough: a busy hour says little about the next one. "Dynamic"
    here therefore means re-fitting on recent weeks, not intraday correction.
  - Shorter windows (14/21 days) or no smoothing: slightly worse, within noise.
  - Weekend/workday split instead of weekdays: no better than weekdays with
    the prior.

The profile is EXPECTED energy. The planner's forecast margin (15% by default)
is the safety buffer on top of it; the profile itself runs ~3.4% low on average.

Local time: rows are keyed by their `local_start`, and lookups use
datetime.fromtimestamp(ts), process-local like arbitrage.local_hour (see there,
hel-127). Pure: no Home Assistant, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SLOTS_PER_DAY = 96
SLOTS_PER_WEEK = 7 * SLOTS_PER_DAY
BUCKET_S = 900


@dataclass(frozen=True)
class LoadProfile:
    """Expected house energy (Wh) per 15-minute bucket, by weekday and time of day."""

    wh: tuple[float, ...]        # SLOTS_PER_WEEK values; index = weekday * 96 + slot
    days: int                    # distinct days of history it rests on

    def at(self, ts: float) -> float:
        """Expected Wh for the 15-minute bucket that starts at (or contains) `ts`."""
        d = datetime.fromtimestamp(ts)
        return self.wh[d.weekday() * SLOTS_PER_DAY + d.hour * 4 + d.minute // 15]

    def energy_between(self, t0: float, t1: float) -> float:
        """Expected Wh from t0 to t1, counting partial buckets pro rata."""
        total, ts = 0.0, t0
        while ts < t1:
            nxt = min(t1, (int(ts // BUCKET_S) + 1) * BUCKET_S)
            total += self.at(ts) * (nxt - ts) / BUCKET_S
            ts = nxt
        return total


def fit_load_profile(
    rows: list[dict],
    now_ts: float,
    window_days: int = 56,
    half_life_days: float = 14.0,
    prior_days: float = 3.0,
    smooth_slots: int = 1,
    min_days: int = 7,
) -> LoadProfile | None:
    """Fit the weekday x 15-minute profile from history-DB `bucket` rows.

    Needs `ts_start`, `local_start` ("YYYY-MM-DDTHH:MM:SS", local) and `house_wh`.
    Rows older than `window_days` are ignored; each day is weighted
    0.5 ** (age_days / half_life_days). Returns None with fewer than `min_days`
    distinct days, too little to trust, and the caller keeps its fallback.
    """
    by_day: dict[str, dict[int, float]] = {}
    weekday_of: dict[str, int] = {}
    age_days: dict[str, float] = {}
    for r in rows:
        wh, local = r.get("house_wh"), r.get("local_start")
        if wh is None or not local or now_ts - r["ts_start"] > window_days * 86400:
            continue
        stamp = datetime.fromisoformat(local[:19])
        day = local[:10]
        by_day.setdefault(day, {})[stamp.hour * 4 + stamp.minute // 15] = float(wh)
        weekday_of[day] = stamp.weekday()
        age_days[day] = (now_ts - r["ts_start"]) / 86400.0
    if len(by_day) < min_days:
        return None
    weight = {day: 0.5 ** (age_days[day] / half_life_days) for day in by_day}

    def weighted_mean(days: list[str], slot: int) -> tuple[float, float] | None:
        num = den = 0.0
        for day in days:
            v = by_day[day].get(slot)
            if v is not None:
                num += weight[day] * v
                den += weight[day]
        return (num / den, den) if den > 0 else None

    all_days = list(by_day)
    all_profile = []
    for slot in range(SLOTS_PER_DAY):
        m = weighted_mean(all_days, slot)
        all_profile.append(m[0] if m else 0.0)
    week: list[float] = []
    for wd in range(7):
        days_wd = [d for d in all_days if weekday_of[d] == wd]
        for slot in range(SLOTS_PER_DAY):
            m = weighted_mean(days_wd, slot)
            if m is None:
                week.append(all_profile[slot])
            else:
                mean_wd, w_wd = m
                # shrink toward the all-days profile by `prior_days` pseudo-days
                week.append((w_wd * mean_wd + prior_days * all_profile[slot]) / (w_wd + prior_days))
    if smooth_slots > 0:
        k = smooth_slots
        # circular over the whole week, so Sunday 23:45 blends into Monday 00:00
        week = [sum(week[(i + j) % SLOTS_PER_WEEK] for j in range(-k, k + 1)) / (2 * k + 1)
                for i in range(SLOTS_PER_WEEK)]
    return LoadProfile(wh=tuple(week), days=len(by_day))
