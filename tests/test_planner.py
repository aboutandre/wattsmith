"""Exhaustive unit tests for the Energy Manager decision logic (pure, no HA/hardware).

These cover the safety-critical paths: stale/missing grid, EV-exclusion edge cases
(blip vs sustained loss), dead/recovering batteries, write throttling, degraded
debounce, and watchdog escalation.

Run: python3 tests/test_planner.py   (or: pytest tests/test_planner.py)
"""
import importlib.util
import sys
from pathlib import Path

_COMP = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _COMP / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


controller = _load("controller")
safety = _load("safety")
planner = _load("planner")  # falls back to absolute imports of controller/safety

ZeroGridController = controller.ZeroGridController
ControllerConfig = controller.ControllerConfig
SafetySupervisor = safety.SafetySupervisor
SafetyConfig = safety.SafetyConfig
Mode = safety.Mode
DispatchPlanner = planner.DispatchPlanner
PlannerConfig = planner.PlannerConfig
Observation = planner.Observation
BatteryReading = planner.BatteryReading


def make(**cfg):
    ctrl = ZeroGridController(ControllerConfig())  # ffunes-aligned defaults
    sup = SafetySupervisor(SafetyConfig(grid_max_age_s=20, battery_fail_threshold=3,
                                        cycle_fail_threshold=3))
    return DispatchPlanner(ctrl, sup, PlannerConfig(**cfg))


def br(socs, read_ok=True):
    return [BatteryReading(id=f"b{i}", soc=s, read_ok=read_ok) for i, s in enumerate(socs)]


def ob(now, grid, *, fresh=True, key="__grid__", enabled=True, ev_cfg=False, ev=None,
       batteries=None):
    return Observation(
        now=now, enabled=enabled, grid_value=grid, grid_fresh=fresh,
        grid_key=(grid if key == "__grid__" else key),
        ev_configured=ev_cfg, ev_raw=ev,
        batteries=batteries if batteries is not None else br([90, 90, 90]),
    )


# ---- enable / disable -------------------------------------------------
def test_disabled_releases_once_then_idle():
    p = make()
    assert p.plan(ob(100, 500, enabled=False)).action == "release"
    p2 = p.plan(ob(103, 500, enabled=False))
    assert p2.action == "idle" and p2.state == "disabled"


def test_enable_resets_released_so_redisable_releases_again():
    p = make()
    p.plan(ob(100, 500, enabled=False))   # released
    p.plan(ob(103, 500, enabled=True))    # clears released
    assert p.plan(ob(106, 500, enabled=False)).action == "release"


# ---- grid safety ------------------------------------------------------
def test_no_grid_ever_is_safe():
    pl = make().plan(ob(100, None))
    assert pl.state == "safe" and pl.action == "release"


def test_stale_grid_is_safe():
    assert make().plan(ob(100, 200, fresh=False)).state == "safe"


def test_grid_none_after_fresh_holds():
    p = make()
    p.plan(ob(100, 300))                  # establish freshness
    pl = p.plan(ob(105, None))            # within max_age, but no value this tick
    assert pl.action == "hold" and "no grid value" in pl.reason


# ---- battery health ---------------------------------------------------
def test_all_batteries_unreadable_holds():
    p = make()
    p.plan(ob(100, 300))
    pl = p.plan(ob(103, 300, batteries=br([90, 90, 90], read_ok=False), key="x"))
    assert pl.action == "hold" and "no healthy batteries" in pl.reason


def test_single_unreadable_battery_excluded_others_continue():
    p = make()
    p.plan(ob(100, 500))
    bb = [BatteryReading("b0", 90, read_ok=True),
          BatteryReading("b1", 90, read_ok=False),
          BatteryReading("b2", 90, read_ok=True)]
    pl = p.plan(ob(103, 500, batteries=bb, key="x"))
    assert "b1" not in pl.healthy_ids
    assert "b0" in pl.healthy_ids and "b2" in pl.healthy_ids


def test_soc_none_excluded():
    bb = [BatteryReading("b0", None, read_ok=True), BatteryReading("b1", 90, read_ok=True)]
    pl = make().plan(ob(100, 500, batteries=bb))
    assert "b0" not in pl.healthy_ids and "b1" in pl.healthy_ids


def test_all_at_min_soc_commands_zero_discharge():
    pl = make().plan(ob(100, 500, batteries=br([11, 11, 11])))  # import, but nothing to give
    assert pl.command_total == 0


# ---- normal dispatch + dedup + throttle -------------------------------
def test_normal_dispatch_sends():
    pl = make().plan(ob(100, 500))
    assert pl.action == "send" and pl.state == "normal" and pl.command_total > 0


def test_repeated_grid_holds_within_resend_window():
    p = make()
    p.plan(ob(100, 500))                  # send (ts=100)
    pl = p.plan(ob(102, 500))             # same value -> dedup; 2s < resend 7 -> hold
    assert pl.action == "hold" and pl.state == "normal"


def test_repeated_grid_resends_after_window():
    p = make()
    p.plan(ob(100, 500))
    pl = p.plan(ob(108, 500))             # 8s >= resend 7 -> re-arm cd_time
    assert pl.action == "send"


def test_new_grid_value_always_recomputes():
    p = make()
    a = p.plan(ob(100, 500))
    b = p.plan(ob(102, 1500))             # different value -> new sample even if <resend
    assert b.action == "send" and b.command_total != a.command_total


# ---- EV exclusion (safety critical) -----------------------------------
def test_ev_not_configured_is_zero():
    assert make().plan(ob(100, 500, ev_cfg=False)).ev_power == 0.0


def test_ev_excluded_so_batteries_dont_feed_car():
    # 1000W grid is entirely the car -> batteries should stay near idle, not chase 1000W
    pl = make().plan(ob(100, 1000, ev_cfg=True, ev=1000))
    assert pl.ev_power == 1000 and pl.command_total < 200


def test_ev_negative_clamped_to_zero():
    assert make().plan(ob(100, 500, ev_cfg=True, ev=-50)).ev_power == 0.0


def test_ev_blip_uses_cached_value():
    p = make(ev_max_age_s=20)
    p.plan(ob(100, 2000, ev_cfg=True, ev=1500))      # cache 1500
    pl = p.plan(ob(105, 2000, ev_cfg=True, ev=None))  # blip within window
    assert pl.ev_power == 1500 and "EV sensor unavailable" not in pl.reason


def test_ev_sustained_loss_holds_never_guesses():
    # The dangerous case: EV sensor dies while car may still be charging.
    p = make(ev_max_age_s=20)
    p.plan(ob(100, 2000, ev_cfg=True, ev=1500))       # cache, ts=100
    pl = p.plan(ob(130, 2000, ev_cfg=True, ev=None))   # 30s > 20s window -> UNKNOWN
    assert pl.action == "hold" and "EV sensor unavailable" in pl.reason


# ---- degraded debounce + watchdog escalation --------------------------
def test_degraded_only_after_consecutive_failures():
    p = make(degraded_threshold=3)
    assert p.record_send(1, {"b0": False})[0] == "normal"
    assert p.record_send(2, {"b0": False})[0] == "normal"
    assert p.record_send(3, {"b0": False})[0] == "degraded"


def test_degraded_recovers_on_success():
    p = make(degraded_threshold=2)
    p.record_send(1, {"b0": False})
    p.record_send(2, {"b0": False})  # degraded
    assert p.record_send(3, {"b0": True})[0] == "normal"


def test_repeated_total_send_failures_escalate_to_safe():
    p = make()
    for _ in range(3):                      # 3 cycles with NO battery reachable -> SAFE
        p.record_send(1, {"b0": False, "b1": False, "b2": False})
    assert p.plan(ob(100, 500)).state == "safe"


def test_partial_send_failure_does_not_trip_safe():
    # One battery dropping acks must NOT be read as "control lost": as long as at
    # least one battery is reachable the cycle is healthy and the watchdog stays out
    # of SAFE (it only flags 'degraded'). This is the contended-radio case.
    p = make(degraded_threshold=3)
    for _ in range(10):
        p.record_send(1, {"b0": True, "b1": False, "b2": True})
    pl = p.plan(ob(100, 500))
    assert pl.state == "normal" and pl.action == "send"  # never SAFE


def test_successful_send_keeps_normal():
    p = make()
    p.plan(ob(100, 500))                    # send
    state, _ = p.record_send(100, {"b0": True, "b1": True, "b2": True})
    assert state == "normal"


def test_safe_is_recoverable_when_conditions_heal():
    # The 11:42 incident: once SAFE, the loop must NOT latch there forever. After the
    # checkable preconditions are healthy for safe_recover_cycles ticks, it resumes.
    p = make(safe_recover_cycles=2)
    for _ in range(3):                       # total comm loss -> SAFE
        p.record_send(1, {"b0": False, "b1": False, "b2": False})
    assert p.plan(ob(100, 500)).state == "safe"   # tick1: recover_streak=1
    assert p.plan(ob(103, 500)).state == "safe"   # tick2: streak=2 -> heals watchdog
    resumed = p.plan(ob(106, 500))                # tick3: mode NORMAL again
    assert resumed.state == "normal" and resumed.action == "send"


def test_safe_does_not_recover_while_grid_stale():
    # If the SAFE cause persists (grid never fresh), it must stay SAFE.
    p = make(safe_recover_cycles=1)
    p.plan(ob(100, None))                     # no grid -> SAFE
    for t in range(5):
        assert p.plan(ob(110 + t, None)).state == "safe"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} planner tests passed ✓")
