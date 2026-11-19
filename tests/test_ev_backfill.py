"""Unit tests for ev_backfill.plan_backfill (pure maths, no Home Assistant).

Run directly:   python3 tests/test_ev_backfill.py
Or with pytest: pytest tests/test_ev_backfill.py
"""
import importlib.util
import sys
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "ev_backfill.py"
_spec = importlib.util.spec_from_file_location("ev_backfill", _path)
eb = importlib.util.module_from_spec(_spec)
sys.modules["ev_backfill"] = eb
_spec.loader.exec_module(eb)

H = 3600
T0 = 1_780_000_000 - (1_780_000_000 % H)   # an hour boundary
StatRow = eb.StatRow


def _existing(start_h, readings):
    """Rows as the recorder compiles them: sum starts at 0 at the first reading."""
    base = readings[0]
    return [StatRow(T0 + (start_h + i) * H, r, r - base) for i, r in enumerate(readings)]


def _sums_continuous(rows):
    rows = sorted(rows, key=lambda r: r.start)
    return all(b.sum >= a.sum - 1e-9 for a, b in zip(rows, rows[1:]))


def test_no_statistics_raises():
    try:
        eb.plan_backfill([], [], [])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_meter_plus_wattsmith_lands_on_sensor_start():
    # old meter: 10 → 12 kWh over hours 0..2; sensor starts at hour 10 reading 20
    meter = [StatRow(T0, 10.0, 0.0), StatRow(T0 + H, 11.0, 1.0), StatRow(T0 + 2 * H, 12.0, 2.0)]
    ws = [(T0 + 4 * H, 3000.0), (T0 + 6 * H, 5000.0)]   # 8 kWh logged, meter says 8
    existing = _existing(10, [20.0, 21.0, 23.0])
    plan = eb.plan_backfill(existing, meter, ws)
    assert plan.meter_used and plan.oldest_reading == 10.0
    assert abs(plan.scale - 1.0) < 1e-9
    assert [r.start for r in plan.rows] == [T0 + H, T0 + 2 * H, T0 + 4 * H, T0 + 6 * H]
    assert plan.rows[-1].state == 20.0 and plan.rows[-1].sum == 10.0
    # existing rows now continue the same counter: sum = reading - oldest
    assert [r.sum for r in plan.shifted] == [10.0, 11.0, 13.0]
    assert _sums_continuous(plan.rows + plan.shifted)
    assert plan.backfilled_kwh == 10.0


def test_scale_spreads_meter_delta_by_wattsmith_shape():
    meter = [StatRow(T0, 0.0, 0.0), StatRow(T0 + H, 1.0, 1.0)]
    ws = [(T0 + 3 * H, 1000.0), (T0 + 4 * H, 3000.0)]   # 4 kWh logged
    existing = _existing(8, [6.0, 6.0])                  # meter gap = 5 kWh
    plan = eb.plan_backfill(existing, meter, ws)
    assert abs(plan.scale - 1.25) < 1e-9
    assert abs(plan.rows[1].state - 2.25) < 1e-6        # 1 + 1.0 * 1.25
    assert plan.rows[2].state == 6.0


def test_without_meter_uses_log_as_is():
    ws = [(T0, 2000.0), (T0 + H, 0.0), (T0 + 2 * H, 1500.0)]
    existing = _existing(5, [100.0, 101.0])
    plan = eb.plan_backfill(existing, [], ws)
    assert not plan.meter_used and plan.scale == 1.0
    assert plan.oldest_reading == 96.5
    assert [r.state for r in plan.rows] == [98.5, 100.0]   # empty hour skipped
    assert abs(plan.shift - 3.5) < 1e-9
    assert [r.sum for r in plan.shifted] == [3.5, 4.5]


def test_ignores_wattsmith_hours_inside_meter_span_and_after_sensor_start():
    meter = [StatRow(T0, 0.0, 0.0), StatRow(T0 + 2 * H, 2.0, 2.0)]
    ws = [(T0 + H, 9999.0), (T0 + 4 * H, 1000.0), (T0 + 9 * H, 9999.0)]
    existing = _existing(6, [3.0])
    plan = eb.plan_backfill(existing, meter, ws)
    assert plan.wattsmith_kwh == 1.0
    assert plan.rows[-1].start == T0 + 4 * H and plan.rows[-1].state == 3.0


def test_rejects_meter_from_another_counter():
    meter = [StatRow(T0, 500.0, 0.0), StatRow(T0 + H, 600.0, 100.0)]
    existing = _existing(5, [20.0])
    plan = eb.plan_backfill(existing, meter, [(T0 + 2 * H, 1000.0)])
    assert not plan.meter_used and plan.meter_rejected
    assert plan.oldest_reading == 19.0


def test_gap_without_log_is_booked_in_one_hour():
    meter = [StatRow(T0, 0.0, 0.0), StatRow(T0 + H, 1.0, 1.0)]
    existing = _existing(5, [4.0])
    plan = eb.plan_backfill(existing, meter, [])
    assert plan.rows[-1].start == T0 + 2 * H and plan.rows[-1].state == 4.0
    assert plan.notes


def test_meter_dips_never_count_backwards():
    meter = [StatRow(T0, 5.0, 0.0), StatRow(T0 + H, 4.0, -1.0), StatRow(T0 + 2 * H, 6.0, 1.0)]
    existing = _existing(4, [6.0])
    plan = eb.plan_backfill(existing, meter, [])
    assert [r.state for r in plan.rows] == [6.0]
    assert _sums_continuous(plan.rows + plan.shifted)


def test_second_run_is_a_no_op():
    meter = [StatRow(T0, 10.0, 0.0), StatRow(T0 + H, 11.0, 1.0)]
    ws = [(T0 + 3 * H, 4000.0)]
    existing = _existing(6, [15.0, 16.0])
    first = eb.plan_backfill(existing, meter, ws)
    after = sorted(first.rows + first.shifted, key=lambda r: r.start)
    for m in (meter, []):
        again = eb.plan_backfill(after, m, ws)
        assert again.rows == [] and again.shifted == [] and abs(again.shift) < 1e-9


def test_monthly_summary_adds_up():
    meter = [StatRow(T0, 0.0, 0.0), StatRow(T0 + H, 2.0, 2.0)]
    ws = [(T0 + 40 * 24 * H, 3000.0)]
    plan = eb.plan_backfill(_existing(50 * 24, [5.0]), meter, ws)
    assert abs(sum(plan.monthly_kwh().values()) - plan.backfilled_kwh) < 1e-6
    s = plan.summary()
    assert s["history_rows"] == 2 and s["backfilled_kwh"] == 5.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} ev_backfill tests passed ✓")
