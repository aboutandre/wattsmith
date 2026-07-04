"""Tariff-arbitrage planner (pure, advisory).

Forward 15-minute simulation + merit-order decision: given the price/PV/load
forecast and the battery + economics, decide whether to grid-charge the fleet in
the CURRENT window and how much stored energy to protect (discharge hold) for a
more valuable upcoming window. No Home Assistant, no actuation — this is the
compute-only brain (Phase 3). The manager consumes its output; Phase 4 gates
actuation behind a switch.

Principles (docs/GRID_ARBITRAGE_AND_HISTORY_DB.md, Part A):
  - PV always wins — grid only fills the deficit PV won't cover;
  - a stored kWh is only worth charging at the current price if some future
    deficit's price beats effective cost (price/η + wear + margin);
  - don't charge now if a cheaper window exists before the earliest deficit;
  - protect earmarked energy from leaking into cheaper intermediate deficits
    (the discharge hold);
  - bounded by max-SOC (charge) and min-SOC (discharge) and any import cap.

Rolling: recomputed every tick; only the current-window action is acted on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .economics import effective_cost_ct, is_profitable

BUCKET_H = 0.25  # 15 minutes


@dataclass(frozen=True)
class Bucket:
    price_ct: float      # import price, EUR-cent/kWh (gross)
    pv_wh: float         # forecast PV production this bucket
    load_wh: float       # forecast house load this bucket (excl. car)


def build_buckets(
    now_ts: float,
    prices: list[tuple[float, float]],
    pv_by_slot: dict[int, float],
    load_by_hour: list[float] | None,
    horizon_h: float = 36.0,
) -> list[Bucket]:
    """Align confirmed prices + PV forecast + load into 15-min Bucket list.

    Pure + testable. buckets[0] covers `now`.
      - prices: [(start_epoch, EUR/kWh), ...] at whatever cadence the tariff
        publishes (hourly or 15-min); the covering price is carried forward.
      - pv_by_slot: {bucket_start_epoch: forecast_Wh} (from Solcast detail).
      - load_by_hour: 24-length Wh/bucket by hour-of-day (learned baseline / 4),
        or None to assume a flat 0 (deficit only from explicit load).
    Only buckets with a confirmed price are emitted (no price forecasting).
    """
    if not prices:
        return []
    prices = sorted(prices)
    start = int(now_ts // 900) * 900
    end = start + int(horizon_h * 3600)
    buckets: list[Bucket] = []
    ts = start
    while ts < end:
        price = _price_at(prices, ts)
        if price is None:
            break  # beyond the confirmed horizon
        pv = pv_by_slot.get(ts, 0.0)
        if load_by_hour:
            hour = int((ts % 86400) // 3600)
            load = load_by_hour[hour % 24]
        else:
            load = 0.0
        buckets.append(Bucket(price_ct=price * 100.0, pv_wh=pv, load_wh=load))
        ts += 900
    return buckets


def pv_slots_from_detailed(periods: list[dict]) -> dict[int, float]:
    """Solcast detailed forecast -> {bucket_start_epoch: forecast_Wh per 15-min}.

    Each period is {period_start: iso, pv_estimate: kW-average}. Energy in a
    15-min slot = pv_estimate(kW) × 0.25 h × 1000 = Wh. A 30-min period seeds
    both of its 15-min sub-slots. Any unparsable period is skipped.
    """
    out: dict[int, float] = {}
    for p in periods:
        start = p.get("period_start")
        est = p.get("pv_estimate")
        if start is None or est is None:
            continue
        if isinstance(start, str):
            try:
                dt = datetime.fromisoformat(start)
            except ValueError:
                continue
        elif isinstance(start, datetime):
            dt = start
        else:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            wh = float(est) * 0.25 * 1000.0
        except (ValueError, TypeError):
            continue
        base = int(dt.timestamp() // 900) * 900
        # a Solcast period is 30 min -> fill both 15-min sub-slots
        out[base] = wh
        out[base + 900] = wh
    return out


def _price_at(prices: list[tuple[float, float]], ts: int) -> float | None:
    """Price (EUR/kWh) of the published interval covering `ts`, or None if past end."""
    covering = None
    for start, price in prices:
        if start <= ts:
            covering = price
        else:
            # ts is before this interval; covering holds the last one that started <= ts
            break
    # guard: if ts is beyond the last published interval + its width, treat as unknown
    last_start = prices[-1][0]
    if ts >= last_start + 3600:   # >1h past the last published start -> unknown
        return None
    return covering


@dataclass(frozen=True)
class BatteryModel:
    soc_pct: float
    capacity_wh: float
    min_soc: float
    max_soc: float
    charge_power_w: float


@dataclass(frozen=True)
class Econ:
    eta: float
    wear_ct: float
    min_margin_ct: float
    import_cap_w: float = 0.0   # 0 = no cap


@dataclass(frozen=True)
class ArbitragePlan:
    grid_charge_now_wh: float   # energy to grid-charge in the current bucket
    target_soc: float           # soc we want to reach this window
    hold_floor_soc: float       # don't discharge below this (protect earmark)
    profitable_deficit_wh: float
    reason: str


def _idle(bat: BatteryModel, reason: str) -> ArbitragePlan:
    return ArbitragePlan(0.0, bat.soc_pct, bat.min_soc, 0.0, reason)


def plan_arbitrage(buckets: list[Bucket], bat: BatteryModel, econ: Econ) -> ArbitragePlan:
    """Decide the current-window grid-charge + discharge-hold. buckets[0] = now."""
    if not buckets or bat.capacity_wh <= 0:
        return _idle(bat, "no forecast")
    cap = bat.capacity_wh
    usable_now = cap * max(0.0, bat.soc_pct - bat.min_soc) / 100.0
    headroom_now = cap * max(0.0, bat.max_soc - bat.soc_pct) / 100.0
    cap_usable = cap * max(0.0, bat.max_soc - bat.min_soc) / 100.0
    per_bucket_charge = bat.charge_power_w * BUCKET_H
    if econ.import_cap_w > 0:
        per_bucket_charge = min(per_bucket_charge, econ.import_cap_w * BUCKET_H)

    # 1. PV-first forward sim (no grid arbitrage) -> residual deficits PV+battery can't cover
    soc_e = usable_now
    unmet: list[tuple[int, float, float]] = []   # (index, price, wh imported)
    for i, b in enumerate(buckets):
        net = b.load_wh - b.pv_wh
        if net < 0:
            soc_e = min(cap_usable, soc_e + min(-net, per_bucket_charge))
        else:
            take = min(net, soc_e, bat.charge_power_w * BUCKET_H)
            soc_e -= take
            if net - take > 1.0:
                unmet.append((i, b.price_ct, net - take))

    charge_price = buckets[0].price_ct
    eff_now = effective_cost_ct(charge_price, econ.eta, econ.wear_ct)

    # 2. future deficits worth pre-charging at the current price
    worth = [(i, p, wh) for (i, p, wh) in unmet
             if i >= 1 and is_profitable(p, charge_price, econ.eta, econ.wear_ct, econ.min_margin_ct)]
    profitable_wh = sum(wh for _i, _p, wh in worth)
    if not worth:
        return _idle(bat, f"no profitable window (need > {eff_now + econ.min_margin_ct:.1f} ct)")

    earliest = min(i for i, _p, _wh in worth)

    # 3. timing: don't charge now if a cheaper window exists before the earliest deficit
    cheaper_ahead = any(
        buckets[j].price_ct < charge_price - 1e-9 for j in range(1, earliest)
    )
    # 4. discharge hold: protect earmarked energy for the upcoming deficits
    hold_e = min(usable_now, profitable_wh)
    hold_floor_soc = bat.min_soc + 100.0 * hold_e / cap

    if cheaper_ahead:
        return ArbitragePlan(
            0.0, bat.soc_pct, hold_floor_soc, profitable_wh,
            f"cheaper window ahead before deficit @ bucket {earliest}; holding {hold_e:.0f} Wh",
        )

    # 5. how much to grid-charge now (bounded by headroom, power/cap, and need)
    need = max(0.0, profitable_wh - usable_now)
    grid_now = min(headroom_now, per_bucket_charge, need)
    target_soc = bat.soc_pct + 100.0 * grid_now / cap
    if grid_now < 1.0:
        return ArbitragePlan(
            0.0, bat.soc_pct, hold_floor_soc, profitable_wh,
            f"already hold enough ({usable_now:.0f} Wh) for {profitable_wh:.0f} Wh of deficits",
        )
    return ArbitragePlan(
        grid_now, target_soc, hold_floor_soc, profitable_wh,
        f"grid-charge {grid_now:.0f} Wh @ {charge_price:.1f} ct "
        f"(eff {eff_now:.1f}) for {profitable_wh:.0f} Wh future deficit",
    )
