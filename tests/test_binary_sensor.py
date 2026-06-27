"""Unit tests for the config-conflict check (pure, no HA needed).

Run directly:   python3 tests/test_binary_sensor.py
"""
import importlib.util
import sys
from pathlib import Path

# Load only the pure helper from binary_sensor.py without importing Home
# Assistant. The module imports HA at top level, so we read + exec just the
# function in an isolated namespace.
_src = (Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"
        / "binary_sensor.py").read_text()
_start = _src.index("def is_reserve_conflict")
_end = _src.index("async def async_setup_entry")
_ns: dict = {}
exec(compile(_src[_start:_end], "binary_sensor_helper", "exec"), _ns)
is_reserve_conflict = _ns["is_reserve_conflict"]


def test_reserve_above_max_is_conflict():
    # The live case: Reserve 80 > Max Charge 75 -> deadlock.
    assert is_reserve_conflict(80.0, 75.0) is True


def test_reserve_below_max_is_ok():
    assert is_reserve_conflict(70.0, 75.0) is False


def test_reserve_equal_max_is_ok():
    # Fleet reaches exactly the reserve, then the cascade moves to the car.
    assert is_reserve_conflict(75.0, 75.0) is False


def test_defaults_80_vs_100_ok():
    # Out-of-the-box defaults (reserve 80, max 100) must not flag.
    assert is_reserve_conflict(80.0, 100.0) is False


def test_just_above_flags():
    assert is_reserve_conflict(75.1, 75.0) is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} binary_sensor tests passed ✓")
