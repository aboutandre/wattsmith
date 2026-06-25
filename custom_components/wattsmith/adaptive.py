"""Adaptive PV charging — pure decision logic (no Home Assistant deps).

The battery Max Charge SOC cap (e.g. 75%) exists for LFP longevity: it stops the
pack sitting at 100% for hours. But that cap also makes the system EXPORT the
day's last rays instead of storing them. This module lets surplus push PAST the
cap up to a ceiling (default 100%) — but TIMED so the fleet crests the ceiling
right as PV production ends, so it never dwells long at 100%.

The decision each tick is a single number: the *effective* Max Charge SOC the
controller should enforce right now — either the normal cap (hold, export the
surplus) or the ceiling (open, soak up the remaining sun).

Rule (v1, reactive gate):
    remaining_surplus = remaining_PV·derate − baseline_load·hours_to_sunset
    headroom          = fleet_capacity · (ceiling − cap) / 100
    open the ceiling once remaining_surplus ≤ headroom (i.e. only enough sun
    left to fill cap→ceiling and no more) → crests the ceiling near sunset.

When surplus exceeds the headroom we hold at the cap and export the excess
(unavoidable — the pack can't hold more than the headroom); we capture the LAST
headroom-worth of energy, which is the part that would otherwise be exported at
dusk. When surplus is below the headroom we open immediately and capture it all
(zero feed-in; we simply won't reach the ceiling).

A later iteration can replace the baseline_load constant with a learned,
history-driven household profile and add Solcast forecast-error correction; the
gate logic stays the same.
"""
from __future__ import annotations

from dataclasses import dataclass

# Status values (also surfaced as the Adaptive Status sensor).
STATUS_DISABLED = "disabled"
STATUS_INACTIVE = "inactive"
STATUS_HOLDING = "holding_at_cap"
STATUS_CHARGING = "charging_to_ceiling"


@dataclass(frozen=True)
class AdaptiveConfig:
    """Tunables for adaptive PV charging."""

    enabled: bool = False
    ceiling_soc: float = 100.0       # push surplus up to this SOC
    baseline_load_w: float = 500.0   # assumed steady household draw during the PV window
    forecast_derate: float = 0.9     # Solcast is generation, not surplus, and over-forecasts;
                                     # derate remaining-PV before using it
    min_hours_to_sunset: float = 0.1  # below this the PV window is effectively over


@dataclass(frozen=True)
class AdaptiveObservation:
    """Inputs for one adaptive evaluation."""

    cap_soc: float                   # the normal Max Charge SOC (manager max_battery_soc)
    fleet_soc: float | None          # current fleet SOC %
    fleet_capacity_wh: float         # Σ battery capacity (Wh)
    remaining_pv_wh: float | None    # Solcast remaining-today forecast (Wh)
    hours_to_sunset: float           # remaining PV window (h)


@dataclass(frozen=True)
class AdaptiveResult:
    """The decision: the effective Max Charge SOC + diagnostics."""

    effective_max_soc: float
    open: bool
    status: str
    reason: str
    remaining_surplus_wh: float      # estimated surplus left in the PV window
    headroom_wh: float               # energy to fill cap→ceiling
    fleet_headroom_wh: float         # energy to fill current SOC→ceiling (dashboard)


def plan_adaptive_ceiling(
    obs: AdaptiveObservation, cfg: AdaptiveConfig
) -> AdaptiveResult:
    """Return the effective Max Charge SOC to enforce this tick.

    Falls back to the cap (no change) whenever disabled, missing data, the sun is
    down, or the ceiling isn't above the cap — i.e. the feature is strictly
    additive and never lowers the existing cap.
    """
    def hold(status: str, reason: str) -> AdaptiveResult:
        return AdaptiveResult(
            effective_max_soc=obs.cap_soc,
            open=False,
            status=status,
            reason=reason,
            remaining_surplus_wh=0.0,
            headroom_wh=0.0,
            fleet_headroom_wh=0.0,
        )

    if not cfg.enabled:
        return hold(STATUS_DISABLED, "adaptive charging disabled")
    if cfg.ceiling_soc <= obs.cap_soc:
        return hold(STATUS_INACTIVE, "ceiling not above cap")
    if obs.fleet_soc is None or obs.remaining_pv_wh is None or obs.fleet_capacity_wh <= 0:
        return hold(STATUS_INACTIVE, "waiting for forecast / battery data")
    if obs.hours_to_sunset <= cfg.min_hours_to_sunset:
        return hold(STATUS_INACTIVE, "outside the PV window")

    headroom_wh = obs.fleet_capacity_wh * (cfg.ceiling_soc - obs.cap_soc) / 100.0
    fleet_headroom_wh = max(
        0.0, obs.fleet_capacity_wh * (cfg.ceiling_soc - obs.fleet_soc) / 100.0
    )
    remaining_surplus_wh = max(
        0.0,
        obs.remaining_pv_wh * cfg.forecast_derate
        - cfg.baseline_load_w * obs.hours_to_sunset,
    )

    # Latch: once the pack has climbed past the cap we keep the ceiling open for
    # the rest of the window so a forecast wobble can't slam it shut mid-climb.
    already_climbing = obs.fleet_soc > obs.cap_soc + 0.5
    open_ceiling = already_climbing or remaining_surplus_wh <= headroom_wh

    if open_ceiling:
        return AdaptiveResult(
            effective_max_soc=cfg.ceiling_soc,
            open=True,
            status=STATUS_CHARGING,
            reason="soaking remaining sun to ceiling",
            remaining_surplus_wh=remaining_surplus_wh,
            headroom_wh=headroom_wh,
            fleet_headroom_wh=fleet_headroom_wh,
        )
    return AdaptiveResult(
        effective_max_soc=obs.cap_soc,
        open=False,
        status=STATUS_HOLDING,
        reason="surplus exceeds headroom; holding at cap",
        remaining_surplus_wh=remaining_surplus_wh,
        headroom_wh=headroom_wh,
        fleet_headroom_wh=fleet_headroom_wh,
    )
