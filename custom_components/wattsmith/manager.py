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
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adaptive import AdaptiveConfig, AdaptiveObservation, AdaptiveResult, plan_adaptive_ceiling
from .baseline_learner import BaselineLearner
from .battery_bridge import BatteryBridge
from .const import (
    CONF_ADAPTIVE_BASELINE_W,
    CONF_ADAPTIVE_CEILING_SOC,
    CONF_ADAPTIVE_ENABLED,
    CONF_ADAPTIVE_FORECAST_DERATE,
    CONF_BRIDGE_FLOOR_SOC,
    CONF_DEADBAND_W,
    CONF_DIRECTION_HYSTERESIS_W,
    CONF_EV_SENSOR,
    CONF_GOE_IP,
    CONF_GRID_SENSOR,
    CONF_HOUSE_CONSUMPTION_SENSOR,
    CONF_KD,
    CONF_KP,
    CONF_MAX_BATTERY_SOC,
    CONF_MAX_STEP_W,
    CONF_MIN_SOC,
    CONF_PHASE_DOWN_W,
    CONF_PHASE_UP_W,
    CONF_RESERVE_SOC,
    CONF_SOLCAST_REMAINING_SENSOR,
    CONF_SUN_SENSOR,
    CONF_TARGET_GRID_W,
    DOMAIN,
)
from .settings import (
    DEFAULT_ADAPTIVE_BASELINE_W,
    DEFAULT_ADAPTIVE_CEILING_SOC,
    DEFAULT_ADAPTIVE_FORECAST_DERATE,
    DEFAULT_BRIDGE_FLOOR_SOC,
    DEFAULT_DEADBAND_W,
    DEFAULT_DIRECTION_HYSTERESIS_W,
    DEFAULT_KD,
    DEFAULT_KP,
    DEFAULT_MAX_BATTERY_POWER,
    DEFAULT_MAX_BATTERY_SOC,
    DEFAULT_MAX_STEP_W,
    DEFAULT_MIN_SOC,
    DEFAULT_PHASE_DOWN_W,
    DEFAULT_PHASE_UP_W,
    DEFAULT_RESERVE_SOC,
    DEFAULT_SUN_SENSOR,
    DEFAULT_TARGET_GRID_W,
    ADAPTIVE_LEARN_MIN_SAMPLES,
    CONSUMPTION_STARVED_AFTER_S,
    HOUSE_CONSUMPTION_SENSOR,
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
from .validate_config import ConfigSnapshot, check_config

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
        # Default OFF on a fresh install: the user enables Zero-Grid Control
        # explicitly once configured (and after disabling any other brain). Once
        # toggled, the choice persists in options.
        self.enabled: bool = entry.options.get("enabled", False)
        self.min_soc: float = float(entry.options.get(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        self.max_battery_soc: float = float(entry.options.get(CONF_MAX_BATTERY_SOC, DEFAULT_MAX_BATTERY_SOC))
        self._read_adaptive_options()
        # Rolling per-(weekday, hour) house-load learner.  Feeds observed
        # consumption each tick; persisted across restarts via HA storage.
        self._learner = BaselineLearner(min_samples=ADAPTIVE_LEARN_MIN_SAMPLES)
        self._store: Store = Store(hass, 1, "wattsmith_learned_baseline")
        self._baseline_loaded: bool = False       # lazy-load on first tick
        self._last_baseline_save: float = 0.0    # monotonic; save at most hourly
        # Latest adaptive decision (published for the Adaptive entities).
        self._adaptive: AdaptiveResult | None = None

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
        # Cross-value config sanity (F-17): computed at startup + every options
        # change, surfaced on the status sensor and logged when it changes.
        self.config_warnings: list[str] = []
        self._refresh_config_warnings()
        # Consumption-starvation runtime warning (F-14): warn once when adaptive
        # is on but the house-consumption sensor has been unreadable for a while.
        self._consumption_ok_ts: float = time.monotonic()
        self._consumption_starved: bool = False

    # ---- configuration --------------------------------------------------
    def _opt(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _read_adaptive_options(self) -> None:
        self.adaptive_enabled: bool = bool(self._opt(CONF_ADAPTIVE_ENABLED, False))
        self.adaptive_ceiling_soc: float = float(self._opt(CONF_ADAPTIVE_CEILING_SOC, DEFAULT_ADAPTIVE_CEILING_SOC))
        self.adaptive_baseline_w: float = float(self._opt(CONF_ADAPTIVE_BASELINE_W, DEFAULT_ADAPTIVE_BASELINE_W))
        self.adaptive_forecast_derate: float = float(self._opt(CONF_ADAPTIVE_FORECAST_DERATE, DEFAULT_ADAPTIVE_FORECAST_DERATE))
        self.solcast_sensor: str | None = self._opt(CONF_SOLCAST_REMAINING_SENSOR, None) or None
        self.sun_sensor: str = self._opt(CONF_SUN_SENSOR, DEFAULT_SUN_SENSOR) or DEFAULT_SUN_SENSOR
        self.house_consumption_sensor: str = (
            self._opt(CONF_HOUSE_CONSUMPTION_SENSOR, HOUSE_CONSUMPTION_SENSOR)
            or HOUSE_CONSUMPTION_SENSOR
        )

    def _refresh_config_warnings(self) -> None:
        """Re-run the cross-value sanity checks (F-17); log when they change."""
        ev_coord = self._ev_coordinator()
        snapshot = ConfigSnapshot(
            min_soc=self.min_soc,
            max_battery_soc=self.max_battery_soc,
            adaptive_enabled=self.adaptive_enabled,
            adaptive_ceiling_soc=self.adaptive_ceiling_soc,
            ev_configured=bool(
                getattr(ev_coord, "wallbox_configured", False)
                or self.ev_sensor
                # at startup the EV coordinator doesn't exist yet — fall back to
                # the raw option so EV checks still apply from the first tick
                or self._opt(CONF_GOE_IP, None)
            ),
            reserve_soc=float(self._opt(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC)),
            bridge_floor_soc=float(self._opt(CONF_BRIDGE_FLOOR_SOC, DEFAULT_BRIDGE_FLOOR_SOC)),
            phase_up_w=float(self._opt(CONF_PHASE_UP_W, DEFAULT_PHASE_UP_W)),
            phase_down_w=float(self._opt(CONF_PHASE_DOWN_W, DEFAULT_PHASE_DOWN_W)),
        )
        warnings = check_config(snapshot)
        if warnings != self.config_warnings:
            for w in warnings:
                _LOGGER.warning("Config check: %s", w)
            if not warnings and self.config_warnings:
                _LOGGER.info("Config check: all clear")
        self.config_warnings = warnings

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
        self.enabled = self.entry.options.get("enabled", False)
        self._read_adaptive_options()
        self.controller.config = self._build_controller_config()
        self.planner.config = self._build_planner_config()
        self._refresh_config_warnings()

    # ---- HA I/O (gathering observations) -------------------------------
    def _battery_readings(self, states) -> tuple[list[BatteryReading], dict[str, str]]:
        """Build planner readings + a battery_id -> device_id map for dispatch."""
        readings: list[BatteryReading] = []
        device_by_id: dict[str, str] = {}
        for st in states:
            readings.append(BatteryReading(
                id=st.battery_id, soc=st.soc, power=st.power, read_ok=st.available,
                min_soc=self.min_soc, max_power=DEFAULT_MAX_BATTERY_POWER,
            ))
            device_by_id[st.battery_id] = st.device_id
        return readings, device_by_id

    # ---- adaptive PV charging ------------------------------------------

    async def _load_baseline(self) -> None:
        """Load the persisted sample buffer from .storage/ on first tick."""
        data = await self._store.async_load()
        if data:
            self._learner = BaselineLearner.from_dict(data, min_samples=ADAPTIVE_LEARN_MIN_SAMPLES)
            _LOGGER.debug(
                "Loaded baseline learner: %d samples, %d trusted slots",
                self._learner.sample_count,
                self._learner.learned_slots_count,
            )

    async def _save_baseline(self) -> None:
        """Persist the sample buffer to .storage/ (called at most once per hour)."""
        await self._store.async_save(self._learner.to_dict())

    def _read_house_consumption(self) -> float | None:
        """Current house consumption from the configured sensor (W), or None."""
        state = self.hass.states.get(self.house_consumption_sensor)
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return None
        try:
            return max(0.0, float(state.state))
        except (ValueError, TypeError):
            return None

    def _track_consumption_health(self, consumption: float | None, now: float) -> None:
        """F-14: surface (once) when the baseline learner is silently starving."""
        if consumption is not None:
            if self._consumption_starved:
                _LOGGER.info("House-consumption sensor %s readable again",
                             self.house_consumption_sensor)
            self._consumption_ok_ts = now
            self._consumption_starved = False
            return
        if (
            self.adaptive_enabled
            and not self._consumption_starved
            and now - self._consumption_ok_ts >= CONSUMPTION_STARVED_AFTER_S
        ):
            self._consumption_starved = True
            _LOGGER.warning(
                "House-consumption sensor %s has been unreadable for over an hour — "
                "the adaptive baseline learner is falling back to the configured "
                "constant (%.0f W)",
                self.house_consumption_sensor, self.adaptive_baseline_w,
            )

    def _runtime_warnings(self) -> list[str]:
        """Config warnings + transient runtime warnings for the status sensor."""
        warnings = list(self.config_warnings)
        if self._consumption_starved:
            warnings.append(
                f"house-consumption sensor {self.house_consumption_sensor} unreadable — "
                "baseline learner starving (using fallback constant)"
            )
        return warnings

    def _eval_adaptive(self, states) -> AdaptiveResult:
        """Compute the effective Max Charge SOC for this tick from the fleet + forecast."""
        fleet_cap = sum(s.capacity for s in states if s.capacity)
        weighted = [(s.soc, s.capacity) for s in states if s.soc is not None and s.capacity]
        fleet_soc = (
            sum(soc * cap for soc, cap in weighted) / sum(cap for _, cap in weighted)
            if weighted else None
        )
        # Use the learned per-(weekday, hour) baseline if available; fall back
        # to the configured constant while the learner is still accumulating data.
        _now = datetime.now()
        baseline_w = self._learner.baseline_for_slot(_now.weekday(), _now.hour, self.adaptive_baseline_w)
        obs = AdaptiveObservation(
            cap_soc=self.max_battery_soc,
            fleet_soc=fleet_soc,
            fleet_capacity_wh=fleet_cap,
            remaining_pv_wh=self._read_remaining_pv_wh(),
            hours_to_sunset=self._hours_to_sunset(),
        )
        return plan_adaptive_ceiling(obs, AdaptiveConfig(
            enabled=self.adaptive_enabled,
            ceiling_soc=self.adaptive_ceiling_soc,
            baseline_load_w=baseline_w,
            forecast_derate=self.adaptive_forecast_derate,
        ))

    def _read_remaining_pv_wh(self) -> float | None:
        """Solcast remaining-today forecast in Wh (the sensor reports kWh)."""
        if not self.solcast_sensor:
            return None
        state = self.hass.states.get(self.solcast_sensor)
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return None
        try:
            return float(state.state) * 1000.0
        except (ValueError, TypeError):
            return None

    def _hours_to_sunset(self) -> float:
        """Hours of PV window left (0 when the sun is down), from the sun entity."""
        state = self.hass.states.get(self.sun_sensor)
        if state is None or state.state != "above_horizon":
            return 0.0
        nxt = state.attributes.get("next_setting")
        if isinstance(nxt, str):
            nxt = dt_util.parse_datetime(nxt)
        elif not isinstance(nxt, datetime):
            nxt = None
        if nxt is None:
            return 0.0
        return max(0.0, (nxt - dt_util.utcnow()).total_seconds() / 3600.0)

    def _adaptive_status(self) -> dict[str, Any]:
        a = self._adaptive
        _now = datetime.now()
        learned_w = self._learner.baseline_for_slot(_now.weekday(), _now.hour, self.adaptive_baseline_w)
        if a is None:
            return {
                "status": "inactive", "effective_max_soc": self.max_battery_soc,
                "fleet_headroom_wh": None, "remaining_surplus_wh": None, "open": False,
                "learned_baseline_w": learned_w,
                "learned_slots_count": self._learner.learned_slots_count,
            }
        return {
            "status": a.status, "effective_max_soc": a.effective_max_soc,
            "fleet_headroom_wh": a.fleet_headroom_wh,
            "remaining_surplus_wh": a.remaining_surplus_wh, "open": a.open,
            "learned_baseline_w": learned_w,
            "learned_slots_count": self._learner.learned_slots_count,
        }

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

    def _ev_coordinator(self):
        """The sibling EV coordinator (registered under "<entry_id>_ev"), or None.

        Coupling contract (F-16): only the EV coordinator's PUBLIC surface is
        used — bridge_active, solar_reserve_soc, wallbox_configured,
        ev_power_recent(). Accessed via getattr with safe defaults so a missing/
        old coordinator degrades gracefully instead of raising.
        """
        return self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id + "_ev")

    def _ev_bridging(self) -> bool:
        """True when the EV coordinator wants the batteries to carry the car this tick.

        During a battery bridge we must NOT exclude the EV load — the whole point is for
        the home batteries to cover the car through a brief PV-surplus dip instead of
        importing.
        """
        return bool(getattr(self._ev_coordinator(), "bridge_active", False))

    def _ev_solar_reserve_soc(self) -> float | None:
        """The EV reserve SOC while the EV is actively solar-charging; else None.

        When fleet SOC sits between the EV reserve and max_battery_soc both the
        battery controller and the EV coordinator can be active simultaneously —
        they compete for the same PV watts and pull from the grid.  While the EV
        is in 'solar' state we cap battery charging at reserve_soc so the car
        gets right-of-way for the surplus (this deliberately overrides the
        adaptive ceiling too: car first, then batteries soak the rest).
        """
        return getattr(self._ev_coordinator(), "solar_reserve_soc", None)

    @property
    def _ev_configured(self) -> bool:
        """An EV load exists to exclude: a wallbox driver and/or a power sensor."""
        if self.ev_sensor:
            return True
        return bool(getattr(self._ev_coordinator(), "wallbox_configured", False))

    def _read_ev_raw(self) -> float | None:
        """Raw EV charger power, or None if unconfigured/unreadable this tick.

        Source priority: the wallbox driver's own reading (via the EV
        coordinator, freshness-bounded) — this is what makes an external
        charger integration unnecessary — then the optional EV power sensor
        as fallback. The planner caches brief gaps (ev_max_age_s) and HOLDs
        on a sustained unknown.
        """
        ev_coord = self._ev_coordinator()
        power_recent = getattr(ev_coord, "ev_power_recent", None)
        if callable(power_recent):
            value = power_recent()
            if value is not None:
                return float(value)
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
        # Lazy-load persisted baseline on first tick (Store.async_load is async).
        if not self._baseline_loaded:
            self._baseline_loaded = True
            await self._load_baseline()
        try:
            states = self.bridge.read_all()
            readings, device_by_id = self._battery_readings(states)
            # Feed the learned baseline from live sensor on every tick (debounced internally).
            consumption = self._read_house_consumption()
            self._track_consumption_health(consumption, now)
            if consumption is not None:
                self._learner.observe(consumption)
            # Persist the buffer at most once per hour so learned data survives restarts.
            if now - self._last_baseline_save >= 3600:
                await self._save_baseline()
                self._last_baseline_save = now
            # Adaptive PV charging: raise the effective Max Charge SOC toward the
            # ceiling when only the day's last rays remain, so the fleet crests
            # near sunset instead of exporting the surplus. No-op (= cap) when
            # disabled / no data / sun down.
            self._adaptive = self._eval_adaptive(states)
            self.planner.config.max_battery_soc = self._adaptive.effective_max_soc
            # EV solar-charging right-of-way: hold batteries at reserve SOC so
            # they don't compete with the car for the same PV watts.
            ev_reserve = self._ev_solar_reserve_soc()
            if ev_reserve is not None:
                self.planner.config.max_battery_soc = min(
                    self.planner.config.max_battery_soc, ev_reserve
                )
            grid, fresh, key = self._read_grid()
            bridge = self._ev_bridging()
            obs = Observation(
                now=now, enabled=self.enabled, grid_value=grid, grid_fresh=fresh,
                grid_key=key, ev_configured=self._ev_configured,
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
            "adaptive": self._adaptive_status(),
            "config_warnings": self._runtime_warnings(),
        }

    def _error_status(self, err: str, now: float) -> dict[str, Any]:
        return {
            "state": "error", "reason": err, "enabled": self.enabled,
            "grid_power": None, "ev_power": 0.0, "effective_grid": None,
            "command_total": 0, "setpoints": {},
            "safety": self.supervisor.status(now),
            "target_grid_w": self.controller.config.target_grid_w,
            "adaptive": self._adaptive_status(),
            "config_warnings": self._runtime_warnings(),
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
