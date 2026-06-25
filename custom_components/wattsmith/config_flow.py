"""Config flow for Wattsmith."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CAR_STATE_SENSOR,
    CONF_EV_SENSOR,
    CONF_GOE_IP,
    CONF_GRID_SENSOR,
    CONF_SOLCAST_REMAINING_SENSOR,
    CONF_TIBBER_SENSOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class WattsmithConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Wattsmith — a single energy-brain instance.

    The brain orchestrates the Marstek battery fleet (discovered automatically
    via the base integration) plus the go-e EV charger. Setup only needs the
    grid power sensor; everything else is set afterwards in the options flow and
    the number/select entities.
    """

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "WattsmithOptionsFlow":
        return WattsmithOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Initial step — pick the grid power sensor."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="Wattsmith",
                data={CONF_GRID_SENSOR: user_input[CONF_GRID_SENSOR]},
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_GRID_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={
                "info": "Select your grid power sensor (positive = importing). "
                "Batteries are discovered automatically from the Marstek integration. "
                "EV charger, Tibber and tuning are configured afterwards."
            },
        )


class WattsmithOptionsFlow(config_entries.OptionsFlow):
    """Options — repoint sensors and wire the go-e EV charger."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure sensors + go-e EV charger."""
        if user_input is not None:
            new_options = {**self._config_entry.options}
            grid = user_input.get(CONF_GRID_SENSOR)
            if grid:
                new_options[CONF_GRID_SENSOR] = grid
            for key in (CONF_EV_SENSOR, CONF_TIBBER_SENSOR, CONF_CAR_STATE_SENSOR,
                        CONF_SOLCAST_REMAINING_SENSOR):
                value = user_input.get(key)
                if value:
                    new_options[key] = value
                else:
                    new_options.pop(key, None)
            goe = (user_input.get(CONF_GOE_IP) or "").strip()
            if goe:
                new_options[CONF_GOE_IP] = goe
            else:
                new_options.pop(CONF_GOE_IP, None)
            return self.async_create_entry(title="", data=new_options)

        opts = self._config_entry.options
        data = self._config_entry.data
        current_grid = opts.get(CONF_GRID_SENSOR, data.get(CONF_GRID_SENSOR, ""))
        current_ev = opts.get(CONF_EV_SENSOR, "")
        current_goe = opts.get(CONF_GOE_IP, "")
        current_tibber = opts.get(CONF_TIBBER_SENSOR, "")
        current_car = opts.get(CONF_CAR_STATE_SENSOR, "")
        current_solcast = opts.get(CONF_SOLCAST_REMAINING_SENSOR, "")

        def _suggest(value: str) -> dict[str, str]:
            return {"suggested_value": value} if value else {}

        schema = vol.Schema(
            {
                vol.Optional(CONF_GRID_SENSOR, description=_suggest(current_grid)):
                    selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class="power")
                    ),
                vol.Optional(CONF_EV_SENSOR, description=_suggest(current_ev)):
                    selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class="power")
                    ),
                vol.Optional(CONF_GOE_IP, description=_suggest(current_goe)):
                    selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                vol.Optional(CONF_TIBBER_SENSOR, description=_suggest(current_tibber)):
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_CAR_STATE_SENSOR, description=_suggest(current_car)):
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_SOLCAST_REMAINING_SENSOR, description=_suggest(current_solcast)):
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "info": "Grid sensor: positive = importing. "
                "EV power sensor: subtracted from grid (batteries cover house, grid covers car), "
                "except during a battery bridge (brief PV dip) when the batteries carry the car too. "
                "go-e IP: leave blank to disable EV control. "
                "Tibber + car-state sensors: needed for solar/cheap EV charging. "
                "Solcast remaining-today sensor: enables adaptive PV charging."
            },
        )
