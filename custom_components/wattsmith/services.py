"""Service handlers for Wattsmith.

query_history is a read-only window onto the never-purged 15-min history DB
(history_db.py) — the whole reason that DB exists is that HA's own recorder
purges at ~10 days and isn't built for analytics. Exposing it as a service (with
SupportsResponse.ONLY) lets any HA API caller — including a plain long-lived
token over the REST API's /api/services/<domain>/<service> endpoint — pull a
long time range back as structured rows without needing filesystem/SSH access
to the HA host.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .history_db import QUERY_TABLES, HistoryRecorder

_LOGGER = logging.getLogger(__name__)

SERVICE_QUERY_HISTORY = "query_history"

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

    _LOGGER.debug("Services registered for %s", DOMAIN)
