"""Sensor platform — Energy Manager status sensors + EV status sensors."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .ev_coordinator import EvCoordinator
from .manager import EnergyManagerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the manager status sensors + EV status sensors."""
    coordinator: EnergyManagerCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [
        ManagerSensor(coordinator, entry.entry_id, entry.title, d)
        for d in MANAGER_SENSORS
    ]
    ev_coord: EvCoordinator | None = hass.data[DOMAIN].get(entry.entry_id + "_ev")
    if ev_coord is not None:
        entities.extend(
            EvSensor(ev_coord, entry.entry_id, entry.title, d) for d in EV_SENSORS
        )
    async_add_entities(entities)


# (key in status dict, name, unit, device_class, icon)
MANAGER_SENSORS: tuple[tuple[str, str, str | None, str | None, str], ...] = (
    ("state", "Status", None, None, "mdi:state-machine"),
    ("grid_power", "Grid Power Seen", "W", "power", "mdi:transmission-tower"),
    ("ev_power", "EV Power (excluded)", "W", "power", "mdi:ev-station"),
    ("command_total", "Total Battery Command", "W", "power", "mdi:home-battery"),
    ("target_grid_w", "Target Grid Power", "W", "power", "mdi:target"),
)

EV_SENSORS: tuple[tuple[str, str, str | None, str | None, str], ...] = (
    ("state", "EV State", None, None, "mdi:ev-station"),
    ("reason", "EV Reason", None, None, "mdi:information-outline"),
    ("amp", "EV Charge Current", "A", "current", "mdi:current-ac"),
    ("phases", "EV Phases", None, None, "mdi:numeric"),
    ("target_power_w", "EV Target Power", "W", "power", "mdi:lightning-bolt"),
)


class ManagerSensor(CoordinatorEntity, SensorEntity):
    """A status sensor of the Energy Manager (reads the coordinator status dict)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id, title, desc) -> None:
        super().__init__(coordinator)
        key, name, unit, device_class, icon = desc
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_mgr_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        return data.get(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._key != "state":
            return None
        data = self.coordinator.data or {}
        return {"setpoints": data.get("setpoints"), "safety": data.get("safety")}


class EvSensor(CoordinatorEntity, SensorEntity):
    """A status sensor of the EV Coordinator (reads the coordinator status dict)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EvCoordinator, entry_id: str, title: str, desc: tuple) -> None:
        super().__init__(coordinator)
        key, name, unit, device_class, icon = desc
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_ev_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id + "_ev")},
            "name": f"{title} EV",
            "manufacturer": "go-e",
            "model": "EV Charger",
            "via_device": (DOMAIN, entry_id),
        }

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        return data.get(self._key)
