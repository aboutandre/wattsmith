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


# ---- grid-charge actuation: positive grid target = deliberate import ----------
def test_positive_target_charges_toward_import_cap():
    # Arbitrage grid-charge sets target_grid_w = +7500 (import). With batteries
    # below max_soc and grid near zero, the loop must command a CHARGE (negative).
    c = ZeroGridController(ControllerConfig(target_grid_w=7500, kp=1.0, kd=0.0,
                                            deadband_w=0, max_step_w=10000))
    sp = c.update(grid_power=0, batteries=_bats([30, 30, 30]))
    assert sum(sp.values()) < 0                     # charging
    assert all(v <= 0 for v in sp.values())


def test_full_fleet_never_discharges_to_chase_import_target():
    # The safety property the manager relies on: once the fleet reaches its
    # (arbitrage-capped) max_soc, charge_cap is 0, so even with a big positive
    # import target the controller must NOT discharge to "reach" it.
    c = ZeroGridController(ControllerConfig(target_grid_w=7500, kp=1.0, kd=0.0,
                                            deadband_w=0, max_step_w=10000))
    full = [BatteryState(id=f"b{i}", soc=60.0, min_soc=11, max_soc=60.0)
            for i in range(3)]
    sp = c.update(grid_power=0, batteries=full)     # grid < target -> big + error
    assert all(v == 0 for v in sp.values())         # no discharge-to-chase
    assert sum(sp.values()) == 0


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



# ── pulse hold (hel-136) ─────────────────────────────────────────────────────
def _pulse_ctrl(hold=True):
    c = ZeroGridController(ControllerConfig())
    c.config.pulse_hold = hold
    return c


def _feed(c, grids, bats, t0=0.0, dt=5.0):
    out = None
    for i, g in enumerate(grids):
        out = c.update(g, bats, now=t0 + i * dt)
    return out


def test_pulsing_load_is_detected_and_held_at_the_peak():
    c = _pulse_ctrl()
    bats = _bats([60, 60, 60])
    # a load flipping +1000 / -1000 W around the target every sample
    _feed(c, [1000, -1000, 1000, -1000, 1000, -1000], bats)
    assert c.pulsing
    assert c.hold_floor_w > 0
    held = c._command_total
    c.update(-1000, bats, now=31.0)           # an off-pulse: would normally cut back 800 W
    assert c._command_total >= held - 1       # held, not chased


def test_pulse_hold_off_chases_the_load_as_before():
    c = _pulse_ctrl(hold=False)
    bats = _bats([60, 60, 60])
    _feed(c, [1000, -1000, 1000, -1000, 1000, -1000], bats)
    assert c.pulsing and c.hold_floor_w == 0.0      # still detected, but not acted on


def test_a_single_load_drop_is_not_pulsing():
    c = _pulse_ctrl()
    bats = _bats([60, 60, 60])
    _feed(c, [1500, 800, 0, -900, -60, -60], bats)  # kettle on, then off: no reversals
    assert not c.pulsing and c.hold_floor_w == 0.0


def test_pulse_hold_never_blocks_pv_charging():
    c = _pulse_ctrl()
    bats = _bats([60, 60, 60])
    c._command_total = -2000.0                       # charging from a PV surplus
    # pulses riding on the surplus: demand stays negative the whole time
    _feed(c, [-1500, -2500, -1500, -2500, -1500, -2500], bats)
    assert c.hold_floor_w == 0.0                      # nothing to hold: no discharge needed
    assert c._command_total < 0                       # still charging


def test_without_a_clock_the_controller_is_unchanged():
    a, b = ZeroGridController(ControllerConfig()), _pulse_ctrl()
    bats = _bats([60, 60, 60])
    for g in (1000, -1000, 1000, -1000, 1000, -1000):
        assert a.update(g, bats) == b.update(g, bats)


def test_replay_induction_hob_import_drops_with_pulse_hold():
    """The 2026-09-22 17:18 case through the REAL controller: 1.08 kW hob, 3.5 s on /
    3.5 s off, 2.26 kW base; grid sampled every 5 s, the manager acts every 3 s on a
    new sample, batteries follow after ~1 s with a ~6 s ramp (tau 2.5 s)."""
    import math

    def replay(hold):
        c = _pulse_ctrl(hold)
        c.config.target_grid_w = -60
        bats = [BatteryState(id="fleet", soc=60, min_soc=11, max_power=7500)]
        load = lambda t: 3342.0 if (t % 7.0) < 3.5 else 2262.0
        dt, t, b, tb = 0.1, -60.0, 2262.0, 2262.0
        sample_t = sample = seen = None
        next_sample = next_tick = -60.0
        pending, imp = [], 0.0
        while t < 520.0:
            if t >= next_sample:
                sample, sample_t, next_sample = load(t) - b, t, next_sample + 5.0
            if t >= next_tick:
                next_tick += 3.0
                if sample_t is not None and sample_t != seen:
                    seen = sample_t
                    sp = c.update(sample, bats, now=t)
                    pending.append((t + 1.0, sum(sp.values())))
            while pending and pending[0][0] <= t:
                tb = pending.pop(0)[1]
            b += (tb - b) * (1 - math.exp(-dt / 2.5))
            g = load(t) - b
            if t >= 0 and g > 0:
                imp += g * dt / 3600
            t += dt
        return imp

    chase, hold = replay(False), replay(True)
    assert chase > 20, chase                     # the flip-flop really imports
    assert hold < 0.3 * chase, (hold, chase)    # pulse hold removes most of it

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} controller tests passed ✓")
