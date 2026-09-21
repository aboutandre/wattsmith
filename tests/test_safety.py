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
    assert set(st) == {
        "mode", "grid_fresh", "bad_cycles", "battery_fails", "battery_faults",
    }


def test_status_reports_only_currently_failing_batteries():
    s = SafetySupervisor(SafetyConfig(battery_fail_threshold=3))
    s.record_grid(True, now=0)
    s.record_battery("F9", ok=True, now=0)
    s.record_battery("F11", ok=False, now=100, error="timeout")
    st = s.status(now=160)
    # healthy batteries stay in the flat counter but out of the fault detail
    assert st["battery_fails"] == {"F9": 0, "F11": 1}
    assert set(st["battery_faults"]) == {"F11"}
    assert st["battery_faults"]["F11"][safety.SOURCE_READ] == {
        "fails": 1, "failing_for_s": 60.0, "excluded": False, "last_error": "timeout",
    }


def test_fault_detail_tracks_streak_start_and_clears_on_recovery():
    s = SafetySupervisor(SafetyConfig(battery_fail_threshold=2))
    for t in (10, 20, 30):
        s.record_battery("F11", ok=False, now=t, error=f"boom@{t}")
    fault = s.battery_fault("F11")
    assert fault.fails == 3
    assert fault.first_fail_ts == 10        # streak start, not the latest failure
    assert fault.last_fail_ts == 30
    assert fault.last_error == "boom@30"    # most recent detail wins
    assert fault.excluded is True
    assert fault.failing_for_s(now=70) == 60.0

    s.record_battery("F11", ok=True, now=80)
    assert s.battery_fault("F11") is None   # streak state is dropped wholesale
    assert s.status(now=80)["battery_faults"] == {}


def test_record_battery_still_works_without_clock_or_error():
    """The pure state machine must stay callable with the old 2-arg signature."""
    s = SafetySupervisor(SafetyConfig(battery_fail_threshold=2))
    s.record_battery("F9", ok=False)
    s.record_battery("F9", ok=False)
    assert not s.battery_healthy("F9")
    detail = s.status(now=0)["battery_faults"]["F9"][safety.SOURCE_READ]
    assert detail["failing_for_s"] is None


def test_read_success_does_not_wipe_the_ack_failure_streak():
    """The two failure modes must not share a counter.

    A battery that answers reads every tick but never acks a setpoint used to have
    its ack streak reset by each read, so it could never accumulate — the failure
    was invisible. Tracked per source, the ack streak now survives.
    """
    s = SafetySupervisor(SafetyConfig(battery_fail_threshold=3))
    for t in range(0, 30, 3):
        s.record_battery("F9", ok=True, now=t)                       # reads fine
        s.record_battery("F9", ok=False, now=t, error="no ack",
                         source=safety.SOURCE_ACK)                    # ignores setpoints
    ack = s.battery_fault("F9", source=safety.SOURCE_ACK)
    assert ack.fails == 10
    assert ack.first_fail_ts == 0
    assert s.battery_fault("F9") is None       # the read path is genuinely healthy
    # Reported, but dispatch is still gated on the read path only (unchanged behaviour)
    assert s.battery_healthy("F9")
    assert ack.excluded is False
    assert set(s.status(now=30)["battery_faults"]["F9"]) == {safety.SOURCE_ACK}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} safety tests passed ✓")
