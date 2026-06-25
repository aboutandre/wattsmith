"""Battery bridge — Wattsmith's decoupled view of the Marstek battery fleet.

Wattsmith does not import the base integration. This module is the *only* place
that knows how the base is shaped. It:

  - discovers the base's battery DEVICES from the entity registry,
  - reads each battery's SOC / power / capacity from the base's SENSOR ENTITIES
    (resolved by their deterministic unique_id: f"{BASE_DOMAIN}_{entry_id}_{sensor_id}"),
  - dispatches setpoints / mode through the base's HA SERVICES (set_passive_mode /
    set_mode, targeted by device_id — see the base's Phase-0 change), reading back
    the per-battery ack from the service response (Phase-0.5 change).

A "battery" is any base-domain config entry that exposes the SOC sensor; this
cleanly excludes the base's own manager/EV devices during cut-over coexistence.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    BASE_BATTERY_MARKER_SENSOR,
    BASE_DOMAIN,
    BASE_SENSOR_CAPACITY,
    BASE_SENSOR_POWER,
    BASE_SENSOR_SOC,
    BASE_SVC_SET_MODE,
    BASE_SVC_SET_PASSIVE_MODE,
)

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = ("unknown", "unavailable", "none", "")


@dataclass(frozen=True)
class BatteryHandle:
    """Resolved references for one base battery."""

    battery_id: str          # base config entry_id — stable planner key
    device_id: str           # for service targeting
    soc_entity: str | None
    power_entity: str | None
    capacity_entity: str | None


@dataclass
class BatteryState:
    """A point-in-time read of one battery."""

    battery_id: str
    device_id: str
    soc: float | None        # %
    power: int               # ongrid_power W, + = discharging, - = charging
    capacity: float | None   # Wh
    available: bool          # SOC present & device responding


def _uid(entry_id: str, sensor_id: str) -> str:
    return f"{BASE_DOMAIN}_{entry_id}_{sensor_id}"


def discover_batteries(hass: HomeAssistant) -> list[BatteryHandle]:
    """Find the base's battery devices via the entity registry."""
    ent_reg = er.async_get(hass)

    # Group base-domain entities by config entry, indexed by unique_id.
    by_entry: dict[str, dict[str, str]] = {}  # entry_id -> {unique_id: entity_id}
    devices: dict[str, str] = {}              # entry_id -> device_id (from marker sensor)
    for ent in ent_reg.entities.values():
        if ent.platform != BASE_DOMAIN or not ent.config_entry_id:
            continue
        by_entry.setdefault(ent.config_entry_id, {})[ent.unique_id] = ent.entity_id

    handles: list[BatteryHandle] = []
    for entry_id, uid_map in by_entry.items():
        soc_uid = _uid(entry_id, BASE_BATTERY_MARKER_SENSOR)
        if soc_uid not in uid_map:
            continue  # not a battery (e.g. the base's manager/EV entry)
        soc_entity = uid_map[soc_uid]
        device_id = _device_id_for(ent_reg, soc_entity)
        if device_id is None:
            _LOGGER.debug("Battery entry %s SOC sensor has no device; skipping", entry_id)
            continue
        handles.append(
            BatteryHandle(
                battery_id=entry_id,
                device_id=device_id,
                soc_entity=soc_entity,
                power_entity=uid_map.get(_uid(entry_id, BASE_SENSOR_POWER)),
                capacity_entity=uid_map.get(_uid(entry_id, BASE_SENSOR_CAPACITY)),
            )
        )
    return handles


def _device_id_for(ent_reg, entity_id: str) -> str | None:
    ent = ent_reg.async_get(entity_id)
    return ent.device_id if ent else None


class BatteryBridge:
    """Reads/writes the base battery fleet through HA entities + services."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    # ---- reads ----------------------------------------------------------
    def read_all(self) -> list[BatteryState]:
        states: list[BatteryState] = []
        for h in discover_batteries(self._hass):
            soc = self._read_float(h.soc_entity)
            power = self._read_float(h.power_entity)
            cap = self._read_float(h.capacity_entity)
            states.append(
                BatteryState(
                    battery_id=h.battery_id,
                    device_id=h.device_id,
                    soc=soc,
                    power=int(power) if power is not None else 0,
                    capacity=cap,
                    # SOC present == device is answering; the base drops a sensor
                    # to unavailable when its coordinator stops updating.
                    available=soc is not None,
                )
            )
        return states

    def device_ids(self) -> list[str]:
        """All battery device_ids (for release/idle without a full read)."""
        return [h.device_id for h in discover_batteries(self._hass)]

    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state.lower() in _UNAVAILABLE:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    # ---- writes ---------------------------------------------------------
    async def set_passive(
        self, setpoints: dict[str, int], device_by_id: dict[str, str], cd_time: int
    ) -> dict[str, bool]:
        """Send a per-battery passive setpoint; return {battery_id: acked}.

        One service call per battery (each carries its own power + device
        target), gathered concurrently. The base returns a per-target ack which
        we map back to the battery_id so a single UDP drop is visible.
        """
        ids = [b for b in setpoints if b in device_by_id]

        async def _one(bid: str) -> tuple[str, bool]:
            try:
                resp = await self._hass.services.async_call(
                    BASE_DOMAIN,
                    BASE_SVC_SET_PASSIVE_MODE,
                    {"power": int(setpoints[bid]), "cd_time": int(cd_time)},
                    target={"device_id": device_by_id[bid]},
                    blocking=True,
                    return_response=True,
                )
                return bid, _resp_ok(resp)
            except Exception as err:  # noqa: BLE001 - isolate one battery's failure
                _LOGGER.debug("set_passive failed for %s: %s", bid, err)
                return bid, False

        pairs = await asyncio.gather(*(_one(b) for b in ids))
        return dict(pairs)

    async def release_all(self, device_ids: list[str]) -> None:
        """Hand the batteries back to Auto (safe idle)."""
        async def _one(device_id: str) -> None:
            try:
                await self._hass.services.async_call(
                    BASE_DOMAIN,
                    BASE_SVC_SET_MODE,
                    {"mode": "Auto"},
                    target={"device_id": device_id},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("release (set_mode Auto) failed for %s: %s", device_id, err)

        await asyncio.gather(*(_one(d) for d in device_ids), return_exceptions=True)


def _resp_ok(resp) -> bool:
    """True if the targeted battery acked in the service response."""
    results = (resp or {}).get("results", {})
    if not results:
        return False
    # We target exactly one device, so the response holds one entry.
    return any(bool(v.get("ok")) for v in results.values())
