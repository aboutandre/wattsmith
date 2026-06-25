"""Switch platform — the Energy Manager enable (Zero-Grid Control) switch."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .manager import EnergyManagerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Energy Manager enable switch."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WattsmithEnableSwitch(coordinator, entry)])


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
