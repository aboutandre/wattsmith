"""Learned house-load baseline for the adaptive PV charging gate.

The adaptive gate needs an estimate of how many Wh the house will consume
between now and sunset. A fixed constant ignores daily AND weekly patterns:
Monday morning looks nothing like Saturday morning.

This module learns a per-(weekday, hour) baseline from observed
`sensor.house_consumption_power` samples over a rolling 90-day window.
Until enough data is accumulated for a given slot, the configured fallback
constant is used instead.

Key: (weekday 0-6, hour 0-23) → 168 possible slots.
Each slot typically accumulates ~13 samples per week (12 per hour-occurrence),
reaching the MIN_SAMPLES threshold within the first occurrence (~25 min).
After 13 weeks the average stabilises to within a few percent.

Persistence: the manager serialises the buffer via to_dict()/from_dict() into
HA's .storage/ directory so learning survives restarts.

Design constraints:
  - Pure Python (no HA imports) → unit-testable without Home Assistant.
  - In-memory deque backed by optional persistence (managed by manager.py).
  - One sample per SAMPLE_INTERVAL_S (5 min) keeps the deque bounded.
  - MIN_SAMPLES per slot before a learned value is trusted.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Final

# Sampling cadence — one reading per 5 min.
SAMPLE_INTERVAL_S: Final[float] = 300.0

# 90-day rolling window at 5-min cadence: 90 × 24 × 12 = 25 920 samples.
_MAX_SAMPLES: Final[int] = 90 * 24 * 12  # 25 920

# Rebuild the per-slot average cache at most once per hour.
_CACHE_TTL_S: Final[float] = 3600.0

# Slot tuple type alias.
_Slot = tuple[int, int]  # (weekday, hour)


class BaselineLearner:
    """Rolling per-(weekday, hour-of-day) house-load learner.

    Usage (in the manager tick):
        learner.observe(consumption_w)                     # every tick; debounced
        b = learner.baseline_for_slot(wd, hr, fallback_w) # in _eval_adaptive
    """

    def __init__(self, min_samples: int = 5) -> None:
        # (wall_time_s, weekday, hour_of_day, consumption_w)
        self._buf: deque[tuple[float, int, int, float]] = deque(maxlen=_MAX_SAMPLES)
        self._min_samples = min_samples
        self._last_sample_at: float | None = None
        self._cache: dict[_Slot, float] = {}
        self._cache_at: float = 0.0

    # ── public API ─────────────────────────────────────────────────────────

    def observe(
        self,
        consumption_w: float,
        *,
        _now: float | None = None,
        _weekday: int | None = None,
        _hour: int | None = None,
    ) -> None:
        """Record a house-consumption sample (ignores negatives; debounced)."""
        if consumption_w is None or consumption_w < 0:
            return
        now = _now if _now is not None else time.time()
        if self._last_sample_at is not None and now - self._last_sample_at < SAMPLE_INTERVAL_S:
            return
        dt = datetime.fromtimestamp(now)
        weekday = _weekday if _weekday is not None else dt.weekday()
        hour = _hour if _hour is not None else dt.hour
        self._buf.append((now, weekday, hour, consumption_w))
        self._last_sample_at = now
        if now - self._cache_at >= _CACHE_TTL_S:
            self._rebuild_cache()

    def baseline_for_slot(self, weekday: int, hour: int, fallback_w: float) -> float:
        """Return the learned baseline for (weekday, hour), or fallback if not yet known."""
        return self._cache.get((weekday, hour), fallback_w)

    def baseline_now(self, fallback_w: float) -> float:
        """Convenience: learned value for the current local (weekday, hour)."""
        now = datetime.now()
        return self.baseline_for_slot(now.weekday(), now.hour, fallback_w)

    @property
    def learned_slots_count(self) -> int:
        """How many (weekday, hour) slots have enough data to be trusted (max 168)."""
        return len(self._cache)

    @property
    def sample_count(self) -> int:
        return len(self._buf)

    # ── persistence ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise the raw sample buffer for HA storage."""
        return {
            "version": 1,
            "samples": [list(entry) for entry in self._buf],
        }

    @classmethod
    def from_dict(cls, data: dict, min_samples: int = 5) -> "BaselineLearner":
        """Reconstruct a learner from a previously serialised dict."""
        learner = cls(min_samples=min_samples)
        if not isinstance(data, dict) or data.get("version") != 1:
            return learner
        for entry in data.get("samples", []):
            try:
                ts, wd, hr, w = entry
                ts, w = float(ts), float(w)
                wd, hr = int(wd), int(hr)
                if not (0 <= wd <= 6 and 0 <= hr <= 23 and w >= 0):
                    continue
                learner._buf.append((ts, wd, hr, w))
                if learner._last_sample_at is None or ts > learner._last_sample_at:
                    learner._last_sample_at = ts
            except (TypeError, ValueError):
                continue
        learner._rebuild_cache()
        return learner

    # ── internals ──────────────────────────────────────────────────────────

    def _rebuild_cache(self) -> None:
        by_slot: dict[_Slot, list[float]] = {}
        for _, wd, hr, w in self._buf:
            by_slot.setdefault((wd, hr), []).append(w)
        self._cache = {
            slot: sum(vals) / len(vals)
            for slot, vals in by_slot.items()
            if len(vals) >= self._min_samples
        }
        self._cache_at = time.time()
