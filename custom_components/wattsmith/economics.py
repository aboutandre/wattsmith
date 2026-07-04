"""Battery economics — pure functions for the tariff-arbitrage decision.

No Home Assistant, no I/O. Everything here is unit-tested. The HA shell
(sensors, the recorder's DB reads) feeds these; the arbitrage planner consumes
them. See docs/GRID_ARBITRAGE_AND_HISTORY_DB.md, Part A.

Units: prices in EUR-cent/kWh (ct), energy in Wh, capacity in Wh, η in [0,1].
"""
from __future__ import annotations

from dataclasses import dataclass


def wear_cost_ct_per_kwh(
    cost_eur: float, expected_cycles: float, capacity_wh: float
) -> float | None:
    """Amortised battery wear per delivered kWh (ct/kWh).

    cost / (expected_cycles × capacity). This is the conservative upper bound;
    a lightly-cycled (calendar-limited) fleet's true marginal wear is lower.
    Returns None if any input is non-positive (can't derive).
    """
    if cost_eur <= 0 or expected_cycles <= 0 or capacity_wh <= 0:
        return None
    capacity_kwh = capacity_wh / 1000.0
    return (cost_eur / (expected_cycles * capacity_kwh)) * 100.0  # EUR -> ct


def effective_cost_ct(price_ct: float, eta: float, wear_ct: float) -> float:
    """Cost per kWh actually delivered from the battery = price/η + wear."""
    if eta <= 0:
        return float("inf")
    return price_ct / eta + wear_ct


def is_profitable(
    price_avoided_ct: float,
    price_charge_ct: float,
    eta: float,
    wear_ct: float,
    min_margin_ct: float = 0.0,
) -> bool:
    """True when displacing `price_avoided` beats storing at `price_charge`.

    Gate: price_avoided > price_charge/η + wear + min_margin. The absolute
    formula (not a fixed % gap) — wear is a fixed adder, so the required % grows
    as the charge price falls.
    """
    return price_avoided_ct >= effective_cost_ct(price_charge_ct, eta, wear_ct) + min_margin_ct


def equivalent_full_cycles(
    discharge_throughput_wh: float, capacity_wh: float, offset: float = 0.0
) -> float | None:
    """Lifetime equivalent full cycles = discharged energy / capacity (+ offset)."""
    if capacity_wh <= 0:
        return None
    return offset + max(0.0, discharge_throughput_wh) / capacity_wh


def state_of_health_pct(
    efc: float, expected_cycles: float, end_of_life_soh: float = 0.8
) -> float | None:
    """Rough SoH %: linear from 100% (new) to end_of_life_soh at expected_cycles.

    A planning estimate, not a BMS reading — LiFePO4 fade isn't perfectly linear,
    but this is enough to flag "replace soon".
    """
    if expected_cycles <= 0:
        return None
    frac = min(1.0, max(0.0, efc / expected_cycles))
    return 100.0 - frac * (100.0 - end_of_life_soh * 100.0)


def remaining_cycles(efc: float, expected_cycles: float) -> float:
    return max(0.0, expected_cycles - efc)


@dataclass(frozen=True)
class EtaResult:
    eta: float
    measured: bool          # False -> seed fallback (not enough clean data yet)
    charge_wh_per_pct: float | None = None
    discharge_wh_per_pct: float | None = None
    samples: int = 0


def estimate_round_trip_eta(
    charge_segments: list[tuple[float, float]],
    discharge_segments: list[tuple[float, float]],
    seed: float,
    min_total_dsoc: float = 8.0,
) -> EtaResult:
    """Round-trip η from matched charge/discharge segments.

    Each segment is (ac_wh, dsoc_pct). Round-trip η = (Wh_out per %SOC) /
    (Wh_in per %SOC) — the capacity cancels, so no capacity input is needed.
    Falls back to `seed` until both directions have accumulated enough SOC swing
    (guards against integer-SOC quantisation noise on tiny segments).
    """
    cin = sum(wh for wh, ds in charge_segments if ds > 0)
    cds = sum(ds for _wh, ds in charge_segments if ds > 0)
    din = sum(wh for wh, ds in discharge_segments if ds > 0)
    dds = sum(ds for _wh, ds in discharge_segments if ds > 0)
    if cds < min_total_dsoc or dds < min_total_dsoc or cin <= 0 or din <= 0:
        return EtaResult(eta=seed, measured=False, samples=len(charge_segments) + len(discharge_segments))
    wh_in = cin / cds
    wh_out = din / dds
    eta = max(0.0, min(1.0, wh_out / wh_in))
    return EtaResult(
        eta=eta, measured=True,
        charge_wh_per_pct=wh_in, discharge_wh_per_pct=wh_out,
        samples=len(charge_segments) + len(discharge_segments),
    )


def fleet_wear_cost_ct(per_battery: list[tuple[float, float, float]]) -> float | None:
    """Capacity-weighted fleet wear cost from [(cost_eur, cycles, cap_wh), ...]."""
    num = den = 0.0
    for cost, cycles, cap in per_battery:
        w = wear_cost_ct_per_kwh(cost, cycles, cap)
        if w is not None and cap > 0:
            num += w * cap
            den += cap
    return num / den if den > 0 else None
