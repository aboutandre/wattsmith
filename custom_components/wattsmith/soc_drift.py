"""BMS SOC drift + full-charge calibration (pure — no Home Assistant, no I/O).

The Marstek BMS reports SOC by counting charge in and out, and that count drifts:
on this fleet the displayed SOC falls ~1.4 points below reality per kWh discharged
(~2.7 pts per day of normal cycling) until the battery reaches full, when the BMS
snaps it back to 100 (hel-134). Two consequences follow, and both are handled here:

  - energy below the displayed floor is STRANDED. The battery's own firmware stops
    discharging at its displayed SOC (depth-of-discharge floor >= 12%), so software
    cannot reach it — only a full charge, which resets the count, releases it.
    Hence the calibration charge: reach 100% whenever the predicted drift grows too
    large. The drift rate is learned per battery from its own past resets, so the
    schedule adapts to how hard the battery is cycled and to a firmware fix.
  - any energy-per-%SOC measurement is biased by the drift. Between two full-charge
    resets the true SOC is exactly 100% at both ends, so the AC energy balance over
    that window is exact — which is where round-trip η is measured (hel-133).

Rows are history-DB battery_bucket dicts: ts_start, battery_id, charge_wh,
discharge_wh, soc_start, soc_end (15-minute buckets).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

FULL_SOC = 99.0          # a bucket ending at or above this counts as "full"
BUCKET_S = 900


@dataclass(frozen=True)
class Window:
    """One battery's history between two consecutive full-charge resets."""
    battery_id: str
    start_ts: int
    end_ts: int
    charge_wh: float
    discharge_wh: float
    jump_pts: float          # size of the BMS correction that closed the window

    @property
    def hours(self) -> float:
        return (self.end_ts - self.start_ts) / 3600.0


def _by_battery(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("soc_start") is None or r.get("soc_end") is None:
            continue
        out.setdefault(r.get("battery_id"), []).append(r)
    for bat_rows in out.values():
        bat_rows.sort(key=lambda r: r["ts_start"])
    return out


def _is_anchor(r: dict) -> bool:
    """First bucket of a full episode: ends full, did not start full."""
    return r["soc_end"] >= FULL_SOC and r["soc_start"] < FULL_SOC


def full_to_full_windows(rows: list[dict], min_buckets: int = 8) -> list[Window]:
    """Every gap-free stretch between two consecutive full-charge anchors."""
    windows: list[Window] = []
    for bid, bat_rows in _by_battery(rows).items():
        anchors = [i for i, r in enumerate(bat_rows) if _is_anchor(r)]
        for i0, i1 in zip(anchors, anchors[1:]):
            seg = bat_rows[i0 + 1:i1 + 1]
            if len(seg) < min_buckets:
                continue
            if any(b["ts_start"] - a["ts_start"] != BUCKET_S for a, b in zip(seg, seg[1:])):
                continue                   # a logging gap breaks the energy balance
            windows.append(Window(
                battery_id=bid,
                start_ts=seg[0]["ts_start"],
                end_ts=seg[-1]["ts_start"] + BUCKET_S,
                charge_wh=sum(r.get("charge_wh") or 0.0 for r in seg),
                discharge_wh=sum(r.get("discharge_wh") or 0.0 for r in seg),
                jump_pts=_jump_pts(seg),
            ))
    return windows


def _jump_pts(seg: list[dict]) -> float:
    """The BMS correction in the window's last bucket, net of that bucket's charging.

    The reset bucket also charged normally before the snap; that part is taken
    out using the Wh-per-point the same window's charge-only buckets showed.
    """
    last = seg[-1]
    raw = last["soc_end"] - last["soc_start"]
    charged = [r for r in seg[:-1] if (r.get("charge_wh") or 0.0) > 20.0
               and (r.get("discharge_wh") or 0.0) < 2.0]
    rise = sum(r["soc_end"] - r["soc_start"] for r in charged)
    if rise <= 0:
        return raw
    wh_per_pt = sum(r["charge_wh"] for r in charged) / rise
    return max(0.0, raw - (last.get("charge_wh") or 0.0) / wh_per_pt)


@dataclass(frozen=True)
class DriftFit:
    pts_per_kwh: float       # SOC under-reading per kWh discharged since the last reset
    windows: int


def fit_drift_rate(windows: list[Window], min_windows: int = 4) -> DriftFit | None:
    """Least squares  jump = k * kWh_discharged + c  over the windows given.

    The intercept soaks up what every reset carries regardless of throughput
    (integer rounding, the last partial bucket); only k is used for prediction.
    """
    pts = [(w.discharge_wh / 1000.0, w.jump_pts) for w in windows]
    if len(pts) < min_windows:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return None
    k = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return DriftFit(pts_per_kwh=max(0.0, k), windows=n)


@dataclass(frozen=True)
class EtaFit:
    eta: float               # marginal round trip (what an extra stored kWh returns)
    standby_w: float         # per-battery draw paid regardless of cycling
    windows: int


def fit_eta_standby(windows: list[Window], min_windows: int = 6) -> EtaFit | None:
    """Least squares  discharged = eta * charged - standby * hours  (no intercept).

    Exact because both ends of every window are a BMS-calibrated 100%. Standby is
    separated out because it is paid whether or not arbitrage buys anything; the
    planner needs the marginal η, not the naive out/in (which includes it).
    """
    if len(windows) < min_windows:
        return None
    a11 = sum(w.charge_wh ** 2 for w in windows)
    a12 = sum(w.charge_wh * w.hours for w in windows)
    a22 = sum(w.hours ** 2 for w in windows)
    b1 = sum(w.charge_wh * w.discharge_wh for w in windows)
    b2 = sum(w.hours * w.discharge_wh for w in windows)
    det = a11 * a22 - a12 * a12
    if det <= 0:
        return None
    # normal equations for x = (eta, -standby)
    eta = (b1 * a22 - b2 * a12) / det
    neg_s = (a11 * b2 - a12 * b1) / det
    return EtaFit(eta=eta, standby_w=-neg_s, windows=len(windows))


@dataclass(frozen=True)
class BatteryDrift:
    battery_id: str
    last_full_ts: float | None    # None: not full anywhere in the rows given
    discharged_wh: float          # since the last full charge
    predicted_pts: float | None   # expected under-reading now (None: no drift model)


def battery_drift_now(
    rows: list[dict],
    fits: dict[str, DriftFit],
    fleet_fit: DriftFit | None,
    live_soc: dict[str, float | None],
    now_ts: float,
) -> dict[str, BatteryDrift]:
    """Per battery: when it was last full and how far its SOC has drifted since.

    A battery reading full right now has zero drift regardless of the DB (which
    lags by up to one bucket). Batteries without their own fit use the fleet's.
    """
    by = _by_battery(rows)
    out: dict[str, BatteryDrift] = {}
    for bid in set(by) | set(live_soc):
        soc = live_soc.get(bid)
        if soc is not None and soc >= FULL_SOC:
            out[bid] = BatteryDrift(bid, now_ts, 0.0, 0.0)
            continue
        bat_rows = by.get(bid, [])
        last = max((i for i, r in enumerate(bat_rows) if r["soc_end"] >= FULL_SOC), default=None)
        if last is None:
            out[bid] = BatteryDrift(bid, None, 0.0, None)
            continue
        since = bat_rows[last + 1:]
        discharged = sum(r.get("discharge_wh") or 0.0 for r in since)
        fit = fits.get(bid) or fleet_fit
        predicted = fit.pts_per_kwh * discharged / 1000.0 if fit else None
        out[bid] = BatteryDrift(bid, bat_rows[last]["ts_start"] + BUCKET_S, discharged, predicted)
    return out


@dataclass(frozen=True)
class CalibrationPlan:
    status: str               # off | ok | due | grid_waiting | grid_charging
    open_ceiling: bool        # let PV charge the fleet to 100%
    grid_charge_now: bool     # buy from the grid this bucket to finish the job
    due_ids: tuple[str, ...]
    reason: str


def plan_calibration(
    drift: dict[str, BatteryDrift],
    now_ts: float,
    enabled: bool,
    threshold_pts: float,
    max_days: float,
    grid_enabled: bool,
    grid_extra_pts: float,
    prices_ct: list[float],
    need_wh: float,
    per_bucket_wh: float,
    lookahead: int = 96,
) -> CalibrationPlan:
    """Decide whether the fleet needs a full charge, and whether from PV or grid.

    A battery is DUE once its predicted drift reaches `threshold_pts`, or after
    `max_days` without a full charge (also the rule when no drift model exists).
    Due opens the charge ceiling so PV can finish the job. If PV has not managed
    it by `threshold_pts + grid_extra_pts` (or `max_days` is exceeded), the fleet
    tops up from the grid in the cheapest buckets of the next `lookahead`.
    """
    if not enabled:
        return CalibrationPlan("off", False, False, (), "calibration disabled")

    def days(d: BatteryDrift) -> float:
        return math.inf if d.last_full_ts is None else (now_ts - d.last_full_ts) / 86400.0

    due, overdue = [], []
    for bid, d in sorted(drift.items()):
        pts = d.predicted_pts
        is_due = days(d) >= max_days or (pts is not None and pts >= threshold_pts)
        if is_due:
            due.append(bid)
            if days(d) >= max_days or (pts is not None and pts >= threshold_pts + grid_extra_pts):
                overdue.append(bid)
    if not due:
        worst = max(drift.values(), key=lambda d: d.predicted_pts or 0.0, default=None)
        reason = (f"ok — worst predicted drift {worst.predicted_pts:.1f} pts"
                  if worst is not None and worst.predicted_pts is not None else "ok")
        return CalibrationPlan("ok", False, False, (), reason)

    ids = tuple(due)
    if not (grid_enabled and overdue and prices_ct and per_bucket_wh > 0):
        return CalibrationPlan("due", True, False, ids,
                               f"due ({', '.join(i[-4:] for i in ids)}) — charge ceiling opened to 100% for PV")
    window = prices_ct[:lookahead]
    n = max(1, math.ceil(need_wh / per_bucket_wh)) + 1   # +1: the house draws meanwhile
    chosen = sorted(range(len(window)), key=lambda i: (window[i], i))[:n]
    if 0 in chosen:
        return CalibrationPlan("grid_charging", True, True, ids,
                               f"overdue — topping up from the grid @ {window[0]:.1f} ct "
                               f"(cheapest {n} buckets)")
    first = min(chosen)
    return CalibrationPlan("grid_waiting", True, False, ids,
                           f"overdue — grid top-up waits for bucket {first} @ {window[first]:.1f} ct")
