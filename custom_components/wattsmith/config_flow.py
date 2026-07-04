"""Config flow for Wattsmith."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .battery_bridge import discover_batteries
from .const import (
    CONF_ARBITRAGE_ENABLED,
    CONF_BATTERY_CONFIG,
    CONF_CAR_STATE_SENSOR,
    CONF_ETA_OVERRIDE,
    CONF_EV_SENSOR,
    CONF_EXPORT_PRICE,
    CONF_GOE_IP,
    CONF_GRID_SENSOR,
    CONF_HISTORY_DB_PATH,
    CONF_HISTORY_ENABLED,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_HOUSE_CONSUMPTION_SENSOR,
    CONF_IMPORT_POWER_CAP_W,
    CONF_MIN_ARBITRAGE_MARGIN_CT,
    CONF_PV_SENSOR,
    CONF_SOLCAST_FORECAST_SENSOR,
    CONF_SOLCAST_REMAINING_SENSOR,
    CONF_TIBBER_SENSOR,
    CONF_WEAR_COST_CT,
    CONF_WEATHER_SENSOR,
    DOMAIN,
)
from .settings import (
    DEFAULT_EXPECTED_CYCLES,
    DEFAULT_EXPORT_PRICE,
    DEFAULT_MIN_ARBITRAGE_MARGIN_CT,
    HISTORY_RETENTION_DAYS,
)

_LOGGER = logging.getLogger(__name__)


class WattsmithConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Wattsmith — a single energy-brain instance."""

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
                "Everything else is set afterwards in Configure."
            },
        )


def _suggest(value: Any) -> dict[str, Any]:
    return {"suggested_value": value} if value not in (None, "") else {}


class WattsmithOptionsFlow(config_entries.OptionsFlow):
    """Options — a small menu: Sensors, Economics, History."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._new: dict[str, Any] = {}

    async def async_step_init(self, user_input=None) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["sensors", "economics", "history"],
        )

    # ---- sensors --------------------------------------------------------
    async def async_step_sensors(self, user_input=None) -> FlowResult:
        if user_input is not None:
            new_options = {**self._config_entry.options}
            grid = user_input.get(CONF_GRID_SENSOR)
            if grid:
                new_options[CONF_GRID_SENSOR] = grid
            for key in (CONF_EV_SENSOR, CONF_TIBBER_SENSOR, CONF_CAR_STATE_SENSOR,
                        CONF_SOLCAST_REMAINING_SENSOR, CONF_HOUSE_CONSUMPTION_SENSOR,
                        CONF_PV_SENSOR, CONF_SOLCAST_FORECAST_SENSOR, CONF_WEATHER_SENSOR):
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

        o = self._config_entry.options
        d = self._config_entry.data
        power = lambda: selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power"))
        anysensor = lambda: selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor"))
        schema = vol.Schema({
            vol.Optional(CONF_GRID_SENSOR,
                         description=_suggest(o.get(CONF_GRID_SENSOR, d.get(CONF_GRID_SENSOR, "")))): power(),
            vol.Optional(CONF_PV_SENSOR, description=_suggest(o.get(CONF_PV_SENSOR, ""))): power(),
            vol.Optional(CONF_EV_SENSOR, description=_suggest(o.get(CONF_EV_SENSOR, ""))): power(),
            vol.Optional(CONF_GOE_IP, description=_suggest(o.get(CONF_GOE_IP, ""))):
                selector.TextSelector(),
            vol.Optional(CONF_TIBBER_SENSOR, description=_suggest(o.get(CONF_TIBBER_SENSOR, ""))): anysensor(),
            vol.Optional(CONF_CAR_STATE_SENSOR, description=_suggest(o.get(CONF_CAR_STATE_SENSOR, ""))): anysensor(),
            vol.Optional(CONF_SOLCAST_REMAINING_SENSOR, description=_suggest(o.get(CONF_SOLCAST_REMAINING_SENSOR, ""))): anysensor(),
            vol.Optional(CONF_SOLCAST_FORECAST_SENSOR, description=_suggest(o.get(CONF_SOLCAST_FORECAST_SENSOR, ""))): anysensor(),
            vol.Optional(CONF_HOUSE_CONSUMPTION_SENSOR, description=_suggest(o.get(CONF_HOUSE_CONSUMPTION_SENSOR, ""))): power(),
            vol.Optional(CONF_WEATHER_SENSOR, description=_suggest(o.get(CONF_WEATHER_SENSOR, ""))):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
        })
        return self.async_show_form(step_id="sensors", data_schema=schema)

    # ---- economics ------------------------------------------------------
    async def async_step_economics(self, user_input=None) -> FlowResult:
        batteries = self._batteries()
        if user_input is not None:
            new_options = {**self._config_entry.options}
            new_options[CONF_EXPORT_PRICE] = float(user_input.get(CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE))
            new_options[CONF_ARBITRAGE_ENABLED] = bool(user_input.get(CONF_ARBITRAGE_ENABLED, False))
            new_options[CONF_MIN_ARBITRAGE_MARGIN_CT] = float(
                user_input.get(CONF_MIN_ARBITRAGE_MARGIN_CT, DEFAULT_MIN_ARBITRAGE_MARGIN_CT))
            for key in (CONF_WEAR_COST_CT, CONF_ETA_OVERRIDE, CONF_IMPORT_POWER_CAP_W):
                v = user_input.get(key)
                if v in (None, ""):
                    new_options.pop(key, None)
                else:
                    new_options[key] = float(v)
            # per-battery cost + expected cycles
            batt_cfg = {**(self._config_entry.options.get(CONF_BATTERY_CONFIG) or {})}
            for bid, _name in batteries:
                cost = user_input.get(f"cost_{bid}")
                cyc = user_input.get(f"cycles_{bid}")
                entry = {**batt_cfg.get(bid, {})}
                if cost not in (None, ""):
                    entry["cost_eur"] = float(cost)
                if cyc not in (None, ""):
                    entry["expected_cycles"] = int(cyc)
                if entry:
                    batt_cfg[bid] = entry
            if batt_cfg:
                new_options[CONF_BATTERY_CONFIG] = batt_cfg
            return self.async_create_entry(title="", data=new_options)

        o = self._config_entry.options
        batt_cfg = o.get(CONF_BATTERY_CONFIG) or {}
        fields: dict[Any, Any] = {
            vol.Optional(CONF_ARBITRAGE_ENABLED,
                         default=bool(o.get(CONF_ARBITRAGE_ENABLED, False))): bool,
            vol.Optional(CONF_EXPORT_PRICE,
                         default=float(o.get(CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE))):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=1, step=0.001, mode="box", unit_of_measurement="EUR/kWh")),
            vol.Optional(CONF_MIN_ARBITRAGE_MARGIN_CT,
                         default=float(o.get(CONF_MIN_ARBITRAGE_MARGIN_CT, DEFAULT_MIN_ARBITRAGE_MARGIN_CT))):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=20, step=0.5, mode="box", unit_of_measurement="ct/kWh")),
            vol.Optional(CONF_WEAR_COST_CT, description=_suggest(o.get(CONF_WEAR_COST_CT, ""))):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=30, step=0.1, mode="box", unit_of_measurement="ct/kWh")),
            vol.Optional(CONF_ETA_OVERRIDE, description=_suggest(o.get(CONF_ETA_OVERRIDE, ""))):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0.5, max=1.0, step=0.01, mode="box")),
            vol.Optional(CONF_IMPORT_POWER_CAP_W, description=_suggest(o.get(CONF_IMPORT_POWER_CAP_W, ""))):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=50000, step=100, mode="box", unit_of_measurement="W")),
        }
        for bid, name in batteries:
            cur = batt_cfg.get(bid, {})
            fields[vol.Optional(f"cost_{bid}",
                                description=_suggest(cur.get("cost_eur", "")))] = \
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=100000, step=10, mode="box", unit_of_measurement="EUR"))
            fields[vol.Optional(f"cycles_{bid}",
                                default=int(cur.get("expected_cycles", DEFAULT_EXPECTED_CYCLES)))] = \
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=100, max=20000, step=100, mode="box"))
        return self.async_show_form(
            step_id="economics", data_schema=vol.Schema(fields),
            description_placeholders={
                "info": "Wear cost / round-trip η blank = auto (derived / measured). "
                "Per-battery: cost + expected cycles feed the wear cost; capacity auto-reads."
            },
        )

    # ---- history --------------------------------------------------------
    async def async_step_history(self, user_input=None) -> FlowResult:
        if user_input is not None:
            new_options = {**self._config_entry.options}
            new_options[CONF_HISTORY_ENABLED] = bool(user_input.get(CONF_HISTORY_ENABLED, True))
            new_options[CONF_HISTORY_RETENTION_DAYS] = int(
                user_input.get(CONF_HISTORY_RETENTION_DAYS, HISTORY_RETENTION_DAYS))
            path = (user_input.get(CONF_HISTORY_DB_PATH) or "").strip()
            if path:
                new_options[CONF_HISTORY_DB_PATH] = path
            else:
                new_options.pop(CONF_HISTORY_DB_PATH, None)
            return self.async_create_entry(title="", data=new_options)

        o = self._config_entry.options
        schema = vol.Schema({
            vol.Optional(CONF_HISTORY_ENABLED, default=bool(o.get(CONF_HISTORY_ENABLED, True))): bool,
            vol.Optional(CONF_HISTORY_RETENTION_DAYS,
                         default=int(o.get(CONF_HISTORY_RETENTION_DAYS, HISTORY_RETENTION_DAYS))):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=3650, step=1, mode="box", unit_of_measurement="days")),
            vol.Optional(CONF_HISTORY_DB_PATH, description=_suggest(o.get(CONF_HISTORY_DB_PATH, ""))):
                selector.TextSelector(),
        })
        return self.async_show_form(
            step_id="history", data_schema=schema,
            description_placeholders={
                "info": "15-min analytical DB (never purged by default). "
                "Retention 0 = keep forever. Path blank = <config>/wattsmith/history.db."
            },
        )

    # ---- helpers --------------------------------------------------------
    def _batteries(self) -> list[tuple[str, str]]:
        """[(battery_id, friendly_name)] discovered from the base integration."""
        try:
            from homeassistant.helpers import device_registry as dr
            dev_reg = dr.async_get(self.hass)
            out = []
            for h in discover_batteries(self.hass):
                dev = dev_reg.async_get(h.device_id)
                out.append((h.battery_id, (dev.name_by_user or dev.name) if dev else h.battery_id))
            return out
        except Exception:  # noqa: BLE001 - config flow must render even if discovery hiccups
            return []
