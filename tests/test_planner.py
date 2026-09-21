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


# ---- discharge anti-windup (stalled battery) ---------------------------
def test_stalled_fleet_excluded_and_command_drops_to_zero():
    # all 3 near the floor, commanded hard, but none actually deliver (real hardware
    # floor above the configured min_soc) -> after stall_ticks the whole fleet is
    # excluded from discharge instead of winding the command up at max forever.
    p = make(min_soc=9.0)
    bb = [BatteryReading(f"b{i}", soc=13.0, power=0) for i in range(3)]
    for i in range(4):
        pl = p.plan(ob(i, 3000, key=f"k{i}", batteries=bb))
    assert set(pl.stalled_ids) == {"b0", "b1", "b2"}
    assert pl.command_total == 0
    assert all(v == 0 for v in pl.setpoints.values())


def test_only_non_delivering_battery_is_excluded():
    # b0 never actually delivers despite being commanded; b1/b2 deliver what's asked
    # of them -> only b0 gets excluded, the healthy pair keeps discharging.
    p = make(min_soc=9.0)
    for i in range(4):
        prev = p._last_setpoints
        bb = [
            BatteryReading("b0", soc=13.0, power=0),
            BatteryReading("b1", soc=13.0, power=prev.get("b1", 0)),
            BatteryReading("b2", soc=13.0, power=prev.get("b2", 0)),
        ]
        pl = p.plan(ob(i, 3000, key=f"k{i}", batteries=bb))
    assert pl.stalled_ids == ["b0"]
    assert pl.setpoints.get("b0", 0) == 0
    assert pl.setpoints.get("b1", 0) > 0
    assert pl.setpoints.get("b2", 0) > 0


def test_stall_clears_once_charged_back_out_of_near_floor_band():
    p = make(min_soc=9.0, stall_soc_margin=5.0)
    bb = [BatteryReading(f"b{i}", soc=13.0, power=0) for i in range(3)]
    for i in range(4):
        pl = p.plan(ob(i, 3000, key=f"k{i}", batteries=bb))
    assert set(pl.stalled_ids) == {"b0", "b1", "b2"}

    # recovered well above the near-floor band (min_soc 9 + margin 5 = 14) -> re-eligible
    recovered = [BatteryReading(f"b{i}", soc=25.0, power=0) for i in range(3)]
    pl = p.plan(ob(10, 3000, key="k-recover", batteries=recovered))
    assert pl.stalled_ids == []
    assert pl.command_total > 0


def test_turnaround_after_a_direction_flip_is_not_a_stall():
    # A battery reversing from charge to discharge takes seconds to deliver. That
    # lag reads exactly like a hardware floor cutoff; latching on it is what locked
    # the fleet out after an arbitrage grid-charge burst (2026-09-21).
    p = make(min_soc=9.0, stall_flip_grace_s=60.0)
    bb = [BatteryReading(f"b{i}", soc=13.0, power=0) for i in range(3)]
    for i in range(3):
        p._flip_at[f"b{i}"] = 0.0          # each just reversed at t=0
    for i in range(4):
        pl = p.plan(ob(i, 3000, key=f"k{i}", batteries=bb))
    assert pl.stalled_ids == []            # inside the grace window

    for i in range(100, 104):              # well past it -> the latch still works
        pl = p.plan(ob(i, 3000, key=f"k{i}", batteries=bb))
    assert set(pl.stalled_ids) == {"b0", "b1", "b2"}


def test_stall_latch_releases_after_timeout_even_at_the_floor():
    # Deadlock guard: an excluded battery can only leave the near-floor band by
    # CHARGING, so on a sunless night it stayed excluded for hours while the house
    # imported at peak price. The timeout forces a re-test.
    p = make(min_soc=9.0, stall_release_s=300.0)
    bb = [BatteryReading(f"b{i}", soc=13.0, power=0) for i in range(3)]
    for i in range(4):
        pl = p.plan(ob(i, 3000, key=f"k{i}", batteries=bb))
    assert set(pl.stalled_ids) == {"b0", "b1", "b2"}
    assert pl.command_total == 0

    pl = p.plan(ob(400, 3000, key="k-retest", batteries=bb))
    assert pl.stalled_ids == []            # still at the floor, but re-tested
    assert pl.command_total > 0


def test_direction_flip_is_recorded_only_on_a_real_reversal():
    p = make()
    p._note_direction_flips({"b0": 500}, now=10.0)      # first non-zero: no flip
    assert "b0" not in p._flip_at
    p._note_direction_flips({"b0": 700}, now=20.0)      # same direction: no flip
    assert "b0" not in p._flip_at
    p._note_direction_flips({"b0": 0}, now=25.0)        # idle doesn't clear the sign
    assert "b0" not in p._flip_at
    p._note_direction_flips({"b0": -400}, now=30.0)     # reversal
    assert p._flip_at["b0"] == 30.0


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


# ---- dispatch-set observability (the F11 blind spot) ------------------
def _dead(bid):
    """A battery the base can no longer read (device unavailable)."""
    return BatteryReading(id=bid, soc=None, read_ok=False)


def test_excluded_map_explains_why_each_battery_is_not_dispatched():
    p = make()
    batts = [
        BatteryReading(id="b0", soc=90),
        _dead("b1"),
        BatteryReading(id="b2", soc=None),   # responding, but no usable SOC
    ]
    plan = p.plan(ob(100, 500, batteries=batts))
    assert plan.healthy_ids == ["b0"]
    assert plan.excluded == {
        "b1": "not responding",
        "b2": "no SOC reading",
    }


def test_unreadable_battery_stays_excluded_while_it_stays_dead():
    p = make()
    dying = [BatteryReading(id="b0", soc=90), _dead("b1")]
    for t in (100, 103, 106, 109):
        plan = p.plan(ob(t, 500, batteries=dying))
    assert plan.healthy_ids == ["b0"]
    assert plan.excluded == {"b1": "not responding"}
    # the watchdog has also latched it out (>= threshold consecutive read failures)
    assert not p.supervisor.battery_healthy("b1")
    assert p.supervisor.battery_fault("b1").excluded is True


def test_dispatch_drop_and_return_are_logged_once_each(caplog=None):
    """A battery leaving/rejoining dispatch logs on the TRANSITION only."""
    import logging as _logging

    records = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    log = _logging.getLogger(planner.__name__)
    log.addHandler(handler)
    log.setLevel(_logging.INFO)
    try:
        p = make()
        alive = [BatteryReading(id="b0", soc=90), BatteryReading(id="b1", soc=90)]
        gone = [BatteryReading(id="b0", soc=90), _dead("b1")]
        p.plan(ob(100, 500, batteries=alive))          # first tick: baseline, no log
        assert not [r for r in records if "DROPPED" in r]
        p.plan(ob(103, 500, batteries=gone))           # b1 leaves -> one warning
        p.plan(ob(106, 500, batteries=gone))           # still gone -> no repeat
        dropped = [r for r in records if "DROPPED" in r]
        assert len(dropped) == 1
        assert "b1" in dropped[0] and "Fleet is now 1" in dropped[0]

        p.plan(ob(109, 500, batteries=alive))          # b1 returns -> one warning
        rejoined = [r for r in records if "RE-ENTERED" in r]
        assert len(rejoined) == 1
        assert "b1" in rejoined[0] and "Fleet is now 2" in rejoined[0]
    finally:
        log.removeHandler(handler)


def test_send_errors_are_attached_to_the_ack_failure_streak():
    p = make()
    p.plan(ob(100, 500))
    p.record_send(100, {"b0": True, "b1": False}, {"b1": "TimeoutError: no ack"})
    fault = p.supervisor.battery_fault("b1", source=safety.SOURCE_ACK)
    assert fault.fails == 1
    assert fault.last_error == "TimeoutError: no ack"
    assert fault.first_fail_ts == 100
    # a failure with no detail still records a usable default
    p.record_send(103, {"b0": True, "b1": False})
    ack = p.supervisor.battery_fault("b1", source=safety.SOURCE_ACK)
    assert ack.fails == 2 and ack.last_error == "setpoint not acknowledged"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} planner tests passed ✓")
