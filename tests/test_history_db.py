"""Unit tests for the 15-minute history DB + config versioning.

Pure helpers (bucket math, energy accumulation, config diff) plus a real
SQLite round-trip against a temp file — no Home Assistant, no network.

Run directly:   python3 tests/test_history_db.py
Or with pytest: pytest tests/test_history_db.py
"""
import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

# ── stub homeassistant + the wattsmith package deps ──────────────────────────
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.event"] = MagicMock()
sys.modules["homeassistant.helpers.entity_registry"] = MagicMock()

# dt_util: battery_bridge.read_health() ages the SOC sensor against the current time.
_util = ModuleType("homeassistant.util")
_dt = ModuleType("homeassistant.util.dt")
_dt.utcnow = lambda: datetime.now(timezone.utc)
_util.dt = _dt
sys.modules["homeassistant.util"] = _util
sys.modules["homeassistant.util.dt"] = _dt

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
_load("safety")          # history_db reads the fault-source labels from here
_load("battery_bridge")
_load("economics")       # arbitrage imports it; history_db reuses solcast_forecast_entities()
_load("arbitrage")
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


def _health(bid="f11", available=True, soc=54.0, age=5.0):
    return SimpleNamespace(
        battery_id=bid, available=available, soc=soc, soc_age_s=age
    )


def test_battery_health_table_round_trip():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        rec._write_health([{
            "ts": 1000, "local_time": "2026-08-26T06:28:18", "battery_id": "f11",
            "available": 0, "soc": None, "soc_age_s": 97000.0, "fails": 32474,
            "excluded": 1, "last_error": "device unavailable",
        }])
        conn = sqlite3.connect(tmp)
        row = conn.execute(
            "SELECT battery_id, available, fails, excluded, last_error"
            " FROM battery_health"
        ).fetchone()
        assert row == ("f11", 0, 32474, 1, "device unavailable")
        conn.close()


def test_health_probe_writes_on_change_then_stays_quiet():
    """A healthy fleet must not write a row per probe — only on change."""
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec.bridge = MagicMock()
        rec.bridge.read_health.return_value = [_health()]
        rec._supervisor_faults = lambda: {}

        assert len(rec._collect_health()) == 1     # first sighting is a change
        assert rec._collect_health() == []         # unchanged -> nothing to write
        assert rec._collect_health() == []


def test_health_probe_writes_when_a_battery_goes_unavailable():
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec.bridge = MagicMock()
        rec.bridge.read_health.return_value = [_health()]
        rec._supervisor_faults = lambda: {}
        rec._collect_health()                       # baseline
        assert rec._collect_health() == []

        # the battery drops out, and the supervisor has fault detail for it
        rec.bridge.read_health.return_value = [
            _health(available=False, soc=None, age=90.0)
        ]
        rec._supervisor_faults = lambda: {
            "f11": {"not_responding": {"fails": 7, "excluded": True,
                                       "last_error": "timeout"}}
        }
        rows = rec._collect_health()
        assert len(rows) == 1
        assert rows[0]["available"] == 0
        assert rows[0]["fails"] == 7
        assert rows[0]["excluded"] == 1
        # the reason is tagged with which failure mode it came from
        assert rows[0]["last_error"] == "[not_responding] timeout"


def test_pick_fault_prefers_the_read_streak_over_the_ack_streak():
    read = {"fails": 5, "excluded": True, "last_error": "unavailable"}
    ack = {"fails": 2, "excluded": False, "last_error": "no ack"}
    assert h._pick_fault({"not_responding": read, "not_acking": ack}) == (
        5, True, "[not_responding] unavailable"
    )
    # ack-only failures are still recorded, clearly labelled
    assert h._pick_fault({"not_acking": ack}) == (2, False, "[not_acking] no ack")
    assert h._pick_fault({}) == (0, False, None)


def test_health_probe_heartbeats_even_when_nothing_changes():
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec.bridge = MagicMock()
        rec.bridge.read_health.return_value = [_health()]
        rec._supervisor_faults = lambda: {}
        rec._collect_health()
        assert rec._collect_health() == []
        # pretend the last write was longer ago than the heartbeat interval
        rec._health_last_write["f11"] -= (h.HISTORY_HEALTH_HEARTBEAT_S + 1)
        assert len(rec._collect_health()) == 1


def test_health_probe_tracks_each_battery_independently():
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec.bridge = MagicMock()
        rec.bridge.read_health.return_value = [_health("f9"), _health("f11")]
        rec._supervisor_faults = lambda: {}
        assert len(rec._collect_health()) == 2
        # only f11 changes -> only f11 is written
        rec.bridge.read_health.return_value = [
            _health("f9"), _health("f11", available=False, soc=None)
        ]
        rows = rec._collect_health()
        assert [r["battery_id"] for r in rows] == ["f11"]


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
    # Every allowed table must actually exist in the schema. Derived from SCHEMA
    # rather than hardcoded, so adding a table can't silently drift from this
    # test (battery_health was added to the schema but left out of QUERY_TABLES,
    # and the old hardcoded set hid it).
    import re
    in_schema = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", h.SCHEMA))
    assert set(QUERY_TABLES) <= in_schema, set(QUERY_TABLES) - in_schema


def test_every_query_table_has_a_timestamp_column():
    # query_range() indexes _QUERY_TS_COLUMN[table]; a missing entry is a KeyError
    # at call time rather than a clean ValueError.
    missing = [t for t in QUERY_TABLES if t not in h._QUERY_TS_COLUMN]
    assert not missing, missing


def test_battery_health_is_queryable_end_to_end():
    # The liveness log is only useful if it can actually be pulled back out.
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec._init_db()
        conn = sqlite3.connect(os.path.join(d, "history.db"))
        conn.execute(
            "INSERT INTO battery_health (ts, local_time, battery_id, available,"
            " soc, soc_age_s, fails, excluded, last_error)"
            " VALUES (500, '1970-01-01T00:08:20', 'b0', 1, 42.0, 3.0, 0, 0, NULL)"
        )
        conn.commit()
        conn.close()
        rows = rec.query_range(0, 1000, table="battery_health")
        assert len(rows) == 1
        assert rows[0]["battery_id"] == "b0" and rows[0]["soc"] == 42.0



# ── v3: calibration logging (hel-134 follow-up) ─────────────────────────────
_V2_BUCKET = """CREATE TABLE bucket (ts_start INTEGER PRIMARY KEY, local_start TEXT NOT NULL,
  pv_wh REAL, grid_charge_wh REAL, arb_reason TEXT, sample_count INTEGER,
  config_version INTEGER, schema_version INTEGER)"""


def test_v3_migration_adds_calibration_status_to_an_existing_bucket_table():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        conn = sqlite3.connect(tmp)
        conn.execute(_V2_BUCKET)
        conn.execute("INSERT INTO bucket(ts_start,local_start,pv_wh) VALUES(900,'x',1.0)")
        conn.commit(); conn.close()
        rec = _recorder(tmp)
        rec._init_db()
        rec._init_db()                                   # idempotent
        conn = sqlite3.connect(tmp)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket)")}
        assert "calibration_status" in cols
        assert conn.execute("SELECT pv_wh FROM bucket WHERE ts_start=900").fetchone()[0] == 1.0
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "calibration_event" in tables
        conn.close()


def test_calibration_status_stamps_the_most_significant_per_bucket():
    rec = _recorder("/nonexistent/x.db")
    for st in ("ok", "grid_charging", "ok", None):
        rec.note_calibration(st)
    assert rec._calibration_status == "grid_charging"


def test_calibration_events_are_idempotent_and_classified_by_source():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        conn = sqlite3.connect(tmp)
        for ts, st in ((9000, "grid_charging"), (90000, "due"), (180000, "ok")):
            conn.execute("INSERT INTO bucket(ts_start,local_start,calibration_status) VALUES(?,?,?)",
                         (ts, "x", st))
        conn.commit(); conn.close()

        def ev(ts, bid="f9"):
            return {"ts": ts, "battery_id": bid, "days_since_full": 3.0, "discharged_kwh": 6.0,
                    "predicted_pts": 7.8, "actual_pts": 8.5, "drift_rate": 1.3}
        events = [ev(9000), ev(90000), ev(180000), ev(500000)]
        rec._write_calibration_events(events)
        rec._write_calibration_events(events)           # re-logged every refresh: no duplicates
        conn = sqlite3.connect(tmp)
        rows = conn.execute("SELECT ts, source FROM calibration_event ORDER BY ts").fetchall()
        conn.close()
        assert rows == [(9000, "grid"), (90000, "pv_calibration"), (180000, "natural"),
                        (500000, "unknown")]


def test_calibration_event_is_queryable():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        rec = _recorder(tmp)
        rec._init_db()
        rec._write_calibration_events([{"ts": 9000, "battery_id": "f9", "days_since_full": 1.0,
                                        "discharged_kwh": 2.0, "predicted_pts": None,
                                        "actual_pts": 3.0, "drift_rate": None}])
        assert len(rec.query_range(0, 10**6, table="calibration_event")) == 1


# ── v4: Solcast p10/p90 on the bucket + the day-ahead snapshot table (hel-132) ──
forecast_levels_at = h.forecast_levels_at
forecast_snapshot_rows = h.forecast_snapshot_rows
parse_snapshot_times = h.parse_snapshot_times
latest_due_snapshot = h.latest_due_snapshot

TODAY_ENTITY = "sensor.solcast_pv_forecast_forecast_today"
TOMORROW_ENTITY = "sensor.solcast_pv_forecast_forecast_tomorrow"


def _iso(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


def _period(start: str, p50=None, p10=None, p90=None) -> dict:
    p = {"period_start": start}
    if p50 is not None:
        p["pv_estimate"] = p50
    if p10 is not None:
        p["pv_estimate10"] = p10
    if p90 is not None:
        p["pv_estimate90"] = p90
    return p


def test_forecast_levels_at_reports_p50_p10_p90_in_watts():
    periods = [
        _period("2026-09-30T10:00:00+02:00", 1.0, 0.4, 1.6),
        _period("2026-09-30T10:30:00+02:00", 2.0, 0.8, 3.2),
        _period("2026-09-30T11:00:00+02:00", 3.0, 1.2, 4.8),
    ]
    now = datetime.fromisoformat("2026-09-30T10:40:00+02:00")
    assert forecast_levels_at(periods, now) == (2000.0, 800.0, 3200.0)


def test_forecast_levels_at_leaves_missing_levels_null_instead_of_borrowing_p50():
    periods = [_period("2026-09-30T10:00:00+02:00", 1.0)]          # no p10/p90 in the feed
    now = datetime.fromisoformat("2026-09-30T10:10:00+02:00")
    assert forecast_levels_at(periods, now) == (1000.0, None, None)


def test_forecast_levels_at_is_none_before_the_first_period_or_on_junk():
    periods = [_period("2026-09-30T10:00:00+02:00", 1.0, 0.4, 1.6)]
    assert forecast_levels_at(periods, datetime.fromisoformat("2026-09-30T09:59:00+02:00")) \
        == (None, None, None)
    now = datetime.now(timezone.utc)
    for junk in (None, "x", [], [None, "y", {"period_start": "not-a-date"}]):
        assert forecast_levels_at(junk, now) == (None, None, None)


def test_bucket_records_forecast_band_next_to_the_central_estimate():
    a = BucketAccumulator(ts_start=0)
    s = Sample(pv_forecast_w=2000, pv_forecast_p10_w=800, pv_forecast_p90_w=3200)
    a.add(s, 0.0)
    a.add(s, 900.0)          # one 15-min bucket's worth
    row = a.bucket_row(1)
    assert row["pv_forecast_wh"] == 500.0
    assert row["pv_forecast_p10_wh"] == 200.0
    assert row["pv_forecast_p90_wh"] == 800.0


def test_bucket_forecast_band_is_null_when_solcast_gives_only_a_central_estimate():
    a = BucketAccumulator(ts_start=0)
    a.add(Sample(pv_forecast_w=2000), 900.0)
    row = a.bucket_row(1)
    assert row["pv_forecast_wh"] == 500.0
    assert row["pv_forecast_p10_wh"] is None and row["pv_forecast_p90_wh"] is None


def test_fresh_db_has_the_forecast_band_columns_and_snapshot_table():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        _recorder(tmp)._init_db()
        conn = sqlite3.connect(tmp)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket)")}
        assert {"pv_forecast_p10_wh", "pv_forecast_p90_wh"} <= cols
        snap = {r[1] for r in conn.execute("PRAGMA table_info(pv_forecast_snapshot)")}
        assert snap == {"taken_ts", "period_ts", "lead_h", "p50_wh", "p10_wh", "p90_wh",
                        "taken_local", "period_local"}
        conn.close()


def _pre_v4_bucket_ddl() -> str:
    """The current bucket DDL with the v4 band columns cut out = what a v3 DB has."""
    import re
    ddl = re.search(r"CREATE TABLE IF NOT EXISTS bucket \(.*?\n\);", h.SCHEMA, re.S).group(0)
    ddl = ddl.replace("IF NOT EXISTS ", "")
    return "\n".join(line for line in ddl.splitlines()
                     if "pv_forecast_p10_wh" not in line and "pv_forecast_p90_wh" not in line)


def test_v4_migration_adds_the_forecast_band_to_an_existing_bucket_table():
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        conn = sqlite3.connect(tmp)
        conn.execute(_V2_BUCKET)          # predates calibration_status AND the band columns
        conn.execute("INSERT INTO bucket(ts_start,local_start,pv_wh) VALUES(900,'x',1.0)")
        conn.commit(); conn.close()
        rec = _recorder(tmp)
        rec._init_db()
        rec._init_db()                    # idempotent
        conn = sqlite3.connect(tmp)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket)")}
        assert {"calibration_status", "pv_forecast_p10_wh", "pv_forecast_p90_wh"} <= cols
        row = conn.execute("SELECT pv_wh, pv_forecast_p10_wh FROM bucket WHERE ts_start=900").fetchone()
        assert row == (1.0, None)         # old rows keep their data; the new columns are NULL
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "pv_forecast_snapshot" in tables
        conn.close()
    # ...and a full-width pre-v4 table (today's schema minus the band columns) accepts a
    # bucket carrying them once migrated
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "history.db")
        conn = sqlite3.connect(tmp)
        conn.execute(_pre_v4_bucket_ddl()); conn.commit(); conn.close()
        rec = _recorder(tmp)
        rec._init_db()
        a = BucketAccumulator(ts_start=900)
        a.add(Sample(pv_forecast_w=1000, pv_forecast_p10_w=400, pv_forecast_p90_w=1600), 0.0)
        a.add(Sample(pv_forecast_w=1000, pv_forecast_p10_w=400, pv_forecast_p90_w=1600), 900.0)
        rec._write_bucket(a.bucket_row(1), a.battery_rows())
        got = sqlite3.connect(tmp).execute(
            "SELECT pv_forecast_wh, pv_forecast_p10_wh, pv_forecast_p90_wh FROM bucket").fetchone()
        assert got == (250.0, 100.0, 400.0)


# -- snapshot row builder -----------------------------------------------------

def test_snapshot_rows_convert_kw_to_wh_and_keep_the_whole_band():
    taken = _iso("2026-09-29T13:15:00+02:00")
    periods = [
        _period("2026-09-30T11:00:00+02:00", 2.0, 0.8, 3.2),     # 30-min period: kW x 0.5 h
        _period("2026-09-30T11:30:00+02:00", 4.0, 1.0, 5.0),
    ]
    rows = forecast_snapshot_rows(periods, taken)
    assert [r["period_ts"] for r in rows] == [_iso("2026-09-30T11:00:00+02:00"),
                                              _iso("2026-09-30T11:30:00+02:00")]
    r = rows[0]
    assert (r["p50_wh"], r["p10_wh"], r["p90_wh"]) == (1000.0, 400.0, 1600.0)
    assert r["taken_ts"] == taken
    assert r["lead_h"] == round((_iso("2026-09-30T11:00:00+02:00") - taken) / 3600, 4)
    assert rows[1]["p50_wh"] == 2000.0


def test_snapshot_rows_store_a_missing_band_as_null_and_drop_periods_without_a_p50():
    taken = _iso("2026-09-29T13:15:00+02:00")
    periods = [
        _period("2026-09-30T11:00:00+02:00", 2.0),               # no p10/p90
        _period("2026-09-30T11:30:00+02:00", None, 1.0, 5.0),    # no p50 -> unusable
        _period("2026-09-30T12:00:00+02:00", 1.0, 0.5, 1.5),
    ]
    rows = forecast_snapshot_rows(periods, taken)
    assert len(rows) == 2
    assert rows[0]["p10_wh"] is None and rows[0]["p90_wh"] is None
    assert rows[1]["p50_wh"] == 500.0


def test_snapshot_keeps_only_periods_that_have_not_ended():
    # The same-day sensor also lists this morning; that is hindsight, not a forecast.
    taken = _iso("2026-09-29T13:15:00+02:00")
    periods = [_period(f"2026-09-29T{hh:02d}:{mm:02d}:00+02:00", 1.0, 0.5, 1.5)
               for hh in (12, 13, 14) for mm in (0, 30)]
    rows = forecast_snapshot_rows(periods, taken)
    # 12:00 and 12:30 have ended; 13:00 is still running (ends 13:30 > 13:15)
    starts = [r["period_ts"] for r in rows]
    assert starts == [_iso("2026-09-29T13:00:00+02:00"), _iso("2026-09-29T13:30:00+02:00"),
                      _iso("2026-09-29T14:00:00+02:00"), _iso("2026-09-29T14:30:00+02:00")]
    assert rows[0]["lead_h"] == round(-15 / 60, 4)              # the running period leads negative


def test_snapshot_lead_is_exact_across_midnight():
    taken = _iso("2026-09-29T21:00:00+02:00")
    rows = forecast_snapshot_rows([
        _period("2026-09-29T23:30:00+02:00", 0.0, 0.0, 0.0),
        _period("2026-09-30T00:00:00+02:00", 0.0, 0.0, 0.0),
        _period("2026-09-30T12:00:00+02:00", 3.0, 1.0, 4.0),
    ], taken)
    assert [r["lead_h"] for r in rows] == [2.5, 3.0, 15.0]


def test_snapshot_lead_is_exact_across_both_dst_changes():
    tz_before = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        # autumn: 25 Oct 2026, 03:00 CEST -> 02:00 CET, so 02:30 happens twice
        taken = _iso("2026-10-24T21:00:00+02:00")
        rows = forecast_snapshot_rows([
            _period("2026-10-25T02:30:00+02:00", 1.0, 0.5, 1.5),    # first 02:30
            _period("2026-10-25T02:30:00+01:00", 1.0, 0.5, 1.5),    # second 02:30, an hour later
            _period("2026-10-25T03:00:00+01:00", 1.0, 0.5, 1.5),
        ], taken)
        assert [r["lead_h"] for r in rows] == [5.5, 6.5, 7.0]
        assert len({r["period_ts"] for r in rows}) == 3            # distinct keys despite equal local text
        assert rows[0]["taken_local"] == "2026-10-24T21:00:00"     # local labels are wall-clock

        # spring: 28 Mar 2027, 02:00 CET -> 03:00 CEST; 02:xx never exists
        taken = _iso("2027-03-27T21:00:00+01:00")
        rows = forecast_snapshot_rows([
            _period("2027-03-28T01:30:00+01:00", 1.0, 0.5, 1.5),
            _period("2027-03-28T03:00:00+02:00", 1.0, 0.5, 1.5),    # the 30-min step after 01:30
        ], taken)
        assert [r["lead_h"] for r in rows] == [4.5, 5.0]
    finally:
        if tz_before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = tz_before
        time.tzset()


def test_snapshot_energy_follows_the_list_spacing():
    taken = _iso("2026-09-29T13:15:00+02:00")
    hourly = [_period("2026-09-30T11:00:00+02:00", 2.0, 1.0, 3.0),
              _period("2026-09-30T12:00:00+02:00", 2.0, 1.0, 3.0)]
    assert forecast_snapshot_rows(hourly, taken)[0]["p50_wh"] == 2000.0     # 2 kW x 1 h
    single = [_period("2026-09-30T11:00:00+02:00", 2.0, 1.0, 3.0)]
    assert forecast_snapshot_rows(single, taken)[0]["p50_wh"] == 1000.0     # defaults to 30 min
    # a missing period must not stretch its neighbour's energy
    gappy = [_period("2026-09-30T11:00:00+02:00", 2.0), _period("2026-09-30T11:30:00+02:00", 2.0),
             _period("2026-09-30T13:00:00+02:00", 2.0)]
    assert [r["p50_wh"] for r in forecast_snapshot_rows(gappy, taken)] == [1000.0] * 3


def test_snapshot_rows_tolerate_junk_and_unsorted_input():
    taken = _iso("2026-09-29T13:15:00+02:00")
    for junk in (None, "x", 5, [], [None, "y", {"period_start": "nope", "pv_estimate": 1.0}]):
        assert forecast_snapshot_rows(junk, taken) == []
    shuffled = [_period("2026-09-30T12:00:00+02:00", 1.0), _period("2026-09-30T11:00:00+02:00", 1.0)]
    rows = forecast_snapshot_rows(shuffled, taken)
    assert [r["period_ts"] for r in rows] == sorted(r["period_ts"] for r in rows)


def test_parse_snapshot_times_skips_malformed_entries():
    assert parse_snapshot_times(("21:00", "13:15")) == [(13, 15), (21, 0)]
    assert parse_snapshot_times(("13:15", "13:15", " 7:05 ")) == [(7, 5), (13, 15)]
    assert parse_snapshot_times(("25:00", "12:99", "noon", "", "13")) == []
    assert parse_snapshot_times(None) == []


def test_latest_due_snapshot_finds_the_last_scheduled_instant():
    times = [(13, 15), (21, 0)]
    day = datetime(2026, 9, 29)
    assert latest_due_snapshot(day.replace(hour=14), times) == day.replace(hour=13, minute=15)
    assert latest_due_snapshot(day.replace(hour=21, minute=0), times) == day.replace(hour=21)
    assert latest_due_snapshot(day.replace(hour=23, minute=59), times) == day.replace(hour=21)
    # early morning: the last one was yesterday evening
    assert latest_due_snapshot(day.replace(hour=3), times) == (day - timedelta(days=1)).replace(hour=21)
    assert latest_due_snapshot(day.replace(hour=14), []) is None


# -- persistence + query -------------------------------------------------------

def _snap_rows(taken_ts, starts, p50=1.0):
    return forecast_snapshot_rows([_period(s, p50, p50 / 2, p50 * 2) for s in starts], taken_ts)


def test_write_snapshot_is_idempotent_and_queryable_by_target_period():
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec._init_db()
        starts = ["2026-09-30T11:00:00+02:00", "2026-09-30T11:30:00+02:00"]
        day_ahead = _snap_rows(_iso("2026-09-29T13:15:00+02:00"), starts, 1.0)
        evening = _snap_rows(_iso("2026-09-29T21:00:00+02:00"), starts, 2.0)
        rec._write_snapshot(day_ahead)
        rec._write_snapshot(day_ahead)               # same snapshot again: no duplicates
        rec._write_snapshot(evening)
        rows = rec.query_range(_iso("2026-09-30T00:00:00+02:00"), _iso("2026-09-30T23:59:00+02:00"),
                               table="pv_forecast_snapshot")
        assert len(rows) == 4
        # one target period, two vintages, ordered by period then by when it was taken
        assert [(r["period_ts"], r["taken_ts"]) for r in rows] == sorted(
            (r["period_ts"], r["taken_ts"]) for r in rows)
        first = rows[0]
        assert first["p50_wh"] == 500.0 and first["p10_wh"] == 250.0 and first["p90_wh"] == 1000.0
        assert rows[1]["p50_wh"] == 1000.0           # the evening vintage of the same period
        assert rows[1]["lead_h"] < rows[0]["lead_h"]


def test_snapshot_query_window_selects_by_target_period():
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec._init_db()
        rec._write_snapshot(_snap_rows(_iso("2026-09-29T13:15:00+02:00"),
                                       ["2026-09-29T14:00:00+02:00", "2026-09-30T14:00:00+02:00"]))
        rows = rec.query_range(_iso("2026-09-29T00:00:00+02:00"), _iso("2026-09-29T23:59:00+02:00"),
                               table="pv_forecast_snapshot")
        assert len(rows) == 1 and rows[0]["period_ts"] == _iso("2026-09-29T14:00:00+02:00")


def test_snapshot_retention_purges_old_periods_only_when_configured():
    with tempfile.TemporaryDirectory() as d:
        rec = _recorder(os.path.join(d, "history.db"))
        rec._init_db()
        rec._write_snapshot(_snap_rows(1000, ["1970-01-01T00:30:00+00:00"]))   # ancient
        assert len(rec.query_range(0, 10**9, table="pv_forecast_snapshot")) == 1   # retention off
        rec._retention_days = 1
        rec._write_snapshot(_snap_rows(int(time.time()), [
            (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()]))
        rows = rec.query_range(0, 10**11, table="pv_forecast_snapshot")
        assert len(rows) == 1 and rows[0]["period_ts"] > 10**9


# -- the recorder's snapshot job (fake hass) ------------------------------------

class _FakeHass:
    """Just enough of hass for the snapshot job: states + the executor hop."""

    def __init__(self, states):
        self._states = states
        self.states = SimpleNamespace(get=self._states.get)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _state(periods):
    return SimpleNamespace(state="16.0", attributes={"detailedForecast": periods})


def _snapshot_recorder(tmp, states, options=None):
    entry = SimpleNamespace(
        options={"history_db_path": tmp, "history_retention_days": 0,
                 "solcast_forecast_sensor": TODAY_ENTITY, **(options or {})},
        data={}, entry_id="e1", title="Wattsmith",
    )
    rec = HistoryRecorder(_FakeHass(states), entry)
    rec._init_db()
    return rec


def test_snapshot_job_copies_rest_of_today_and_all_of_tomorrow():
    NOW = _iso("2026-09-29T13:15:00+02:00")
    today = [_period(f"2026-09-29T{hh:02d}:{mm:02d}:00+02:00", 1.0, 0.5, 1.5)
             for hh in range(6, 20) for mm in (0, 30)]
    tomorrow = [_period(f"2026-09-30T{hh:02d}:{mm:02d}:00+02:00", 2.0, 1.0, 3.0)
                for hh in range(0, 24) for mm in (0, 30)]
    with tempfile.TemporaryDirectory() as d:
        rec = _snapshot_recorder(os.path.join(d, "history.db"),
                                 {TODAY_ENTITY: _state(today), TOMORROW_ENTITY: _state(tomorrow)})
        with patch.object(h.time, "time", return_value=NOW):
            written = asyncio.run(rec.async_snapshot_forecast())
        rows = rec.query_range(0, 10**11, table="pv_forecast_snapshot")
        assert written == len(rows)
        assert len(rows) == 14 + 48         # today 13:00..19:30 = 14 periods, tomorrow = 48
        assert all(r["taken_ts"] == NOW for r in rows)
        assert min(r["lead_h"] for r in rows) == round(-15 / 60, 4)


def test_snapshot_job_does_nothing_without_a_solcast_sensor_or_data():
    with tempfile.TemporaryDirectory() as d:
        rec = _snapshot_recorder(os.path.join(d, "history.db"), {}, {"solcast_forecast_sensor": ""})
        assert asyncio.run(rec.async_snapshot_forecast()) == 0            # not configured
        rec = _snapshot_recorder(os.path.join(d, "history.db"), {TODAY_ENTITY: None})
        assert asyncio.run(rec.async_snapshot_forecast()) == 0            # sensor unavailable
        assert rec.query_range(0, 10**11, table="pv_forecast_snapshot") == []


def test_catchup_takes_a_missed_snapshot_once_and_not_when_already_taken():
    tomorrow = [_period((datetime.now(timezone.utc) + timedelta(hours=h)).isoformat(), 1.0, 0.5, 1.5)
                for h in range(1, 5)]
    with tempfile.TemporaryDirectory() as d:
        rec = _snapshot_recorder(os.path.join(d, "history.db"),
                                 {TODAY_ENTITY: _state([]), TOMORROW_ENTITY: _state(tomorrow)})
        rec._snapshot_times = [(0, 0)]          # 00:00 today has always already passed
        taken = []                               # spy: two takes inside one second would merge
        real = rec.async_snapshot_forecast       # into the same rows, so count the calls instead

        async def spy():
            taken.append(1)
            return await real()
        rec.async_snapshot_forecast = spy
        asyncio.run(rec._catchup_cb(None))       # nothing recorded since -> take it now
        assert len(taken) == 1
        assert len(rec.query_range(0, 10**11, table="pv_forecast_snapshot")) == 4
        asyncio.run(rec._catchup_cb(None))       # already have one after the due time -> skip
        assert len(taken) == 1


def test_snapshot_schedule_is_registered_at_start_and_released_at_stop():
    with tempfile.TemporaryDirectory() as d:
        rec = _snapshot_recorder(os.path.join(d, "history.db"), {})
        rec.async_on_config_change = lambda source="": asyncio.sleep(0)
        h.async_track_time_change.reset_mock()
        h.async_call_later.reset_mock()
        asyncio.run(rec.async_start())
        assert h.async_track_time_change.call_count == 2                 # 13:15 and 21:00
        hours = sorted((c.kwargs["hour"], c.kwargs["minute"])
                       for c in h.async_track_time_change.call_args_list)
        assert hours == [(13, 15), (21, 0)]
        assert h.async_call_later.call_count == 1                        # the restart catch-up
        assert len(rec._unsub_snapshots) == 3
        asyncio.run(rec.async_stop())
        assert rec._unsub_snapshots == []


def test_snapshot_table_is_registered_for_query_history():
    assert "pv_forecast_snapshot" in QUERY_TABLES
    assert h._QUERY_TS_COLUMN["pv_forecast_snapshot"] == "period_ts"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} history_db tests passed ✓")
