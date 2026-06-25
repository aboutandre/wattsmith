"""Unit tests for the zero-grid controller (pure logic, no hardware/HA needed).

Run directly:   python3 tests/test_controller.py
Or with pytest: pytest tests/test_controller.py
"""
import importlib.util
import sys
from pathlib import Path

# Load controller.py directly (avoids importing the full HA integration package).
# Register in sys.modules so @dataclass can resolve the module namespace.
_path = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / "controller.py"
_spec = importlib.util.spec_from_file_location("controller", _path)
controller = importlib.util.module_from_spec(_spec)
sys.modules["controller"] = controller
_spec.loader.exec_module(controller)

ZeroGridController = controller.ZeroGridController
ControllerConfig = controller.ControllerConfig
BatteryState = controller.BatteryState


def _bats(socs, min_soc=11):
    return [BatteryState(id=f"b{i}", soc=s, min_soc=min_soc) for i, s in enumerate(socs)]


def test_discharge_split_favors_high_soc():
    # max_step_w high so the single step isn't ramp-limited (this tests the SPLIT).
    c = ZeroGridController(ControllerConfig(target_grid_w=0, kp=1.0, kd=0.0,
                                            deadband_w=0, max_step_w=5000))
    sp = c.update(grid_power=1500, batteries=_bats([90, 50, 20]))
    assert sp["b0"] > sp["b1"] > sp["b2"]
    assert abs(sum(sp.values()) - 1500) < 5


def test_charge_split_favors_low_soc():
    c = ZeroGridController(ControllerConfig(target_grid_w=0, kp=1.0, kd=0.0,
                                            deadband_w=0, max_step_w=5000))
    sp = c.update(grid_power=-1500, batteries=_bats([90, 50, 20]))
    assert sp["b2"] < sp["b1"] < sp["b0"]
    assert abs(sum(sp.values()) + 1500) < 5


def test_per_battery_cap_respected():
    c = ZeroGridController(ControllerConfig(target_grid_w=0, kp=1.0, kd=0.0,
                                            deadband_w=0, max_step_w=10000))
    sp = c.update(grid_power=9000, batteries=_bats([90, 90, 90]))
    assert all(v <= 2500 for v in sp.values())
    assert sum(sp.values()) <= 7500


def test_min_soc_blocks_discharge():
    c = ZeroGridController(ControllerConfig(target_grid_w=0, kp=1.0, kd=0.0, deadband_w=0))
    sp = c.update(grid_power=1500, batteries=_bats([11, 11, 50]))
    assert sp["b0"] == 0 and sp["b1"] == 0 and sp["b2"] > 0


def test_ramp_limit():
    c = ZeroGridController(ControllerConfig(target_grid_w=0, kp=1.0, kd=0.0,
                                            deadband_w=0, max_step_w=500))
    sp = c.update(grid_power=5000, batteries=_bats([90, 90, 90]))
    # One tick is capped by the ramp limit. Allow a few W of per-battery int rounding
    # (the command itself is clamped to 500; only the split rounding can nudge it).
    assert sum(sp.values()) <= 500 + len(sp)


def test_empty_battery_list_no_crash():
    c = ZeroGridController()
    assert c.update(grid_power=1500, batteries=[]) == {}


def test_deadband_holds_without_kick():
    # Regression: entering the deadband must not produce a derivative kick.
    c = ZeroGridController()  # default target -50, kd=0.2, deadband 30
    c.update(grid_power=1500, batteries=[])          # pollutes prev_error (capacity 0)
    sp = c.update(grid_power=-50, batteries=[BatteryState("b0", 98)])  # exactly at target
    assert sp["b0"] == 0


def test_direction_hysteresis_suppresses_small_flip():
    # Establish a discharge, then a small opposite correction must be held at 0 (no flip).
    c = ZeroGridController(ControllerConfig(target_grid_w=0, kp=1.0, kd=0.0,
                                            deadband_w=0, direction_hysteresis_w=60))
    c.update(grid_power=500, batteries=_bats([90, 90, 90]))     # discharge, sign +
    sp = c.update(grid_power=-520, batteries=_bats([90, 90, 90]))  # would be ~-20 -> suppressed
    assert sum(sp.values()) == 0


def test_direction_hysteresis_allows_large_flip():
    # A large opposite correction overcomes the hysteresis band and flips.
    c = ZeroGridController(ControllerConfig(target_grid_w=0, kp=1.0, kd=0.0,
                                            deadband_w=0, direction_hysteresis_w=60))
    c.update(grid_power=500, batteries=_bats([50, 50, 50]))      # discharge, sign +
    sp = c.update(grid_power=-1000, batteries=_bats([50, 50, 50]))  # ~-500 -> allowed
    assert sum(sp.values()) < 0


def test_convergence_to_target():
    c = ZeroGridController(ControllerConfig(target_grid_w=-50, kp=0.8, kd=0.2, deadband_w=30))
    grid = 1710.0
    b = _bats([98, 97, 99])
    for _ in range(10):
        total = sum(c.update(grid_power=grid, batteries=b).values())
        grid = 1710.0 - total  # 1:1 simulated plant
    assert abs(grid - (-50)) < 60


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} controller tests passed ✓")
