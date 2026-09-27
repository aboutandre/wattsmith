"""House-load forecast from the history DB (load_profile.py, hel-141).

The walk-forward backtest at the bottom is the reason the model looks the way it
does (see the module docstring for the full comparison). It runs on 82 real days
(fixtures/house_load_2026-07_09.json) and guards against a change that would make
the forecast worse, or bring back the old fixed 500 W.
"""
import importlib.util
import json
import os
import statistics as st
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_p = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "load_profile.py"
_spec = importlib.util.spec_from_file_location("load_profile", _p)
lp = importlib.util.module_from_spec(_spec)
sys.modules["load_profile"] = lp
_spec.loader.exec_module(lp)

FX = json.loads((Path(__file__).resolve().parent / "fixtures" /
                 "house_load_2026-07_09.json").read_text())


@pytest.fixture(autouse=True)
def _berlin():
    """Rows are keyed by local time; lookups use the process TZ, as in production."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def _rows(days: dict[str, list[float]]) -> list[dict]:
    rows = []
    for day, values in days.items():
        base = datetime.fromisoformat(day)
        for s, wh in enumerate(values):
            local = base + timedelta(minutes=15 * s)
            rows.append({"ts_start": int(time.mktime(local.timetuple())),
                         "local_start": local.isoformat(), "house_wh": wh})
    return rows


def _flat_days(start: str, n: int, wh=100.0, bump_weekday=None, bump=0.0):
    out, d0 = {}, datetime.fromisoformat(start)
    for i in range(n):
        d = d0 + timedelta(days=i)
        out[d.date().isoformat()] = [wh + (bump if d.weekday() == bump_weekday else 0.0)] * 96
    return out


def _ts(day: str, hh=0, mm=0) -> float:
    return time.mktime(datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").timetuple())


# ── behaviour ─────────────────────────────────────────────────────────────────
def test_too_little_history_keeps_the_fallback():
    rows = _rows(_flat_days("2026-09-01", 6))
    assert lp.fit_load_profile(rows, _ts("2026-09-07")) is None


def test_a_flat_house_gives_a_flat_profile():
    prof = lp.fit_load_profile(_rows(_flat_days("2026-08-01", 28)), _ts("2026-08-29"))
    assert all(v == pytest.approx(100.0) for v in prof.wh)


def test_a_weekday_that_differs_is_learned_but_shrunk_toward_all_days():
    # Wednesdays draw 80 Wh more per bucket; with 4 Wednesdays against a 3-day
    # prior, the Wednesday profile sits between the all-days mean and the truth
    days = _flat_days("2026-08-01", 28, bump_weekday=2, bump=80.0)
    prof = lp.fit_load_profile(_rows(days), _ts("2026-08-29"), smooth_slots=0)
    wed = prof.at(_ts("2026-09-02", 12))            # a Wednesday
    thu = prof.at(_ts("2026-09-03", 12))
    assert thu < wed < 180.0 and wed > 140.0, (wed, thu)


def test_recent_weeks_count_more():
    # load rose from 100 to 200 Wh two weeks ago: the profile leans to the new level
    days = {**_flat_days("2026-08-01", 28, wh=100.0), **_flat_days("2026-08-29", 14, wh=200.0)}
    prof = lp.fit_load_profile(_rows(days), _ts("2026-09-12"))
    assert 150.0 < prof.at(_ts("2026-09-13", 3)) < 200.0


def test_energy_between_counts_partial_buckets():
    prof = lp.fit_load_profile(_rows(_flat_days("2026-08-01", 28)), _ts("2026-08-29"))
    t0 = _ts("2026-08-30", 10, 5)                   # 10 min into a bucket
    assert prof.energy_between(t0, t0 + 3600) == pytest.approx(400.0)


def test_each_bucket_uses_its_own_weekday():
    # the old per-hour list applied TODAY's weekday to the whole horizon
    days = _flat_days("2026-08-01", 28, bump_weekday=6, bump=100.0)   # Sundays
    prof = lp.fit_load_profile(_rows(days), _ts("2026-08-29"), smooth_slots=0)
    sat_late, sun_early = _ts("2026-08-29", 23, 0), _ts("2026-08-30", 1, 0)
    assert prof.at(sun_early) > prof.at(sat_late) + 30.0


# ── walk-forward backtest on the real 82 days ─────────────────────────────────
def _backtest(forecast):
    days = sorted(FX["days"])
    first = datetime.fromisoformat(days[0])
    six_h, night, bias = [], [], []
    for day in days:
        if (datetime.fromisoformat(day) - first).days < 28:
            continue
        nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        hist = {d: v for d, v in FX["days"].items() if d < day}
        f = forecast(hist, day)
        a = FX["days"][day]
        for s0 in range(0, 96, 24):
            six_h.append(abs(sum(f(s) for s in range(s0, s0 + 24)) - sum(a[s0:s0 + 24]))
                         / sum(a[s0:s0 + 24]))
        if nxt in FX["days"]:
            fn = sum(f(s) for s in range(72, 120))
            an = sum(a[72:]) + sum(FX["days"][nxt][:24])
            night.append(abs(fn - an) / an)
            bias.append((fn - an) / an)
    return st.mean(six_h), st.mean(night), st.mean(bias)


def _profile_forecast(hist, day):
    prof = lp.fit_load_profile(_rows(hist), _ts(day))
    return lambda s: prof.at(_ts(day) + s * 900)


def test_backtest_profile_beats_the_old_fixed_500_w():
    six_h, night, bias = _backtest(_profile_forecast)
    old6, old_night, old_bias = _backtest(lambda hist, day: (lambda s: 125.0))
    print(f"\n  profile: 6h {six_h:.1%} night {night:.1%} bias {bias:+.1%}"
          f" | fixed 500 W: 6h {old6:.1%} night {old_night:.1%} bias {old_bias:+.1%}")
    assert six_h <= 0.14 and night <= 0.10, (six_h, night)       # measured 12.7% / 8.1%
    assert abs(bias) <= 0.06, bias                               # measured -3.4%
    assert night < old_night / 3                                 # 500 W was +32% at night
