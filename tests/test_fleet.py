"""Unit tests for fleet.py — which batteries count toward the fleet.

Run directly:   python3 tests/test_fleet.py
Or with pytest: pytest tests/test_fleet.py
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_path = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "fleet.py"
_spec = importlib.util.spec_from_file_location("wattsmith_fleet", _path)
_fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fleet)
ActiveTracker = _fleet.ActiveTracker


def _st(bid, available=True):
    return SimpleNamespace(battery_id=bid, available=available)


def test_available_batteries_are_active():
    t = ActiveTracker(grace_s=900)
    assert t.update([_st("a"), _st("b")], 0.0) == {"a", "b"}


def test_a_short_dropout_keeps_the_battery():
    t = ActiveTracker(grace_s=900)
    t.update([_st("a"), _st("b")], 0.0)
    assert t.update([_st("a"), _st("b", False)], 600.0) == {"a", "b"}


def test_a_long_dropout_drops_the_battery_and_it_returns_when_back():
    t = ActiveTracker(grace_s=900)
    t.update([_st("a"), _st("b")], 0.0)
    assert t.update([_st("a"), _st("b", False)], 901.0) == {"a"}
    assert t.update([_st("a"), _st("b")], 1000.0) == {"a", "b"}


def test_a_battery_never_seen_available_does_not_count():
    t = ActiveTracker(grace_s=900)
    assert t.update([_st("a"), _st("b", False)], 0.0) == {"a"}


def test_a_battery_that_left_discovery_is_forgotten():
    t = ActiveTracker(grace_s=900)
    t.update([_st("a"), _st("b")], 0.0)
    assert t.update([_st("a")], 10.0) == {"a"}


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
            n += 1
    print(f"\n{n} fleet tests passed ✓")
