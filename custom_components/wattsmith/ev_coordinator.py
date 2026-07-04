"""EV charge coordinator — HA I/O shell around EvChargePlanner.

All decision logic lives in ev_planner.py (pure, unit-tested). This module only:
  - gathers raw observations from HA (grid sensor, EV power sensor, the battery
    fleet via BatteryBridge, Tibber price, car state),
  - asks the planner what to do,
  - executes the action via the go-e local HTTP API,
  - publishes status for HA sensor entities.

Runs as a DataUpdateCoordinator. The tick never raises: any failure is recorded and
the last plan is left in place.

Each tick the coordinator reconciles the go-e's *actual* frc/amp/psm against the plan
and re-asserts on any drift — it does NOT assume the charger remembers its last command.
A go-e reboot/firmware/app reset can silently revert frc to 0 (charge-by-default); the
per-tick reconcile catches that and re-sends the desired state (see reconcile_goe).

If go-e IP is not configured the coordinator runs silently without sending any commands.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

import aiohttp

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
    CONF_PHASE_DOWN_W,
    CONF_PHASE_UP_W,
    CONF_RESERVE_SOC,
    CONF_TIBBER_SENSOR,
)
from .settings import (
    DEFAULT_BRIDGE_FLOOR_SOC,
    DEFAULT_BRIDGE_GRACE_S,
    DEFAULT_CHEAP_PRICE_THRESHOLD,
    DEFAULT_CHEAP_TARGET,
    DEFAULT_EV_MODE,
    DEFAULT_PHASE_DOWN_W,
    DEFAULT_PHASE_UP_W,
    DEFAULT_RESERVE_SOC,
    EV_GOE_TIMEOUT_S,
    EV_TICK_S,
)
from .ev_planner import EvChargePlanner, EvMode, EvObservation, EvPlan, EvPlannerConfig

_LOGGER = logging.getLogger(__name__)


class EvCoordinator(DataUpdateCoordinator):
    """EV charge control brain (HA I/O shell around EvChargePlanner)."""

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
        self._last_plan: EvPlan | None = None
        self._last_amp: int = 6
        self._last_phases: int = 1
        # read by the battery manager each tick: when True it stops excluding the EV
        # load so the home batteries carry the car through a brief PV-surplus dip.
        self.bridge_active: bool = False
        self._apply_config()

    # ---- configuration --------------------------------------------------

    def _opt(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _apply_config(self) -> None:
        self._goe_ip: str | None = self._opt(CONF_GOE_IP, None) or None
        self._ev_mode: str = self._opt(CONF_EV_MODE, DEFAULT_EV_MODE)
        self._cheap_target: str = self._opt(CONF_CHEAP_TARGET, DEFAULT_CHEAP_TARGET)
        self._grid_sensor: str | None = self._opt(CONF_GRID_SENSOR, None)
        self._ev_sensor: str | None = self._opt(CONF_EV_SENSOR, None)
        self._tibber_sensor: str | None = self._opt(CONF_TIBBER_SENSOR, None)
        self._car_state_sensor: str | None = self._opt(CONF_CAR_STATE_SENSOR, None)
        self._planner.config = EvPlannerConfig(
            reserve_soc=float(self._opt(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC)),
            cheap_price=float(self._opt(CONF_CHEAP_PRICE_THRESHOLD, DEFAULT_CHEAP_PRICE_THRESHOLD)),
            phase_up_w=float(self._opt(CONF_PHASE_UP_W, DEFAULT_PHASE_UP_W)),
            phase_down_w=float(self._opt(CONF_PHASE_DOWN_W, DEFAULT_PHASE_DOWN_W)),
            bridge_grace_s=float(self._opt(CONF_BRIDGE_GRACE_S, DEFAULT_BRIDGE_GRACE_S)),
            bridge_floor_soc=float(self._opt(CONF_BRIDGE_FLOOR_SOC, DEFAULT_BRIDGE_FLOOR_SOC)),
        )

    async def async_apply_options(self) -> None:
        """Re-read options into the live planner config (after a setting changes)."""
        self._apply_config()

    # ---- HA entity reads ------------------------------------------------

    def _read_fleet_soc(self) -> float | None:
        """Return the minimum SOC across all batteries, or None if no battery data."""
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

    def _read_car_state(self) -> tuple[bool, bool]:
        """Return (connected, done) from the configured car-state entity."""
        if not self._car_state_sensor:
            return False, False
        state = self.hass.states.get(self._car_state_sensor)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return False, False
        return _parse_car_state(state.state)

    # ---- go-e control ---------------------------------------------------

    async def _read_goe_state(self) -> dict[str, int] | None:
        """Read the go-e's actual frc/amp/psm, or None if unreachable/unparseable.

        None means "could not verify" — the caller then re-asserts the plan to be safe.
        """
        try:
            session = async_get_clientsession(self.hass)
            url = f"http://{self._goe_ip}/api/status"
            timeout = aiohttp.ClientTimeout(total=EV_GOE_TIMEOUT_S)
            async with session.get(url, params={"filter": "frc,amp,psm"}, timeout=timeout) as resp:
                if resp.status != 200:
                    _LOGGER.debug("go-e status HTTP %s", resp.status)
                    return None
                data = await resp.json()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("go-e status read failed: %s", err)
            return None
        out: dict[str, int] = {}
        for key in ("frc", "amp", "psm"):
            try:
                out[key] = int(data[key])
            except (KeyError, TypeError, ValueError):
                pass
        return out

    async def _apply_plan(self, plan: EvPlan) -> None:
        if not self._goe_ip:
            return

        actual = await self._read_goe_state()
        params = reconcile_goe(plan, actual)
        if params is None:
            # go-e already matches the plan — nothing to send (don't hammer the charger).
            self._last_amp = plan.amp
            self._last_phases = plan.phases
            self._last_plan = plan
            return

        # actual is not None but mismatched → the charger drifted from the plan (e.g. it
        # reset frc to 0). Surface it: this is the desync that let the car charge unbidden.
        if actual is not None:
            _LOGGER.info("go-e drifted from plan (actual=%s, want=%s) — re-asserting", actual, params)

        try:
            session = async_get_clientsession(self.hass)
            url = f"http://{self._goe_ip}/api/set"
            timeout = aiohttp.ClientTimeout(total=EV_GOE_TIMEOUT_S)
            async with session.get(url, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    _LOGGER.warning("go-e returned HTTP %s for %s", resp.status, params)
                    return
            _LOGGER.debug("go-e set: %s", params)
            self._last_amp = plan.amp
            self._last_phases = plan.phases
            self._last_plan = plan
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("go-e command failed (%s): %s", params, err)

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
            connected, done = self._read_car_state()
            try:
                mode = EvMode(self._ev_mode)
            except ValueError:
                mode = EvMode.OFF

            battery_charge_w = self._read_battery_charge_w()
            obs = EvObservation(
                now=now,
                mode=mode,
                grid_w=self._read_sensor_float(self._grid_sensor),
                car_power_w=self._read_sensor_float(self._ev_sensor) or 0.0,
                battery_soc=self._read_fleet_soc(),
                price=self._read_sensor_float(self._tibber_sensor),
                cheap_target=self._cheap_target,
                car_connected=connected,
                car_done=done,
                max_amp=16,
                cur_amp=self._last_amp,
                cur_phases=self._last_phases,
                battery_charge_w=battery_charge_w,
            )
            plan = self._planner.plan(obs)
            self.bridge_active = plan.bridge_active
            await self._apply_plan(plan)
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
                "goe_configured": bool(self._goe_ip),
            }
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("EV coordinator tick failed: %s", err)
            self.bridge_active = False  # fail safe: don't ask batteries to cover the car
            return {
                "state": "error", "reason": str(err), "charge": False,
                "amp": 0, "phases": self._last_phases, "target_power_w": 0,
                "bridge_active": False,
                "car_connected": False, "car_done": False,
                "battery_soc": None, "battery_charge_w": 0.0, "grid_w": None, "price": None,
                "ev_mode": self._ev_mode, "goe_configured": bool(self._goe_ip),
            }


def reconcile_goe(plan: EvPlan, actual: dict[str, int] | None) -> dict[str, str] | None:
    """Decide what to write to the go-e so it matches ``plan``.

    Pure decision helper (unit-tested). Returns the ``/api/set`` params to send, or
    ``None`` if the charger is already in the desired state and no write is needed.

    ``actual`` is the go-e's real state ``{"frc", "amp", "psm"}`` (ints), or ``None``
    when it could not be read — in which case we always (re)assert the plan to be safe.

    go-e frc: 0 = neutral/charge-by-default, 1 = off (never charge), 2 = force charge.
    A stale frc=0 is exactly what let the car draw grid power while the planner said "off",
    so "off" must be actively held as frc=1, never left implicit.
    """
    desired_frc = 2 if plan.charge else 1
    params: dict[str, str] = {"frc": str(desired_frc)}
    if plan.charge:
        params["amp"] = str(plan.amp)
        params["psm"] = "1" if plan.phases == 1 else "2"

    if actual is None:
        return params  # cannot verify → assert the desired state

    if actual.get("frc") != desired_frc:
        return params
    if plan.charge:
        if actual.get("amp") != plan.amp:
            return params
        desired_psm = 1 if plan.phases == 1 else 2
        if actual.get("psm") != desired_psm:
            return params
    return None  # already in sync


def _parse_car_state(value: str) -> tuple[bool, bool]:
    """Interpret a go-e car-state entity value → (connected, done).

    go-e API v2 `car` field: 1=Idle/no-car, 2=Charging, 3=Connected/waiting, 4=Finished.
    The ha-goecharger-api2 integration may expose these as numeric strings or descriptive text.

    car=4 (Finished) means the *previous* charge session ended — the car is still physically
    connected. We treat it as connected-but-not-done so the planner can restart charging via
    frc=2 when PV surplus is available. The car's own BMS refuses additional power if it is
    genuinely full, so sending frc=2 is safe.
    """
    v = value.strip().lower()
    if v in ("1", "idle", "no car", "no_car"):
        return False, False
    # 2=charging, 3=connected/waiting, 4=finished (previous session), or any other non-idle
    return True, False
