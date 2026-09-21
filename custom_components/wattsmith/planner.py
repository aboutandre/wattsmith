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

import logging
from dataclasses import dataclass, field

try:  # imported as part of the HA integration package
    from .controller import BatteryState, ZeroGridController
    from .safety import SOURCE_ACK, Mode, SafetySupervisor
except ImportError:  # imported standalone (unit tests)
    from controller import BatteryState, ZeroGridController
    from safety import SOURCE_ACK, Mode, SafetySupervisor

_LOGGER = logging.getLogger(__name__)


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
    stalled_ids: list[str] = field(default_factory=list)
    # Batteries present in the fleet but NOT dispatched this tick, id -> why.
    # Distinct from `stalled_ids` (those are dispatchable, just not for discharge).
    excluded: dict[str, str] = field(default_factory=dict)


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
    # Discharge anti-windup: a battery near its configured floor that's commanded to
    # discharge but isn't actually delivering (BMS/hardware cutoff above min_soc) gets
    # excluded from further discharge instead of the command winding up at max forever.
    stall_soc_margin: float = 5.0    # only consider batteries within this many % of min_soc
    stall_power_w: float = 50.0      # actual power below this counts as "not delivering"
    stall_cmd_w: float = 200.0       # previous commanded discharge above this counts as "asked"
    stall_ticks: int = 3             # consecutive stalled ticks before excluding
    # A battery takes seconds to reverse from charge to discharge; that turnaround
    # looks exactly like a floor cutoff (asked, not delivering). Don't count stall
    # ticks this soon after the commanded direction flipped.
    stall_flip_grace_s: float = 60.0
    # Hard re-test of a latched battery. The near_floor exit alone can deadlock: an
    # excluded battery can only leave the floor band by CHARGING, so at night with
    # no PV it stays excluded for hours while the house imports at peak price.
    stall_release_s: float = 900.0


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
        self._stall_streak: dict[str, int] = {}  # id -> consecutive commanded-but-not-delivering ticks
        self._stalled: dict[str, bool] = {}       # id -> latched "excluded from discharge" state
        self._stalled_at: dict[str, float] = {}   # id -> obs.now when the latch closed
        self._cmd_sign: dict[str, int] = {}       # id -> sign of its last non-zero command
        self._flip_at: dict[str, float] = {}      # id -> obs.now of its last direction flip
        self._prev_dispatch: set[str] | None = None  # last tick's dispatch set (None = first tick)

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

    def _note_direction_flips(self, setpoints: dict[str, int], now: float) -> None:
        """Stamp when a battery's commanded direction reverses (charge <-> discharge).

        Feeds the stall detector's turnaround grace: right after a flip a battery
        is legitimately not delivering yet, which is not a floor cutoff.
        """
        for bid, cmd in setpoints.items():
            sign = 1 if cmd > 0 else (-1 if cmd < 0 else 0)
            if sign == 0:
                continue
            if self._cmd_sign.get(bid, 0) not in (0, sign):
                self._flip_at[bid] = now
            self._cmd_sign[bid] = sign

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
        stalled_ids: list[str] = []
        excluded: dict[str, str] = {}
        for b in obs.batteries:
            self.supervisor.record_battery(
                b.id, b.read_ok, now=obs.now,
                error=None if b.read_ok else "base integration reports the device unavailable",
            )
            if not (b.read_ok and b.soc is not None and self.supervisor.battery_healthy(b.id)):
                excluded[b.id] = (
                    "not responding" if not b.read_ok
                    else "no SOC reading" if b.soc is None
                    else "excluded by the safety watchdog (repeated failures)"
                )
            if b.read_ok and b.soc is not None and self.supervisor.battery_healthy(b.id):
                soc = float(b.soc)
                # Anti-windup: near the floor, commanded to discharge, but not actually
                # delivering for several ticks running -> latch as stalled at its real
                # (hardware) floor instead of letting the PD loop keep demanding max power.
                # Latched (not just streak-reset) so it doesn't immediately re-qualify the
                # instant the command drops to 0 — it stays excluded until it's charged
                # back out of the near-floor band, proving it can take/give real power again.
                near_floor = soc <= cfg.min_soc + cfg.stall_soc_margin
                stalled = self._stalled.get(b.id, False)
                latched_for = obs.now - self._stalled_at.get(b.id, obs.now)
                if stalled and (not near_floor or latched_for >= cfg.stall_release_s):
                    stalled = False
                    self._stall_streak[b.id] = 0
                    self._stalled_at.pop(b.id, None)
                elif not stalled:
                    prev_cmd = self._last_setpoints.get(b.id, 0)
                    just_flipped = (
                        obs.now - self._flip_at.get(b.id, -1e9) < cfg.stall_flip_grace_s
                    )
                    stalled_this_tick = (
                        near_floor and prev_cmd > cfg.stall_cmd_w
                        and b.power < cfg.stall_power_w and not just_flipped
                    )
                    streak = self._stall_streak.get(b.id, 0) + 1 if stalled_this_tick else 0
                    self._stall_streak[b.id] = streak
                    stalled = streak >= cfg.stall_ticks
                    if stalled:
                        self._stalled_at[b.id] = obs.now
                self._stalled[b.id] = stalled
                if stalled:
                    stalled_ids.append(b.id)
                healthy.append(BatteryState(
                    id=b.id, soc=soc, power=int(b.power),
                    min_soc=max(cfg.min_soc, soc) if stalled else cfg.min_soc,
                    max_soc=cfg.max_battery_soc,
                    max_power=cfg.max_battery_power,
                ))
        healthy_ids = [b.id for b in healthy]
        self._log_dispatch_changes(healthy_ids, excluded, obs)

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
                            healthy_ids, stalled_ids, excluded)
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
                            obs.grid_value, ev or 0.0, healthy_ids, stalled_ids, excluded)

        # 6) Dispatch. Recompute only on a NEW grid sample (avoid double-counting a
        #    repeated reading -> overshoot). Otherwise reuse the held setpoints.
        new_sample = obs.grid_key != self._last_grid_key
        if new_sample:
            setpoints = self.controller.update(grid_power=obs.grid_value - ev,
                                               batteries=healthy)
            self._note_direction_flips(setpoints, obs.now)
            self._last_setpoints = setpoints
            self._last_grid_key = obs.grid_key
        else:
            setpoints = dict(self._last_setpoints)

        # 7) Throttle writes: only send on change, or when cd_time is about to lapse.
        if not new_sample and (obs.now - self._last_send_ts) < cfg.resend_s:
            self.supervisor.record_cycle(ok=True)
            return self._mk("hold", setpoints, "normal", "holding (cd_time still armed)",
                            obs.grid_value, ev, healthy_ids, stalled_ids, excluded)

        self._last_send_ts = obs.now
        return self._mk("send", setpoints, "normal", "", obs.grid_value, ev, healthy_ids,
                        stalled_ids, excluded)

    def record_send(
        self,
        now: float,
        results: dict[str, bool],
        errors: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Fold in per-battery send results ({id: acked}). Returns (state, reason).

        Cycle health (the SAFE watchdog) is based on reaching AT LEAST ONE battery:
        a single dropped UDP ack on a contended radio must NOT be read as "control
        lost". Persistently-failing individual batteries are excluded via record_battery.
        'degraded' is the softer signal: any battery missing its ack.

        `errors` ({id: message}, from the I/O layer) is what turns a bare failure
        counter into a diagnosable one — pass it whenever the transport knows why.
        """
        errors = errors or {}
        for bid, ok in results.items():
            self.supervisor.record_battery(
                bid, ok, now=now,
                error=None if ok else errors.get(bid, "setpoint not acknowledged"),
                source=SOURCE_ACK,
            )
        any_ok = any(results.values()) if results else False
        all_ok = all(results.values()) if results else False
        self.supervisor.record_cycle(ok=any_ok)   # SAFE only if NONE reachable
        self._send_fail_streak = 0 if all_ok else self._send_fail_streak + 1
        if self._send_fail_streak >= self.config.degraded_threshold:
            return "degraded", f"setpoints not fully acknowledged for {self._send_fail_streak} cycles"
        return "normal", ""

    # ---- helper --------------------------------------------------------
    def _log_dispatch_changes(
        self, healthy_ids: list[str], excluded: dict[str, str], obs: Observation
    ) -> None:
        """Log membership changes of the dispatch set.

        A battery silently vanishing from dispatch is a *different* event from the
        transient degraded blips the status sensor already shows, and it is the one
        that matters: it means the fleet is now smaller and the remaining batteries
        are absorbing its share indefinitely. Only transitions are logged, so a
        permanently-dead battery costs one line, not one per tick.
        """
        current = set(healthy_ids)
        if self._prev_dispatch is None:      # first tick: nothing to compare against
            self._prev_dispatch = current
            return
        if current == self._prev_dispatch:
            return

        for bid in sorted(self._prev_dispatch - current):
            _LOGGER.warning(
                "Battery %s DROPPED from the dispatch set (%s). Fleet is now %d "
                "battery(ies): %s",
                bid, excluded.get(bid, "no longer present in the fleet"),
                len(current), ", ".join(sorted(current)) or "NONE",
            )
        for bid in sorted(current - self._prev_dispatch):
            _LOGGER.warning(
                "Battery %s RE-ENTERED the dispatch set. Fleet is now %d battery(ies): %s",
                bid, len(current), ", ".join(sorted(current)),
            )
        self._prev_dispatch = current

    def _mk(self, action, setpoints, state, reason, grid, ev, healthy_ids=None,
            stalled_ids=None, excluded=None) -> Plan:
        return Plan(
            action=action,
            setpoints=setpoints,
            state=state,
            reason=reason,
            grid=grid,
            ev_power=ev,
            command_total=sum(setpoints.values()),
            healthy_ids=healthy_ids or [],
            stalled_ids=stalled_ids or [],
            excluded=excluded or {},
        )
