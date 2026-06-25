"""Pure decision logic for the Energy Manager — NO Home Assistant, NO I/O.

The HA coordinator (manager.py) gathers raw observations, calls plan(), executes the
returned action (release/hold/send), reports the send result via record_send(), and
publishes the status. All decision-making lives here so it can be exhaustively unit-tested.

Safety stance (these are real batteries on a real grid):
  - Never dispatch on stale/missing grid data            -> HOLD/SAFE
  - Never dispatch when EV exclusion can't be determined  -> HOLD
    (if we don't know the car's draw, we must NOT let batteries try to cover it)
  - Never get stuck commanding a battery: every setpoint carries cd_time (battery
    auto-reverts to 0 W) and the watchdog escalates to SAFE on repeated failure.
  - A single dropped UDP ack is normal; only flag degraded after repeated failures.
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:  # imported as part of the HA integration package
    from .controller import BatteryState, ZeroGridController
    from .safety import Mode, SafetySupervisor
except ImportError:  # imported standalone (unit tests)
    from controller import BatteryState, ZeroGridController
    from safety import Mode, SafetySupervisor


@dataclass
class BatteryReading:
    """Raw per-battery read for one tick."""
    id: str
    soc: float | None        # % ; None = no valid SOC this read
    power: int = 0           # current W, + = discharging (informational)
    read_ok: bool = True     # did the battery coordinator update successfully?
    min_soc: float = 11.0
    max_power: int = 2500


@dataclass
class Observation:
    """Everything the planner needs for one tick (all gathered by the I/O layer)."""
    now: float                       # monotonic seconds
    enabled: bool
    grid_value: float | None         # W, + = import ; None = unreadable
    grid_fresh: bool                 # is the grid sample recent enough?
    grid_key: object | None          # changes when the grid VALUE changes (dedup)
    ev_configured: bool              # is an EV-exclusion sensor configured?
    ev_raw: float | None             # raw EV charger power, or None if unreadable
    batteries: list[BatteryReading] = field(default_factory=list)


@dataclass
class Plan:
    """What the I/O layer should do this tick + the status to publish."""
    action: str                      # "release" | "idle" | "hold" | "send"
    setpoints: dict[str, int]
    state: str                       # disabled|safe|hold|normal|degraded
    reason: str
    grid: float | None
    ev_power: float
    command_total: int
    healthy_ids: list[str]


@dataclass
class PlannerConfig:
    cd_time: int = 10
    resend_s: float = 7.0
    degraded_threshold: int = 3
    ev_max_age_s: float = 20.0       # reuse last EV reading up to this long on a blip
    min_soc: float = 11.0
    max_battery_soc: float = 100.0   # stop charging above this SOC (per-battery)
    max_battery_power: int = 2500
    safe_recover_cycles: int = 3     # consecutive healthy ticks in SAFE before resuming


class DispatchPlanner:
    """Owns the control loop's decision state machine (pure)."""

    def __init__(self, controller: ZeroGridController, supervisor: SafetySupervisor,
                 config: PlannerConfig | None = None) -> None:
        self.controller = controller
        self.supervisor = supervisor
        self.config = config or PlannerConfig()
        # state
        self._released = False
        self._last_grid_key: object | None = None
        self._last_setpoints: dict[str, int] = {}
        self._send_fail_streak = 0
        self._last_send_ts = 0.0
        self._last_ev = 0.0
        self._last_ev_ts = -1e9          # "never read" sentinel
        self._safe_recover_streak = 0    # consecutive healthy ticks while parked in SAFE

    # ---- EV exclusion (safety-critical) --------------------------------
    def resolve_ev(self, obs: Observation) -> float | None:
        """Resolve EV power to exclude. Returns None = UNKNOWN (caller must HOLD).

        - not configured           -> 0 (nothing to exclude)
        - fresh reading            -> that value (>=0), cached
        - brief gap (within window)-> last good value (fail toward EXCLUDE = never feed car)
        - sustained gap            -> None (UNKNOWN; we must not guess the car's draw)
        """
        if not obs.ev_configured:
            return 0.0
        if obs.ev_raw is not None:
            ev = max(0.0, float(obs.ev_raw))
            self._last_ev = ev
            self._last_ev_ts = obs.now
            return ev
        if obs.now - self._last_ev_ts <= self.config.ev_max_age_s:
            return self._last_ev
        return None  # configured but unknown for too long -> hold

    # ---- main decision -------------------------------------------------
    def plan(self, obs: Observation) -> Plan:
        cfg = self.config

        # 1) Disabled -> hand batteries back to Auto once, then idle.
        if not obs.enabled:
            if not self._released:
                self._released = True
                self.controller.reset()
                return self._mk("release", {}, "disabled",
                                "Zero-Grid Control switch is off", None, 0.0)
            return self._mk("idle", {}, "disabled",
                            "Zero-Grid Control switch is off", None, 0.0)
        self._released = False

        # 2) Record grid freshness + resolve EV exclusion.
        self.supervisor.record_grid(obs.grid_value is not None and obs.grid_fresh, obs.now)
        ev = self.resolve_ev(obs)

        # 3) Battery health + healthy set.
        healthy: list[BatteryState] = []
        for b in obs.batteries:
            self.supervisor.record_battery(b.id, b.read_ok)
            if b.read_ok and b.soc is not None and self.supervisor.battery_healthy(b.id):
                healthy.append(BatteryState(
                    id=b.id, soc=float(b.soc), power=int(b.power),
                    min_soc=cfg.min_soc, max_soc=cfg.max_battery_soc,
                    max_power=cfg.max_battery_power,
                ))
        healthy_ids = [b.id for b in healthy]

        # 4) SAFE: stale grid or repeated failures -> actively release, don't dispatch.
        #    SAFE must be RECOVERABLE. Do NOT record more failures here (that would
        #    latch us in SAFE forever). Instead, once the checkable preconditions are
        #    healthy again (fresh grid + at least one reachable battery) for a few
        #    consecutive ticks, clear the watchdog so the next tick resumes control.
        if self.supervisor.mode(obs.now) is Mode.SAFE:
            self.controller.reset()
            grid_ok = obs.grid_value is not None and obs.grid_fresh and ev is not None
            if grid_ok and healthy:
                self._safe_recover_streak += 1
                if self._safe_recover_streak >= cfg.safe_recover_cycles:
                    self.supervisor.reset_cycles()   # heals -> leaves SAFE next tick
                    self._safe_recover_streak = 0
            else:
                self._safe_recover_streak = 0
            reason = ("grid sensor stale/unavailable"
                      if not self.supervisor.grid_fresh(obs.now)
                      else "repeated control-cycle failures (recovering)")
            return self._mk("release", {}, "safe", reason, obs.grid_value, ev or 0.0,
                            healthy_ids)
        self._safe_recover_streak = 0

        # 5) Can't dispatch safely this tick -> HOLD (setpoints persist via cd_time).
        hold_reasons = []
        if obs.grid_value is None:
            hold_reasons.append("no grid value")
        elif not obs.grid_fresh:
            hold_reasons.append("grid stale this tick")
        if ev is None:
            hold_reasons.append("EV sensor unavailable (can't exclude safely)")
        if not healthy:
            hold_reasons.append("no healthy batteries")
        if hold_reasons:
            self.supervisor.record_cycle(ok=False)
            return self._mk("hold", {}, "hold", ", ".join(hold_reasons),
                            obs.grid_value, ev or 0.0, healthy_ids)

        # 6) Dispatch. Recompute only on a NEW grid sample (avoid double-counting a
        #    repeated reading -> overshoot). Otherwise reuse the held setpoints.
        new_sample = obs.grid_key != self._last_grid_key
        if new_sample:
            setpoints = self.controller.update(grid_power=obs.grid_value - ev,
                                               batteries=healthy)
            self._last_setpoints = setpoints
            self._last_grid_key = obs.grid_key
        else:
            setpoints = dict(self._last_setpoints)

        # 7) Throttle writes: only send on change, or when cd_time is about to lapse.
        if not new_sample and (obs.now - self._last_send_ts) < cfg.resend_s:
            self.supervisor.record_cycle(ok=True)
            return self._mk("hold", setpoints, "normal", "holding (cd_time still armed)",
                            obs.grid_value, ev, healthy_ids)

        self._last_send_ts = obs.now
        return self._mk("send", setpoints, "normal", "", obs.grid_value, ev, healthy_ids)

    def record_send(self, now: float, results: dict[str, bool]) -> tuple[str, str]:
        """Fold in per-battery send results ({id: acked}). Returns (state, reason).

        Cycle health (the SAFE watchdog) is based on reaching AT LEAST ONE battery:
        a single dropped UDP ack on a contended radio must NOT be read as "control
        lost". Persistently-failing individual batteries are excluded via record_battery.
        'degraded' is the softer signal: any battery missing its ack.
        """
        for bid, ok in results.items():
            self.supervisor.record_battery(bid, ok)
        any_ok = any(results.values()) if results else False
        all_ok = all(results.values()) if results else False
        self.supervisor.record_cycle(ok=any_ok)   # SAFE only if NONE reachable
        self._send_fail_streak = 0 if all_ok else self._send_fail_streak + 1
        if self._send_fail_streak >= self.config.degraded_threshold:
            return "degraded", f"setpoints not fully acknowledged for {self._send_fail_streak} cycles"
        return "normal", ""

    # ---- helper --------------------------------------------------------
    def _mk(self, action, setpoints, state, reason, grid, ev, healthy_ids=None) -> Plan:
        return Plan(
            action=action,
            setpoints=setpoints,
            state=state,
            reason=reason,
            grid=grid,
            ev_power=ev,
            command_total=sum(setpoints.values()),
            healthy_ids=healthy_ids or [],
        )
