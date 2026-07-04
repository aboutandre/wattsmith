"""Switch platform — the Energy Manager enable (Zero-Grid Control) switch."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADAPTIVE_ENABLED, CONF_ARBITRAGE_ENABLED, DOMAIN
from .manager import EnergyManagerCoordinator

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
