"""Switch platform — the Energy Manager enable (Zero-Grid Control) switch."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ADAPTIVE_ENABLED,
    CONF_ARBITRAGE_ENABLED,
    CONF_CALIBRATION_ENABLED,
    CONF_CALIBRATION_GRID,
    CONF_PULSE_HOLD_ENABLED,
    DOMAIN,
)
from .manager import EnergyManagerCoordinator
from .settings import (
    DEFAULT_CALIBRATION_ENABLED,
    DEFAULT_CALIBRATION_GRID,
    DEFAULT_PULSE_HOLD_ENABLED,
)

# Plain-language intent, published as each switch's `purpose` attribute so the
# more-info dialog explains what the toggle does long after it was set up.
PURPOSE_PULSE_HOLD = (
    "Stops the import/export flip-flop caused by loads that switch on and off faster than "
    "the batteries can follow (induction hob, washing-machine heater, mixer, stove). The "
    "batteries need ~7 s to follow a new command, so they cannot track a 3.5 s pulse or a "
    "5-15 s burst: without this, every burst imports and the late reaction exports. While "
    "such pulsing is detected (3 up/down swings within 60 s), the command is held at the "
    "highest demand of the last 30 s. When the bursts need DISCHARGE, the battery output is "
    "held up, only while stored energy is in SURPLUS (the forecast sees no shortfall before "
    "the batteries refill), because exported battery energy only earns the feed-in price. "
    "When the batteries are CHARGING from PV, the charge rate is held down instead so solar "
    "covers the bursts, only while the remaining sun still FILLS the batteries to their "
    "ceiling (with ~2 kWh to spare), so the held-back PV would have been exported anyway. "
    "ON = allowed (still waits for pulsing + its gate); OFF = always chase the load."
)
PURPOSE_CALIBRATION = (
    "The Marstek battery's SOC reading drifts below reality by ~1.3 points per kWh it "
    "discharges and only corrects itself when the battery is truly full, so energy below the "
    "displayed floor is stranded. When a battery's predicted drift reaches the Calibration "
    "Drift Threshold (or the Max Interval passes), Wattsmith lifts the charge ceiling to 100% "
    "so PV can fill it and reset the count. OFF = never force a full charge."
)
PURPOSE_CALIBRATION_GRID = (
    "If PV has not finished a due calibration after 4 more points of drift, top the fleet up "
    "to 100% from the grid in the cheapest window of the next 24 h (needs Arbitrage Control "
    "on). OFF = calibrate from PV only, however long that takes."
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Energy Manager switches."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    switches = [
        WattsmithEnableSwitch(coordinator, entry),
        AdaptiveEnableSwitch(coordinator, entry),
    ]
    arb = hass.data[DOMAIN].get(entry.entry_id + "_arb")
    if arb is not None:
        switches.append(ArbitrageEnableSwitch(arb, entry))
        switches.append(OptionSwitch(
            arb, entry, CONF_CALIBRATION_ENABLED, DEFAULT_CALIBRATION_ENABLED,
            "SOC Calibration", "mdi:battery-sync", PURPOSE_CALIBRATION))
        switches.append(OptionSwitch(
            arb, entry, CONF_CALIBRATION_GRID, DEFAULT_CALIBRATION_GRID,
            "SOC Calibration Grid Top-Up", "mdi:transmission-tower-import",
            PURPOSE_CALIBRATION_GRID))
    # on the manager coordinator (3 s) so its live attributes follow the control loop
    switches.append(PulseHoldSwitch(coordinator, entry))
    async_add_entities(switches)


class WattsmithEnableSwitch(CoordinatorEntity, SwitchEntity):
    """Enable/disable the zero-grid Energy Manager control loop."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator: EnergyManagerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_name = "Zero-Grid Control"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.enabled)

    async def _set_enabled(self, value: bool) -> None:
        self.coordinator.enabled = value
        new_options = {**self.coordinator.entry.options, "enabled": value}
        self.hass.config_entries.async_update_entry(
            self.coordinator.entry, options=new_options
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)


class AdaptiveEnableSwitch(CoordinatorEntity, SwitchEntity):
    """Enable/disable adaptive PV charging (push past the cap toward the ceiling)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-sunny"

    def __init__(self, coordinator: EnergyManagerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_adaptive_pv_charging"
        self._attr_name = "Adaptive Charging"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.adaptive_enabled)

    async def _set(self, value: bool) -> None:
        self.coordinator.adaptive_enabled = value
        new_options = {**self.coordinator.entry.options, CONF_ADAPTIVE_ENABLED: value}
        self.hass.config_entries.async_update_entry(
            self.coordinator.entry, options=new_options
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class ArbitrageEnableSwitch(CoordinatorEntity, SwitchEntity):
    """Enable tariff-arbitrage ACTUATION (discharge-hold + grid-charge).

    Default OFF: with the switch off the arbitrage brain still computes and logs
    its plan (advisory — visible on the Arbitrage sensors and in the history DB),
    but never changes dispatch. Turn on only after validating the advisory plan
    against the logged data.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:cash-sync"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_arbitrage_enabled"
        self._attr_name = "Arbitrage Control"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(CONF_ARBITRAGE_ENABLED, False))

    async def _set(self, value: bool) -> None:
        new_options = {**self._entry.options, CONF_ARBITRAGE_ENABLED: value}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class OptionSwitch(CoordinatorEntity, SwitchEntity):
    """A boolean option on the manager device, read live by the arbitrage coordinator.

    SOC Calibration (hel-134): a full charge whenever a battery's predicted SOC
    drift gets too large, so the BMS resets its count. Grid Top-Up lets it finish
    from the cheapest grid window when PV has not managed it (needs Arbitrage Control).
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str, default: bool,
                 name: str, icon: str, purpose: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._default = default
        self._purpose = purpose
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(self._key, self._default))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"purpose": self._purpose}

    async def _set(self, value: bool) -> None:
        new_options = {**self._entry.options, self._key: value}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class PulseHoldSwitch(OptionSwitch):
    """Pulse hold (hel-136) — see PURPOSE_PULSE_HOLD; attributes show it working live."""

    def __init__(self, coordinator: EnergyManagerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, CONF_PULSE_HOLD_ENABLED, DEFAULT_PULSE_HOLD_ENABLED,
                         "Pulse Hold", "mdi:stove", PURPOSE_PULSE_HOLD)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        live = (self.coordinator.data or {}).get("pulse_hold") or {}
        return {
            "purpose": self._purpose,
            # is the stored energy in surplus right now (the gate)?
            "energy_surplus": live.get("energy_surplus"),
            # will the remaining sun fill the batteries anyway (the gate while charging)?
            "pv_fills_fleet": live.get("pv_fills_fleet"),
            # is a pulsing load being detected right now?
            "pulsing_load": live.get("pulsing_load"),
            # None, "discharge" (output held up) or "charge" (charge rate held down)
            "hold_mode": live.get("hold_mode"),
            # the held command (W): + = discharge floor, - = charge capped at this
            "holding_w": live.get("holding_w"),
        }
