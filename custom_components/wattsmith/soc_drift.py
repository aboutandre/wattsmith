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
    missing_buckets: int = 0  # logging gaps inside the window (restarts)

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


def _anchors(bat_rows: list[dict]) -> list[int]:
    """First bucket of each full episode: ends full, and the battery was not full
    before it. "Before" is the previous bucket's END, not this bucket's start: a
    restart can split the reset bucket so it starts already full (seen live on
    2026-09-22: F11 snapped at 15:16, the post-restart bucket started at 100)."""
    out = []
    for i, r in enumerate(bat_rows):
        before = bat_rows[i - 1]["soc_end"] if i > 0 else r["soc_start"]
        if r["soc_end"] >= FULL_SOC and before < FULL_SOC:
            out.append(i)
    return out


def full_to_full_windows(
    rows: list[dict], min_buckets: int = 8, max_gap_buckets: int = 0,
) -> list[Window]:
    """Every stretch between two consecutive full-charge anchors.

    `max_gap_buckets` tolerates short logging gaps (an HA restart drops a bucket).
    The η fit must stay strict (a gap is missing energy in the balance), but the
    reset itself is measured exactly, and one missing bucket barely changes the
    energy discharged since the last full charge — so the drift fit and the
    reset log accept small gaps rather than discard a whole week.
    """
    windows: list[Window] = []
    for bid, bat_rows in _by_battery(rows).items():
        anchors = _anchors(bat_rows)
        for i0, i1 in zip(anchors, anchors[1:]):
            seg = bat_rows[i0 + 1:i1 + 1]
            if len(seg) < min_buckets:
                continue
            missing = sum((b["ts_start"] - a["ts_start"]) // BUCKET_S - 1
                          for a, b in zip(seg, seg[1:]))
            if missing > max_gap_buckets:
                continue
            windows.append(Window(
                battery_id=bid,
                start_ts=seg[0]["ts_start"],
                end_ts=seg[-1]["ts_start"] + BUCKET_S,
                charge_wh=sum(r.get("charge_wh") or 0.0 for r in seg),
                discharge_wh=sum(r.get("discharge_wh") or 0.0 for r in seg),
                jump_pts=_jump_pts(seg),
                missing_buckets=int(missing),
            ))
    return windows


def _jump_pts(seg: list[dict]) -> float:
    """The BMS correction in the window's last bucket, net of that bucket's charging.

    The reset bucket also charged normally before the snap; that part is taken
    out using the Wh-per-point the same window's charge-only buckets showed.
    The jump is measured from the PREVIOUS bucket's end SOC: an HA restart inside
    the reset bucket starts a partial bucket whose soc_start is sampled after the
    snap (seen live: 98 -> 100 recorded for an 80 -> 100 reset).
    """
    last = seg[-1]
    before = seg[-2]["soc_end"] if len(seg) >= 2 else last["soc_start"]
    raw = last["soc_end"] - before
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


def reset_events(windows: list[Window], min_windows: int = 4) -> list[dict]:
    """One record per BMS reset, for the history DB's calibration_event table.

    The prediction for each reset uses only resets that ENDED BEFORE its window
    began (walk-forward, like the backtest) — the battery's own history if it has
    enough, otherwise the fleet's — so the log shows how the model would really
    have done. predicted_pts is None while there is not enough prior history.
    """
    ordered = sorted(windows, key=lambda w: w.end_ts)
    out: list[dict] = []
    for w in ordered:
        prior = [p for p in ordered if p.end_ts <= w.start_ts]
        own = [p for p in prior if p.battery_id == w.battery_id]
        fit = fit_drift_rate(own, min_windows) or fit_drift_rate(prior, min_windows)
        kwh = w.discharge_wh / 1000.0
        out.append({
            "ts": w.end_ts - BUCKET_S,              # the bucket in which the BMS reset
            "battery_id": w.battery_id,
            "days_since_full": round(w.hours / 24.0, 3),
            "discharged_kwh": round(kwh, 3),
            "predicted_pts": round(fit.pts_per_kwh * kwh, 2) if fit else None,
            "actual_pts": round(w.jump_pts, 2),
            "drift_rate": round(fit.pts_per_kwh, 3) if fit else None,
            "missing_buckets": w.missing_buckets,
        })
    return out


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
class DeliveryFit:
    factor: float                    # socket Wh delivered per nominal Wh of displayed SOC
    points: float                    # displayed-SOC points of discharge it rests on
    per_battery: dict[str, float]    # the same ratio per battery (diagnostics)


def fit_delivery_factor(
    rows: list[dict],
    capacity_wh: dict[str, float],
    min_points: float = 100.0,
    min_run_pts: float = 3.0,
) -> DeliveryFit | None:
    """How much energy a displayed SOC point really delivers (hel-139).

        factor = sum(discharge_wh) / sum(displayed SOC drop / 100 * capacity_wh)

    over runs of consecutive discharge-only buckets. The planner multiplies the
    rated capacity by this factor, so it counts stored energy in the same unit as
    the loads it has to cover: Wh at the socket.

    Why not just use the rated capacity: over a discharge, the displayed SOC falls
    faster than the socket energy would suggest, for two stacking reasons. The
    inverter and standby lose part of every discharged kWh, and the BMS count
    drifts low between full charges (see the module docstring). On this fleet the
    ratio is 0.82-0.84 (2026-09-27, 3/7/14-day windows, all three batteries alike):
    every displayed % is worth ~127 Wh, not the nominal 154 Wh.

    Why runs and not single buckets: SOC is logged in whole percent, so one
    bucket's drop is 0 or 1 point regardless of the energy in it. Over a run of
    consecutive buckets the drops telescope (start of the first minus end of the
    last) and the rounding cancels, leaving at most one point of error per run.
    A run ends at a gap, at any charging (charge_wh >= 5 Wh), at a missing
    reading, or when SOC moves up. An upward move while discharging is a BMS reset
    (hel-134): the jump is a correction, not energy, so it must not be counted.
    Runs shorter than `min_run_pts` are dropped because their +-1 point of
    rounding would dominate. With fewer than `min_points` in total (about one
    night of discharge) there is not enough to go on, so the result is None and
    the caller keeps its fallback.

    This is deliberately NOT the round-trip eta (fit_eta_standby): eta prices a
    purchase (AC in -> AC out between two exact 100% points), while this factor
    converts what the display shows now into what it will deliver. The two answer
    different questions and are measured on different windows.
    """
    by_battery: dict[str, list[dict]] = {}
    for r in rows:
        by_battery.setdefault(r["battery_id"], []).append(r)
    total_wh = total_nominal = total_pts = 0.0
    per_battery: dict[str, float] = {}
    for bid, rs in by_battery.items():
        cap = capacity_wh.get(bid)
        if not cap or cap <= 0:
            continue
        wh = pts = 0.0
        for run in _discharge_runs(sorted(rs, key=lambda r: r["ts_start"])):
            drop = run[0]["soc_start"] - run[-1]["soc_end"]
            if drop < min_run_pts:
                continue
            wh += sum(r["discharge_wh"] for r in run)
            pts += drop
        if pts > 0:
            per_battery[bid] = wh / (pts / 100.0 * cap)
            total_wh += wh
            total_nominal += pts / 100.0 * cap
            total_pts += pts
    if total_pts < min_points or total_nominal <= 0:
        return None
    return DeliveryFit(factor=total_wh / total_nominal, points=total_pts, per_battery=per_battery)


def _discharge_runs(rows: list[dict]):
    """Maximal runs of consecutive, gap-free, discharge-only buckets whose SOC only
    moves down, each bucket continuing exactly where the previous one ended."""
    run: list[dict] = []
    for r in rows:
        ok = (r.get("soc_start") is not None and r.get("soc_end") is not None
              and (r.get("charge_wh") or 0.0) < 5.0 and (r.get("discharge_wh") or 0.0) > 0.0
              and r["soc_end"] <= r["soc_start"])
        if (ok and run and r["ts_start"] - run[-1]["ts_start"] == BUCKET_S
                and r["soc_start"] == run[-1]["soc_end"]):
            run.append(r)
            continue
        if run:
            yield run
        run = [r] if ok else []
    if run:
        yield run


@dataclass(frozen=True)
class BatteryDrift:
    battery_id: str
    last_full_ts: float | None    # None: not full anywhere in the rows given
    discharged_wh: float          # since the last full charge
    predicted_pts: float | None   # expected under-reading now (None: no drift model)
    # earliest history the answer rests on. With last_full_ts None this says
    # "not full for at least this long"; None too means NO DATA — unknown, which
    # must never be read as "never full" (a restart race did exactly that).
    covered_since_ts: float | None = None


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
        covered = float(bat_rows[0]["ts_start"]) if bat_rows else None
        last = max((i for i, r in enumerate(bat_rows) if r["soc_end"] >= FULL_SOC), default=None)
        if last is None:
            out[bid] = BatteryDrift(bid, None, 0.0, None, covered)
            continue
        since = bat_rows[last + 1:]
        discharged = sum(r.get("discharge_wh") or 0.0 for r in since)
        fit = fits.get(bid) or fleet_fit
        predicted = fit.pts_per_kwh * discharged / 1000.0 if fit else None
        out[bid] = BatteryDrift(bid, bat_rows[last]["ts_start"] + BUCKET_S, discharged, predicted,
                                covered)
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
    in_progress: bool = False,
    commit_margin_ct: float = 3.0,
    eta: float = 1.0,
    wear_ct: float = 0.0,
) -> CalibrationPlan:
    """Decide whether the fleet needs a full charge, and whether from PV or grid.

    A battery is DUE once its predicted drift reaches `threshold_pts`, or after
    `max_days` without a full charge (also the rule when no drift model exists).
    Due opens the charge ceiling so PV can finish the job. If PV has not managed
    it by `threshold_pts + grid_extra_pts` (or `max_days` is exceeded), the fleet
    tops up from the grid in the cheapest buckets of the next `lookahead`.

    Finishing a top-up that is already running (`in_progress`, hel-139):
    the cheapest-buckets search looks 24 h ahead, and that horizon jumps when
    Tibber publishes tomorrow's prices (~13:00). On 2026-09-26 a top-up was
    buying at 14.3 ct when tomorrow's noon appeared at 13.4-13.8 ct. The search
    moved to tomorrow and stopped halfway, at 61%. That evening and night cost
    33-42 ct, the fleet ran empty at 06:00, and ~2 kWh were bought at 33 ct to save
    <1 ct/kWh on the top-up. Moving a purchase past an expensive stretch trades
    cheap energy for tonight against a slightly cheaper top-up tomorrow.

    So once charging has started it continues, unless BOTH:
      - a later window is cheaper than now by more than `commit_margin_ct`, and
      - nothing between now and that window costs as much as energy bought now
        delivers at (price_now / eta + wear_ct, the planner's own buy test, cf.
        economics.effective_cost_ct). If something does, what we buy now will be
        used in between at a profit, whatever the calibration does later.
    The first tick of a top-up is unchanged: it starts in the cheapest window as
    before. The rule only stops a started top-up from being abandoned for a
    marginal saving. With the defaults (eta 1, wear 0, in_progress False) the
    behaviour is exactly the old one.
    """
    if not enabled:
        return CalibrationPlan("off", False, False, (), "calibration disabled")

    def days(d: BatteryDrift) -> float:
        if d.last_full_ts is not None:
            return (now_ts - d.last_full_ts) / 86400.0
        # not full anywhere in the history: at least as long as the history reaches
        return (now_ts - d.covered_since_ts) / 86400.0

    due, overdue = [], []
    for bid, d in sorted(drift.items()):
        if d.last_full_ts is None and d.covered_since_ts is None:
            continue                     # no data about this battery: unknown, not due
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
    if in_progress:
        now_ct = window[0]
        later_ct = min(window[i] for i in chosen)
        delivered_ct = now_ct / eta + wear_ct if eta > 0 else float("inf")
        dearest_between = max(window[1:first], default=0.0)
        if now_ct - later_ct <= commit_margin_ct:
            return CalibrationPlan("grid_charging", True, True, ids,
                                   f"overdue — finishing the top-up @ {now_ct:.1f} ct "
                                   f"(bucket {first} is only {now_ct - later_ct:.1f} ct cheaper)")
        if dearest_between >= delivered_ct:
            return CalibrationPlan("grid_charging", True, True, ids,
                                   f"overdue — finishing the top-up @ {now_ct:.1f} ct "
                                   f"(it pays before bucket {first}: up to {dearest_between:.1f} ct "
                                   f"vs {delivered_ct:.1f} ct delivered)")
    return CalibrationPlan("grid_waiting", True, False, ids,
                           f"overdue — grid top-up waits for bucket {first} @ {window[first]:.1f} ct")
