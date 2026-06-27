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
MON, TUE, SAT, SUN = 0, 1, 5, 6


def _make_samples(learner, weekday, hour, watts, *, count, t0=0.0):
    """Inject `count` synthetic samples for (weekday, hour), spaced SAMPLE_INTERVAL_S apart."""
    for i in range(count):
        learner.observe(watts, _now=t0 + i * SAMPLE_INTERVAL_S, _weekday=weekday, _hour=hour)
    return t0 + count * SAMPLE_INTERVAL_S


# ══════════════════════════════════════════════════════════════════════════════
# Empty / insufficient data
# ══════════════════════════════════════════════════════════════════════════════

def test_empty_learner_returns_fallback():
    lrn = BaselineLearner(min_samples=5)
    assert lrn.baseline_for_slot(MON, 14, FALLBACK) == FALLBACK


def test_insufficient_samples_returns_fallback():
    lrn = BaselineLearner(min_samples=5)
    _make_samples(lrn, MON, hour=10, watts=400.0, count=4, t0=0.0)
    lrn._rebuild_cache()
    assert lrn.baseline_for_slot(MON, 10, FALLBACK) == FALLBACK


def test_sufficient_samples_returns_learned_value():
    lrn = BaselineLearner(min_samples=5)
    _make_samples(lrn, MON, hour=10, watts=400.0, count=5, t0=0.0)
    lrn._rebuild_cache()
    assert abs(lrn.baseline_for_slot(MON, 10, FALLBACK) - 400.0) < 0.1


# ══════════════════════════════════════════════════════════════════════════════
# Weekday independence
# ══════════════════════════════════════════════════════════════════════════════

def test_different_weekdays_same_hour_learned_independently():
    lrn = BaselineLearner(min_samples=3)
    t0 = 0.0
    t0 = _make_samples(lrn, MON, hour=9, watts=300.0, count=3, t0=t0)
    t0 = _make_samples(lrn, SAT, hour=9, watts=700.0, count=3, t0=t0)
    lrn._rebuild_cache()
    assert abs(lrn.baseline_for_slot(MON, 9, FALLBACK) - 300.0) < 0.1
    assert abs(lrn.baseline_for_slot(SAT, 9, FALLBACK) - 700.0) < 0.1
    # Sunday has no data → fallback
    assert lrn.baseline_for_slot(SUN, 9, FALLBACK) == FALLBACK


def test_different_hours_same_weekday_learned_independently():
    lrn = BaselineLearner(min_samples=3)
    t0 = 0.0
    t0 = _make_samples(lrn, MON, hour=8, watts=250.0, count=3, t0=t0)
    t0 = _make_samples(lrn, MON, hour=18, watts=900.0, count=3, t0=t0)
    lrn._rebuild_cache()
    assert abs(lrn.baseline_for_slot(MON, 8, FALLBACK) - 250.0) < 0.1
    assert abs(lrn.baseline_for_slot(MON, 18, FALLBACK) - 900.0) < 0.1
    assert lrn.baseline_for_slot(MON, 12, FALLBACK) == FALLBACK


# ══════════════════════════════════════════════════════════════════════════════
# Averaging
# ══════════════════════════════════════════════════════════════════════════════

def test_averages_multiple_values():
    lrn = BaselineLearner(min_samples=2)
    t0 = 0.0
    t0 = _make_samples(lrn, TUE, hour=8, watts=300.0, count=2, t0=t0)
    t0 = _make_samples(lrn, TUE, hour=8, watts=700.0, count=2, t0=t0)
    lrn._rebuild_cache()
    assert abs(lrn.baseline_for_slot(TUE, 8, FALLBACK) - 500.0) < 0.1


# ══════════════════════════════════════════════════════════════════════════════
# learned_slots_count
# ══════════════════════════════════════════════════════════════════════════════

def test_learned_slots_count_zero_initially():
    lrn = BaselineLearner(min_samples=5)
    assert lrn.learned_slots_count == 0


def test_learned_slots_count_increases_with_data():
    lrn = BaselineLearner(min_samples=3)
    t0 = 0.0
    t0 = _make_samples(lrn, MON, hour=6, watts=200.0, count=3, t0=t0)
    t0 = _make_samples(lrn, SAT, hour=12, watts=400.0, count=3, t0=t0)
    lrn._rebuild_cache()
    assert lrn.learned_slots_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# Debouncing
# ══════════════════════════════════════════════════════════════════════════════

def test_debounce_drops_rapid_observations():
    lrn = BaselineLearner(min_samples=1)
    for _ in range(5):
        lrn.observe(300.0, _now=1000.0, _weekday=MON, _hour=9)
    assert lrn.sample_count == 1


def test_debounce_accepts_after_interval():
    lrn = BaselineLearner(min_samples=2)
    lrn.observe(300.0, _now=0.0, _weekday=MON, _hour=9)
    lrn.observe(400.0, _now=SAMPLE_INTERVAL_S, _weekday=MON, _hour=9)
    assert lrn.sample_count == 2


def test_debounce_drops_before_interval():
    lrn = BaselineLearner(min_samples=2)
    lrn.observe(300.0, _now=0.0, _weekday=MON, _hour=9)
    lrn.observe(400.0, _now=SAMPLE_INTERVAL_S - 1, _weekday=MON, _hour=9)
    assert lrn.sample_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════════════

def test_negative_consumption_ignored():
    lrn = BaselineLearner(min_samples=1)
    lrn.observe(-100.0, _now=0.0, _weekday=MON, _hour=10)
    assert lrn.sample_count == 0


def test_zero_consumption_accepted():
    lrn = BaselineLearner(min_samples=1)
    lrn.observe(0.0, _now=0.0, _weekday=MON, _hour=10)
    lrn._rebuild_cache()
    assert abs(lrn.baseline_for_slot(MON, 10, FALLBACK) - 0.0) < 0.1


def test_fallback_independent_per_call():
    lrn = BaselineLearner(min_samples=5)
    assert lrn.baseline_for_slot(MON, 10, 400.0) == 400.0
    assert lrn.baseline_for_slot(MON, 10, 600.0) == 600.0


def test_sample_count_property():
    lrn = BaselineLearner(min_samples=1)
    assert lrn.sample_count == 0
    _make_samples(lrn, MON, hour=14, watts=500.0, count=3)
    assert lrn.sample_count == 3


def test_maxlen_bounds_buffer():
    lrn = BaselineLearner(min_samples=1)
    for i in range(30000):
        lrn.observe(500.0, _now=float(i) * SAMPLE_INTERVAL_S, _weekday=i % 7, _hour=i % 24)
    assert lrn.sample_count <= _mod._MAX_SAMPLES


# ══════════════════════════════════════════════════════════════════════════════
# Persistence: to_dict / from_dict
# ══════════════════════════════════════════════════════════════════════════════

def test_to_dict_contains_version_and_samples():
    lrn = BaselineLearner(min_samples=1)
    _make_samples(lrn, MON, hour=9, watts=400.0, count=3)
    d = lrn.to_dict()
    assert d["version"] == 1
    assert len(d["samples"]) == 3


def test_round_trip_preserves_learned_values():
    lrn = BaselineLearner(min_samples=3)
    t0 = 0.0
    t0 = _make_samples(lrn, MON, hour=9, watts=350.0, count=3, t0=t0)
    t0 = _make_samples(lrn, SAT, hour=14, watts=800.0, count=3, t0=t0)
    lrn._rebuild_cache()

    restored = BaselineLearner.from_dict(lrn.to_dict(), min_samples=3)
    assert abs(restored.baseline_for_slot(MON, 9, FALLBACK) - 350.0) < 0.1
    assert abs(restored.baseline_for_slot(SAT, 14, FALLBACK) - 800.0) < 0.1
    assert restored.sample_count == lrn.sample_count


def test_from_dict_empty_returns_fresh_learner():
    lrn = BaselineLearner.from_dict({}, min_samples=5)
    assert lrn.sample_count == 0
    assert lrn.learned_slots_count == 0


def test_from_dict_wrong_version_returns_fresh_learner():
    lrn = BaselineLearner.from_dict({"version": 99, "samples": [[0, 0, 9, 500]]}, min_samples=1)
    assert lrn.sample_count == 0


def test_from_dict_skips_bad_entries():
    data = {
        "version": 1,
        "samples": [
            [0.0, 0, 9, 400.0],        # valid
            "garbage",                  # invalid — wrong type
            [0.0, 9, -1, 400.0],       # invalid — hour out of range
            [1 * SAMPLE_INTERVAL_S, 0, 9, 350.0],  # valid
        ],
    }
    lrn = BaselineLearner.from_dict(data, min_samples=2)
    assert lrn.sample_count == 2


def test_from_dict_rebuilds_cache():
    lrn = BaselineLearner(min_samples=3)
    t0 = _make_samples(lrn, TUE, hour=11, watts=600.0, count=5, t0=0.0)
    restored = BaselineLearner.from_dict(lrn.to_dict(), min_samples=3)
    assert abs(restored.baseline_for_slot(TUE, 11, FALLBACK) - 600.0) < 0.1


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} baseline_learner tests passed ✓")
