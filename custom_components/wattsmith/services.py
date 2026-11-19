"""Service handlers for Wattsmith.

query_history is a read-only window onto the never-purged 15-min history DB
(history_db.py) — the whole reason that DB exists is that HA's own recorder
purges at ~10 days and isn't built for analytics. Exposing it as a service (with
SupportsResponse.ONLY) lets any HA API caller — including a plain long-lived
token over the REST API's /api/services/<domain>/<service> endpoint — pull a
long time range back as structured rows without needing filesystem/SSH access
to the HA host.

backfill_ev_energy writes the EV Energy sensor's history before it existed into
HA's long-term statistics (see ev_backfill.py for the maths). Dry run by default.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .ev_backfill import StatRow, plan_backfill
from .history_db import QUERY_TABLES, HistoryRecorder

_LOGGER = logging.getLogger(__name__)

SERVICE_QUERY_HISTORY = "query_history"
SERVICE_BACKFILL_EV_ENERGY = "backfill_ev_energy"

SERVICE_BACKFILL_EV_ENERGY_SCHEMA = vol.Schema(
    {
        vol.Optional("meter_statistic_id"): cv.string,
        vol.Optional("dry_run", default=True): cv.boolean,
    }
)

# unique_id suffix of the EV Energy sensor (sensor.py: f"{entry_id}_ev_{key}")
_EV_ENERGY_UID = "_ev_ev_energy_total_kwh"

SERVICE_QUERY_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Optional("table", default="bucket"): vol.In(QUERY_TABLES),
        vol.Optional("battery_id"): cv.string,
        vol.Optional("limit", default=1000): vol.All(vol.Coerce(int), vol.Range(min=1, max=5000)),
    }
)


def _history_recorders(hass: HomeAssistant) -> dict[str, HistoryRecorder]:
    """All live HistoryRecorders, keyed by config entry_id."""
    return {
        key[: -len("_history")]: value
        for key, value in hass.data.get(DOMAIN, {}).items()
        if key.endswith("_history") and isinstance(value, HistoryRecorder)
    }


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Wattsmith's services (currently just query_history)."""

    async def query_history_handler(call: ServiceCall) -> dict[str, Any]:
        recorders = _history_recorders(hass)
        if not recorders:
            raise HomeAssistantError(
                "Wattsmith history DB is not running (history_enabled is off, or "
                "the integration hasn't finished starting up yet)"
            )
        # Single-hub integration in practice — take the first (only) recorder.
        recorder = next(iter(recorders.values()))
        start_ts = int(call.data["start"].timestamp())
        end_ts = int(call.data["end"].timestamp())
        rows = await recorder.async_query_range(
            start_ts,
            end_ts,
            table=call.data.get("table", "bucket"),
            battery_id=call.data.get("battery_id"),
            limit=call.data.get("limit", 1000),
        )
        return {"count": len(rows), "rows": rows}

    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY_HISTORY,
        query_history_handler,
        schema=SERVICE_QUERY_HISTORY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    async def backfill_ev_energy_handler(call: ServiceCall) -> dict[str, Any]:
        recorders = _history_recorders(hass)
        if not recorders:
            raise HomeAssistantError("Wattsmith history DB is not running")
        entry_id, history = next(iter(recorders.items()))
        entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, entry_id + _EV_ENERGY_UID
        )
        if entity_id is None:
            raise HomeAssistantError(
                "EV Energy sensor not found (needs a configured wallbox and Wattsmith v0.18+)"
            )
        result = await _async_backfill_ev_energy(
            hass, history, entity_id,
            call.data.get("meter_statistic_id"),
            call.data.get("dry_run", True),
        )
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_EV_ENERGY,
        backfill_ev_energy_handler,
        schema=SERVICE_BACKFILL_EV_ENERGY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.debug("Services registered for %s", DOMAIN)


# ---- backfill_ev_energy -----------------------------------------------------

def _ts(value: Any) -> int:
    """Statistics rows carry `start` as epoch float (or datetime on old cores)."""
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(float(value))


async def _async_read_stats(hass: HomeAssistant, statistic_id: str) -> list[StatRow]:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        None,
        {statistic_id},
        "hour",
        {"energy": "kWh"},
        {"state", "sum"},
    )
    rows = []
    for r in result.get(statistic_id, []):
        if r.get("state") is None:
            continue
        rows.append(StatRow(_ts(r["start"]), float(r["state"]), float(r.get("sum") or 0.0)))
    return rows


def _import(hass: HomeAssistant, entity_id: str, rows: list[StatRow]) -> None:
    from homeassistant.components.recorder.statistics import async_import_statistics

    metadata: dict[str, Any] = {
        "has_mean": False,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": entity_id,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
    }
    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        metadata["mean_type"] = StatisticMeanType.NONE
    except ImportError:  # pragma: no cover - older cores
        pass
    stats = [
        {
            "start": datetime.fromtimestamp(r.start, timezone.utc),
            "state": r.state,
            "sum": r.sum,
        }
        for r in rows
    ]
    async_import_statistics(hass, metadata, stats)  # type: ignore[arg-type]


async def _async_backfill_ev_energy(
    hass: HomeAssistant,
    history: HistoryRecorder,
    entity_id: str,
    meter_statistic_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    from homeassistant.components.recorder import get_instance

    existing = await _async_read_stats(hass, entity_id)
    if not existing:
        raise HomeAssistantError(
            f"{entity_id} has no long-term statistics yet; run this again once "
            "it has recorded for a full hour"
        )
    meter = await _async_read_stats(hass, meter_statistic_id) if meter_statistic_id else []
    first_start = min(r.start for r in existing)
    ws_hours = await history.async_ev_hourly_wh(0, first_start)
    try:
        plan = plan_backfill(existing, meter, ws_hours)
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err

    summary = plan.summary(dt_util.get_default_time_zone())
    summary.update({"statistic_id": entity_id, "dry_run": dry_run})
    if dry_run or (not plan.rows and not plan.shifted):
        return summary

    _import(hass, entity_id, plan.rows + plan.shifted)
    await get_instance(hass).async_block_till_done()

    # An hour compiled between the read and the import still carries the old
    # sum; shift those too so the counter has no seam.
    if plan.shifted:
        last_seen = max(r.start for r in existing)
        b = plan.sensor_start_reading or 0.0
        late = [
            StatRow(r.start, r.state, round(r.sum + plan.shift, 4))
            for r in await _async_read_stats(hass, entity_id)
            if r.start > last_seen and abs((r.state - r.sum) - b) < 0.01
        ]
        if late:
            _import(hass, entity_id, late)
            await get_instance(hass).async_block_till_done()
            summary["late_rows_shifted"] = len(late)
    _LOGGER.info(
        "EV energy backfill: %d history rows (%.1f kWh), %d rows shifted by %.3f kWh, scale %s",
        len(plan.rows), plan.backfilled_kwh, len(plan.shifted), plan.shift, plan.scale,
    )
    return summary
