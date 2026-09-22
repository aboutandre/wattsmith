"""Unit tests for BMS SOC drift + calibration (pure). No HA.

A simulated battery reproduces the real defect (hel-134): its SOC counter
over-counts discharge, so the displayed SOC sinks below the true charge until the
cells are really full and the BMS snaps it back to 100.

Run: python3 tests/test_soc_drift.py   |   pytest tests/test_soc_drift.py
"""
import importlib.util
import math
import sys
from pathlib import Path

_p = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "soc_drift.py"
_spec = importlib.util.spec_from_file_location("soc_drift", _p)
sd = importlib.util.module_from_spec(_spec)
sys.modules["soc_drift"] = sd  # dataclasses resolves cls.__module__ here
_spec.loader.exec_module(sd)

CAP = 5120.0
ETA_C, ETA_D = 0.93, 0.86          # true legs -> true round trip 0.80
STANDBY_W = 9.0
DRIFT = 0.07                        # counter over-counts discharge by 7 %
FLOOR = 13


def simulate(days=40, bid="bat", sunny=(1, 1, 0, 0, 0), t0=0):
    """Daily cycle: charge by day (strong on sunny days), discharge by night to the
    DISPLAYED floor. Returns battery_bucket rows like the history DB writes."""
    cells, counter, rows, ts = CAP * 0.5, 50.0, [], t0
    for day in range(days):
        strong = sunny[day % len(sunny)]
        for q in range(96):
            h = q / 4
            soc0 = math.floor(counter)
            ch = dis = 0.0
            full = False
            if 9 <= h < 16:
                ch = (1400.0 if strong else 500.0) * 0.25
                if cells + ch * ETA_C - STANDBY_W * 0.25 >= CAP:
                    ch = max(0.0, (CAP - cells + STANDBY_W * 0.25) / ETA_C)   # energy-conserving
                    full = True
            elif (h >= 18 or h < 7) and math.floor(counter) > FLOOR:
                dis = 350.0 * 0.25
            cells += ch * ETA_C - dis / ETA_D - STANDBY_W * 0.25
            counter += (ch * ETA_C - dis / ETA_D * (1 + DRIFT) - STANDBY_W * 0.25) / CAP * 100
            if full:                        # truly full: BMS recalibrates
                cells, counter = CAP, 100.0
            counter = min(100.0, counter)
            rows.append({"ts_start": ts, "battery_id": bid, "charge_wh": ch, "discharge_wh": dis,
                         "soc_start": float(soc0), "soc_end": float(math.floor(counter))})
            ts += 900
    return rows


def expected_pts_per_kwh():
    return 1000.0 / ETA_D * DRIFT / CAP * 100


def test_windows_run_between_full_charges_and_break_on_gaps():
    rows = simulate(days=20)
    ws = sd.full_to_full_windows(rows)
    assert len(ws) >= 5
    assert all(w.jump_pts > 0 for w in ws)
    gapped = rows[:500] + rows[510:]                 # a 10-bucket logging hole
    assert len(sd.full_to_full_windows(gapped)) < len(ws)
    small = rows[:500] + rows[501:]                  # one bucket (an HA restart)
    tolerant = sd.full_to_full_windows(small, max_gap_buckets=4)
    assert len(tolerant) == len(ws)
    assert sum(w.missing_buckets for w in tolerant) == 1
    assert len(sd.full_to_full_windows(small)) < len(ws)    # strict (η) still drops it


def test_drift_rate_is_learned_from_the_resets():
    fit = sd.fit_drift_rate(sd.full_to_full_windows(simulate(days=40)))
    assert fit is not None
    assert abs(fit.pts_per_kwh - expected_pts_per_kwh()) < 0.3, fit


def test_eta_and_standby_come_out_unbiased_despite_the_drift():
    fit = sd.fit_eta_standby(sd.full_to_full_windows(simulate(days=60)))
    assert fit is not None
    assert abs(fit.eta - ETA_C * ETA_D) < 0.02, fit
    assert abs(fit.standby_w - STANDBY_W) < 3.0, fit


def test_fits_refuse_thin_data():
    ws = sd.full_to_full_windows(simulate(days=40))
    assert sd.fit_drift_rate(ws[:2]) is None
    assert sd.fit_eta_standby(ws[:3]) is None


def test_drift_now_follows_discharge_since_last_full():
    rows = simulate(days=10)
    fit = sd.fit_drift_rate(sd.full_to_full_windows(simulate(days=40)))
    now = rows[-1]["ts_start"] + 900
    d = sd.battery_drift_now(rows, {"bat": fit}, None, {"bat": 60.0}, now)["bat"]
    last = max(i for i, r in enumerate(rows) if r["soc_end"] >= sd.FULL_SOC)
    discharged = sum(r["discharge_wh"] for r in rows[last + 1:])
    assert d.discharged_wh == discharged
    assert abs(d.predicted_pts - fit.pts_per_kwh * discharged / 1000) < 1e-9
    # reading full right now beats the (lagging) DB
    assert sd.battery_drift_now(rows, {"bat": fit}, None, {"bat": 100.0}, now)["bat"].predicted_pts == 0.0


def test_drift_now_uses_fleet_fit_and_handles_never_full():
    rows = simulate(days=10)
    fleet = sd.DriftFit(pts_per_kwh=1.4, windows=9)
    d = sd.battery_drift_now(rows, {}, fleet, {}, rows[-1]["ts_start"] + 900)["bat"]
    assert d.predicted_pts is not None
    never = [dict(r, soc_end=min(r["soc_end"], 90.0), soc_start=min(r["soc_start"], 90.0)) for r in rows]
    d2 = sd.battery_drift_now(never, {}, fleet, {}, rows[-1]["ts_start"] + 900)["bat"]
    assert d2.last_full_ts is None and d2.predicted_pts is None


def _drift(pts, days_ago, now=100 * 86400.0):
    return sd.BatteryDrift("x", now - days_ago * 86400.0, 0.0, pts)


def _plan(drift, grid=True, prices=(30.0, 25.0, 20.0, 28.0), need=2000.0, **kw):
    args = dict(now_ts=100 * 86400.0, enabled=True, threshold_pts=8.0, max_days=7.0,
                grid_enabled=grid, grid_extra_pts=4.0, prices_ct=list(prices),
                need_wh=need, per_bucket_wh=1875.0)
    args.update(kw)
    return sd.plan_calibration(drift, **args)


def test_calibration_ok_below_threshold():
    p = _plan({"a": _drift(5.0, 1.0)})
    assert p.status == "ok" and not p.open_ceiling and not p.grid_charge_now


def test_calibration_due_opens_the_ceiling_for_pv_first():
    p = _plan({"a": _drift(9.0, 2.0), "b": _drift(2.0, 1.0)})
    assert p.status == "due" and p.open_ceiling and not p.grid_charge_now
    assert p.due_ids == ("a",)


def test_calibration_overdue_waits_for_the_cheapest_grid_buckets():
    p = _plan({"a": _drift(13.0, 4.0)})                    # >= 8 + 4
    assert p.status == "grid_waiting" and p.open_ceiling and not p.grid_charge_now
    assert "bucket 1" in p.reason          # needs 3 buckets: 1, 2, 3 are the cheapest


def test_calibration_overdue_charges_when_now_is_among_the_cheapest():
    p = _plan({"a": _drift(13.0, 4.0)}, prices=(18.0, 25.0, 30.0, 28.0))
    assert p.status == "grid_charging" and p.grid_charge_now


def test_calibration_max_days_applies_without_a_drift_model():
    p = _plan({"a": _drift(None, 8.0)})
    assert p.status in ("grid_waiting", "grid_charging")
    now = 100 * 86400.0
    # not full anywhere in 10 days of history -> genuinely overdue
    never = _plan({"a": sd.BatteryDrift("a", None, 0.0, None, now - 10 * 86400.0)})
    assert never.status in ("grid_waiting", "grid_charging")
    # ...but only 2 days of history is not evidence of 7 days without a full charge
    short = _plan({"a": sd.BatteryDrift("a", None, 0.0, None, now - 2 * 86400.0)})
    assert short.status == "ok"


def test_no_history_is_unknown_not_overdue():
    """Regression (v0.12.0 on live HA): right after a restart the history DB is not
    up yet, the first tick saw no rows, read that as "never full" and scheduled a
    grid top-up for batteries that had been full two hours earlier."""
    assert _plan({"a": sd.BatteryDrift("a", None, 0.0, None)}).status == "ok"
    d = sd.battery_drift_now([], {}, None, {"a": 95.0}, 100 * 86400.0)["a"]
    assert d.last_full_ts is None and d.covered_since_ts is None
    assert _plan(sd.battery_drift_now([], {}, None, {"a": 95.0}, 100 * 86400.0)).status == "ok"


def test_calibration_grid_off_or_disabled():
    assert _plan({"a": _drift(13.0, 4.0)}, grid=False).status == "due"
    assert _plan({"a": _drift(13.0, 4.0)}, enabled=False).status == "off"



def test_reset_events_predict_walk_forward_only():
    ws = sd.full_to_full_windows(simulate(days=40))
    evs = sd.reset_events(ws, min_windows=4)
    assert len(evs) == len(ws)
    assert evs[0]["predicted_pts"] is None             # nothing before the first reset
    later = [e for e in evs if e["predicted_pts"] is not None]
    assert later, "predictions appear once enough resets are known"
    # honest predictions track the real jumps on the simulated battery
    err = sum(abs(e["predicted_pts"] - e["actual_pts"]) for e in later) / len(later)
    assert err < 3.0, err
    assert all(e["ts"] % 900 == 0 for e in evs)


def test_reset_measured_across_a_restart_split_bucket():
    """Regression (live 2026-09-22 15:15, F9): a restart inside the reset bucket made
    it start at 98 although the battery snapped 80 -> 100; the jump must come from
    the previous bucket's end."""
    rows = simulate(days=12)
    ws = sd.full_to_full_windows(rows)
    last_anchor = max(i for i, r in enumerate(rows) if r["soc_end"] >= sd.FULL_SOC
                      and r["soc_start"] < sd.FULL_SOC)
    split = [dict(r) for r in rows]
    split[last_anchor]["soc_start"] = 98.0              # partial bucket after a restart
    split[last_anchor]["charge_wh"] = 5.0
    ws2 = sd.full_to_full_windows(split)
    assert abs(ws2[-1].jump_pts - ws[-1].jump_pts) < 2.5, (ws2[-1].jump_pts, ws[-1].jump_pts)
    # ...and when the split bucket starts already FULL it is still recognised as the reset
    split[last_anchor]["soc_start"] = 100.0
    ws3 = sd.full_to_full_windows(split)
    assert len(ws3) == len(ws) and abs(ws3[-1].jump_pts - ws[-1].jump_pts) < 2.5

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} soc drift tests passed ✓")
