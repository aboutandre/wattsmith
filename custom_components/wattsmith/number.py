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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ADAPTIVE_BASELINE_W,
    CONF_ADAPTIVE_CEILING_SOC,
    CONF_ADAPTIVE_FORECAST_DERATE,
    CONF_BRIDGE_FLOOR_SOC,
    CONF_BRIDGE_GRACE_S,
    CONF_CALIBRATION_MAX_DAYS,
    CONF_CALIBRATION_THRESHOLD_PTS,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_DEADBAND_W,
    CONF_DIRECTION_HYSTERESIS_W,
    CONF_ETA_OVERRIDE,
    CONF_FORECAST_MARGIN_PCT,
    CONF_KD,
    CONF_KP,
    CONF_MAX_BATTERY_SOC,
    CONF_MAX_STEP_W,
    CONF_MIN_SOC,
    CONF_PHASE_DOWN_W,
    CONF_PHASE_UP_W,
    CONF_RESERVE_SOC,
    CONF_TARGET_GRID_W,
    DOMAIN,
)
from .settings import (
    DEFAULT_BRIDGE_FLOOR_SOC,
    DEFAULT_BRIDGE_GRACE_S,
    DEFAULT_CALIBRATION_MAX_DAYS,
    DEFAULT_CALIBRATION_THRESHOLD_PTS,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_FORECAST_MARGIN_PCT,
    DEFAULT_MAX_BATTERY_SOC,
    DEFAULT_PHASE_DOWN_W,
    DEFAULT_PHASE_UP_W,
    DEFAULT_RESERVE_SOC,
)
from .arbitrage_coordinator import ArbitrageCoordinator
from .economics import parse_eta_override
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
    purpose: str | None = None    # plain-language intent, shown as the `purpose` attribute


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
    # Read by the ARBITRAGE coordinator (it re-reads options every tick), but it
    # belongs on the manager device next to the other tuning knobs.
    ManagerNumber(CONF_FORECAST_MARGIN_PCT, "Arbitrage Forecast Margin", 0, 100, 5, "%",
                  "mdi:cloud-percent",
                  lambda c: float(c.entry.options.get(CONF_FORECAST_MARGIN_PCT,
                                                      DEFAULT_FORECAST_MARGIN_PCT))),
    # SOC calibration (hel-134), read live by the arbitrage coordinator: a battery is
    # due when its predicted SOC under-reading reaches the threshold, or after max days
    ManagerNumber(CONF_CALIBRATION_THRESHOLD_PTS, "Calibration Drift Threshold", 2, 30, 1, "%",
                  "mdi:battery-sync-outline",
                  lambda c: float(c.entry.options.get(CONF_CALIBRATION_THRESHOLD_PTS,
                                                      DEFAULT_CALIBRATION_THRESHOLD_PTS)),
                  "A battery gets a calibration full charge once its SOC reading is predicted "
                  "to under-report by this many points (the BMS drifts ~1.3 points per kWh "
                  "discharged and resets at full). Lower = more frequent full charges."),
    ManagerNumber(CONF_CALIBRATION_MAX_DAYS, "Calibration Max Interval", 1, 30, 1, "d",
                  "mdi:calendar-sync",
                  lambda c: float(c.entry.options.get(CONF_CALIBRATION_MAX_DAYS,
                                                      DEFAULT_CALIBRATION_MAX_DAYS)),
                  "Safety net: a battery gets a calibration full charge after this many days "
                  "without reaching 100%, whatever its predicted drift."),
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
    arb: ArbitrageCoordinator | None = hass.data[DOMAIN].get(entry.entry_id + "_arb")
    if arb is not None:
        entities.append(EtaOverrideNumber(arb, entry))
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

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        return {"purpose": self._desc.purpose} if self._desc.purpose else None

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
            "manufacturer": "Wattsmith",
            "model": "EV Charge Control",
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


class EtaOverrideNumber(CoordinatorEntity, NumberEntity):
    """Round-trip efficiency override — 0 = auto (measured from the history DB).

    Writes the same `eta_override` option as the Economics options step (stored as
    a fraction), so the two UIs can never disagree.
    """

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_name = "Round-Trip Efficiency Override"
    _attr_icon = "mdi:sync-alert"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: ArbitrageCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{CONF_ETA_OVERRIDE}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"purpose": (
            "Round-trip efficiency the arbitrage planner uses to decide whether buying now "
            "for later pays off. 0 = auto: measured from the history DB between full-charge "
            "resets (the only windows where the drifting SOC reading is exact). Set 50-100 "
            "only to override the measurement.")}

    @property
    def native_value(self) -> float:
        v = parse_eta_override(self._entry.options.get(CONF_ETA_OVERRIDE))
        return round(v * 100.0, 1) if v is not None else 0.0

    async def async_set_native_value(self, value: float) -> None:
        options = dict(self._entry.options)
        if value <= 0:
            options.pop(CONF_ETA_OVERRIDE, None)          # back to auto
        elif value < 50:
            raise HomeAssistantError(
                "Round-trip efficiency override must be 0 (auto) or 50-100 %")
        else:
            options[CONF_ETA_OVERRIDE] = round(value / 100.0, 4)
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        await self.coordinator.async_request_refresh()
