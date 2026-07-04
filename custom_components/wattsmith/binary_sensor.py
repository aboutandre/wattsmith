"""Binary sensors — configuration sanity checks (pure status, no I/O).

These never command anything; they just surface a misconfiguration so it isn't a
silent trap. Currently one check: the EV-reserve / battery-max-SOC conflict.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_MAX_BATTERY_SOC,
    CONF_RESERVE_SOC,
    DOMAIN,
)
from .settings import DEFAULT_MAX_BATTERY_SOC, DEFAULT_RESERVE_SOC


def is_reserve_conflict(reserve_soc: float, max_charge_soc: float) -> bool:
    """True when EV Reserve SOC exceeds the battery Max Charge SOC.

    In EV "solar" mode the cascade fills the home batteries up to Reserve SOC
    *before* the car. If Reserve > Max Charge SOC, the fleet is capped below the
    reserve and can never reach it. Since v0.7.0 the EV coordinator CLAMPS the
    effective reserve to the cap (validate_config.effective_reserve_soc), so the
    car still solar-charges — this sensor now flags that the configured value is
    misleading rather than a hard failure. Equal is fine. Pure + unit-tested.
    """
    return reserve_soc > max_charge_soc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the configuration-check binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ReserveConflictBinarySensor(coordinator, entry)])


class ReserveConflictBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Problem sensor: EV Reserve SOC is set above the battery Max Charge SOC."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:battery-alert-variant"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reserve_conflict"
        self._attr_name = "EV Reserve / Max Charge SOC Conflict"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Wattsmith",
            "model": "Energy Brain",
        }

    def _reserve(self) -> float:
        return float(self._entry.options.get(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC))

    def _max_charge(self) -> float:
        return float(self._entry.options.get(CONF_MAX_BATTERY_SOC, DEFAULT_MAX_BATTERY_SOC))

    @property
    def is_on(self) -> bool:
        return is_reserve_conflict(self._reserve(), self._max_charge())

    @property
    def extra_state_attributes(self) -> dict[str, float | str]:
        reserve, max_charge = self._reserve(), self._max_charge()
        attrs: dict[str, float | str] = {
            "ev_reserve_soc": reserve,
            "max_charge_soc": max_charge,
        }
        if is_reserve_conflict(reserve, max_charge):
            attrs["detail"] = (
                f"EV Reserve SOC ({reserve:.0f}%) is above Max Charge SOC "
                f"({max_charge:.0f}%): the effective reserve is clamped to "
                f"{max_charge:.0f}%. Lower Reserve SOC or raise Max Charge SOC "
                f"so the setting matches actual behavior."
            )
        return attrs
