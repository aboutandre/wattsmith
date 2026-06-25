"""Pure decision logic for EV charge control — NO Home Assistant, NO I/O.

Implements the surplus-priority cascade that replaces evcc's loadpoint, integrated with
the home-battery reserve:

  PV surplus priority:  house  ->  batteries (up to Reserve SOC)  ->  car  ->  batteries(->100%)
  Cheap-grid window:    if price <= threshold, charge the chosen target(s) at full power.

The HA coordinator gathers raw observations (grid, car power, fleet SOC, price, car state),
calls plan(), and applies the returned go-e command (charge on/off, amps, phases) via the
go-e local API. All policy lives here so it is unit-testable like controller/safety/planner.

Safety / anti-wear stance:
  - normally never charge the car from the home batteries (handled by EV-exclusion on the
    battery side). EXCEPTION: the battery BRIDGE — when PV surplus briefly dips below the
    charge minimum while the car is on, the batteries carry the car at minimum current for
    a bounded grace window (bridge_grace_s) down to a separate floor (bridge_floor_soc), to
    avoid buying grid power for a couple of minutes. Signalled via EvPlan.bridge_active, which
    tells the battery manager to stop excluding the EV load for that tick.
  - never import grid power just to "charge on solar" (pause below the minimum charge current);
  - anti-flap: minimum on/off dwell and minimum phase-switch dwell so we don't hammer the car;
  - if we don't know battery SOC or grid, don't start solar charging (fail safe = off).

Sign conventions: grid_w + = importing; car_power_w >= 0 (a load).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EvMode(Enum):
    OFF = "off"               # never charge
    SOLAR = "solar"           # PV surplus only
    SOLAR_CHEAP = "solar_cheap"  # PV surplus + charge in cheap-price windows
    FAST = "fast"             # always charge at max (manual override)


# cheap-window targets (what the cheap price should charge)
TARGET_NONE = "none"
TARGET_BATTERY = "battery"
TARGET_CAR = "car"
TARGET_BOTH = "both"


@dataclass
class EvPlannerConfig:
    min_amp: int = 6                 # go-e minimum charge current
    max_amp: int = 16                # cable/adapter limit (this go-e: 16 A)
    phase_voltage: float = 230.0
    reserve_soc: float = 80.0        # batteries get PV priority below this; car gets it above
    cheap_price: float = 0.10        # EUR/kWh threshold for the cheap window
    # phase switching (3-phase needs ~3x the power of 1-phase at the same amps)
    phase_up_w: float = 4500.0       # surplus to step UP to 3-phase (3ph min ~4140 + margin)
    phase_down_w: float = 4140.0     # drop to 1-phase once 3-phase min can't be sustained
    min_phase_dwell_s: float = 300.0 # don't switch phases more often than this
    # anti-flap on/off dwell
    min_charge_s: float = 120.0      # keep charging at least this long before pausing
    min_pause_s: float = 120.0       # stay paused at least this long before restarting
    import_stop_w: float = 400.0     # if importing more than this, stop now (override dwell)
    ev_margin_w: float = 0.0         # keep this much export as headroom
    # battery bridge: when surplus dips below the charge minimum while the car is on,
    # let the home batteries carry the car (instead of importing) for a grace window.
    bridge_grace_s: float = 180.0    # how long to bridge from batteries after surplus drops
    bridge_floor_soc: float = 50.0   # stop bridging once fleet SOC falls to this (< reserve_soc)


@dataclass
class EvObservation:
    now: float
    mode: EvMode
    grid_w: float | None        # + = importing (whole-house, car included)
    car_power_w: float          # current charging power (W)
    battery_soc: float | None   # home battery fleet SOC (%)
    price: float | None         # EUR/kWh
    cheap_target: str           # TARGET_BATTERY | TARGET_CAR | TARGET_BOTH | TARGET_NONE
    car_connected: bool
    car_done: bool              # car reports full / charge complete
    max_amp: int = 16
    cur_amp: int = 0
    cur_phases: int = 3
    # When the battery manager is running in zero-grid mode it absorbs all PV surplus so
    # grid_w ≈ 0 even when there is plenty of solar. The raw grid reading alone would make
    # available ≈ 0 and the car would never start. Adding battery_charge_w (total power
    # currently being absorbed by home batteries, positive when charging) makes available
    # equal to true PV surplus: available = car + battery_absorbing + grid_export.
    battery_charge_w: float = 0.0


@dataclass
class EvPlan:
    charge: bool        # go-e frc: True = force on, False = force off
    amp: int            # target charge current (A)
    phases: int         # 1 or 3
    state: str          # off | not_connected | full | fast | cheap | solar | waiting | hold | bridge
    reason: str
    target_power_w: int
    bridge_active: bool = False  # batteries should carry the car this tick (manager drops EV exclusion)


class EvChargePlanner:
    """Pure EV charge-control state machine."""

    def __init__(self, config: EvPlannerConfig | None = None) -> None:
        self.config = config or EvPlannerConfig()
        self._charging = False
        self._phases = 3
        self._last_toggle = -1e9       # last on/off change
        self._last_phase_switch = -1e9
        self._surplus_dropped_at: float | None = None  # when surplus first fell below min (bridge timer)

    # ---- main decision -------------------------------------------------
    def plan(self, obs: EvObservation) -> EvPlan:
        cfg = self.config
        self._phases = obs.cur_phases or self._phases or 3

        # 1) Hard offs.
        if obs.mode is EvMode.OFF:
            return self._off(obs, "off", "EV control is off")
        if not obs.car_connected:
            return self._off(obs, "not_connected", "no car connected")
        if obs.car_done:
            return self._off(obs, "full", "car reports charge complete")

        # 2) Full-power modes: manual FAST, or an active cheap-price window for the car.
        cheap = (
            obs.mode is EvMode.SOLAR_CHEAP
            and obs.price is not None
            and obs.price <= cfg.cheap_price
            and obs.cheap_target in (TARGET_CAR, TARGET_BOTH)
        )
        if obs.mode is EvMode.FAST or cheap:
            return self._charge(obs, cfg.max_amp, 3,
                                "fast" if obs.mode is EvMode.FAST else "cheap",
                                "fast charging" if obs.mode is EvMode.FAST
                                else "cheap-grid window")

        # 3) Solar follow (SOLAR, or SOLAR_CHEAP outside a cheap window).
        if obs.battery_soc is None or obs.grid_w is None:
            return self._maybe_stop(obs, "waiting", "battery/grid data unavailable")

        # Power the car could take without importing = its current draw + grid export +
        # power currently absorbed by home batteries (which can be redirected to the car).
        available = obs.car_power_w - obs.grid_w + obs.battery_charge_w - cfg.ev_margin_w
        phases = self._decide_phases(available, obs.now)
        min_power = cfg.min_amp * phases * cfg.phase_voltage

        # Surplus dipped below the minimum charge current. Rather than import from the grid,
        # let the home batteries bridge the car for a bounded grace window. The bridge may
        # continue BELOW the reserve SOC (down to the separate bridge floor), so this must
        # be evaluated before the reserve gate.
        if available < min_power:
            return self._bridge_or_stop(obs, "waiting", "insufficient PV surplus")

        # Surplus is sufficient, but if the batteries are below their reserve the PV should
        # refill them first — don't (start) solar-charging the car here.
        if obs.battery_soc < cfg.reserve_soc:
            self._surplus_dropped_at = None
            return self._maybe_stop(
                obs, "waiting",
                f"battery below reserve ({obs.battery_soc:.0f}% < {cfg.reserve_soc:.0f}%)",
            )

        self._surplus_dropped_at = None  # surplus healthy again -> reset bridge timer
        amp = int(round(available / (phases * cfg.phase_voltage)))
        amp = max(cfg.min_amp, min(obs.max_amp, amp))
        return self._charge(obs, amp, phases, "solar", "solar charging")

    # ---- helpers -------------------------------------------------------
    def _decide_phases(self, available: float, now: float) -> int:
        cfg = self.config
        phases = self._phases or 3
        if now - self._last_phase_switch < cfg.min_phase_dwell_s:
            return phases
        if phases == 1 and available >= cfg.phase_up_w:
            phases, self._last_phase_switch = 3, now
        elif phases == 3 and available <= cfg.phase_down_w:
            phases, self._last_phase_switch = 1, now
        self._phases = phases
        return phases

    def _charge(self, obs: EvObservation, amp: int, phases: int, state: str, reason: str) -> EvPlan:
        cfg = self.config
        if not self._charging:
            # starting: respect the minimum pause since we last stopped (anti-flap)
            if obs.now - self._last_toggle < cfg.min_pause_s:
                return self._mk(False, 0, phases, "hold", "anti-flap pause before restart")
            self._charging = True
            self._last_toggle = obs.now
        self._phases = phases
        self._surplus_dropped_at = None  # a real charge tick is not a dip
        return self._mk(True, amp, phases, state, reason,
                        amp * phases * int(cfg.phase_voltage))

    def _bridge_or_stop(self, obs: EvObservation, state: str, reason: str) -> EvPlan:
        """Surplus dipped below the charge minimum while the car is connected.

        If we're already charging and the home batteries have headroom above the EV
        bridge floor, keep the car charging at the minimum current and signal the
        battery manager to cover that load (bridge_active) — for up to bridge_grace_s
        after the surplus first dropped. This trades a little battery round-trip loss
        to avoid buying grid power for a brief cloud/load dip. Once the grace window
        elapses or the batteries reach the bridge floor, stop normally.
        """
        cfg = self.config
        if (
            self._charging
            and obs.battery_soc is not None
            and obs.battery_soc > cfg.bridge_floor_soc
        ):
            if self._surplus_dropped_at is None:
                self._surplus_dropped_at = obs.now
            if obs.now - self._surplus_dropped_at < cfg.bridge_grace_s:
                phases = self._phases or 3
                power = cfg.min_amp * phases * int(cfg.phase_voltage)
                return self._mk(True, cfg.min_amp, phases, "bridge",
                                "battery bridge (riding surplus dip)", power,
                                bridge_active=True)
        # grace expired, below the bridge floor, or not charging -> stop
        return self._maybe_stop(obs, state, reason)

    def _maybe_stop(self, obs: EvObservation, state: str, reason: str) -> EvPlan:
        cfg = self.config
        if self._charging:
            importing = obs.grid_w is not None and obs.grid_w > cfg.import_stop_w
            # honor a minimum on-time to ride out brief dips, UNLESS we're importing hard
            if not importing and (obs.now - self._last_toggle) < cfg.min_charge_s:
                return self._mk(True, cfg.min_amp, self._phases, "hold",
                                "min on-time (riding out dip)",
                                cfg.min_amp * self._phases * int(cfg.phase_voltage))
            self._charging = False
            self._last_toggle = obs.now
            self._surplus_dropped_at = None
        return self._mk(False, 0, self._phases, state, reason)

    def _off(self, obs: EvObservation, state: str, reason: str) -> EvPlan:
        if self._charging:
            self._charging = False
            self._last_toggle = obs.now
            self._surplus_dropped_at = None
        return self._mk(False, 0, self._phases, state, reason)

    @staticmethod
    def _mk(charge: bool, amp: int, phases: int, state: str, reason: str,
            target_power_w: int = 0, bridge_active: bool = False) -> EvPlan:
        return EvPlan(charge=charge, amp=amp, phases=phases, state=state,
                      reason=reason, target_power_w=target_power_w,
                      bridge_active=bridge_active)
