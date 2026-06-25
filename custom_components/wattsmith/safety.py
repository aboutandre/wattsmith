"""Safety supervisor for the zero-grid controller.

Pure state machine — NO I/O — so it is fully unit-testable. The orchestration layer
(HA coordinator or the live test harness) feeds it observations each cycle and obeys
its decisions:

  - which batteries are currently healthy (only these are dispatched)
  - whether the system must drop to SAFE mode (actively command batteries to Auto)

This complements the per-setpoint `cd_time` auto-revert (which fails *idle* passively):
the supervisor adds ACTIVE detection + reaction to partial and total failures.

Failure model:
  - grid reading stale/missing  -> SAFE (we must not dispatch blind)
  - too many consecutive bad cycles -> SAFE (loop is unhealthy)
  - a battery times out repeatedly  -> exclude it; controller redistributes to the rest
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Mode(Enum):
    NORMAL = "normal"
    SAFE = "safe"


@dataclass
class SafetyConfig:
    grid_max_age_s: float = 10.0       # grid sample older than this => SAFE
    battery_fail_threshold: int = 3    # consecutive fails => exclude battery
    cycle_fail_threshold: int = 3      # consecutive bad cycles => SAFE


@dataclass
class SafetySupervisor:
    config: SafetyConfig = field(default_factory=SafetyConfig)
    _last_grid_ok: float | None = None
    _battery_fails: dict[str, int] = field(default_factory=dict)
    _bad_cycles: int = 0

    # ---- observations ---------------------------------------------------
    def record_grid(self, ok: bool, now: float) -> None:
        if ok:
            self._last_grid_ok = now

    def record_battery(self, battery_id: str, ok: bool) -> None:
        if ok:
            self._battery_fails[battery_id] = 0
        else:
            self._battery_fails[battery_id] = self._battery_fails.get(battery_id, 0) + 1

    def record_cycle(self, ok: bool) -> None:
        self._bad_cycles = 0 if ok else self._bad_cycles + 1

    def reset_cycles(self) -> None:
        """Clear the bad-cycle counter so SAFE can be left once conditions heal.

        SAFE must be RECOVERABLE: without this, parking in SAFE (which records bad
        cycles) would latch _bad_cycles above the threshold forever.
        """
        self._bad_cycles = 0

    # ---- decisions ------------------------------------------------------
    def grid_fresh(self, now: float) -> bool:
        if self._last_grid_ok is None:
            return False
        return (now - self._last_grid_ok) <= self.config.grid_max_age_s

    def battery_healthy(self, battery_id: str) -> bool:
        return self._battery_fails.get(battery_id, 0) < self.config.battery_fail_threshold

    def mode(self, now: float) -> Mode:
        if not self.grid_fresh(now):
            return Mode.SAFE
        if self._bad_cycles >= self.config.cycle_fail_threshold:
            return Mode.SAFE
        return Mode.NORMAL

    def status(self, now: float) -> dict:
        """Human/UI-friendly snapshot (mapped to an HA diagnostic sensor)."""
        return {
            "mode": self.mode(now).value,
            "grid_fresh": self.grid_fresh(now),
            "bad_cycles": self._bad_cycles,
            "battery_fails": dict(self._battery_fails),
        }
