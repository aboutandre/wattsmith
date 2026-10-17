"""Human wording for the calibration and arbitrage plans (pure — no Home Assistant, no I/O).

The planners decide in 15-minute buckets counted from now ("bucket 23 @ 28.7 ct").
That is what the code needs and tells a person nothing: when does it start, when
does it stop, how much, why? This module renders the same decision as what / how
much / when / at what price / why, in the owner's number format:

    Calibration top-up: ~15 kWh at 12:15–14:30 (@ 28,7 ct). Overdue (drift 17,2 pts, 5,6 d since full).

Conventions:
  ~    an estimate (kWh). The plan is re-made every few minutes, so energy and times
       move with prices and PV.
  Ø    the average price when the bought slots are not all the same price (German
       "Durchschnitt"). One price for every slot is shown plain.
  Times are 24 h local clock; a window that is not today says "tomorrow".

The planners keep their own machine `reason` strings (the history DB stores them);
these functions only read the structured fields next to them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

BUCKET_S = 900

# Decimal comma. Country wins over language: the same HA can run an English UI in Germany,
# and the numbers should follow where the system is, not the UI language.
_COMMA_COUNTRIES = frozenset({
    "DE", "AT", "FR", "ES", "IT", "NL", "BE", "PT", "PL", "CZ", "SK", "SE", "DK", "NO", "FI",
    "HU", "RO", "BG", "GR", "HR", "SI", "TR", "BR", "AR", "RU", "UA",
})
_COMMA_LANGUAGES = frozenset({
    "de", "fr", "es", "it", "nl", "pt", "pl", "cs", "sk", "sv", "da", "nb", "no", "fi",
    "hu", "ro", "bg", "el", "hr", "sl", "tr", "ru", "uk",
})
# "Ø" for an average is the German-speaking convention
_AVG_COUNTRIES = frozenset({"DE", "AT", "CH"})


@dataclass(frozen=True)
class Locale:
    decimal: str = "."
    avg: str = "avg"


def locale_for(country: str | None, language: str | None) -> Locale:
    """Number format for HA's configured country (preferred) or, without one, language."""
    c = (country or "").upper()
    lang = (language or "").lower().replace("_", "-").split("-")[0]
    if c:
        return Locale("," if c in _COMMA_COUNTRIES else ".",
                      "Ø" if c in _AVG_COUNTRIES else "avg")
    return Locale("," if lang in _COMMA_LANGUAGES else ".", "Ø" if lang == "de" else "avg")


@dataclass(frozen=True)
class Ctx:
    """What the wording needs to turn bucket numbers into clock times and numbers."""
    base_ts: float                              # start of bucket 0, the 15-min slot holding "now"
    now_ts: float
    local: Callable[[float], datetime]          # epoch -> timezone-aware local datetime
    loc: Locale = Locale()


# ---- numbers -----------------------------------------------------------------------------

def _fmt(x: float, decimals: int, loc: Locale) -> str:
    return f"{x:.{decimals}f}".replace(".", loc.decimal)


def _g(x: float, loc: Locale) -> str:
    """A threshold as written in a sentence: 8 -> "8", 7.5 -> "7,5"."""
    return _fmt(x, 0 if float(x).is_integer() else 1, loc)


def kwh_text(wh: float, loc: Locale) -> str:
    kwh = wh / 1000.0
    return f"~{_fmt(kwh, 0 if kwh >= 10 else 1, loc)} kWh"


def ct_text(ct: float, loc: Locale) -> str:
    return f"{_fmt(ct, 1, loc)} ct"


def price_text(prices_ct: Sequence[float], loc: Locale,
               weights: Sequence[float] | None = None) -> str:
    """"28,7 ct" when every slot costs the same at the shown precision, else "Ø 28,7 ct"."""
    if not prices_ct:
        return ""
    if len({round(p, 1) for p in prices_ct}) == 1:
        return ct_text(prices_ct[0], loc)
    w = weights if weights and sum(weights) > 0 else [1.0] * len(prices_ct)
    avg = sum(p * x for p, x in zip(prices_ct, w)) / sum(w)
    return f"{loc.avg} {ct_text(avg, loc)}"


# ---- time --------------------------------------------------------------------------------

def _slot_ts(idx: int, ctx: Ctx) -> float:
    return ctx.base_ts + idx * BUCKET_S


def clock(ts: float, ctx: Ctx) -> str:
    return ctx.local(ts).strftime("%H:%M")


def merge_slots(slots: Sequence[int]) -> list[tuple[int, int]]:
    """Consecutive buckets as inclusive (first, last) runs: [4,5,6,9] -> [(4,6),(9,9)]."""
    runs: list[tuple[int, int]] = []
    for i in sorted(set(slots)):
        if runs and i == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def _day_prefix(ts: float, since_date, ctx: Ctx) -> str:
    """"tomorrow " / "Sat " when `ts` falls on another day than `since_date` ("" if the same)."""
    d = ctx.local(ts).date()
    if d == since_date:
        return ""
    if (d - ctx.local(ctx.now_ts).date()).days == 1:
        return "tomorrow "
    return ctx.local(ts).strftime("%a ")


def windows_text(slots: Sequence[int], ctx: Ctx, approx_end: bool = False) -> str:
    """Exact windows of a set of buckets: "at 12:15–13:00 + 13:30–14:45", "until 14:30".

    A run that includes the current bucket is already under way and reads "until".
    `approx_end` marks that end as an estimate (a top-up that finishes when full).
    """
    runs = merge_slots(slots)
    if not runs:
        return ""
    parts: list[str] = []
    today = ctx.local(ctx.now_ts).date()
    shown = today
    for n, (a, b) in enumerate(runs):
        start, end = _slot_ts(a, ctx), _slot_ts(b + 1, ctx)
        if a == 0:
            tilde = "~" if approx_end else ""
            parts.append(f"until {_day_prefix(end - 1, today, ctx)}{tilde}{clock(end, ctx)}")
            continue
        day = _day_prefix(start, shown, ctx)
        shown = ctx.local(start).date()
        word = "at " if n == 0 else ""
        parts.append(f"{word}{day}{clock(start, ctx)}–{clock(end, ctx)}")
    return " + ".join(parts)


# ---- structured attributes for the sensors -----------------------------------------------

def plan_summary(slots: Sequence[int], prices_ct: Sequence[float],
                 energy_wh: float | None, ctx: Ctx) -> dict:
    """What a dashboard can use without parsing the sentence: windows, energy, prices."""
    if not slots:
        return {"windows": [], "energy_kwh": None, "price_ct": None,
                "price_ct_min": None, "price_ct_max": None}
    windows = [{"start": ctx.local(_slot_ts(a, ctx)).isoformat(),
                "end": ctx.local(_slot_ts(b + 1, ctx)).isoformat()}
               for a, b in merge_slots(slots)]
    prices = list(prices_ct)
    return {
        "windows": windows,
        "energy_kwh": round(energy_wh / 1000.0, 2) if energy_wh else None,
        "price_ct": round(sum(prices) / len(prices), 1) if prices else None,
        "price_ct_min": round(min(prices), 1) if prices else None,
        "price_ct_max": round(max(prices), 1) if prices else None,
    }


def clip(text: str, limit: int = 250) -> str:
    """HA refuses a state longer than 255 characters; a sensor state must stay under it."""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---- calibration -------------------------------------------------------------------------

def _drift_text(p, loc: Locale) -> str:
    bits = []
    if p.worst_pts is not None:
        bits.append(f"drift {_fmt(p.worst_pts, 1, loc)} pts")
    if p.worst_days is not None:
        bits.append(f"{_fmt(p.worst_days, 1, loc)} d since full")
    return ", ".join(bits)


def calibration_text(p, ctx: Ctx) -> str:
    """One sentence for a soc_drift.CalibrationPlan."""
    loc = ctx.loc
    why = _drift_text(p, loc)
    why_overdue = f" Overdue ({why})." if why else " Overdue."
    if p.status == "off":
        return "Calibration is off"
    if p.status == "ok":
        if p.worst_pts is None or p.limit_pts is None:
            return "No calibration needed"
        return (f"No calibration needed: worst drift {_fmt(p.worst_pts, 1, loc)} "
                f"of {_g(p.limit_pts, loc)} pts")
    if p.status == "due":
        if p.detail == "grid_off":
            tail = "grid top-up is off"
        elif p.detail == "no_prices":
            tail = "no prices available for a grid top-up"
        else:
            tail = (f"grid top-up from {_g(p.grid_from_pts, loc)} pts or "
                    f"{_g(p.max_days, loc)} d")
        return f"Calibration due ({why}). Charging to 100 % from PV; {tail}."

    # a grid top-up is planned (grid_waiting) or running (grid_charging)
    what = kwh_text(p.energy_wh, loc)
    price = price_text(p.slot_prices_ct, loc)
    price = f" (@ {price})" if price else ""
    if p.status == "grid_waiting":
        return f"Calibration top-up: {what} {windows_text(p.slots, ctx)}{price}.{why_overdue}"
    running = (f"Calibration top-up running: {what} "
               f"{windows_text(p.slots, ctx, approx_end=p.detail in ('margin', 'pays'))}{price}.")
    later = clock(_slot_ts(p.later_idx, ctx), ctx) if p.later_idx is not None else None
    if p.detail == "margin" and later:
        running += f" Not waiting for {later}: only {_fmt(p.later_gain_ct, 1, loc)} ct cheaper."
    elif p.detail == "pays" and later:
        running += (f" Not waiting for {later}: prices up to "
                    f"{ct_text(p.dearest_between_ct, loc)} come first.")
    return running + why_overdue


# ---- arbitrage ---------------------------------------------------------------------------

def arbitrage_text(p, ctx: Ctx, lift_gain_ct: float | None = None) -> str:
    """One sentence for an arbitrage.ArbitragePlan.

    `lift_gain_ct` is set when the plan charges above the usual Max Charge SOC.
    """
    loc = ctx.loc
    if p.kind == "idle":
        return "No plan: prices or forecast missing"
    if p.kind == "no_deficit":
        return "No top-up needed: PV and stored energy cover the forecast"
    if p.kind == "no_window":
        if p.breakeven_ct is None:
            return "No top-up worth it: buying ahead would not pay"
        return (f"No top-up worth it: no shortfall ahead is dearer than "
                f"{ct_text(p.breakeven_ct, loc)}, the break-even for the cheapest slot")
    if p.kind in ("waiting", "charging") and p.buy_slots:
        what = kwh_text(sum(p.buy_slot_wh), loc)
        price = price_text(p.buy_slot_prices_ct, loc, p.buy_slot_wh)
        head = "Arbitrage top-up running" if p.kind == "charging" else "Arbitrage top-up planned"
        text = f"{head}: {what} {windows_text(p.buy_slots, ctx)} (@ {price})"
        if p.next_need_idx is not None and p.next_need_price_ct is not None:
            text += (f" to avoid buying at {ct_text(p.next_need_price_ct, loc)} "
                     f"from {clock(_slot_ts(p.next_need_idx, ctx), ctx)}")
        text += "."
        if lift_gain_ct is not None:
            text += f" Above the usual charge cap: saves ~{_fmt(lift_gain_ct, 1, loc)} ct more."
        return text
    return "No top-up planned"
