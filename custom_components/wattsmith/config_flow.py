"""Config flow for Wattsmith."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class WattsmithConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Wattsmith config flow.

    Phase 1: a single-instance entry with no options yet. Phase 2 (hel-109)
    adds the site configuration here and in an options flow — grid/EV/forecast
    sensors, battery selection, and the control thresholds that currently live
    in the Marstek Energy Manager.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Wattsmith", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
