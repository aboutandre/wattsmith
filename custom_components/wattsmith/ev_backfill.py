"""Backfill plan for the EV Energy sensor's long-term statistics.

The EV Energy sensor (the charger's lifetime meter, `eto` on a go-e) only gets
HA long-term statistics from the hour it first appears. Two older sources can
fill in the time before that:

  1. an older meter statistic of the SAME counter, e.g. `sensor.goe_318468_eto`
     left behind by a removed charger integration. Exact, hourly.
  2. Wattsmith's own history DB, which has logged EV energy (`ev_wh`, the
     integral of the charger's power reading) every 15 minutes.

The plan is pure maths (no Home Assistant) so it can be unit-tested:

  - B  = the meter reading when the sensor's own statistics start
         (first row's state - its sum; the recorder starts `sum` at 0).
  - M0 = the oldest meter reading we can reconstruct. After the backfill,
         every row's sum = meter reading - M0, so the Energy dashboard sees one
         continuous counter with no seam.
  - Meter rows are taken as they are. Between the last meter row and B, the
    meter delta is spread over the hours in proportion to Wattsmith's ev_wh,
    so the totals are the meter's and only the hour-by-hour shape comes from
    the power integral. `scale` (meter delta / ev_wh total) says how well the
    two agree: ~1.0 means the 15-min log matches the charger's meter.
  - Without a meter statistic the ev_wh values are used as they are (scale 1).

Running it twice is a no-op: after the first run the earliest row IS the
oldest reading, so there is nothing before it to add and the shift is 0.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

HOUR = 3600
# a meter that moved more than this past B is not the same counter as the sensor
_MATCH_TOLERANCE_KWH = 0.05


@dataclass(frozen=True)
class StatRow:
    """One hourly long-term statistics row (kWh)."""

    start: int          # hour start, UTC epoch seconds
    state: float        # meter reading at the end of the hour
    sum: float          # recorder running sum


@dataclass
class BackfillPlan:
    rows: list[StatRow] = field(default_factory=list)       # new history rows
    shifted: list[StatRow] = field(default_factory=list)    # existing rows, re-summed
    shift: float = 0.0          # added to every existing row's sum
    oldest_reading: float | None = None     # M0
    sensor_start_reading: float | None = None   # B
    scale: float | None = None  # meter delta / Wattsmith ev_wh over the same span
    meter_used: bool = False
    meter_rejected: str | None = None
    wattsmith_kwh: float = 0.0  # raw ev_wh total over the spread span
    notes: list[str] = field(default_factory=list)

    @property
    def backfilled_kwh(self) -> float:
        if not self.rows or self.oldest_reading is None:
            return 0.0
        return round(self.rows[-1].state - self.oldest_reading, 3)

    def monthly_kwh(self, tz=timezone.utc) -> dict[str, float]:
        """kWh per calendar month (in `tz`) of the backfilled rows."""
        out: dict[str, float] = defaultdict(float)
        prev = self.oldest_reading
        for r in self.rows:
            key = datetime.fromtimestamp(r.start, tz).strftime("%Y-%m")
            out[key] += r.state - (prev if prev is not None else r.state)
            prev = r.state
        return {k: round(v, 2) for k, v in sorted(out.items())}

    def summary(self, tz=timezone.utc) -> dict:
        def iso(ts: int | None) -> str | None:
            return datetime.fromtimestamp(ts, tz).isoformat() if ts is not None else None

        return {
            "history_rows": len(self.rows),
            "existing_rows_shifted": len(self.shifted),
            "backfill_start": iso(self.rows[0].start) if self.rows else None,
            "backfill_end": iso(self.rows[-1].start + HOUR) if self.rows else None,
            "backfilled_kwh": self.backfilled_kwh,
            "monthly_kwh": self.monthly_kwh(tz),
            "oldest_meter_reading_kwh": self.oldest_reading,
            "sensor_start_reading_kwh": self.sensor_start_reading,
            "shift_kwh": round(self.shift, 3),
            "meter_used": self.meter_used,
            "meter_rejected": self.meter_rejected,
            "wattsmith_kwh": round(self.wattsmith_kwh, 3),
            "scale": round(self.scale, 4) if self.scale is not None else None,
            "notes": self.notes,
        }


def plan_backfill(
    existing: list[StatRow],
    meter: list[StatRow],
    wattsmith_hours: list[tuple[int, float]],
) -> BackfillPlan:
    """Build the backfill for `existing` (the EV Energy sensor's own rows).

    `meter`: an older statistic of the same counter, kWh (may be empty).
    `wattsmith_hours`: [(hour_start, Wh)] from the history DB.
    Raises ValueError when the sensor has no statistics yet.
    """
    if not existing:
        raise ValueError(
            "the EV Energy sensor has no long-term statistics yet; wait until it "
            "has been recording for at least one full hour"
        )
    existing = sorted(existing, key=lambda r: r.start)
    first = existing[0]
    plan = BackfillPlan()
    b = first.state - first.sum
    plan.sensor_start_reading = round(b, 3)

    meter = sorted((r for r in meter if r.start < first.start), key=lambda r: r.start)
    if meter and meter[-1].state > b + _MATCH_TOLERANCE_KWH:
        plan.meter_rejected = (
            f"last reading {meter[-1].state:.3f} kWh is above the sensor's first "
            f"reading {b:.3f} kWh, so it is not the same counter"
        )
        meter = []
    elif meter and meter[0].state <= 0 and meter[-1].state <= 0:
        plan.meter_rejected = "meter statistic holds no readings"
        meter = []

    rows: list[StatRow] = []
    if meter:
        plan.meter_used = True
        m0 = meter[0].state
        prev = m0
        for r in meter[1:]:
            reading = max(r.state, prev)    # a dip is noise or a reset: never count backwards
            if reading > prev:
                rows.append(StatRow(r.start, round(reading, 4), round(reading - m0, 4)))
            prev = reading
        spread_from = meter[-1].start + HOUR
        gap_kwh = b - prev
    else:
        m0 = None
        spread_from = None
        gap_kwh = None

    hours = sorted(
        (h, wh / 1000.0)
        for h, wh in wattsmith_hours
        if wh and wh > 0 and h < first.start and (spread_from is None or h >= spread_from)
    )
    plan.wattsmith_kwh = sum(kwh for _, kwh in hours)

    if gap_kwh is None:
        # no meter: take the 15-min log as it is
        scale = 1.0
        m0 = b - plan.wattsmith_kwh
        prev = m0
    else:
        if gap_kwh < -_MATCH_TOLERANCE_KWH:
            plan.notes.append(f"meter ends {-gap_kwh:.3f} kWh above the sensor start; gap ignored")
        gap_kwh = max(gap_kwh, 0.0)
        if plan.wattsmith_kwh > 0:
            scale = gap_kwh / plan.wattsmith_kwh
        elif gap_kwh > _MATCH_TOLERANCE_KWH:
            # charged while nothing was logging: book it in the first gap hour
            scale = None
            hours = []
            reading = prev + gap_kwh
            rows.append(StatRow(spread_from, round(reading, 4), round(reading - m0, 4)))
            prev = reading
            plan.notes.append(
                f"{gap_kwh:.3f} kWh between the meter statistic and the sensor had "
                "no 15-min log; booked in one hour"
            )
        else:
            scale = None
            hours = []
    plan.scale = scale

    for h, kwh in hours:
        prev += kwh * (scale or 0.0)
        rows.append(StatRow(h, round(prev, 4), round(prev - m0, 4)))

    if rows and meter:
        # land exactly on B despite float rounding
        last = rows[-1]
        rows[-1] = StatRow(last.start, round(b, 4), round(b - m0, 4))

    plan.oldest_reading = round(m0, 4)
    plan.rows = rows
    plan.shift = b - m0
    if abs(plan.shift) > 1e-6:
        plan.shifted = [StatRow(r.start, r.state, round(r.sum + plan.shift, 4)) for r in existing]
    return plan
