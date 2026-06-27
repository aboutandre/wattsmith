"""Wattsmith — the standalone home-energy orchestration brain.

Sets up the zero-grid Energy Manager + the EV coordinator and their entities.
Wattsmith drives the Marstek battery fleet through the base integration's HA
services (see battery_bridge.py) — it has no code dependency on the base.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .ev_coordinator import EvCoordinator
from .manager import EnergyManagerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Wattsmith integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wattsmith (Energy Manager + EV coordinator) from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = EnergyManagerCoordinator(hass, entry)
    # The manager tick never raises, so first refresh always succeeds; it simply
    # starts in SAFE/hold until the grid sensor is fresh and batteries are known.
    await coordinator.async_config_entry_first_refresh()

    ev_coordinator = EvCoordinator(hass, entry)
    await ev_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    hass.data[DOMAIN][entry.entry_id + "_ev"] = ev_coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _LOGGER.debug("Wattsmith entry %s set up", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if isinstance(coordinator, EnergyManagerCoordinator):
            await coordinator._release_batteries()
            await coordinator.async_shutdown()
        ev_coord = hass.data[DOMAIN].pop(entry.entry_id + "_ev", None)
        if ev_coord is not None:
            await ev_coord.async_shutdown()
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options live (no full reload)."""
    domain_data = hass.data.get(DOMAIN, {})
    coordinator = domain_data.get(entry.entry_id)
    if isinstance(coordinator, EnergyManagerCoordinator):
        await coordinator.async_apply_options()
    ev_coord = domain_data.get(entry.entry_id + "_ev")
    if isinstance(ev_coord, EvCoordinator):
        await ev_coord.async_apply_options()
