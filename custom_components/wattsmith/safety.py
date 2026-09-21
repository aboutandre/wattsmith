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

Observability: a battery failing is a *traceable event*, not just a number. The
raw counter alone hid a real 27-hour outage (F11, 2026-08-26: 32 474 silent
failures, zero log lines), so every streak now logs its transitions — first
failure, exclusion, a throttled still-down reminder, and recovery — and carries
when it started plus the last error, which `status()` surfaces to the UI.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

_LOGGER = logging.getLogger(__name__)


class Mode(Enum):
    NORMAL = "normal"
    SAFE = "safe"


@dataclass
class SafetyConfig:
    grid_max_age_s: float = 10.0       # grid sample older than this => SAFE
    battery_fail_threshold: int = 3    # consecutive fails => exclude battery
    cycle_fail_threshold: int = 3      # consecutive bad cycles => SAFE
    # An excluded battery stays excluded silently otherwise: re-log it this often
    # so a multi-hour outage leaves a trail instead of one line at the start.
    fail_reminder_s: float = 900.0


@dataclass
class BatteryFault:
    """Bookkeeping for one battery's CURRENT consecutive-failure streak.

    Reset wholesale on the first success — `fails == 0` means healthy, and the
    timestamps then describe nothing rather than a stale streak.
    """

    fails: int = 0
    first_fail_ts: float | None = None   # when this streak began (monotonic)
    last_fail_ts: float | None = None
    last_error: str | None = None        # why it failed, as reported by the I/O layer
    excluded: bool = False               # crossed the threshold -> not dispatched
    next_reminder_ts: float | None = None

    def failing_for_s(self, now: float | None) -> float | None:
        """How long this streak has been running, or None if untimed."""
        if now is None or self.first_fail_ts is None:
            return None
        return max(0.0, now - self.first_fail_ts)


# The two independent ways a battery can fail, tracked separately because they
# mean different things and arrive on different paths:
#   READ — the base can't read the device at all (sensors unavailable). Gates dispatch.
#   ACK  — the device answers reads but doesn't acknowledge its setpoints, i.e. it is
#          present but not obeying. Reported only; see battery_healthy().
SOURCE_READ = "not_responding"
SOURCE_ACK = "not_acking"
_SOURCE_LABEL = {
    SOURCE_READ: "not responding to reads",
    SOURCE_ACK: "not acknowledging setpoints",
}


@dataclass
class SafetySupervisor:
    config: SafetyConfig = field(default_factory=SafetyConfig)
    _last_grid_ok: float | None = None
    _faults: dict[str, dict[str, BatteryFault]] = field(default_factory=dict)
    _bad_cycles: int = 0

    # ---- observations ---------------------------------------------------
    def record_grid(self, ok: bool, now: float) -> None:
        if ok:
            self._last_grid_ok = now

    def record_battery(
        self,
        battery_id: str,
        ok: bool,
        now: float | None = None,
        error: str | None = None,
        source: str = SOURCE_READ,
    ) -> None:
        """Record one battery read/ack outcome.

        `now` (monotonic) and `error` are optional so the pure state machine stays
        callable without them; supply both from the I/O layer and the streak
        becomes timestamped and self-describing in the logs.

        `source` keeps the read and ack streaks apart. They MUST NOT share a
        counter: a battery that answers reads every tick but never acks a setpoint
        would otherwise have its ack streak reset on every read, so it could never
        cross the threshold and the failure stayed invisible.
        """
        per_source = self._faults.setdefault(battery_id, {})
        fault = per_source.setdefault(source, BatteryFault())
        gates_dispatch = source == SOURCE_READ

        if ok:
            if fault.fails:
                _LOGGER.warning(
                    "Battery %s RECOVERED (%s) after %d consecutive failures (%s)",
                    battery_id, _SOURCE_LABEL.get(source, source), fault.fails,
                    _fmt_duration(fault.failing_for_s(now)),
                )
            per_source[source] = BatteryFault()
            return

        fault.fails += 1
        fault.last_fail_ts = now
        if error:
            fault.last_error = error

        if fault.fails == 1:
            fault.first_fail_ts = now
            fault.next_reminder_ts = (
                None if now is None else now + self.config.fail_reminder_s
            )
            _LOGGER.warning(
                "Battery %s: %s (1st in a row) — %s",
                battery_id, _SOURCE_LABEL.get(source, source),
                error or "no detail from the I/O layer",
            )
        elif (
            gates_dispatch
            and not fault.excluded
            and fault.fails >= self.config.battery_fail_threshold
        ):
            fault.excluded = True
            _LOGGER.warning(
                "Battery %s EXCLUDED from dispatch after %d consecutive failures (%s) — "
                "remaining batteries absorb its share. Last error: %s",
                battery_id, fault.fails, _fmt_duration(fault.failing_for_s(now)),
                fault.last_error or "none recorded",
            )
        elif (
            now is not None
            and fault.next_reminder_ts is not None
            and now >= fault.next_reminder_ts
        ):
            fault.next_reminder_ts = now + self.config.fail_reminder_s
            _LOGGER.warning(
                "Battery %s STILL %s: %d consecutive failures over %s. Last error: %s",
                battery_id, _SOURCE_LABEL.get(source, source), fault.fails,
                _fmt_duration(fault.failing_for_s(now)),
                fault.last_error or "none recorded",
            )

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
        """Is this battery dispatchable?

        Deliberately gated on the READ streak only — an unreadable device can't be
        controlled, so it must leave the fleet. A battery that reads fine but isn't
        acking is REPORTED (see status()/battery_fault) but still dispatched, which
        preserves the long-standing live behaviour; changing that is a safety-ladder
        decision, not an observability one.
        """
        fault = self._faults.get(battery_id, {}).get(SOURCE_READ)
        return fault is None or fault.fails < self.config.battery_fail_threshold

    def battery_fault(
        self, battery_id: str, source: str = SOURCE_READ
    ) -> BatteryFault | None:
        """The live failure streak for one battery/source (None once it's healthy)."""
        fault = self._faults.get(battery_id, {}).get(source)
        return fault if fault is not None and fault.fails else None

    def mode(self, now: float) -> Mode:
        if not self.grid_fresh(now):
            return Mode.SAFE
        if self._bad_cycles >= self.config.cycle_fail_threshold:
            return Mode.SAFE
        return Mode.NORMAL

    def status(self, now: float) -> dict:
        """Human/UI-friendly snapshot (mapped to an HA diagnostic sensor).

        `battery_fails` is the flat legacy counter (read path, all known batteries,
        healthy ones at 0). `battery_faults` carries the detail that makes an outage
        diagnosable without hand-correlating raw sensor timestamps: per battery, one
        entry per ACTIVE failure mode, so an empty dict means "all well".
        """
        faults: dict[str, dict[str, dict]] = {}
        for bid, per_source in self._faults.items():
            active = {
                source: {
                    "fails": f.fails,
                    "failing_for_s": (
                        None if (d := f.failing_for_s(now)) is None else round(d, 1)
                    ),
                    "excluded": f.excluded,
                    "last_error": f.last_error,
                }
                for source, f in per_source.items()
                if f.fails
            }
            if active:
                faults[bid] = active
        return {
            "mode": self.mode(now).value,
            "grid_fresh": self.grid_fresh(now),
            "bad_cycles": self._bad_cycles,
            "battery_fails": {
                bid: (per_source[SOURCE_READ].fails if SOURCE_READ in per_source else 0)
                for bid, per_source in self._faults.items()
            },
            "battery_faults": faults,
        }


def _fmt_duration(seconds: float | None) -> str:
    """Compact human duration for log lines ('untimed' when no clock was given)."""
    if seconds is None:
        return "untimed"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.1f}h"
