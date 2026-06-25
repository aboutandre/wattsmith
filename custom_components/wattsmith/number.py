"""Number platform — Energy Manager tuning controls.

Only Energy Manager entries forward the NUMBER platform, so these entities are
manager-only. Each writes to the config entry's options (persisted) and the manager
applies the change live via its options-update listener.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ADAPTIVE_BASELINE_W,
    CONF_ADAPTIVE_CEILING_SOC,
    CONF_ADAPTIVE_FORECAST_DERATE,
    CONF_BRIDGE_FLOOR_SOC,
    CONF_BRIDGE_GRACE_S,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_DEADBAND_W,
    CONF_DIRECTION_HYSTERESIS_W,
    CONF_KD,
    CONF_KP,
    CONF_MAX_BATTERY_SOC,
    CONF_MAX_STEP_W,
    CONF_MIN_SOC,
    CONF_PHASE_DOWN_W,
    CONF_PHASE_UP_W,
    CONF_RESERVE_SOC,
    CONF_TARGET_GRID_W,
    DEFAULT_BRIDGE_FLOOR_SOC,
    DEFAULT_BRIDGE_GRACE_S,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_MAX_BATTERY_SOC,
    DEFAULT_PHASE_DOWN_W,
    DEFAULT_PHASE_UP_W,
    DEFAULT_RESERVE_SOC,
    DOMAIN,
)
from .ev_coordinator import EvCoordinator
from .manager import EnergyManagerCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagerNumber:
    key: str
    name: str
    min: float
    max: float
    step: float
    unit: str | None
    icon: str
    getter: Callable[[EnergyManagerCoordinator], float]


@dataclass(frozen=True)
class EvNumberDesc:
    """Descriptor for an EV coordinator tuning number."""
    key: str
    name: str
    min: float
    max: float
    step: float
    unit: str | None
    icon: str
    default: float


EV_NUMBERS: tuple[EvNumberDesc, ...] = (
    EvNumberDesc(CONF_RESERVE_SOC, "EV Reserve SOC", 5, 100, 1, "%",
                 "mdi:battery-charging-80", DEFAULT_RESERVE_SOC),
    EvNumberDesc(CONF_CHEAP_PRICE_THRESHOLD, "EV Cheap Price", 0.0, 0.5, 0.01,
                 "EUR/kWh", "mdi:cash-clock", DEFAULT_CHEAP_PRICE_THRESHOLD),
    EvNumberDesc(CONF_PHASE_UP_W, "EV Phase Up Threshold", 1000, 8000, 100, "W",
                 "mdi:lightning-bolt-circle", DEFAULT_PHASE_UP_W),
    EvNumberDesc(CONF_PHASE_DOWN_W, "EV Phase Down Threshold", 1000, 8000, 100, "W",
                 "mdi:lightning-bolt-outline", DEFAULT_PHASE_DOWN_W),
    EvNumberDesc(CONF_BRIDGE_GRACE_S, "EV Bridge Grace", 0, 1800, 10, "s",
                 "mdi:timer-sand", DEFAULT_BRIDGE_GRACE_S),
    EvNumberDesc(CONF_BRIDGE_FLOOR_SOC, "EV Bridge Floor SOC", 5, 100, 1, "%",
                 "mdi:battery-arrow-down", DEFAULT_BRIDGE_FLOOR_SOC),
)


NUMBERS: tuple[ManagerNumber, ...] = (
    ManagerNumber(CONF_TARGET_GRID_W, "Target Grid Power", -2000, 2000, 10, "W",
                  "mdi:transmission-tower", lambda c: c.controller.config.target_grid_w),
    ManagerNumber(CONF_KP, "Proportional Gain (Kp)", 0.0, 3.0, 0.05, None,
                  "mdi:tune", lambda c: c.controller.config.kp),
    ManagerNumber(CONF_KD, "Derivative Gain (Kd)", 0.0, 2.0, 0.05, None,
                  "mdi:tune-variant", lambda c: c.controller.config.kd),
    ManagerNumber(CONF_DEADBAND_W, "Deadband", 0, 200, 5, "W",
                  "mdi:arrow-expand-horizontal", lambda c: c.controller.config.deadband_w),
    ManagerNumber(CONF_MAX_STEP_W, "Max Power Change", 100, 2500, 50, "W",
                  "mdi:speedometer", lambda c: c.controller.config.max_step_w),
    ManagerNumber(CONF_DIRECTION_HYSTERESIS_W, "Direction Hysteresis", 0, 300, 10, "W",
                  "mdi:swap-horizontal", lambda c: c.controller.config.direction_hysteresis_w),
    ManagerNumber(CONF_MIN_SOC, "Minimum SOC", 5, 50, 1, "%",
                  "mdi:battery-low", lambda c: c.min_soc),
    ManagerNumber(CONF_MAX_BATTERY_SOC, "Maximum Charge SOC", 50, 100, 1, "%",
                  "mdi:battery-high", lambda c: c.max_battery_soc),
    ManagerNumber(CONF_ADAPTIVE_CEILING_SOC, "Adaptive Ceiling SOC", 50, 100, 1, "%",
                  "mdi:battery-charging-100", lambda c: c.adaptive_ceiling_soc),
    ManagerNumber(CONF_ADAPTIVE_BASELINE_W, "Adaptive Baseline Load", 0, 3000, 50, "W",
                  "mdi:home-lightning-bolt", lambda c: c.adaptive_baseline_w),
    ManagerNumber(CONF_ADAPTIVE_FORECAST_DERATE, "Adaptive Forecast Derate", 0.5, 1.0, 0.05, None,
                  "mdi:cloud-percent", lambda c: c.adaptive_forecast_derate),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Energy Manager and EV number entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if not isinstance(coordinator, EnergyManagerCoordinator):
        return
    entities: list = [MarstekManagerNumber(coordinator, entry, d) for d in NUMBERS]
    ev_coord: EvCoordinator | None = hass.data[DOMAIN].get(entry.entry_id + "_ev")
    if ev_coord is not None:
        entities.extend(EvNumberEntity(ev_coord, entry, d) for d in EV_NUMBERS)
    async_add_entities(entities)


class MarstekManagerNumber(CoordinatorEntity, NumberEntity):
    """A tunable parameter of the Energy Manager."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: EnergyManagerCoordinator,
        entry: ConfigEntry,
        desc: ManagerNumber,
    ) -> None:
        super().__init__(coordinator)
        self._desc = desc
        self._attr_unique_id = f"{entry.entry_id}_{desc.key}"
        self._attr_name = desc.name
        self._attr_native_min_value = desc.min
        self._attr_native_max_value = desc.max
        self._attr_native_step = desc.step
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_icon = desc.icon
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def native_value(self) -> float:
        return float(self._desc.getter(self.coordinator))

    async def async_set_native_value(self, value: float) -> None:
        # Persist to options; the manager's options-update listener applies it live.
        new_options = {**self.coordinator.entry.options, self._desc.key: value}
        self.hass.config_entries.async_update_entry(
            self.coordinator.entry, options=new_options
        )
        await self.coordinator.async_request_refresh()


class EvNumberEntity(CoordinatorEntity, NumberEntity):
    """A tunable EV charging parameter."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: EvCoordinator,
        entry: ConfigEntry,
        desc: EvNumberDesc,
    ) -> None:
        super().__init__(coordinator)
        self._desc = desc
        self._attr_unique_id = f"{entry.entry_id}_ev_{desc.key}"
        self._attr_name = desc.name
        self._attr_native_min_value = desc.min
        self._attr_native_max_value = desc.max
        self._attr_native_step = desc.step
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_icon = desc.icon
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id + "_ev")},
            "name": f"{entry.title} EV",
            "manufacturer": "go-e",
            "model": "EV Charger",
            "via_device": (DOMAIN, entry.entry_id),
        }

    @property
    def native_value(self) -> float:
        return float(self.coordinator.entry.options.get(
            self._desc.key,
            self.coordinator.entry.data.get(self._desc.key, self._desc.default),
        ))

    async def async_set_native_value(self, value: float) -> None:
        new_options = {**self.coordinator.entry.options, self._desc.key: value}
        self.hass.config_entries.async_update_entry(
            self.coordinator.entry, options=new_options
        )
        await self.coordinator.async_request_refresh()
