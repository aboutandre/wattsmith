"""Unit tests for the safety supervisor (pure state machine, no hardware/HA needed).

Run directly:   python3 tests/test_safety.py
Or with pytest: pytest tests/test_safety.py
"""
import importlib.util
import sys
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "safety.py"
_spec = importlib.util.spec_from_file_location("safety", _path)
safety = importlib.util.module_from_spec(_spec)
sys.modules["safety"] = safety
_spec.loader.exec_module(safety)

SafetySupervisor = safety.SafetySupervisor
SafetyConfig = safety.SafetyConfig
Mode = safety.Mode


def test_grid_staleness_triggers_safe():
    s = SafetySupervisor(SafetyConfig(grid_max_age_s=10))
    s.record_grid(True, now=100)
    assert s.mode(now=105) is Mode.NORMAL
    assert s.mode(now=115) is Mode.SAFE


def test_no_grid_is_safe():
    assert SafetySupervisor().mode(now=0) is Mode.SAFE


def test_battery_excluded_after_threshold_and_recovers():
    s = SafetySupervisor(SafetyConfig(battery_fail_threshold=3))
    s.record_grid(True, now=0)
    s.record_battery("F9", ok=False)
    s.record_battery("F9", ok=False)
    assert s.battery_healthy("F9")           # 2 < 3
    s.record_battery("F9", ok=False)
    assert not s.battery_healthy("F9")        # 3 => excluded
    s.record_battery("F9", ok=True)
    assert s.battery_healthy("F9")            # recovers on success


def test_bad_cycles_trigger_safe_and_recover():
    s = SafetySupervisor(SafetyConfig(cycle_fail_threshold=3))
    s.record_grid(True, now=0)
    for _ in range(3):
        s.record_cycle(ok=False)
    assert s.mode(now=1) is Mode.SAFE
    s.record_cycle(ok=True)
    assert s.mode(now=1) is Mode.NORMAL


def test_status_snapshot_shape():
    s = SafetySupervisor()
    s.record_grid(True, now=0)
    st = s.status(now=0)
    assert set(st) == {"mode", "grid_fresh", "bad_cycles", "battery_fails"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} safety tests passed ✓")
