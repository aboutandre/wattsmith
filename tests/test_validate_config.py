"""Unit tests for validate_config — cross-value clash detection (audit F-17).

Pure logic, no Home Assistant.

Run directly:   python3 tests/test_validate_config.py
Or with pytest: pytest tests/test_validate_config.py
"""
import importlib.util
import sys
from pathlib import Path

_path = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "wattsmith" / "validate_config.py"
)
_spec = importlib.util.spec_from_file_location("validate_config", _path)
_vc = importlib.util.module_from_spec(_spec)
sys.modules["validate_config"] = _vc  # dataclasses resolves cls.__module__ here
_spec.loader.exec_module(_vc)

ConfigSnapshot = _vc.ConfigSnapshot
check_config = _vc.check_config
effective_reserve_soc = _vc.effective_reserve_soc


def _snap(**overrides) -> "ConfigSnapshot":
    """A sane baseline config; individual tests break one relationship."""
    values = dict(
        min_soc=11.0,
        max_battery_soc=80.0,
        adaptive_enabled=True,
        adaptive_ceiling_soc=100.0,
        ev_configured=True,
        reserve_soc=80.0,
        bridge_floor_soc=50.0,
        phase_up_w=4500.0,
        phase_down_w=4140.0,
    )
    values.update(overrides)
    return ConfigSnapshot(**values)


def test_sane_config_no_warnings():
    assert check_config(_snap()) == []


def test_effective_reserve_clamps_to_cap():
    assert effective_reserve_soc(85.0, 80.0) == 80.0
    assert effective_reserve_soc(75.0, 80.0) == 75.0


def test_reserve_above_cap_warns():
    # the pre-2026-06-26 live config: reserve 80 vs cap 75 → EV starved silently
    warnings = check_config(_snap(reserve_soc=80.0, max_battery_soc=75.0))
    assert any("Reserve SOC" in w and "clamping" in w for w in warnings), warnings


def test_reserve_at_cap_no_warning():
    # equality is allowed (the planner's reserve tolerance handles the boundary)
    assert check_config(_snap(reserve_soc=80.0, max_battery_soc=80.0)) == []


def test_min_soc_at_or_above_max_warns():
    warnings = check_config(_snap(min_soc=80.0, max_battery_soc=80.0))
    assert any("Minimum SOC" in w for w in warnings), warnings


def test_adaptive_ceiling_not_above_cap_warns():
    warnings = check_config(_snap(adaptive_ceiling_soc=80.0, max_battery_soc=80.0))
    assert any("Ceiling" in w for w in warnings), warnings


def test_adaptive_disabled_ceiling_ignored():
    assert check_config(_snap(adaptive_enabled=False, adaptive_ceiling_soc=70.0)) == []


def test_bridge_floor_at_reserve_warns():
    warnings = check_config(_snap(bridge_floor_soc=80.0))
    assert any("Bridge Floor" in w for w in warnings), warnings


def test_bridge_floor_checked_against_effective_reserve():
    # reserve 90 clamped to cap 80 → floor 85 is above the EFFECTIVE reserve
    warnings = check_config(_snap(reserve_soc=90.0, bridge_floor_soc=85.0))
    assert any("Bridge Floor" in w for w in warnings), warnings


def test_phase_thresholds_inverted_warns():
    warnings = check_config(_snap(phase_up_w=4000.0, phase_down_w=4140.0))
    assert any("Phase Up" in w for w in warnings), warnings


def test_ev_checks_skipped_without_wallbox():
    # EV-related clashes are irrelevant when no wallbox/EV sensor is configured
    warnings = check_config(_snap(
        ev_configured=False, reserve_soc=95.0, bridge_floor_soc=95.0,
        phase_up_w=1000.0, phase_down_w=4140.0,
    ))
    assert warnings == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} validate_config tests passed ✓")
