"""Energy Manager coordinator — the HA I/O shell around the pure DispatchPlanner.

All decision logic lives in planner.py (pure, unit-tested). This module only:
  - gathers raw observations from HA (grid sensor, EV sensor, the battery fleet
    via BatteryBridge),
  - asks the planner what to do,
  - executes the action (release / hold / send) through the base integration's
    HA services (via BatteryBridge — no code import of the base),
  - reports the send result back to the planner and publishes status.

Runs as a DataUpdateCoordinator whose _async_update_data IS the control tick. It never
raises out of the tick: any failure is recorded and the system degrades to SAFE.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .battery_bridge import BatteryBridge
from .const import (
    CONF_DEADBAND_W,
    CONF_DIRECTION_HYSTERESIS_W,
    CONF_EV_SENSOR,
    CONF_GRID_SENSOR,
    CONF_KD,
    CONF_KP,
    CONF_MAX_BATTERY_SOC,
    CONF_MAX_STEP_W,
    CONF_MIN_SOC,
    CONF_TARGET_GRID_W,
    DEFAULT_DEADBAND_W,
    DEFAULT_DIRECTION_HYSTERESIS_W,
    DEFAULT_KD,
    DEFAULT_KP,
    DEFAULT_MAX_BATTERY_POWER,
    DEFAULT_MAX_BATTERY_SOC,
    DEFAULT_MAX_STEP_W,
    DEFAULT_MIN_SOC,
    DEFAULT_TARGET_GRID_W,
    DOMAIN,
    MANAGER_BATTERY_FAIL_THRESHOLD,
    MANAGER_CD_TIME_S,
    MANAGER_CYCLE_FAIL_THRESHOLD,
    MANAGER_DEGRADED_THRESHOLD,
    MANAGER_GRID_MAX_AGE_S,
    MANAGER_RESEND_S,
    MANAGER_TICK_S,
)
from .controller import ControllerConfig, ZeroGridController
from .planner import BatteryReading, DispatchPlanner, Observation, Plan, PlannerConfig
from .safety import SafetyConfig, SafetySupervisor

_LOGGER = logging.getLogger(__name__)


class EnergyManagerCoordinator(DataUpdateCoordinator):
    """Zero-grid coordination brain (HA I/O shell)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_energy_manager",
            update_interval=timedelta(seconds=MANAGER_TICK_S),
        )
        self.entry = entry
        self.bridge = BatteryBridge(hass)
        # grid sensor: option override (repointable via options flow) else original data value
        self.grid_sensor: str = entry.options.get(CONF_GRID_SENSOR) or entry.data[CONF_GRID_SENSOR]
        self.ev_sensor: str | None = entry.options.get(CONF_EV_SENSOR) or None
        self.enabled: bool = entry.options.get("enabled", True)
        self.min_soc: float = float(entry.options.get(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        self.max_battery_soc: float = float(entry.options.get(CONF_MAX_BATTERY_SOC, DEFAULT_MAX_BATTERY_SOC))

        self.controller = ZeroGridController(self._build_controller_config())
        self.supervisor = SafetySupervisor(
            SafetyConfig(
                grid_max_age_s=MANAGER_GRID_MAX_AGE_S,
                battery_fail_threshold=MANAGER_BATTERY_FAIL_THRESHOLD,
                cycle_fail_threshold=MANAGER_CYCLE_FAIL_THRESHOLD,
            )
        )
        self.planner = DispatchPlanner(self.controller, self.supervisor,
                                       self._build_planner_config())
        self._prev_state: str | None = None  # for transition logging

    # ---- configuration --------------------------------------------------
    def _opt(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _build_controller_config(self) -> ControllerConfig:
        return ControllerConfig(
            target_grid_w=int(self._opt(CONF_TARGET_GRID_W, DEFAULT_TARGET_GRID_W)),
            kp=float(self._opt(CONF_KP, DEFAULT_KP)),
            kd=float(self._opt(CONF_KD, DEFAULT_KD)),
            deadband_w=int(self._opt(CONF_DEADBAND_W, DEFAULT_DEADBAND_W)),
            max_step_w=int(self._opt(CONF_MAX_STEP_W, DEFAULT_MAX_STEP_W)),
            direction_hysteresis_w=int(
                self._opt(CONF_DIRECTION_HYSTERESIS_W, DEFAULT_DIRECTION_HYSTERESIS_W)
            ),
        )

    def _build_planner_config(self) -> PlannerConfig:
        return PlannerConfig(
            cd_time=MANAGER_CD_TIME_S,
            resend_s=MANAGER_RESEND_S,
            degraded_threshold=MANAGER_DEGRADED_THRESHOLD,
            ev_max_age_s=MANAGER_GRID_MAX_AGE_S,
            min_soc=self.min_soc,
            max_battery_soc=self.max_battery_soc,
            max_battery_power=DEFAULT_MAX_BATTERY_POWER,
        )

    async def async_apply_options(self) -> None:
        """Re-read options into the live controller/planner (after a setting changes)."""
        self.min_soc = float(self._opt(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        self.max_battery_soc = float(self._opt(CONF_MAX_BATTERY_SOC, DEFAULT_MAX_BATTERY_SOC))
        self.grid_sensor = self.entry.options.get(CONF_GRID_SENSOR) or self.entry.data[CONF_GRID_SENSOR]
        self.ev_sensor = self.entry.options.get(CONF_EV_SENSOR) or None
        self.enabled = self.entry.options.get("enabled", True)
        self.controller.config = self._build_controller_config()
        self.planner.config = self._build_planner_config()

    # ---- HA I/O (gathering observations) -------------------------------
    def _battery_readings(self) -> tuple[list[BatteryReading], dict[str, str]]:
        """Build planner readings + a battery_id -> device_id map for dispatch."""
        readings: list[BatteryReading] = []
        device_by_id: dict[str, str] = {}
        for st in self.bridge.read_all():
            readings.append(BatteryReading(
                id=st.battery_id, soc=st.soc, power=st.power, read_ok=st.available,
                min_soc=self.min_soc, max_power=DEFAULT_MAX_BATTERY_POWER,
            ))
            device_by_id[st.battery_id] = st.device_id
        return readings, device_by_id

    def _read_grid(self) -> tuple[float | None, bool, Any]:
        """Return (grid_power_w, fresh, sample_key). + = importing.

        sample_key = entity last_changed (changes only when the VALUE changes) so a
        re-published identical value isn't treated as a new sample (avoids double-counting
        a repeated reading -> overshoot). Freshness uses last_updated (is it alive).
        """
        state = self.hass.states.get(self.grid_sensor)
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return None, False, None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None, False, None
        age = (dt_util.utcnow() - state.last_updated).total_seconds()
        return value, age <= MANAGER_GRID_MAX_AGE_S, state.last_changed

    def _ev_bridging(self) -> bool:
        """True when the EV coordinator wants the batteries to carry the car this tick.

        During a battery bridge we must NOT exclude the EV load — the whole point is for
        the home batteries to cover the car through a brief PV-surplus dip instead of
        importing. The EV coordinator is registered alongside us under "<entry_id>_ev".
        """
        ev_coord = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id + "_ev")
        return bool(getattr(ev_coord, "bridge_active", False))

    def _read_ev_raw(self) -> float | None:
        """Raw EV charger power, or None if unconfigured/unreadable (planner handles caching)."""
        if not self.ev_sensor:
            return None
        state = self.hass.states.get(self.ev_sensor)
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    async def _release_batteries(self) -> None:
        """Hand all batteries back to their own Auto mode (safe idle)."""
        await self.bridge.release_all(self.bridge.device_ids())

    # ---- the control tick ----------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        now = time.monotonic()
        try:
            readings, device_by_id = self._battery_readings()
            grid, fresh, key = self._read_grid()
            bridge = self._ev_bridging()
            obs = Observation(
                now=now, enabled=self.enabled, grid_value=grid, grid_fresh=fresh,
                grid_key=key, ev_configured=self.ev_sensor is not None,
                # bridge: fold the car back into the load the batteries zero out
                ev_raw=0.0 if bridge else self._read_ev_raw(), batteries=readings,
            )
            plan = self.planner.plan(obs)
            state, reason = plan.state, plan.reason

            if plan.action == "release":
                await self._release_batteries()
            elif plan.action == "send":
                setpoints = {b: plan.setpoints[b] for b in plan.setpoints if b in device_by_id}
                results = await self.bridge.set_passive(setpoints, device_by_id, MANAGER_CD_TIME_S)
                state, reason = self.planner.record_send(now, results)
            # "hold" / "idle": nothing to execute

            result = self._status(plan, state, reason, now)
            result["ev_bridge"] = bridge
        except Exception as err:  # noqa: BLE001 - tick must never raise
            _LOGGER.exception("Energy manager tick failed: %s", err)
            self.supervisor.record_cycle(ok=False)
            result = self._error_status(str(err), now)

        result.setdefault("ev_bridge", False)
        self._log_transition(result, now)
        return result

    def _status(self, plan: Plan, state: str, reason: str, now: float) -> dict[str, Any]:
        return {
            "state": state,
            "reason": reason,
            "enabled": self.enabled,
            "grid_power": plan.grid,
            "ev_power": plan.ev_power,
            "effective_grid": (plan.grid - plan.ev_power) if plan.grid is not None else None,
            "command_total": plan.command_total,
            "setpoints": plan.setpoints,
            "safety": self.supervisor.status(now),
            "target_grid_w": self.controller.config.target_grid_w,
        }

    def _error_status(self, err: str, now: float) -> dict[str, Any]:
        return {
            "state": "error", "reason": err, "enabled": self.enabled,
            "grid_power": None, "ev_power": 0.0, "effective_grid": None,
            "command_total": 0, "setpoints": {},
            "safety": self.supervisor.status(now),
            "target_grid_w": self.controller.config.target_grid_w,
        }

    def _log_transition(self, result: dict[str, Any], now: float) -> None:
        """Log only when the manager's state CHANGES (avoids per-tick spam)."""
        state = result.get("state")
        _LOGGER.debug(
            "tick: state=%s grid=%s ev=%s cmd=%s reason=%s safety=%s",
            state, result.get("grid_power"), result.get("ev_power"),
            result.get("command_total"), result.get("reason"), result.get("safety"),
        )
        if state == self._prev_state:
            return
        msg = ("Energy manager state: %s → %s | reason=%s | grid=%sW ev=%sW cmd=%sW | safety=%s")
        args = (self._prev_state, state, result.get("reason") or "-",
                result.get("grid_power"), result.get("ev_power"),
                result.get("command_total"), result.get("safety"))
        if state in ("safe", "hold", "degraded", "error"):
            _LOGGER.warning(msg, *args)
        else:
            _LOGGER.info(msg, *args)
        self._prev_state = state
