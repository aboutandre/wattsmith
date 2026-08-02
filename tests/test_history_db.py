"""Unit tests for the 15-minute history DB + config versioning.

Pure helpers (bucket math, energy accumulation, config diff) plus a real
SQLite round-trip against a temp file — no Home Assistant, no network.

Run directly:   python3 tests/test_history_db.py
Or with pytest: pytest tests/test_history_db.py
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# ── stub homeassistant + the wattsmith package deps ──────────────────────────
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.event"] = MagicMock()
sys.modules["homeassistant.helpers.entity_registry"] = MagicMock()

_pkg = ModuleType("wattsmith")
sys.modules["wattsmith"] = _pkg
_base = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"wattsmith.{name}", _base / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "wattsmith"
    sys.modules[f"wattsmith.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
_load("settings")
_load("battery_bridge")
h = _load("history_db")

bucket_start = h.bucket_start
flatten_config = h.flatten_config
diff_config = h.diff_config
Sample = h.Sample
BucketAccumulator = h.BucketAccumulator
BatteryAccum = h.BatteryAccum
HistoryRecorder = h.HistoryRecorder
QUERY_TABLES = h.QUERY_TABLES


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_bucket_start_floors_to_15min():
    assert bucket_start(0) == 0
    assert bucket_start(899) == 0
    assert bucket_start(900) == 900
    assert bucket_start(1799) == 900
    assert bucket_start(1800) == 1800


def test_flatten_nested_battery_config():
    f = flatten_config({"min_soc": 11, "battery": {"f9": {"cost_eur": 1000}}})
    assert f["min_soc"] == "11"
    assert f["battery.f9.cost_eur"] == "1000"


def test_diff_config_add_change_remove():
    changes = diff_config({"a": 1, "c": 9}, {"a": 2, "b": 3})
    keys = {k: (o, n) for k, o, n in changes}
    assert keys["a"] == ("1", "2")     # changed
    assert keys["b"] == (None, "3")    # added
    assert keys["c"] == ("9", None)    # removed


def test_diff_config_no_change():
    assert diff_config({"a": 1}, {"a": 1}) == []


def test_split_scope_key():
    assert h._split_scope_key("battery.f9.cost_eur") == ("f9", "cost_eur")
    assert h._split_scope_key("min_soc") == ("global", "min_soc")


def test_battery_accum_sign_split():
    b = BatteryAccum()
    b.integrate(-2000, 3600)   # charging 2 kW for 1 h
    b.integrate(1000, 1800)    # discharging 1 kW for 0.5 h
    assert round(b.charge_wh) == 2000
    assert round(b.discharge_wh) == 500


def test_bucket_accumulator_energy_and_snapshots():
    a = BucketAccumulator(ts_start=0)
    s = Sample(pv_w=1000, house_w=400, ev_w=0, grid_w=-500,
               price_import=0.20, fleet_soc=50.0,
               batteries={"f9": (-2000.0, 50.0, 25.0)})
    a.add(s, 0.0)      # first sample: snapshots, no energy
    a.add(s, 3600.0)   # 1 hour
    row = a.bucket_row(config_version=7)
    assert row["pv_wh"] == 1000.0
    assert row["house_wh"] == 400.0
    assert row["grid_export_wh"] == 500.0
    assert row["grid_import_wh"] == 0.0
    assert row["fleet_charge_wh"] == 2000.0
    assert row["fleet_soc_start"] == 50.0
    assert row["price_import"] == 0.20
    assert row["config_version"] == 7
    assert row["sample_count"] == 2
    brows = a.battery_rows()
    assert brows[0]["battery_id"] == "f9"
    assert brows[0]["charge_wh"] == 2000.0
    assert brows[0]["soc_start"] == 50.0 and brows[0]["temp_c"] == 25.0


def test_bucket_forecast_null_when_absent():
    a = BucketAccumulator(ts_start=0)
    a.add(Sample(pv_w=100), 3600.0)
    assert a.bucket_row(1)["pv_forecast_wh"] is None


# ── SQLite round-trip (temp file) ────────────────────────────────────────────

def _recorder(tmp):
    entry = SimpleNamespace(
        options={"history_db_path": tmp, "history_retention_days": 0},
        data={}, entry_id="e1", title="Wattsmith",
    )
    rec = HistoryRecorder(MagicMock(), entry)
    return rec


def test_init_db_creates_schema():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        conn = sqlite3.connect(tmp)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"bucket", "battery_bucket", "battery", "config_snapshot",
                "config_event", "meta"} <= tables
        conn.close()


def test_config_versioning_snapshot_and_events():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        rec._write_config({"min_soc": 11, "battery": {"f9": {"cost_eur": 1000}}}, "startup")
        rec._write_config({"min_soc": 20, "battery": {"f9": {"cost_eur": 1000}}}, "options")
        rec._write_config({"min_soc": 20, "battery": {"f9": {"cost_eur": 1000}}}, "options")  # no-op
        conn = sqlite3.connect(tmp)
        versions = [r[0] for r in conn.execute("SELECT version FROM config_snapshot ORDER BY version")]
        assert versions == [1, 2]     # third call changed nothing -> no new version
        ev = conn.execute("SELECT scope,key,old_value,new_value FROM config_event "
                          "WHERE version=2").fetchall()
        assert ("global", "min_soc", "11", "20") in ev
        # battery dimension populated
        cost = conn.execute("SELECT cost_eur FROM battery WHERE battery_id='f9'").fetchone()[0]
        assert cost == 1000
        conn.close()


def test_write_bucket_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        a = BucketAccumulator(ts_start=900)
        a.add(Sample(pv_w=2000, grid_w=300, fleet_soc=60.0,
                     batteries={"f9": (500.0, 60.0, 24.0)}), 0.0)
        a.add(Sample(pv_w=2000, grid_w=300, fleet_soc=61.0,
                     batteries={"f9": (500.0, 61.0, 24.0)}), 900.0)
        rec._config_version = 1
        rec._write_bucket(a.bucket_row(1), a.battery_rows())
        conn = sqlite3.connect(tmp)
        row = conn.execute("SELECT pv_wh,grid_import_wh,fleet_discharge_wh,config_version "
                           "FROM bucket WHERE ts_start=900").fetchone()
        assert row[0] == 500.0          # 2000 W × 0.25 h
        assert row[1] == 75.0           # 300 W × 0.25 h import
        assert round(row[2]) == 125     # 500 W × 0.25 h discharge
        assert row[3] == 1
        b = conn.execute("SELECT charge_wh,discharge_wh,soc_start,soc_end "
                         "FROM battery_bucket WHERE ts_start=900").fetchone()
        assert b[3] == 61.0 and b[2] == 60.0
        conn.close()


def test_retention_purges_old_rows():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._retention_days = 1
        rec._init_db()
        old = BucketAccumulator(ts_start=0)          # epoch 0 = ancient
        old.add(Sample(pv_w=100, batteries={"f9": (0.0, 50.0, 20.0)}), 900.0)
        rec._write_bucket(old.bucket_row(1), old.battery_rows())
        n = sqlite3.connect(tmp).execute("SELECT COUNT(*) FROM bucket").fetchone()[0]
        assert n == 0     # older than retention -> purged on write


def test_query_range_filters_ts_window_and_orders():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        for ts in (0, 900, 1800, 2700):
            a = BucketAccumulator(ts_start=ts)
            a.add(Sample(pv_w=100, fleet_soc=50.0), 900.0)
            rec._write_bucket(a.bucket_row(1), a.battery_rows())
        rows = rec.query_range(900, 1800, table="bucket")
        assert [r["ts_start"] for r in rows] == [900, 1800]


def test_query_range_limit():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        for ts in (0, 900, 1800, 2700):
            a = BucketAccumulator(ts_start=ts)
            a.add(Sample(pv_w=100, fleet_soc=50.0), 900.0)
            rec._write_bucket(a.bucket_row(1), a.battery_rows())
        rows = rec.query_range(0, 2700, table="bucket", limit=2)
        assert [r["ts_start"] for r in rows] == [0, 900]


def test_query_range_battery_bucket_filters_by_battery_id():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        a = BucketAccumulator(ts_start=900)
        a.add(Sample(batteries={"f9": (100.0, 60.0, 24.0), "f10": (200.0, 55.0, 25.0)}), 900.0)
        rec._write_bucket(a.bucket_row(1), a.battery_rows())
        rows = rec.query_range(900, 900, table="battery_bucket", battery_id="f10")
        assert len(rows) == 1
        assert rows[0]["battery_id"] == "f10"


def test_query_range_rejects_unknown_table():
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec._init_db()
        try:
            rec.query_range(0, 1, table="sqlite_master")
            raised = False
        except ValueError:
            raised = True
        assert raised


def test_query_tables_matches_schema():
    # every allowed table must actually exist in the schema
    assert set(QUERY_TABLES) == {"bucket", "battery_bucket", "config_snapshot", "config_event"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} history_db tests passed ✓")
