"""Wallbox abstraction — the brand-agnostic contract between the EV brain and chargers.

The EV coordinator and EvChargePlanner know NOTHING about any specific wallbox.
Everything brand-specific (protocols, state codes, force semantics, hardware
limits) lives in a driver implementing `WallboxDriver`. Adding support for a
new charger brand = one new driver module; the planner, coordinator, entities
and tests are untouched. This is what keeps Wattsmith portable beyond go-e.

Drivers currently available:
  - wallbox_goe.GoeWallbox — go-e Charger via its local HTTP API v2

Normalized semantics (every driver must map its hardware onto these):
  force:  "on" (charging forced), "off" (charging blocked),
          "neutral" (charger's own default logic decides), None (unknown)
  phases: 1 or 3 (None = unknown / not reported)
  connected/done: tri-state — None means "could not determine" and the
          coordinator fails safe (treats a sustained unknown as disconnected).

`needs_write` is the pure reconcile decision used every tick: drivers report
the charger's ACTUAL state and the coordinator re-asserts on any drift. Never
assume a wallbox remembers its last command — the go-e provably doesn't
(resets force on unplug/restart/its own internal logic; see go-e API issue #117).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

FORCE_ON = "on"
FORCE_OFF = "off"
FORCE_NEUTRAL = "neutral"


@dataclass(frozen=True)
class WallboxState:
    """A normalized point-in-time read of one wallbox."""

    force: str | None = None       # FORCE_ON / FORCE_OFF / FORCE_NEUTRAL / None=unknown
    amp: int | None = None         # currently commanded charge current (A)
    phases: int | None = None      # 1 or 3; None = unknown
    power_w: float | None = None   # actual charging power right now (W)
    connected: bool | None = None  # car plugged in; None = unknown
    done: bool | None = None       # car reports charge complete; None = unknown
    max_amp: int | None = None     # hardware/cable limit reported by the box (A)


class WallboxDriver(Protocol):
    """What the EV coordinator needs from any wallbox."""

    async def read(self) -> WallboxState | None:
        """Read the charger's actual state. None = unreachable this tick."""

    async def apply(self, charge: bool, amp: int, phases: int) -> bool:
        """Command the charger (True = accepted). charge=False must actively
        BLOCK charging (not merely stop the current session)."""

    async def release(self) -> None:
        """Hand control back to the charger's own default logic (used when
        Wattsmith is unloaded — the wallbox equivalent of batteries → Auto)."""


def needs_write(charge: bool, amp: int, phases: int, actual: WallboxState | None) -> bool:
    """Pure per-tick reconcile decision: must the plan be (re-)asserted?

    True whenever the charger's actual state is unknown (fail toward asserting)
    or differs from the plan. "Off" must be actively held as FORCE_OFF — a
    charger drifting to neutral typically means "charge when plugged", which is
    exactly the failure that let the car charge from the grid unbidden.
    """
    if actual is None:
        return True
    desired_force = FORCE_ON if charge else FORCE_OFF
    if actual.force != desired_force:
        return True
    if charge:
        if actual.amp != amp:
            return True
        if actual.phases is not None and actual.phases != phases:
            return True
    return False
