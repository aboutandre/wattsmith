"""15-minute history database + config versioning (Part B of the arbitrage spec).

A dedicated, never-purged SQLite log of 15-minute energy buckets for offline
pattern analysis and to feed / validate the tariff-arbitrage brain. Separate
from HA's own recorder (which purges at ~10 days and isn't built for analytics).

How it works:
  - a HistoryRecorder samples the relevant HA sensors on a fixed interval,
    integrating power -> energy into the current 15-min bucket accumulator;
  - at each wall-clock 15-min boundary it flushes one `bucket` row plus one
    `battery_bucket` row per battery;
  - every Wattsmith config change is versioned (config_snapshot + config_event),
    and each bucket is stamped with the config version in effect, so config
    changes can be correlated with their downstream effect.

Sign conventions (fixed): all energy columns are Wh and >= 0 — direction is
encoded by *which* column (grid_import_wh / grid_export_wh, charge_wh /
discharge_wh), never by sign. Prices are EUR/kWh gross. SOC %, temp degC.

The pure helpers (bucket math, energy accumulation, config diff) carry no HA or
sqlite dependency and are unit-tested; the recorder is the thin I/O shell that
writes via the executor so it never blocks the event loop.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .battery_bridge import BatteryBridge
from .const import (
    CONF_EXPORT_PRICE,
    CONF_GRID_SENSOR,
    CONF_HISTORY_DB_PATH,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_HOUSE_CONSUMPTION_SENSOR,
    CONF_PV_SENSOR,
    CONF_SOLCAST_FORECAST_SENSOR,
    CONF_TIBBER_SENSOR,
    CONF_WEATHER_SENSOR,
    DOMAIN,
)
from .settings import (
    DEFAULT_EXPORT_PRICE,
    HISTORY_RETENTION_DAYS,
    HISTORY_SAMPLE_INTERVAL_S,
    HOUSE_CONSUMPTION_SENSOR,
)

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BUCKET_SECONDS = 900  # 15 minutes
_UNAVAILABLE = ("unknown", "unavailable", "none", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bucket (
  ts_start        INTEGER PRIMARY KEY,
  local_start     TEXT NOT NULL,
  pv_wh              REAL,
  pv_forecast_wh     REAL,
  house_wh           REAL,
  ev_wh              REAL,
  grid_import_wh     REAL,
  grid_export_wh     REAL,
  fleet_charge_wh    REAL,
  fleet_discharge_wh REAL,
  fleet_soc_start REAL,
  fleet_soc_end   REAL,
  price_import    REAL,
  price_export    REAL,
  outdoor_temp_c  REAL,
  manager_state   TEXT,
  ev_mode         TEXT,
  adaptive_status TEXT,
  grid_charge_wh  REAL,
  arb_reason      TEXT,
  eta_used        REAL,
  wear_used       REAL,
  sample_count    INTEGER,
  config_version  INTEGER,
  schema_version  INTEGER
);
CREATE TABLE IF NOT EXISTS battery_bucket (
  ts_start     INTEGER NOT NULL,
  battery_id   TEXT NOT NULL,
  charge_wh    REAL,
  discharge_wh REAL,
  soc_start    REAL,
  soc_end      REAL,
  temp_c       REAL,
  PRIMARY KEY (ts_start, battery_id)
);
CREATE TABLE IF NOT EXISTS battery (
  battery_id      TEXT PRIMARY KEY,
  name            TEXT,
  cost_eur        REAL,
  capacity_wh     REAL,
  expected_cycles INTEGER,
  cycle_offset    REAL,
  install_date    TEXT
);
CREATE TABLE IF NOT EXISTS config_snapshot (
  version      INTEGER PRIMARY KEY,
  ts           INTEGER NOT NULL,
  local_time   TEXT NOT NULL,
  source       TEXT,
  config_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_event (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  version    INTEGER NOT NULL,
  ts         INTEGER NOT NULL,
  local_time TEXT NOT NULL,
  scope      TEXT NOT NULL,
  key        TEXT NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  source     TEXT
);
CREATE TABLE IF NOT EXISTS meta ( key TEXT PRIMARY KEY, value TEXT );
"""


# ---------------------------------------------------------------------------
# Pure helpers (no HA / sqlite) — unit tested
# ---------------------------------------------------------------------------

def bucket_start(ts: float) -> int:
    """Floor an epoch timestamp to the start of its 15-minute bucket."""
    return int(ts // BUCKET_SECONDS) * BUCKET_SECONDS


def flatten_config(config: dict[str, Any]) -> dict[str, str]:
    """Flatten a (possibly nested) config dict to dotted keys with str values.

    Nested per-battery config becomes e.g. "battery.<id>.cost_eur". Values are
    JSON-encoded scalars so diffing is exact and type-stable.
    """
    out: dict[str, str] = {}

    def _walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                _walk(f"{prefix}.{k}" if prefix else str(k), v)
        else:
            out[prefix] = json.dumps(value, sort_keys=True, default=str)

    _walk("", config)
    return out


def diff_config(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """Return [(key, old_value, new_value)] for every changed/added/removed key.

    Keys are the flattened dotted form; values are JSON strings (or None when the
    key was absent). Deterministically ordered for stable event logs / tests.
    """
    fo, fn = flatten_config(old), flatten_config(new)
    changes: list[tuple[str, str | None, str | None]] = []
    for key in sorted(set(fo) | set(fn)):
        ov, nv = fo.get(key), fn.get(key)
        if ov != nv:
            changes.append((key, ov, nv))
    return changes


def _split_scope_key(dotted: str) -> tuple[str, str]:
    """'battery.<id>.cost_eur' -> ('<id>', 'cost_eur'); else ('global', dotted)."""
    parts = dotted.split(".")
    if len(parts) >= 3 and parts[0] == "battery":
        return parts[1], ".".join(parts[2:])
    return "global", dotted


@dataclass
class BatteryAccum:
    """Per-battery energy/SOC accumulation within one bucket."""

    charge_wh: float = 0.0
    discharge_wh: float = 0.0
    soc_start: float | None = None
    soc_end: float | None = None
    temp_c: float | None = None

    def integrate(self, power_w: float | None, dt_s: float) -> None:
        # power sign convention: + = discharging, - = charging (base sensor)
        if power_w is None or dt_s <= 0:
            return
        wh = abs(power_w) * dt_s / 3600.0
        if power_w > 0:
            self.discharge_wh += wh
        elif power_w < 0:
            self.charge_wh += wh

    def snapshot(self, soc: float | None, temp: float | None) -> None:
        if soc is not None:
            if self.soc_start is None:
                self.soc_start = soc
            self.soc_end = soc
        if temp is not None:
            self.temp_c = temp


@dataclass
class Sample:
    """One instantaneous read of every logged channel."""

    pv_w: float | None = None
    house_w: float | None = None
    ev_w: float | None = None
    grid_w: float | None = None          # + import, - export
    pv_forecast_w: float | None = None
    price_import: float | None = None
    price_export: float | None = None
    outdoor_temp_c: float | None = None
    manager_state: str | None = None
    ev_mode: str | None = None
    adaptive_status: str | None = None
    fleet_soc: float | None = None
    batteries: dict[str, tuple[float | None, float | None, float | None]] = field(default_factory=dict)
    # batteries: {battery_id: (power_w, soc, temp_c)}


@dataclass
class BucketAccumulator:
    """Accumulates energy + snapshots for one 15-minute bucket."""

    ts_start: int
    pv_wh: float = 0.0
    house_wh: float = 0.0
    ev_wh: float = 0.0
    import_wh: float = 0.0
    export_wh: float = 0.0
    pv_forecast_wh: float = 0.0
    _forecast_seen: bool = False
    fleet_soc_start: float | None = None
    fleet_soc_end: float | None = None
    price_import: float | None = None
    price_export: float | None = None
    outdoor_temp_c: float | None = None
    manager_state: str | None = None
    ev_mode: str | None = None
    adaptive_status: str | None = None
    sample_count: int = 0
    batteries: dict[str, BatteryAccum] = field(default_factory=dict)

    def add(self, sample: Sample, dt_s: float) -> None:
        """Integrate power channels over dt_s and update snapshots."""
        self.sample_count += 1
        if sample.pv_w is not None and dt_s > 0:
            self.pv_wh += max(0.0, sample.pv_w) * dt_s / 3600.0
        if sample.house_w is not None and dt_s > 0:
            self.house_wh += max(0.0, sample.house_w) * dt_s / 3600.0
        if sample.ev_w is not None and dt_s > 0:
            self.ev_wh += max(0.0, sample.ev_w) * dt_s / 3600.0
        if sample.grid_w is not None and dt_s > 0:
            wh = abs(sample.grid_w) * dt_s / 3600.0
            if sample.grid_w > 0:
                self.import_wh += wh
            else:
                self.export_wh += wh
        if sample.pv_forecast_w is not None and dt_s > 0:
            self.pv_forecast_wh += max(0.0, sample.pv_forecast_w) * dt_s / 3600.0
            self._forecast_seen = True
        # snapshots (representative = last seen; start = first seen)
        if sample.fleet_soc is not None:
            if self.fleet_soc_start is None:
                self.fleet_soc_start = sample.fleet_soc
            self.fleet_soc_end = sample.fleet_soc
        for name, val in (
            ("price_import", sample.price_import),
            ("price_export", sample.price_export),
            ("outdoor_temp_c", sample.outdoor_temp_c),
            ("manager_state", sample.manager_state),
            ("ev_mode", sample.ev_mode),
            ("adaptive_status", sample.adaptive_status),
        ):
            if val is not None:
                setattr(self, name, val)
        for bid, (power, soc, temp) in sample.batteries.items():
            acc = self.batteries.setdefault(bid, BatteryAccum())
            acc.integrate(power, dt_s)
            acc.snapshot(soc, temp)

    def fleet_charge_wh(self) -> float:
        return sum(b.charge_wh for b in self.batteries.values())

    def fleet_discharge_wh(self) -> float:
        return sum(b.discharge_wh for b in self.batteries.values())

    def bucket_row(self, config_version: int) -> dict[str, Any]:
        local = datetime.fromtimestamp(self.ts_start).isoformat(timespec="seconds")
        return {
            "ts_start": self.ts_start,
            "local_start": local,
            "pv_wh": round(self.pv_wh, 2),
            "pv_forecast_wh": round(self.pv_forecast_wh, 2) if self._forecast_seen else None,
            "house_wh": round(self.house_wh, 2),
            "ev_wh": round(self.ev_wh, 2),
            "grid_import_wh": round(self.import_wh, 2),
            "grid_export_wh": round(self.export_wh, 2),
            "fleet_charge_wh": round(self.fleet_charge_wh(), 2),
            "fleet_discharge_wh": round(self.fleet_discharge_wh(), 2),
            "fleet_soc_start": self.fleet_soc_start,
            "fleet_soc_end": self.fleet_soc_end,
            "price_import": self.price_import,
            "price_export": self.price_export,
            "outdoor_temp_c": self.outdoor_temp_c,
            "manager_state": self.manager_state,
            "ev_mode": self.ev_mode,
            "adaptive_status": self.adaptive_status,
            "grid_charge_wh": None,   # filled by the arbitrage planner (Phase 3/4)
            "arb_reason": None,
            "eta_used": None,
            "wear_used": None,
            "sample_count": self.sample_count,
            "config_version": config_version,
            "schema_version": SCHEMA_VERSION,
        }

    def battery_rows(self) -> list[dict[str, Any]]:
        rows = []
        for bid, b in self.batteries.items():
            rows.append({
                "ts_start": self.ts_start,
                "battery_id": bid,
                "charge_wh": round(b.charge_wh, 2),
                "discharge_wh": round(b.discharge_wh, 2),
                "soc_start": b.soc_start,
                "soc_end": b.soc_end,
                "temp_c": b.temp_c,
            })
        return rows


# ---------------------------------------------------------------------------
# The recorder — HA + sqlite shell
# ---------------------------------------------------------------------------

class HistoryRecorder:
    """Samples HA state into 15-min buckets and persists them to SQLite."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.bridge = BatteryBridge(hass)
        self._db_path = self._resolve_path()
        self._retention_days = int(
            entry.options.get(CONF_HISTORY_RETENTION_DAYS, HISTORY_RETENTION_DAYS)
        )
        self._accum: BucketAccumulator | None = None
        self._last_sample_ts: float | None = None
        self._config_version = 1
        self._unsub = None
        self._started = False

    # ---- lifecycle ------------------------------------------------------
    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self._init_db)
        # baseline config snapshot + battery dimension
        await self.async_on_config_change(source="startup")
        self._unsub = async_track_time_interval(
            self.hass, self._sample_cb, timedelta(seconds=HISTORY_SAMPLE_INTERVAL_S)
        )
        self._started = True
        _LOGGER.info("Wattsmith history DB active at %s", self._db_path)

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        # flush the partial bucket so a restart doesn't lose it
        if self._accum is not None and self._accum.sample_count > 0:
            await self._flush(self._accum)
            self._accum = None
        await self.hass.async_add_executor_job(self._checkpoint)

    def _resolve_path(self) -> str:
        configured = self.entry.options.get(CONF_HISTORY_DB_PATH)
        if configured:
            return configured
        return self.hass.config.path("wattsmith", "history.db")

    # ---- sampling -------------------------------------------------------
    async def _sample_cb(self, _now) -> None:
        try:
            sample = self._read_sample()
        except Exception as err:  # noqa: BLE001 - logging must never break the loop
            _LOGGER.debug("history sample read failed: %s", err)
            return
        mono = time.monotonic()
        wall = time.time()
        b_start = bucket_start(wall)
        if self._accum is None:
            self._accum = BucketAccumulator(ts_start=b_start)
            self._last_sample_ts = mono
            self._accum.add(sample, 0.0)
            return
        if b_start != self._accum.ts_start:
            # crossed a boundary: finalise the old bucket, open a new one
            finished, self._accum = self._accum, BucketAccumulator(ts_start=b_start)
            self._last_sample_ts = mono
            self._accum.add(sample, 0.0)
            await self._flush(finished)
            return
        dt = mono - (self._last_sample_ts or mono)
        self._last_sample_ts = mono
        # guard against a stalled loop crediting a huge dt to one sample
        self._accum.add(sample, min(dt, 2 * HISTORY_SAMPLE_INTERVAL_S))

    def _read_sample(self) -> Sample:
        o = self.entry.options
        batteries: dict[str, tuple[float | None, float | None, float | None]] = {}
        socs: list[tuple[float, float]] = []
        for st in self.bridge.read_all():
            temp = self._battery_temp(st.battery_id)
            batteries[st.battery_id] = (float(st.power), st.soc, temp)
            if st.soc is not None and st.capacity:
                socs.append((st.soc, st.capacity))
        fleet_soc = (
            sum(s * c for s, c in socs) / sum(c for _, c in socs) if socs else None
        )
        return Sample(
            pv_w=self._num(o.get(CONF_PV_SENSOR)),
            house_w=self._num(o.get(CONF_HOUSE_CONSUMPTION_SENSOR) or HOUSE_CONSUMPTION_SENSOR),
            ev_w=self._ev_power(),
            grid_w=self._num(o.get(CONF_GRID_SENSOR) or self.entry.data.get(CONF_GRID_SENSOR)),
            pv_forecast_w=self._forecast_w(o.get(CONF_SOLCAST_FORECAST_SENSOR)),
            price_import=self._num(o.get(CONF_TIBBER_SENSOR)),
            price_export=float(o.get(CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE)),
            outdoor_temp_c=self._weather_temp(o.get(CONF_WEATHER_SENSOR)),
            manager_state=self._attr_state(f"sensor.{DOMAIN}_status"),
            ev_mode=self._attr_state(f"select.{DOMAIN}_ev_ev_charging_mode"),
            adaptive_status=self._attr_state(f"sensor.{DOMAIN}_adaptive_status"),
            fleet_soc=fleet_soc,
            batteries=batteries,
        )

    # ---- state readers --------------------------------------------------
    def _num(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or str(st.state).lower() in _UNAVAILABLE:
            return None
        try:
            return float(st.state)
        except (ValueError, TypeError):
            return None

    def _attr_state(self, entity_id: str) -> str | None:
        st = self.hass.states.get(entity_id)
        if st is None or str(st.state).lower() in _UNAVAILABLE:
            return None
        return str(st.state)

    def _ev_power(self) -> float | None:
        ev = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id + "_ev")
        recent = getattr(ev, "ev_power_recent", None)
        if callable(recent):
            v = recent()
            if v is not None:
                return float(v)
        return self._num(f"sensor.{DOMAIN}_ev_ev_power")

    def _battery_temp(self, battery_id: str) -> float | None:
        # best-effort: the base exposes battery_temperature per device; try the
        # deterministic entity if the friendly slug is derivable, else None.
        return None

    def _weather_temp(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None:
            return None
        val = st.attributes.get("temperature") if st.attributes else None
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    def _forecast_w(self, entity_id: str | None) -> float | None:
        """Best-effort per-slot PV forecast (W) from a Solcast detailed sensor.

        Solcast's forecast-today sensor carries a `detailedForecast` /
        `detailedHourly` attribute: a list of {period_start, pv_estimate(kW)}.
        Find the period covering now. Any missing piece -> None (column stays null).
        """
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or not st.attributes:
            return None
        periods = st.attributes.get("detailedForecast") or st.attributes.get("detailedHourly")
        if not isinstance(periods, list):
            return None
        now = datetime.now(timezone.utc)
        best = None
        for p in periods:
            start = p.get("period_start")
            if isinstance(start, str):
                try:
                    start = datetime.fromisoformat(start)
                except ValueError:
                    continue
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start <= now:
                best = p.get("pv_estimate")
            else:
                break
        try:
            return float(best) * 1000.0 if best is not None else None
        except (ValueError, TypeError):
            return None

    # ---- config versioning ----------------------------------------------
    async def async_on_config_change(self, source: str = "options") -> None:
        """Snapshot + diff the config; bump the version. Idempotent on no change."""
        config = self._current_config()
        await self.hass.async_add_executor_job(self._write_config, config, source)

    def _current_config(self) -> dict[str, Any]:
        cfg = dict(self.entry.options)
        cfg["_data"] = dict(self.entry.data)
        cfg["battery"] = self._battery_config()
        return cfg

    def _battery_config(self) -> dict[str, Any]:
        """Per-battery economics config (from options), keyed by battery_id."""
        raw = self.entry.options.get("battery_config") or {}
        out: dict[str, Any] = {}
        for st in self.bridge.read_all():
            bid = st.battery_id
            b = dict(raw.get(bid, {}))
            b.setdefault("capacity_wh", st.capacity)
            out[bid] = b
        return out

    # ---- sqlite (executor thread) --------------------------------------
    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('created_at',?)",
                (datetime.now().isoformat(timespec="seconds"),),
            )
            for k, v in (
                ("schema_version", str(SCHEMA_VERSION)),
                ("bucket_seconds", str(BUCKET_SECONDS)),
                ("sign_conventions",
                 "energy Wh >=0; import/export & charge/discharge split by column; "
                 "prices EUR/kWh gross; soc %; temp degC"),
            ):
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, v))
            row = conn.execute("SELECT MAX(version) FROM config_snapshot").fetchone()
            self._config_version = (row[0] or 0)
            conn.commit()
        finally:
            conn.close()

    def _write_config(self, config: dict[str, Any], source: str) -> None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT config_json FROM config_snapshot "
                               "ORDER BY version DESC LIMIT 1").fetchone()
            prev = json.loads(row[0]) if row else {}
            changes = diff_config(prev, config)
            if not changes and row is not None:
                return  # nothing changed — don't bump the version
            self._config_version += 1
            ts = int(time.time())
            local = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO config_snapshot(version,ts,local_time,source,config_json) "
                "VALUES(?,?,?,?,?)",
                (self._config_version, ts, local, source,
                 json.dumps(config, sort_keys=True, default=str)),
            )
            for dotted, ov, nv in changes:
                scope, key = _split_scope_key(dotted)
                conn.execute(
                    "INSERT INTO config_event"
                    "(version,ts,local_time,scope,key,old_value,new_value,source) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (self._config_version, ts, local, scope, key, ov, nv, source),
                )
            # keep the battery dimension current
            for bid, b in (config.get("battery") or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO battery"
                    "(battery_id,name,cost_eur,capacity_wh,expected_cycles,cycle_offset,install_date)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (bid, b.get("name"), b.get("cost_eur"), b.get("capacity_wh"),
                     b.get("expected_cycles"), b.get("cycle_offset"), b.get("install_date")),
                )
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_config_version',?)",
                         (str(self._config_version),))
            conn.commit()
            _LOGGER.debug("history: config v%d written (%d changes, %s)",
                          self._config_version, len(changes), source)
        finally:
            conn.close()

    async def _flush(self, accum: BucketAccumulator) -> None:
        row = accum.bucket_row(self._config_version)
        brows = accum.battery_rows()
        await self.hass.async_add_executor_job(self._write_bucket, row, brows)

    def _write_bucket(self, row: dict[str, Any], brows: list[dict[str, Any]]) -> None:
        conn = self._connect()
        try:
            cols = ",".join(row)
            ph = ",".join("?" for _ in row)
            conn.execute(f"INSERT OR REPLACE INTO bucket({cols}) VALUES({ph})",
                         tuple(row.values()))
            for b in brows:
                bc = ",".join(b)
                bp = ",".join("?" for _ in b)
                conn.execute(f"INSERT OR REPLACE INTO battery_bucket({bc}) VALUES({bp})",
                             tuple(b.values()))
            if self._retention_days > 0:
                cutoff = int(time.time()) - self._retention_days * 86400
                conn.execute("DELETE FROM bucket WHERE ts_start < ?", (cutoff,))
                conn.execute("DELETE FROM battery_bucket WHERE ts_start < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    def _checkpoint(self) -> None:
        try:
            conn = self._connect()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("history checkpoint failed: %s", err)
