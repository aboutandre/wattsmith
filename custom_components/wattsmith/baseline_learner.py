"""Learned house-load baseline for the adaptive PV charging gate.

The adaptive gate needs an estimate of how many Wh the house will consume
between now and sunset so it can decide whether to open the battery ceiling.
A fixed constant (the manual `adaptive_baseline_load` setting) works but
it ignores daily patterns: the house is lighter at midday than at 18:00.

This module learns a per-hour-of-day baseline from observed
`sensor.house_consumption_power` samples, using a rolling 7-day window.
Until enough data is accumulated for a given hour, the configured fallback
constant is used instead.

Design constraints:
  - Pure Python (no HA imports) → unit-testable without Home Assistant.
  - In-memory only — resets on HA restart and re-learns within hours.
  - One sample per SAMPLE_INTERVAL_S (5 min) keeps the deque bounded.
  - MIN_SAMPLES per hour-of-day before a learned value is trusted.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Final

# Sampling cadence — one reading per 5 min keeps the 7-day buffer to ~2 k samples.
SAMPLE_INTERVAL_S: Final[float] = 300.0

# 7 days × 24 h × 12 samples/h — the deque auto-evicts beyond this.
_MAX_SAMPLES: Final[int] = 7 * 24 * 12  # 2 016

# How often to rebuild the per-hour cache from the raw buffer.
_CACHE_TTL_S: Final[float] = 3600.0


class BaselineLearner:
    """Rolling per-hour-of-day house-load learner.

    Usage (in the manager tick):
        learner.observe(consumption_w)          # every tick; debounced internally
        b = learner.baseline_for_hour(now_hour, fallback_w)  # in _eval_adaptive
    """

    def __init__(self, min_samples: int = 5) -> None:
        # (wall_time_s, hour_of_day, consumption_w)
        self._buf: deque[tuple[float, int, float]] = deque(maxlen=_MAX_SAMPLES)
        self._min_samples = min_samples
        self._last_sample_at: float | None = None
        self._cache: dict[int, float] = {}     # hour → learned W
        self._cache_at: float = 0.0

    # ── public API ─────────────────────────────────────────────────────────

    def observe(
        self,
        consumption_w: float,
        *,
        _now: float | None = None,   # injectable for tests
        _hour: int | None = None,    # injectable for tests
    ) -> None:
        """Record a house-consumption sample (ignores negatives; debounced)."""
        if consumption_w is None or consumption_w < 0:
            return
        now = _now if _now is not None else time.time()
        if self._last_sample_at is not None and now - self._last_sample_at < SAMPLE_INTERVAL_S:
            return  # debounce: one sample per SAMPLE_INTERVAL_S
        hour = _hour if _hour is not None else datetime.fromtimestamp(now).hour
        self._buf.append((now, hour, consumption_w))
        self._last_sample_at = now
        # Rebuild cache once the TTL has expired.
        if now - self._cache_at >= _CACHE_TTL_S:
            self._rebuild_cache()

    def baseline_for_hour(self, hour: int, fallback_w: float) -> float:
        """Return the learned baseline for `hour`, or `fallback_w` if not yet known."""
        return self._cache.get(hour, fallback_w)

    @property
    def learned_hours_count(self) -> int:
        """How many hour-of-day slots have enough data to be trusted."""
        return len(self._cache)

    @property
    def sample_count(self) -> int:
        return len(self._buf)

    def learned_baseline_now(self, fallback_w: float) -> float:
        """Convenience: learned value for the current local hour."""
        return self.baseline_for_hour(datetime.now().hour, fallback_w)

    # ── internals ──────────────────────────────────────────────────────────

    def _rebuild_cache(self) -> None:
        by_hour: dict[int, list[float]] = {}
        for _, h, w in self._buf:
            by_hour.setdefault(h, []).append(w)
        self._cache = {
            h: sum(vals) / len(vals)
            for h, vals in by_hour.items()
            if len(vals) >= self._min_samples
        }
        self._cache_at = time.time()
