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
    entities += [
        AdaptiveSensor(coordinator, entry.entry_id, entry.title, d)
        for d in ADAPTIVE_SENSORS
    ]
    ev_coord: EvCoordinator | None = hass.data[DOMAIN].get(entry.entry_id + "_ev")
    if ev_coord is not None:
        entities.extend(
            EvSensor(ev_coord, entry.entry_id, entry.title, d) for d in EV_SENSORS
        )
    arb = hass.data[DOMAIN].get(entry.entry_id + "_arb")
    if arb is not None:
        entities.extend(
            ArbitrageSensor(arb, entry.entry_id, entry.title, d) for d in ARBITRAGE_SENSORS
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
    # actual charger power + car connection state, read from the wallbox driver —
    # native replacements for an external charger integration's power / car-state
    # entities (recorder history included), so that integration can be removed
    ("ev_power_w", "EV Power", "W", "power", "mdi:flash"),
    ("car", "EV Car", None, None, "mdi:car-electric"),
)

# Arbitrage advisory + economics (read from the arbitrage coordinator).
ARBITRAGE_SENSORS: tuple[tuple[str, str, str | None, str | None, str], ...] = (
    ("reason", "Arbitrage Plan", None, None, "mdi:cash-clock"),
    ("grid_charge_now_wh", "Arbitrage Grid Charge Now", "Wh", "energy", "mdi:transmission-tower-import"),
    ("profitable_deficit_wh", "Arbitrage Profitable Deficit", "Wh", "energy", "mdi:cash-plus"),
    ("hold_floor_soc", "Arbitrage Discharge Hold SOC", "%", "battery", "mdi:battery-lock"),
    ("eta", "Round-Trip Efficiency", None, None, "mdi:sync"),
    ("wear_ct", "Battery Wear Cost", None, None, "mdi:battery-heart-variant"),
)

# Adaptive PV charging sub-keys (read from coordinator.data["adaptive"]).
ADAPTIVE_SENSORS: tuple[tuple[str, str, str | None, str | None, str], ...] = (
    ("status", "Adaptive Status", None, None, "mdi:auto-fix"),
    ("effective_max_soc", "Adaptive Effective Max SOC", "%", "battery", "mdi:battery-charging-100"),
    ("fleet_headroom_wh", "Adaptive Fleet Headroom", "Wh", "energy", "mdi:battery-plus-variant"),
    ("learned_baseline_w", "Adaptive Learned Baseline", "W", "power", "mdi:chart-bell-curve"),
    ("learned_slots_count", "Adaptive Learned Slots", None, None, "mdi:clock-check-outline"),
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
        return {
            "setpoints": data.get("setpoints"),
            "safety": data.get("safety"),
            # cross-value config sanity findings (F-17) — empty list = all clear
            "config_warnings": data.get("config_warnings", []),
        }


class AdaptiveSensor(CoordinatorEntity, SensorEntity):
    """A status sensor of adaptive PV charging (reads coordinator.data['adaptive'])."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id, title, desc) -> None:
        super().__init__(coordinator)
        key, name, unit, device_class, icon = desc
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_adaptive_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def native_value(self) -> Any:
        adaptive = (self.coordinator.data or {}).get("adaptive") or {}
        return adaptive.get(self._key)


class ArbitrageSensor(CoordinatorEntity, SensorEntity):
    """Advisory arbitrage + economics sensor (reads the arbitrage coordinator)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id, title, desc) -> None:
        super().__init__(coordinator)
        key, name, unit, device_class, icon = desc
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_arb_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def native_value(self) -> Any:
        value = (self.coordinator.data or {}).get(self._key)
        if self._key == "eta" and value is not None:
            return round(value * 100.0, 1)      # show as %
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._key != "reason":
            return None
        data = self.coordinator.data or {}
        return {
            "enabled": data.get("enabled"),
            "eta_source": data.get("eta_source"),
            "target_soc": data.get("target_soc"),
            "horizon_buckets": data.get("horizon_buckets"),
        }


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
            "manufacturer": "Wattsmith",
            "model": "EV Charge Control",
            "via_device": (DOMAIN, entry_id),
        }

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        return data.get(self._key)
