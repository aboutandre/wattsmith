"""Unit tests for the adaptive PV baseline learner (pure, no HA needed).

Run directly:   python3 tests/test_baseline_learner.py
Or with pytest: pytest tests/test_baseline_learner.py
"""
import importlib.util
import sys
from pathlib import Path

_path = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "wattsmith"
    / "baseline_learner.py"
)
_spec = importlib.util.spec_from_file_location("baseline_learner", _path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["baseline_learner"] = _mod
_spec.loader.exec_module(_mod)

BaselineLearner = _mod.BaselineLearner
SAMPLE_INTERVAL_S = _mod.SAMPLE_INTERVAL_S

FALLBACK = 500.0


def _make_samples(learner, hour, watts, *, count, t0=0.0):
    """Inject `count` synthetic samples for `hour`, spaced SAMPLE_INTERVAL_S apart."""
    for i in range(count):
        learner.observe(watts, _now=t0 + i * SAMPLE_INTERVAL_S, _hour=hour)
    return t0 + count * SAMPLE_INTERVAL_S


# ══════════════════════════════════════════════════════════════════════════════
# Empty / insufficient data
# ══════════════════════════════════════════════════════════════════════════════

def test_empty_learner_returns_fallback():
    lrn = BaselineLearner(min_samples=5)
    assert lrn.baseline_for_hour(14, FALLBACK) == FALLBACK


def test_insufficient_samples_returns_fallback():
    lrn = BaselineLearner(min_samples=5)
    # 4 samples < min_samples=5 → fallback
    _make_samples(lrn, hour=10, watts=400.0, count=4, t0=0.0)
    lrn._rebuild_cache()
    assert lrn.baseline_for_hour(10, FALLBACK) == FALLBACK


def test_sufficient_samples_returns_learned_value():
    lrn = BaselineLearner(min_samples=5)
    _make_samples(lrn, hour=10, watts=400.0, count=5, t0=0.0)
    lrn._rebuild_cache()
    result = lrn.baseline_for_hour(10, FALLBACK)
    assert abs(result - 400.0) < 0.1


# ══════════════════════════════════════════════════════════════════════════════
# Averaging
# ══════════════════════════════════════════════════════════════════════════════

def test_averages_multiple_values():
    lrn = BaselineLearner(min_samples=2)
    t0 = 0.0
    t0 = _make_samples(lrn, hour=8, watts=300.0, count=2, t0=t0)
    t0 = _make_samples(lrn, hour=8, watts=700.0, count=2, t0=t0)
    lrn._rebuild_cache()
    # Expected average: (300*2 + 700*2) / 4 = 500
    assert abs(lrn.baseline_for_hour(8, FALLBACK) - 500.0) < 0.1


def test_different_hours_learned_independently():
    lrn = BaselineLearner(min_samples=3)
    t0 = 0.0
    t0 = _make_samples(lrn, hour=12, watts=350.0, count=3, t0=t0)
    t0 = _make_samples(lrn, hour=19, watts=750.0, count=3, t0=t0)
    lrn._rebuild_cache()
    assert abs(lrn.baseline_for_hour(12, FALLBACK) - 350.0) < 0.1
    assert abs(lrn.baseline_for_hour(19, FALLBACK) - 750.0) < 0.1
    # Hour 6 has no samples → fallback
    assert lrn.baseline_for_hour(6, FALLBACK) == FALLBACK


# ══════════════════════════════════════════════════════════════════════════════
# learned_hours_count
# ══════════════════════════════════════════════════════════════════════════════

def test_learned_hours_count_zero_initially():
    lrn = BaselineLearner(min_samples=5)
    assert lrn.learned_hours_count == 0


def test_learned_hours_count_increases_with_data():
    lrn = BaselineLearner(min_samples=3)
    t0 = 0.0
    t0 = _make_samples(lrn, hour=6, watts=200.0, count=3, t0=t0)
    t0 = _make_samples(lrn, hour=12, watts=400.0, count=3, t0=t0)
    lrn._rebuild_cache()
    assert lrn.learned_hours_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# Debouncing
# ══════════════════════════════════════════════════════════════════════════════

def test_debounce_drops_rapid_observations():
    lrn = BaselineLearner(min_samples=1)
    # Send 5 observations all at the same timestamp → only 1 stored
    for _ in range(5):
        lrn.observe(300.0, _now=1000.0, _hour=9)
    assert lrn.sample_count == 1


def test_debounce_accepts_after_interval():
    lrn = BaselineLearner(min_samples=2)
    lrn.observe(300.0, _now=0.0, _hour=9)
    lrn.observe(400.0, _now=SAMPLE_INTERVAL_S, _hour=9)  # exactly at interval → accepted
    assert lrn.sample_count == 2


def test_debounce_drops_before_interval():
    lrn = BaselineLearner(min_samples=2)
    lrn.observe(300.0, _now=0.0, _hour=9)
    lrn.observe(400.0, _now=SAMPLE_INTERVAL_S - 1, _hour=9)  # 1 s short → dropped
    assert lrn.sample_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════════════

def test_negative_consumption_ignored():
    lrn = BaselineLearner(min_samples=1)
    lrn.observe(-100.0, _now=0.0, _hour=10)
    assert lrn.sample_count == 0


def test_zero_consumption_accepted():
    lrn = BaselineLearner(min_samples=1)
    lrn.observe(0.0, _now=0.0, _hour=10)
    lrn._rebuild_cache()
    assert abs(lrn.baseline_for_hour(10, FALLBACK) - 0.0) < 0.1


def test_fallback_independent_per_call():
    lrn = BaselineLearner(min_samples=5)
    # No data → each call can supply a different fallback
    assert lrn.baseline_for_hour(10, 400.0) == 400.0
    assert lrn.baseline_for_hour(10, 600.0) == 600.0


def test_sample_count_property():
    lrn = BaselineLearner(min_samples=1)
    assert lrn.sample_count == 0
    _make_samples(lrn, hour=14, watts=500.0, count=3)
    assert lrn.sample_count == 3


def test_maxlen_bounds_buffer():
    """Buffer should not grow beyond _MAX_SAMPLES (2016)."""
    lrn = BaselineLearner(min_samples=1)
    # Insert more than _MAX_SAMPLES entries across different hours
    for i in range(3000):
        lrn.observe(500.0, _now=float(i) * SAMPLE_INTERVAL_S, _hour=i % 24)
    assert lrn.sample_count <= _mod._MAX_SAMPLES


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} baseline_learner tests passed ✓")
