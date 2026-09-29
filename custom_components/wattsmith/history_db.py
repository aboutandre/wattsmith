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
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_change,
    async_track_time_interval,
)

from .arbitrage import solcast_forecast_entities
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
from .safety import SOURCE_ACK, SOURCE_READ
from .settings import (
    DEFAULT_EXPORT_PRICE,
    HISTORY_FORECAST_SNAPSHOT_TIMES,
    HISTORY_HEALTH_HEARTBEAT_S,
    HISTORY_HEALTH_PROBE_S,
    HISTORY_RETENTION_DAYS,
    HISTORY_SAMPLE_INTERVAL_S,
    HISTORY_SNAPSHOT_CATCHUP_DELAY_S,
    HOUSE_CONSUMPTION_SENSOR,
)

_LOGGER = logging.getLogger(__name__)

# 2: + battery_health liveness log; 3: + calibration (bucket column + event log);
# 4: + p10/p90 forecast on the bucket, + pv_forecast_snapshot (day-ahead Solcast log)
SCHEMA_VERSION = 4
BUCKET_SECONDS = 900  # 15 minutes
_UNAVAILABLE = ("unknown", "unavailable", "none", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bucket (
  ts_start        INTEGER PRIMARY KEY,
  local_start     TEXT NOT NULL,
  pv_wh              REAL,
  pv_forecast_wh     REAL,
  pv_forecast_p10_wh REAL,   -- Solcast pessimistic (p10) for the same slot
  pv_forecast_p90_wh REAL,   -- Solcast optimistic (p90) for the same slot
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
  calibration_status TEXT,
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
CREATE TABLE IF NOT EXISTS battery_health (
  ts          INTEGER NOT NULL,
  local_time  TEXT NOT NULL,
  battery_id  TEXT NOT NULL,
  available   INTEGER NOT NULL,
  soc         REAL,
  soc_age_s   REAL,
  fails       INTEGER,
  excluded    INTEGER,
  last_error  TEXT,
  PRIMARY KEY (ts, battery_id)
);
CREATE INDEX IF NOT EXISTS battery_health_by_battery
  ON battery_health (battery_id, ts);
CREATE TABLE IF NOT EXISTS calibration_event (
  ts               INTEGER NOT NULL,   -- start of the 15-min bucket in which the BMS reset to 100%
  local_time       TEXT NOT NULL,
  battery_id       TEXT NOT NULL,
  days_since_full  REAL,               -- since the previous reset
  discharged_kwh   REAL,               -- AC energy discharged since the previous reset
  predicted_pts    REAL,               -- drift the model predicted (walk-forward; NULL = no model yet)
  actual_pts       REAL,               -- size of the BMS correction, net of that bucket's charging
  drift_rate       REAL,               -- pts per kWh the prediction used
  source           TEXT,               -- grid | pv_calibration | natural | unknown
  missing_buckets  INTEGER,            -- logging gaps in the window (discharged_kwh slightly low)
  PRIMARY KEY (ts, battery_id)
);
-- What Solcast promised, and when. One row per 30-min period per snapshot; the same
-- target period appears once per snapshot that covered it, so lead time is a column.
CREATE TABLE IF NOT EXISTS pv_forecast_snapshot (
  taken_ts     INTEGER NOT NULL,   -- when the snapshot was taken (epoch)
  period_ts    INTEGER NOT NULL,   -- start of the forecast period (epoch)
  lead_h       REAL NOT NULL,      -- (period start - taken) in hours; < 0 = period already running
  p50_wh       REAL NOT NULL,      -- central estimate, energy over the period
  p10_wh       REAL,               -- pessimistic (NULL if Solcast gave none)
  p90_wh       REAL,               -- optimistic
  taken_local  TEXT NOT NULL,
  period_local TEXT NOT NULL,
  PRIMARY KEY (taken_ts, period_ts)
);
CREATE INDEX IF NOT EXISTS pv_forecast_snapshot_by_period
  ON pv_forecast_snapshot (period_ts, taken_ts);
CREATE TABLE IF NOT EXISTS meta ( key TEXT PRIMARY KEY, value TEXT );
"""

# Tables the query_history service is allowed to read (fixed allow-list — the
# table name is interpolated into SQL, so anything outside this set is rejected
# before it ever reaches sqlite).
QUERY_TABLES = ("bucket", "battery_bucket", "battery_health", "config_snapshot", "config_event",
                "calibration_event", "pv_forecast_snapshot")
_QUERY_TS_COLUMN = {
    "bucket": "ts_start",
    "battery_bucket": "ts_start",
    "battery_health": "ts",
    "config_snapshot": "ts",
    "config_event": "ts",
    "calibration_event": "ts",
    # keyed on the TARGET period, so a window of days returns every forecast made for
    # them (day-ahead and same-day) next to the bucket actuals for the same window.
    "pv_forecast_snapshot": "period_ts",
}
# Tie-break for tables where many rows share the timestamp column.
_QUERY_TIEBREAK = {"pv_forecast_snapshot": "taken_ts"}

# bucket columns added after v1: (name, type). CREATE IF NOT EXISTS leaves an existing
# table as it was, so _init_db adds whichever of these an older DB is missing.
_BUCKET_MIGRATIONS = (
    ("calibration_status", "TEXT"),      # v3
    ("pv_forecast_p10_wh", "REAL"),      # v4
    ("pv_forecast_p90_wh", "REAL"),      # v4
)

# Calibration status stamped on a bucket = the most significant one seen during it,
# so a bucket that grid-charged and ended "ok" still says it grid-charged.
_CALIBRATION_RANK = {"grid_charging": 5, "grid_waiting": 4, "due": 3, "ok": 2,
                     "off": 1, "unknown": 0, "error": 0}


# ---------------------------------------------------------------------------
# Pure helpers (no HA / sqlite) — unit tested
# ---------------------------------------------------------------------------

def _pick_fault(per_source: dict[str, Any]) -> tuple[int, bool, str | None]:
    """Flatten one battery's per-source faults into the health row's columns.

    The read streak wins when both are active: an unreadable device is the more
    severe fault and the one that actually gates dispatch. The surviving reason is
    tagged with its source so a row is self-describing.
    """
    for source in (SOURCE_READ, SOURCE_ACK):
        fault = per_source.get(source)
        if not fault:
            continue
        error = fault.get("last_error")
        return (
            int(fault.get("fails", 0) or 0),
            bool(fault.get("excluded", False)),
            f"[{source}] {error}" if error else f"[{source}]",
        )
    return 0, False, None


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


def _parse_period_start(value: Any) -> datetime | None:
    """A detailedForecast `period_start` as an aware datetime (naive -> UTC), or None."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _kw(value: Any) -> float | None:
    """A Solcast kW figure as a float, or None when absent or not a number."""
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def forecast_levels_at(
    periods: Any, now: datetime
) -> tuple[float | None, float | None, float | None]:
    """(p50, p10, p90) in W for the period covering `now`, from a detailedForecast list.

    The period is the last one that started at or before `now`. Anything missing is
    None: the p10/p90 columns record what Solcast said, so they stay NULL rather than
    borrowing p50 (the planner's own fallback lives in arbitrage.pv_slots_from_detailed).
    """
    if not isinstance(periods, list):
        return None, None, None
    best: dict[str, Any] | None = None
    for p in periods:
        start = _parse_period_start(p.get("period_start")) if isinstance(p, dict) else None
        if start is None:
            continue
        if start <= now:
            best = p
        else:
            break
    if best is None:
        return None, None, None
    p50, p10, p90 = (_kw(best.get(k)) for k in ("pv_estimate", "pv_estimate10", "pv_estimate90"))
    return (
        None if p50 is None else p50 * 1000.0,
        None if p10 is None else p10 * 1000.0,
        None if p90 is None else p90 * 1000.0,
    )


SNAPSHOT_DEFAULT_PERIOD_S = 1800  # Solcast detailedForecast is 30-min; used for a one-period list


def forecast_snapshot_rows(periods: Any, taken_ts: int) -> list[dict[str, Any]]:
    """pv_forecast_snapshot rows from one Solcast detailedForecast list (hel-132).

    - Only periods still to come (or running) at `taken_ts` are kept: an earlier period
      on the same-day sensor is hindsight, not forecast, and would corrupt lead-time stats.
    - Energy per period = kW x period length. The length is the spacing of the list (30
      min for Solcast), so the Wh stay right if the granularity ever changes.
    - lead_h comes from epoch seconds, so it is exact across midnight and DST changes.
    - A period with no p50 is dropped; a missing p10/p90 is stored as NULL.
    """
    if not isinstance(periods, list):
        return []
    parsed: list[tuple[int, dict[str, Any]]] = []
    for p in periods:
        start = _parse_period_start(p.get("period_start")) if isinstance(p, dict) else None
        if start is not None:
            parsed.append((int(start.timestamp()), p))
    if not parsed:
        return []
    parsed.sort(key=lambda item: item[0])
    gaps = [b[0] - a[0] for a, b in zip(parsed, parsed[1:]) if b[0] > a[0]]
    period_s = min(gaps) if gaps else SNAPSHOT_DEFAULT_PERIOD_S

    def wh(value: Any) -> float | None:
        kw = _kw(value)
        return None if kw is None else round(kw * 1000.0 * period_s / 3600.0, 2)

    taken_local = datetime.fromtimestamp(taken_ts).isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    for ts, p in parsed:
        if ts + period_s <= taken_ts:
            continue
        p50 = wh(p.get("pv_estimate"))
        if p50 is None:
            continue
        rows.append({
            "taken_ts": taken_ts,
            "period_ts": ts,
            "lead_h": round((ts - taken_ts) / 3600.0, 4),
            "p50_wh": p50,
            "p10_wh": wh(p.get("pv_estimate10")),
            "p90_wh": wh(p.get("pv_estimate90")),
            "taken_local": taken_local,
            "period_local": datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
        })
    return rows


def parse_snapshot_times(times: Any) -> list[tuple[int, int]]:
    """['13:15', '21:00'] -> [(13, 15), (21, 0)]; malformed entries are skipped, not fatal."""
    out: set[tuple[int, int]] = set()
    for raw in times or ():
        try:
            hh, mm = str(raw).strip().split(":")
            hour, minute = int(hh), int(mm)
        except ValueError:
            _LOGGER.warning("ignoring malformed forecast snapshot time %r (want 'HH:MM')", raw)
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            out.add((hour, minute))
        else:
            _LOGGER.warning("ignoring out-of-range forecast snapshot time %r", raw)
    return sorted(out)


def latest_due_snapshot(now: datetime, times: list[tuple[int, int]]) -> datetime | None:
    """The most recent scheduled snapshot instant at or before `now` (today or yesterday).

    Used at startup to tell whether a snapshot came due while HA was down.
    """
    due = [
        (now - timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
        for back in (0, 1)
        for h, m in times
    ]
    due = [d for d in due if d <= now]
    return max(due) if due else None


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
    pv_forecast_p10_w: float | None = None
    pv_forecast_p90_w: float | None = None
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
    pv_forecast_p10_wh: float = 0.0
    _p10_seen: bool = False
    pv_forecast_p90_wh: float = 0.0
    _p90_seen: bool = False
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
        if sample.pv_forecast_p10_w is not None and dt_s > 0:
            self.pv_forecast_p10_wh += max(0.0, sample.pv_forecast_p10_w) * dt_s / 3600.0
            self._p10_seen = True
        if sample.pv_forecast_p90_w is not None and dt_s > 0:
            self.pv_forecast_p90_wh += max(0.0, sample.pv_forecast_p90_w) * dt_s / 3600.0
            self._p90_seen = True
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
            "pv_forecast_p10_wh": round(self.pv_forecast_p10_wh, 2) if self._p10_seen else None,
            "pv_forecast_p90_wh": round(self.pv_forecast_p90_wh, 2) if self._p90_seen else None,
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
            "calibration_status": None,   # stamped by the recorder at flush
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
        self._unsub_health = None
        self._started = False
        # latest arbitrage advisory, stamped onto the bucket when it flushes
        self._advisory: tuple[float, str, float, float] | None = None
        # most significant calibration status seen in the current bucket
        self._calibration_status: str | None = None
        # battery liveness: last row written per battery, for change detection
        self._health_last: dict[str, tuple[bool, int, bool]] = {}
        self._health_last_write: dict[str, float] = {}
        # Solcast forecast snapshots (hel-132): local wall-clock schedule + its listeners
        self._snapshot_times = parse_snapshot_times(HISTORY_FORECAST_SNAPSHOT_TIMES)
        self._unsub_snapshots: list[Any] = []

    # ---- lifecycle ------------------------------------------------------
    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self._init_db)
        # baseline config snapshot + battery dimension
        await self.async_on_config_change(source="startup")
        self._unsub = async_track_time_interval(
            self.hass, self._sample_cb, timedelta(seconds=HISTORY_SAMPLE_INTERVAL_S)
        )
        self._unsub_health = async_track_time_interval(
            self.hass, self._health_cb, timedelta(seconds=HISTORY_HEALTH_PROBE_S)
        )
        for hour, minute in self._snapshot_times:
            self._unsub_snapshots.append(
                async_track_time_change(
                    self.hass, self._snapshot_cb, hour=hour, minute=minute, second=0
                )
            )
        if self._snapshot_times:
            self._unsub_snapshots.append(
                async_call_later(self.hass, HISTORY_SNAPSHOT_CATCHUP_DELAY_S, self._catchup_cb)
            )
        self._started = True
        _LOGGER.info("Wattsmith history DB active at %s", self._db_path)

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._unsub_health is not None:
            self._unsub_health()
            self._unsub_health = None
        for unsub in self._unsub_snapshots:
            unsub()
        self._unsub_snapshots = []
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

    # ---- battery liveness -----------------------------------------------
    async def _health_cb(self, _now) -> None:
        """Log per-battery liveness — change-triggered, with a slow heartbeat.

        Deliberately independent of the control loop: when a battery goes silent
        the loop just stops mentioning it, which is exactly when a separate,
        timestamped record is worth having.
        """
        try:
            rows = self._collect_health()
        except Exception as err:  # noqa: BLE001 - observability must never break HA
            _LOGGER.debug("battery health probe failed: %s", err)
            return
        if rows:
            await self.hass.async_add_executor_job(self._write_health, rows)

    def _collect_health(self) -> list[dict[str, Any]]:
        """Build the rows that actually need writing this probe."""
        now = time.time()
        faults = self._supervisor_faults()
        rows: list[dict[str, Any]] = []
        for h in self.bridge.read_health():
            fails, excluded, last_error = _pick_fault(faults.get(h.battery_id) or {})
            key = (h.available, fails, excluded)
            changed = self._health_last.get(h.battery_id) != key
            stale = (
                now - self._health_last_write.get(h.battery_id, 0.0)
                >= HISTORY_HEALTH_HEARTBEAT_S
            )
            if not (changed or stale):
                continue
            self._health_last[h.battery_id] = key
            self._health_last_write[h.battery_id] = now
            if changed:
                _LOGGER.info(
                    "Battery %s liveness: available=%s fails=%d excluded=%s soc=%s age=%ss",
                    h.battery_id, h.available, fails, excluded, h.soc,
                    None if h.soc_age_s is None else round(h.soc_age_s),
                )
            rows.append({
                "ts": int(now),
                "local_time": datetime.now().isoformat(timespec="seconds"),
                "battery_id": h.battery_id,
                "available": 1 if h.available else 0,
                "soc": h.soc,
                "soc_age_s": h.soc_age_s,
                "fails": fails,
                "excluded": 1 if excluded else 0,
                "last_error": last_error,
            })
        return rows

    def _supervisor_faults(self) -> dict[str, dict[str, Any]]:
        """The manager's live per-battery fault detail, if the manager is up."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        data = getattr(coordinator, "data", None) or {}
        safety = data.get("safety") or {}
        faults = safety.get("battery_faults")
        return faults if isinstance(faults, dict) else {}

    def _write_health(self, rows: list[dict[str, Any]]) -> None:
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO battery_health"
                " (ts, local_time, battery_id, available, soc, soc_age_s, fails,"
                "  excluded, last_error)"
                " VALUES (:ts, :local_time, :battery_id, :available, :soc, :soc_age_s,"
                "         :fails, :excluded, :last_error)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _read_sample(self) -> Sample:
        o = self.entry.options
        fc_p50, fc_p10, fc_p90 = self._forecast_levels_w(o.get(CONF_SOLCAST_FORECAST_SENSOR))
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
            pv_forecast_w=fc_p50,
            pv_forecast_p10_w=fc_p10,
            pv_forecast_p90_w=fc_p90,
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

    def _forecast_levels_w(
        self, entity_id: str | None
    ) -> tuple[float | None, float | None, float | None]:
        """Best-effort (p50, p10, p90) PV forecast in W for the slot covering now.

        Solcast's forecast-today sensor carries a `detailedForecast` / `detailedHourly`
        attribute: a list of {period_start, pv_estimate, pv_estimate10, pv_estimate90}
        in kW. Any missing piece -> None (that column stays null).
        """
        if not entity_id:
            return None, None, None
        st = self.hass.states.get(entity_id)
        if st is None or not st.attributes:
            return None, None, None
        periods = st.attributes.get("detailedForecast") or st.attributes.get("detailedHourly")
        return forecast_levels_at(periods, datetime.now(timezone.utc))

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
            # CREATE IF NOT EXISTS leaves an existing bucket table as is: add what it lacks
            cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket)")}
            for name, ctype in _BUCKET_MIGRATIONS:
                if name not in cols:
                    conn.execute(f"ALTER TABLE bucket ADD COLUMN {name} {ctype}")
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

    def note_advisory(self, grid_charge_wh: float, reason: str,
                      eta: float, wear_eur_per_kwh: float) -> None:
        """Record the current arbitrage advisory; stamped onto the next flush."""
        self._advisory = (grid_charge_wh, reason, eta, wear_eur_per_kwh)

    def note_calibration(self, status: str | None) -> None:
        """Record the calibration status; the most significant one per bucket is stamped."""
        if status is None:
            return
        cur = self._calibration_status
        if cur is None or _CALIBRATION_RANK.get(status, 0) > _CALIBRATION_RANK.get(cur, 0):
            self._calibration_status = status

    async def async_record_calibration_events(self, events: list[dict[str, Any]]) -> None:
        """Log BMS resets (idempotent: one row per bucket and battery)."""
        if events:
            await self.hass.async_add_executor_job(self._write_calibration_events, events)

    def _write_calibration_events(self, events: list[dict[str, Any]]) -> None:
        conn = self._connect()
        try:
            for ev in events:
                # what drove it, from the calibration status stamped on the reset
                # bucket and the hour before it
                seen = {r[0] for r in conn.execute(
                    "SELECT calibration_status FROM bucket WHERE ts_start BETWEEN ? AND ? "
                    "AND calibration_status IS NOT NULL",
                    (ev["ts"] - 3600, ev["ts"]))}
                if "grid_charging" in seen:
                    source = "grid"
                elif seen & {"due", "grid_waiting"}:
                    source = "pv_calibration"
                elif seen:
                    source = "natural"
                else:
                    source = "unknown"      # before v0.12.2, or no status logged
                conn.execute(
                    "INSERT OR IGNORE INTO calibration_event(ts,local_time,battery_id,"
                    "days_since_full,discharged_kwh,predicted_pts,actual_pts,drift_rate,source,"
                    "missing_buckets) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ev["ts"], datetime.fromtimestamp(ev["ts"]).isoformat(timespec="seconds"),
                     ev["battery_id"], ev["days_since_full"], ev["discharged_kwh"],
                     ev["predicted_pts"], ev["actual_pts"], ev["drift_rate"], source,
                     ev.get("missing_buckets", 0)),
                )
            conn.commit()
        finally:
            conn.close()

    async def _flush(self, accum: BucketAccumulator) -> None:
        row = accum.bucket_row(self._config_version)
        row["calibration_status"] = self._calibration_status
        self._calibration_status = None
        if self._advisory is not None:
            gc, reason, eta, wear = self._advisory
            row["grid_charge_wh"] = round(gc, 2)
            row["arb_reason"] = reason
            row["eta_used"] = eta
            row["wear_used"] = wear
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

    # ---- Solcast forecast snapshots (hel-132) ---------------------------
    async def _snapshot_cb(self, _now) -> None:
        await self.async_snapshot_forecast()

    async def _catchup_cb(self, _now) -> None:
        """After a start/reload: take the snapshot that came due while HA was down."""
        try:
            due = latest_due_snapshot(datetime.now(), self._snapshot_times)
            if due is None:
                return
            last = await self.hass.async_add_executor_job(self._last_snapshot_ts)
            if last is None or last < int(due.timestamp()):
                _LOGGER.info("Solcast forecast snapshot due %s was missed; taking it now", due)
                await self.async_snapshot_forecast()
        except Exception as err:  # noqa: BLE001 - observability must never break HA
            _LOGGER.warning("Solcast forecast snapshot catch-up failed: %s", err)

    async def async_snapshot_forecast(self) -> int:
        """Copy Solcast's current day-ahead + rest-of-today forecast into the snapshot table.

        Returns the number of rows written (0 when Solcast has nothing to give).
        """
        try:
            entities = solcast_forecast_entities(
                self.entry.options.get(CONF_SOLCAST_FORECAST_SENSOR) or ""
            )
            if not entities:
                return 0            # no Solcast sensor configured: nothing to record
            taken = int(time.time())
            rows: list[dict[str, Any]] = []
            for entity_id in entities:
                st = self.hass.states.get(entity_id)
                periods = st.attributes.get("detailedForecast") if st is not None and st.attributes else None
                rows.extend(forecast_snapshot_rows(periods, taken))
            if not rows:
                _LOGGER.warning(
                    "Solcast forecast snapshot: no forecast periods available from %s", entities
                )
                return 0
            await self.hass.async_add_executor_job(self._write_snapshot, rows)
            _LOGGER.info("Solcast forecast snapshot: %d periods stored", len(rows))
            return len(rows)
        except Exception as err:  # noqa: BLE001 - observability must never break HA
            _LOGGER.warning("Solcast forecast snapshot failed: %s", err)
            return 0

    def _last_snapshot_ts(self) -> int | None:
        conn = self._connect()
        try:
            return conn.execute("SELECT MAX(taken_ts) FROM pv_forecast_snapshot").fetchone()[0]
        finally:
            conn.close()

    def _write_snapshot(self, rows: list[dict[str, Any]]) -> None:
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO pv_forecast_snapshot"
                " (taken_ts, period_ts, lead_h, p50_wh, p10_wh, p90_wh, taken_local, period_local)"
                " VALUES (:taken_ts, :period_ts, :lead_h, :p50_wh, :p10_wh, :p90_wh,"
                "         :taken_local, :period_local)",
                rows,
            )
            if self._retention_days > 0:
                cutoff = int(time.time()) - self._retention_days * 86400
                conn.execute("DELETE FROM pv_forecast_snapshot WHERE period_ts < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    # ---- read-only queries (query_history service) -----------------------
    def query_range(
        self,
        start_ts: int,
        end_ts: int,
        table: str = "bucket",
        battery_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read rows from `table` with a timestamp in [start_ts, end_ts].

        Blocking (sqlite) — call via `async_query_range` from the event loop.
        `table` is checked against the fixed QUERY_TABLES allow-list before use,
        so it's safe to interpolate into the SQL string. `battery_id` further
        filters `battery_bucket` rows when given.
        """
        if table not in QUERY_TABLES:
            raise ValueError(f"unknown table {table!r}; must be one of {QUERY_TABLES}")
        ts_col = _QUERY_TS_COLUMN[table]
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            sql = f"SELECT * FROM {table} WHERE {ts_col} BETWEEN ? AND ?"
            params: list[Any] = [start_ts, end_ts]
            if table == "battery_bucket" and battery_id:
                sql += " AND battery_id = ?"
                params.append(battery_id)
            tiebreak = _QUERY_TIEBREAK.get(table)
            sql += f" ORDER BY {ts_col}" + (f", {tiebreak}" if tiebreak else "") + " LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def async_query_range(
        self,
        start_ts: int,
        end_ts: int,
        table: str = "bucket",
        battery_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Event-loop-safe wrapper around `query_range` (runs on the executor)."""
        return await self.hass.async_add_executor_job(
            lambda: self.query_range(start_ts, end_ts, table, battery_id, limit)
        )

    def _checkpoint(self) -> None:
        try:
            conn = self._connect()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("history checkpoint failed: %s", err)
