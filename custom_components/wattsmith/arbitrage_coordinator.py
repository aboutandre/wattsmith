"""Arbitrage coordinator — the HA I/O shell around the pure arbitrage planner.

Advisory by default (Phase 3): fetches confirmed tariff prices + PV forecast +
learned load, builds 15-min buckets, runs the pure planner, and exposes the
plan + economics. It NEVER commands anything itself — the manager reads the
public surface (charge_floor_soc / hold_floor_soc) and only acts on it when the
Arbitrage switch is on (Phase 4). Every tick writes the advisory decision into
the history DB's arb_reason / grid_charge_wh columns for later validation.

Slow cadence (a few minutes): prices/forecast change on the order of 15 min.
Like the other coordinators, the tick never raises — any failure yields an
inert plan (no charge, no hold) so a forecast hiccup can't affect dispatch.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .arbitrage import BatteryModel, Econ, build_buckets, plan_arbitrage, pv_slots_from_detailed
from .battery_bridge import BatteryBridge
from .const import (
    CONF_ARBITRAGE_ENABLED,
    CONF_BATTERY_CONFIG,
    CONF_ETA_OVERRIDE,
    CONF_IMPORT_POWER_CAP_W,
    CONF_MAX_BATTERY_SOC,
    CONF_MIN_ARBITRAGE_MARGIN_CT,
    CONF_MIN_SOC,
    CONF_SOLCAST_FORECAST_SENSOR,
    CONF_TIBBER_SENSOR,
    CONF_WEAR_COST_CT,
    DOMAIN,
)
from .economics import fleet_wear_cost_ct, wear_cost_ct_per_kwh
from .settings import (
    ARBITRAGE_HORIZON_H,
    DEFAULT_BATTERY_COST_EUR,
    DEFAULT_ETA_SEED,
    DEFAULT_EXPECTED_CYCLES,
    DEFAULT_MAX_BATTERY_SOC,
    DEFAULT_MAX_BATTERY_POWER,
    DEFAULT_MIN_ARBITRAGE_MARGIN_CT,
    DEFAULT_MIN_SOC,
)

_LOGGER = logging.getLogger(__name__)

ARBITRAGE_TICK_S = 300.0  # 5 min — prices/forecast move on ~15-min timescales


class ArbitrageCoordinator(DataUpdateCoordinator):
    """Computes the advisory arbitrage plan + economics for the fleet."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_arbitrage",
            update_interval=timedelta(seconds=ARBITRAGE_TICK_S),
        )
        self.entry = entry
        self.bridge = BatteryBridge(hass)
        self._measured_eta: float | None = None

    # ---- public surface (read by the manager, gated by the switch) ------
    @property
    def enabled(self) -> bool:
        return bool(self.entry.options.get(CONF_ARBITRAGE_ENABLED, False))

    @property
    def charge_floor_soc(self) -> float | None:
        """Grid-charge target SOC for this window, or None (advisory/off)."""
        if not self.enabled or not self.data:
            return None
        return self.data.get("target_soc")

    @property
    def hold_floor_soc(self) -> float | None:
        """Protect-until SOC (discharge hold), or None."""
        if not self.enabled or not self.data:
            return None
        return self.data.get("hold_floor_soc")

    def set_measured_eta(self, eta: float | None) -> None:
        self._measured_eta = eta

    # ---- economics inputs ----------------------------------------------
    def _eta(self) -> tuple[float, str]:
        override = self.entry.options.get(CONF_ETA_OVERRIDE)
        if override not in (None, ""):
            return float(override), "override"
        if self._measured_eta is not None:
            return self._measured_eta, "measured"
        return DEFAULT_ETA_SEED, "seed"

    def _wear_ct(self) -> float:
        configured = self.entry.options.get(CONF_WEAR_COST_CT)
        if configured not in (None, ""):
            return float(configured)
        per_battery = []
        batt_cfg = self.entry.options.get(CONF_BATTERY_CONFIG) or {}
        for st in self.bridge.read_all():
            if not st.capacity:
                continue
            b = batt_cfg.get(st.battery_id, {})
            # fall back to the conservative fleet default when a battery has no
            # configured cost — never treat wear as 0 (that reads as "free" and
            # makes arbitrage over-aggressive).
            cost = float(b.get("cost_eur") or DEFAULT_BATTERY_COST_EUR)
            cycles = float(b.get("expected_cycles") or DEFAULT_EXPECTED_CYCLES)
            per_battery.append((cost, cycles, float(st.capacity)))
        w = fleet_wear_cost_ct(per_battery)
        return w if w is not None else 0.0

    def _fleet_model(self) -> BatteryModel | None:
        states = self.bridge.read_all()
        socs = [(s.soc, s.capacity) for s in states if s.soc is not None and s.capacity]
        if not socs:
            return None
        cap = sum(c for _s, c in socs)
        soc = sum(s * c for s, c in socs) / cap
        min_soc = float(self.entry.options.get(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        max_soc = float(self.entry.options.get(CONF_MAX_BATTERY_SOC, DEFAULT_MAX_BATTERY_SOC))
        charge_power = DEFAULT_MAX_BATTERY_POWER * len(states)
        return BatteryModel(soc_pct=soc, capacity_wh=cap, min_soc=min_soc,
                            max_soc=max_soc, charge_power_w=charge_power)

    # ---- forecast inputs ------------------------------------------------
    async def _prices(self) -> list[tuple[float, float]]:
        """Confirmed tariff prices as [(start_epoch, EUR/kWh)]. Tibber get_prices.

        MUST pass `end` — get_prices with no args returns TODAY ONLY, which caps
        the horizon at midnight and defeats overnight arbitrage. `end` = now +
        horizon pulls tomorrow's published curve (its evening peak is what makes
        overnight grid-charging worthwhile).
        """
        end = (dt_util.now() + timedelta(hours=ARBITRAGE_HORIZON_H)).isoformat()
        try:
            resp = await self.hass.services.async_call(
                "tibber", "get_prices", {"end": end},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("arbitrage: get_prices failed: %s", err)
            return []
        # in-process HA returns the service_response directly; be robust to a wrapper
        resp = (resp or {}).get("service_response", resp) or {}
        out: list[tuple[float, float]] = []
        homes = resp.get("prices", {})
        for series in homes.values() if isinstance(homes, dict) else []:
            for p in series or []:
                start = p.get("start_time") or p.get("startsAt")
                price = p.get("price") if p.get("price") is not None else p.get("total")
                if start is None or price is None:
                    continue
                try:
                    dt = datetime.fromisoformat(str(start))
                    out.append((dt.timestamp(), float(price)))
                except (ValueError, TypeError):
                    continue
        return out

    def _pv_by_slot(self) -> dict[int, float]:
        eid = self.entry.options.get(CONF_SOLCAST_FORECAST_SENSOR)
        if not eid:
            return {}
        st = self.hass.states.get(eid)
        if st is None or not st.attributes:
            return {}
        periods = st.attributes.get("detailedForecast") or st.attributes.get("detailedHourly")
        return pv_slots_from_detailed(periods if isinstance(periods, list) else [])

    def _load_by_hour(self) -> list[float] | None:
        mgr = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        learner = getattr(mgr, "_learner", None)
        baseline = getattr(mgr, "adaptive_baseline_w", 500.0)
        if learner is None:
            return None
        # W per hour -> Wh per 15-min bucket
        return [learner.baseline_for_slot(datetime.now().weekday(), h, baseline) * 0.25
                for h in range(24)]

    # ---- the (advisory) tick -------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        import time
        try:
            bat = self._fleet_model()
            if bat is None:
                return self._inert("no batteries")
            eta, eta_src = self._eta()
            wear = self._wear_ct()
            econ = Econ(
                eta=eta, wear_ct=wear,
                min_margin_ct=float(self.entry.options.get(
                    CONF_MIN_ARBITRAGE_MARGIN_CT, DEFAULT_MIN_ARBITRAGE_MARGIN_CT)),
                import_cap_w=float(self.entry.options.get(CONF_IMPORT_POWER_CAP_W, 0) or 0),
            )
            prices = await self._prices()
            buckets = build_buckets(
                time.time(), prices, self._pv_by_slot(), self._load_by_hour(),
                horizon_h=ARBITRAGE_HORIZON_H,
            )
            plan = plan_arbitrage(buckets, bat, econ)
            self._write_advisory(plan, eta, wear)
            return {
                "grid_charge_now_wh": plan.grid_charge_now_wh,
                "target_soc": plan.target_soc,
                "hold_floor_soc": plan.hold_floor_soc,
                "profitable_deficit_wh": plan.profitable_deficit_wh,
                "reason": plan.reason,
                "eta": eta, "eta_source": eta_src, "wear_ct": wear,
                "horizon_buckets": len(buckets),
                "enabled": self.enabled,
            }
        except Exception as err:  # noqa: BLE001 - advisory tick must never raise
            _LOGGER.exception("arbitrage tick failed: %s", err)
            return self._inert(str(err))

    def _inert(self, reason: str) -> dict[str, Any]:
        return {
            "grid_charge_now_wh": 0.0, "target_soc": None, "hold_floor_soc": None,
            "profitable_deficit_wh": 0.0, "reason": reason,
            "eta": None, "eta_source": None, "wear_ct": None,
            "horizon_buckets": 0, "enabled": self.enabled,
        }

    def _write_advisory(self, plan, eta: float, wear: float) -> None:
        """Best-effort stamp the current bucket's advisory columns in the DB."""
        rec = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id + "_history")
        writer = getattr(rec, "note_advisory", None)
        if callable(writer):
            writer(plan.grid_charge_now_wh, plan.reason, eta, wear / 100.0)
