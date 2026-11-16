"""Which batteries make up the working fleet (pure, no Home Assistant imports)."""
from __future__ import annotations

# A battery that has not answered for this long no longer counts toward the
# fleet's capacity, calibration or charge power. Long enough to ride out a
# missed poll or a base-integration reconnect; short enough that a battery
# that was unplugged and stored drops out of the maths the same afternoon.
ACTIVE_GRACE_S = 900.0


class ActiveTracker:
    """Which batteries are part of the working fleet right now.

    A battery counts while it is available, and for ACTIVE_GRACE_S after its
    last available read, so a single dropout doesn't shrink the fleet. A battery
    that was never available since start-up does not count at all.
    """

    def __init__(self, grace_s: float = ACTIVE_GRACE_S) -> None:
        self.grace_s = grace_s
        self._last_seen: dict[str, float] = {}

    def update(self, states, now: float) -> set[str]:
        """Record this read and return the ids of the active batteries."""
        seen = {s.battery_id for s in states}
        for s in states:
            if s.available:
                self._last_seen[s.battery_id] = now
        # forget batteries that left discovery (deleted / disabled in HA)
        for bid in list(self._last_seen):
            if bid not in seen:
                del self._last_seen[bid]
        return {bid for bid, ts in self._last_seen.items() if now - ts <= self.grace_s}
