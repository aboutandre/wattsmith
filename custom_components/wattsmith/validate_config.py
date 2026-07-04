"""Cross-value configuration sanity checks — pure, NO Home Assistant.

Individual settings are range-limited by their number entities, but nothing
used to validate the RELATIONSHIPS between them — and several pairs can be
configured into silent dead ends (e.g. EV Reserve SOC above Maximum Charge SOC
means the car never solar-charges, with no hint why). This module makes those
clashes visible and, where safe, provides the clamped effective value.

The manager and EV coordinator call check_config() on startup and on every
options change; warnings are logged and surfaced as attributes on the
Wattsmith Status sensor. Checks warn rather than block: the user stays in
control, but never silently misconfigured.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigSnapshot:
    """The cross-dependent knobs, gathered from manager + EV options."""

    min_soc: float
    max_battery_soc: float
    adaptive_enabled: bool
    adaptive_ceiling_soc: float
    ev_configured: bool            # a wallbox is configured (EV checks apply)
    reserve_soc: float
    bridge_floor_soc: float
    phase_up_w: float
    phase_down_w: float


def effective_reserve_soc(reserve_soc: float, max_battery_soc: float) -> float:
    """The reserve the EV planner should actually gate on.

    A reserve above the battery charge cap is unreachable (batteries stop
    charging at the cap) → the car would wait forever. Clamp to the cap;
    check_config() surfaces a warning whenever the clamp is active.
    """
    return min(reserve_soc, max_battery_soc)


def check_config(cfg: ConfigSnapshot) -> list[str]:
    """Return human-readable warnings for every configured clash (empty = sane)."""
    warnings: list[str] = []

    if cfg.min_soc >= cfg.max_battery_soc:
        warnings.append(
            f"Minimum SOC ({cfg.min_soc:.0f}%) >= Maximum Charge SOC "
            f"({cfg.max_battery_soc:.0f}%): no battery can charge or discharge — "
            "zero-grid control is effectively disabled"
        )

    if cfg.adaptive_enabled and cfg.adaptive_ceiling_soc <= cfg.max_battery_soc:
        warnings.append(
            f"Adaptive Ceiling SOC ({cfg.adaptive_ceiling_soc:.0f}%) is not above "
            f"Maximum Charge SOC ({cfg.max_battery_soc:.0f}%): adaptive charging "
            "can never open"
        )

    if cfg.ev_configured:
        if cfg.reserve_soc > cfg.max_battery_soc:
            warnings.append(
                f"EV Reserve SOC ({cfg.reserve_soc:.0f}%) is above Maximum Charge SOC "
                f"({cfg.max_battery_soc:.0f}%): batteries can never reach the reserve — "
                f"clamping the effective reserve to {cfg.max_battery_soc:.0f}%"
            )
        if cfg.bridge_floor_soc >= effective_reserve_soc(cfg.reserve_soc, cfg.max_battery_soc):
            warnings.append(
                f"EV Bridge Floor SOC ({cfg.bridge_floor_soc:.0f}%) is not below the "
                f"effective EV Reserve SOC "
                f"({effective_reserve_soc(cfg.reserve_soc, cfg.max_battery_soc):.0f}%): "
                "the battery bridge has no room to work"
            )
        if cfg.phase_up_w <= cfg.phase_down_w:
            warnings.append(
                f"EV Phase Up Threshold ({cfg.phase_up_w:.0f} W) is not above the "
                f"Phase Down Threshold ({cfg.phase_down_w:.0f} W): phase selection "
                "can oscillate (only the dwell timer prevents flapping)"
            )

    return warnings
