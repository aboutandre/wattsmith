"""EV charge coordinator — HA I/O shell around EvChargePlanner.

All decision logic lives in ev_planner.py (pure, unit-tested); all charger-brand
specifics live in a wallbox driver (see wallbox.py / wallbox_goe.py). This module
only:
  - reads the wallbox's ACTUAL state through the driver (power, car state, force,
    limits) with optional HA-sensor fallbacks,
  - gathers the remaining observations from HA (grid, battery fleet, price),
  - asks the planner what to do,
  - reconciles the wallbox to the plan every tick (never assumes the charger
    remembers its last command — see wallbox.needs_write),
  - publishes status for the HA entities.

Fail-safe stances:
  - Wallbox unreachable → the plan is asserted blind (an "off" plan keeps being
    sent; a charger that is also offline isn't charging anyway).
  - Car state unknown → last known value is reused for a short grace window
    (EV_CAR_STATE_GRACE_S), then treated as disconnected (stops charging).
  - The tick never raises; any failure is recorded and fails toward "off".

If no wallbox is configured the coordinator runs silently without sending
any commands (observation-only).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .battery_bridge import BatteryBridge
from .const import (
    CONF_BRIDGE_FLOOR_SOC,
    CONF_BRIDGE_GRACE_S,
    CONF_CAR_STATE_SENSOR,
    CONF_CHEAP_PRICE_THRESHOLD,
    CONF_CHEAP_TARGET,
    CONF_EV_MODE,
    CONF_EV_SENSOR,
    CONF_GOE_IP,
    CONF_GRID_SENSOR,
    CONF_MAX_BATTERY_SOC,
    CONF_PHASE_DOWN_W,
    CONF_PHASE_UP_W,
    CONF_RESERVE_SOC,
    CONF_TIBBER_SENSOR,
    CONF_WALLBOX_TYPE,
    WALLBOX_TYPE_GOE,
)
from .settings import (
    DEFAULT_BRIDGE_FLOOR_SOC,
    DEFAULT_BRIDGE_GRACE_S,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_CHEAP_TARGET,
    DEFAULT_EV_MODE,
    DEFAULT_MAX_BATTERY_SOC,
    DEFAULT_PHASE_DOWN_W,
    DEFAULT_PHASE_UP_W,
    DEFAULT_RESERVE_SOC,
    EV_CAR_STATE_GRACE_S,
    EV_POWER_MAX_AGE_S,
    EV_TICK_S,
)
from .ev_planner import EvChargePlanner, EvMode, EvObservation, EvPlan, EvPlannerConfig
from .validate_config import effective_reserve_soc
from .wallbox import WallboxDriver, WallboxState, needs_write
from .wallbox_goe import GoeWallbox

_LOGGER = logging.getLogger(__name__)


class EvCoordinator(DataUpdateCoordinator):
    """EV charge control brain (HA I/O shell around EvChargePlanner + a wallbox driver)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="wattsmith_ev",
            update_interval=timedelta(seconds=EV_TICK_S),
        )
        self.entry = entry
        self.bridge = BatteryBridge(hass)
        self._planner = EvChargePlanner()
        self._driver: WallboxDriver | None = None
        self._last_amp: int = 6
        self._last_phases: int = 1
        # last known car state + charger power, for short-grace reuse (F-05)
        self._car_cache: tuple[bool, bool] | None = None
        self._car_cache_ts: float = -1e9
        self._power_cache: float | None = None
        self._power_cache_ts: float = -1e9
        # read by the battery manager each tick: when True it stops excluding the EV
        # load so the home batteries carry the car through a brief PV-surplus dip.
        self.bridge_active: bool = False
        self._apply_config()

    # ---- configuration --------------------------------------------------

    def _opt(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _apply_config(self) -> None:
        self._ev_mode: str = self._opt(CONF_EV_MODE, DEFAULT_EV_MODE)
        self._cheap_target: str = self._opt(CONF_CHEAP_TARGET, DEFAULT_CHEAP_TARGET)
        self._grid_sensor: str | None = self._opt(CONF_GRID_SENSOR, None)
        self._ev_sensor: str | None = self._opt(CONF_EV_SENSOR, None)
        self._tibber_sensor: str | None = self._opt(CONF_TIBBER_SENSOR, None)
        self._car_state_sensor: str | None = self._opt(CONF_CAR_STATE_SENSOR, None)
        self._driver = self._build_driver()
        # EV Reserve is clamped to the battery charge cap: a reserve the
        # batteries can never reach would block solar charging forever (F-17).
        # check_config() (manager side) warns the user when the clamp is active.
        reserve = float(self._opt(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC))
        cap = float(self._opt(CONF_MAX_BATTERY_SOC, DEFAULT_MAX_BATTERY_SOC))
        self._planner.config = EvPlannerConfig(
            reserve_soc=effective_reserve_soc(reserve, cap),
            cheap_price=float(self._opt(CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD)),
            phase_up_w=float(self._opt(CONF_PHASE_UP_W, DEFAULT_PHASE_UP_W)),
            phase_down_w=float(self._opt(CONF_PHASE_DOWN_W, DEFAULT_PHASE_DOWN_W)),
            bridge_grace_s=float(self._opt(CONF_BRIDGE_GRACE_S, DEFAULT_BRIDGE_GRACE_S)),
            bridge_floor_soc=float(self._opt(CONF_BRIDGE_FLOOR_SOC, DEFAULT_BRIDGE_FLOOR_SOC)),
        )

    def _build_driver(self) -> WallboxDriver | None:
        """Instantiate the configured wallbox driver (None = observation-only).

        Brand dispatch happens HERE and only here. A new charger brand is a new
        driver module + one case below (selected via the wallbox_type option).
        """
        wallbox_type = self._opt(CONF_WALLBOX_TYPE, WALLBOX_TYPE_GOE)
        host = (self._opt(CONF_GOE_IP, None) or "").strip()
        if not host:
            return None
        if wallbox_type == WALLBOX_TYPE_GOE:
            return GoeWallbox(host, async_get_clientsession(self.hass))
        _LOGGER.warning("Unknown wallbox type %r — EV control disabled", wallbox_type)
        return None

    async def async_apply_options(self) -> None:
        """Re-read options into the live planner config (after a setting changes)."""
        self._apply_config()

    # ---- public surface for the manager (F-16: no private reach-ins) -----

    @property
    def wallbox_configured(self) -> bool:
        return self._driver is not None

    @property
    def solar_reserve_soc(self) -> float | None:
        """The reserve SOC while the EV is actively solar-charging, else None.

        The manager caps battery charging at this value so the car has
        right-of-way for the PV surplus while in 'solar' state.
        """
        if not self.data or self.data.get("state") != "solar":
            return None
        return self._planner.config.reserve_soc

    def ev_power_recent(self, max_age_s: float = EV_POWER_MAX_AGE_S) -> float | None:
        """Last known charger power if recent enough, else None.

        Used by the manager for EV grid-exclusion when no separate EV power
        sensor is configured — this is what makes the external go-e integration
        optional. None = unknown (the manager's planner then fails safe to HOLD).
        """
        if self._power_cache is None:
            return None
        if time.monotonic() - self._power_cache_ts > max_age_s:
            return None
        return self._power_cache

    # ---- HA entity reads ------------------------------------------------

    def _read_fleet_soc(self) -> float | None:
        """MINIMUM SOC across all batteries (None if no battery data).

        Deliberately min, not mean: the EV reserve gate should not start taking
        the fleet's PV surplus while any single battery is still below reserve.
        (Adaptive uses a capacity-weighted mean instead — it does energy math,
        not a safety gate. The dispatch planner uses per-battery SOC.)
        """
        socs = [st.soc for st in self.bridge.read_all() if st.soc is not None]
        return min(socs) if socs else None

    def _read_battery_charge_w(self) -> float:
        """Total power currently being absorbed by home batteries (positive = charging).

        When the battery manager is zeroing the grid, all PV surplus flows into the
        batteries and grid_w ≈ 0. Without this, the EV coordinator would compute
        available ≈ 0 and never decide to start the car.
        ongrid_power sign: + = discharging, - = charging (absorbing PV).
        """
        return sum(max(0.0, -float(st.power)) for st in self.bridge.read_all() if st.available)

    def _read_sensor_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _read_car_state_sensor(self) -> tuple[bool, bool] | None:
        """(connected, done) from the fallback car-state entity, None if unreadable."""
        if not self._car_state_sensor:
            return None
        state = self.hass.states.get(self._car_state_sensor)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        return _parse_car_state(state.state)

    # ---- observation assembly --------------------------------------------

    def _resolve_car_state(self, wb: WallboxState | None, now: float) -> tuple[bool, bool]:
        """(connected, done): driver first, sensor fallback, short grace on unknown.

        A sustained unknown (past the grace window) fails safe to disconnected,
        which stops charging — but a brief driver/sensor blip no longer kills an
        active session outright (F-05).
        """
        current: tuple[bool, bool] | None = None
        if wb is not None and wb.connected is not None:
            current = (wb.connected, bool(wb.done))
        if current is None:
            current = self._read_car_state_sensor()
        if current is not None:
            self._car_cache = current
            self._car_cache_ts = now
            return current
        if self._car_cache is not None and now - self._car_cache_ts <= EV_CAR_STATE_GRACE_S:
            return self._car_cache
        return False, False

    def _resolve_car_power(self, wb: WallboxState | None, now: float) -> float:
        """Charger power (W): driver first, sensor fallback, short grace, else 0."""
        value: float | None = None
        if wb is not None and wb.power_w is not None:
            value = float(wb.power_w)
        if value is None:
            value = self._read_sensor_float(self._ev_sensor)
        if value is not None:
            self._power_cache = value
            self._power_cache_ts = now
            return value
        if self._power_cache is not None and now - self._power_cache_ts <= EV_POWER_MAX_AGE_S:
            return self._power_cache
        return 0.0

    # ---- wallbox control -------------------------------------------------

    async def _reconcile_wallbox(self, plan: EvPlan, wb: WallboxState | None) -> None:
        if self._driver is None:
            return
        if not needs_write(plan.charge, plan.amp, plan.phases, wb):
            self._last_amp = plan.amp or self._last_amp
            self._last_phases = plan.phases
            return
        if wb is not None:
            # The charger drifted from the plan (e.g. its force state was reset
            # externally) — this is the desync that once let the car charge unbidden.
            _LOGGER.info(
                "wallbox drifted from plan (actual force=%s amp=%s phases=%s; "
                "want charge=%s amp=%s phases=%s) — re-asserting",
                wb.force, wb.amp, wb.phases, plan.charge, plan.amp, plan.phases,
            )
        if await self._driver.apply(plan.charge, plan.amp, plan.phases):
            self._last_amp = plan.amp or self._last_amp
            self._last_phases = plan.phases

    async def async_release_wallbox(self) -> None:
        """Hand the charger back to its own logic (called on unload)."""
        if self._driver is not None:
            await self._driver.release()

    # ---- state-change logging -------------------------------------------

    _prev_ev_state: str | None = None

    def _log_transition(self, state: str, reason: str, obs: "EvObservation") -> None:
        """Log only when the EV state changes (avoids per-tick spam)."""
        _LOGGER.debug(
            "ev tick: state=%s reason=%s grid=%.0fW bat_charge=%.0fW soc=%s",
            state, reason, obs.grid_w or 0.0, obs.battery_charge_w,
            f"{obs.battery_soc:.0f}%" if obs.battery_soc is not None else "?",
        )
        if state == self._prev_ev_state:
            return
        _LOGGER.info(
            "EV state: %s → %s | %s | grid=%.0fW bat_charge=%.0fW soc=%s",
            self._prev_ev_state, state, reason,
            obs.grid_w or 0.0, obs.battery_charge_w,
            f"{obs.battery_soc:.0f}%" if obs.battery_soc is not None else "?",
        )
        self._prev_ev_state = state

    # ---- control tick ---------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        now = time.monotonic()
        try:
            wb = await self._driver.read() if self._driver else None
            connected, done = self._resolve_car_state(wb, now)
            car_power_w = self._resolve_car_power(wb, now)
            try:
                mode = EvMode(self._ev_mode)
            except ValueError:
                mode = EvMode.OFF

            battery_charge_w = self._read_battery_charge_w()
            max_amp = (wb.max_amp if wb and wb.max_amp else None) or self._planner.config.max_amp
            obs = EvObservation(
                now=now,
                mode=mode,
                grid_w=self._read_sensor_float(self._grid_sensor),
                car_power_w=car_power_w,
                battery_soc=self._read_fleet_soc(),
                price=self._read_sensor_float(self._tibber_sensor),
                cheap_target=self._cheap_target,
                car_connected=connected,
                car_done=done,
                max_amp=max_amp,
                cur_amp=self._last_amp,
                cur_phases=self._last_phases,
                battery_charge_w=battery_charge_w,
            )
            plan = self._planner.plan(obs)
            self.bridge_active = plan.bridge_active
            await self._reconcile_wallbox(plan, wb)
            self._log_transition(plan.state, plan.reason, obs)
            return {
                "state": plan.state,
                "reason": plan.reason,
                "charge": plan.charge,
                "amp": plan.amp if plan.charge else 0,
                "phases": plan.phases,
                "target_power_w": plan.target_power_w,
                "bridge_active": plan.bridge_active,
                "car_connected": connected,
                "car_done": done,
                "battery_soc": obs.battery_soc,
                "battery_charge_w": battery_charge_w,
                "grid_w": obs.grid_w,
                "price": obs.price,
                "ev_mode": self._ev_mode,
                "ev_power_w": car_power_w,
                "max_amp": max_amp,
                "wallbox_reachable": wb is not None,
                "goe_configured": self._driver is not None,  # legacy key, kept for entities
            }
        except Exception as err:  # noqa: BLE001 - tick must never raise
            _LOGGER.exception("EV coordinator tick failed: %s", err)
            self.bridge_active = False  # fail safe: don't ask batteries to cover the car
            return {
                "state": "error", "reason": str(err), "charge": False,
                "amp": 0, "phases": self._last_phases, "target_power_w": 0,
                "bridge_active": False,
                "car_connected": False, "car_done": False,
                "battery_soc": None, "battery_charge_w": 0.0, "grid_w": None, "price": None,
                "ev_mode": self._ev_mode, "ev_power_w": None, "max_amp": None,
                "wallbox_reachable": False,
                "goe_configured": self._driver is not None,
            }


def _parse_car_state(value: str) -> tuple[bool, bool]:
    """Interpret a car-state ENTITY value (fallback path) → (connected, done).

    Sensor integrations expose the go-e car field as text. Explicit mapping;
    idle-like values = disconnected, everything else non-empty = connected
    (matches the previous behavior for the marq24 goecharger entities:
    Charging / Complete / "Wait for car" etc. all mean a car is plugged in).
    The primary car-state source is the wallbox driver, which uses the raw
    numeric codes with an explicit allowlist instead.
    """
    v = value.strip().lower()
    if v in ("1", "idle", "no car", "no_car", "unknown", "none", ""):
        return False, False
    return True, False
