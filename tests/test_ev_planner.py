"""Edge-case unit tests for the EV charge-control policy (pure, no HA/hardware).

Covers: hard offs, fast/cheap full-power, the PV-surplus follow + battery reserve,
1<->3 phase switching with dwell, min-current gating, and anti-flap on/off dwell.

Run: python3 tests/test_ev_planner.py   (or: pytest tests/test_ev_planner.py)
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


evp = _load("ev_planner")
EvChargePlanner = evp.EvChargePlanner
EvPlannerConfig = evp.EvPlannerConfig
EvObservation = evp.EvObservation
EvMode = evp.EvMode
TARGET_BATTERY, TARGET_CAR, TARGET_BOTH = evp.TARGET_BATTERY, evp.TARGET_CAR, evp.TARGET_BOTH


def mk(**cfg):
    return EvChargePlanner(EvPlannerConfig(**cfg))


def ob(now=10000.0, mode=EvMode.SOLAR, grid=None, car=0.0, soc=90.0, price=None,
       target=TARGET_BOTH, connected=True, done=False, max_amp=16, cur_amp=0, cur_phases=3):
    return EvObservation(now=now, mode=mode, grid_w=grid, car_power_w=car, battery_soc=soc,
                         price=price, cheap_target=target, car_connected=connected,
                         car_done=done, max_amp=max_amp, cur_amp=cur_amp, cur_phases=cur_phases)


# ---- hard offs --------------------------------------------------------
def test_mode_off():
    p = mk().plan(ob(mode=EvMode.OFF, grid=-9000))
    assert p.charge is False and p.state == "off"


def test_car_not_connected():
    assert mk().plan(ob(connected=False, grid=-9000)).state == "not_connected"


def test_car_done():
    assert mk().plan(ob(done=True, grid=-9000)).state == "full"


# ---- full power: fast + cheap ----------------------------------------
def test_fast_mode_charges_max():
    p = mk().plan(ob(mode=EvMode.FAST, grid=2000, soc=10))  # ignores grid/soc
    assert p.charge and p.amp == 16 and p.phases == 3 and p.state == "fast"


def test_cheap_window_charges_car():
    p = mk(cheap_price=0.10).plan(
        ob(mode=EvMode.SOLAR_CHEAP, price=0.05, target=TARGET_BOTH, grid=1000, soc=10))
    assert p.charge and p.amp == 16 and p.state == "cheap"


def test_cheap_window_target_battery_only_does_not_charge_car():
    # cheap target is BATTERY -> the car must NOT cheap-charge; falls back to solar
    p = mk(cheap_price=0.10).plan(
        ob(mode=EvMode.SOLAR_CHEAP, price=0.05, target=TARGET_BATTERY, grid=-6000, soc=90))
    assert p.state == "solar"


def test_price_above_threshold_falls_back_to_solar():
    p = mk(cheap_price=0.10).plan(
        ob(mode=EvMode.SOLAR_CHEAP, price=0.30, target=TARGET_CAR, grid=-6000, soc=90))
    assert p.state in ("solar",) and p.charge


# ---- solar follow + battery reserve ----------------------------------
def test_below_reserve_waits():
    p = mk(reserve_soc=80).plan(ob(soc=60, grid=-6000))
    assert p.charge is False and p.state == "waiting" and "reserve" in p.reason


def test_missing_soc_waits():
    assert mk().plan(ob(soc=None, grid=-6000)).charge is False


def test_missing_grid_waits():
    assert mk().plan(ob(grid=None, soc=90)).charge is False


def test_insufficient_surplus_waits():
    # 1000 W export < 1-phase minimum (~1380 W)
    p = mk().plan(ob(soc=90, grid=-1000))
    assert p.charge is False and p.state == "waiting"


def test_solar_charges_single_phase():
    # 2000 W surplus -> 1-phase (3-phase needs 4140), ~8-9 A
    p = mk().plan(ob(soc=90, grid=-2000))
    assert p.charge and p.phases == 1 and 6 <= p.amp <= 16 and p.state == "solar"


def test_solar_amp_clamped_to_max():
    p = mk(max_amp=16).plan(ob(soc=95, grid=-20000))
    assert p.charge and p.amp == 16


def test_solar_amp_at_minimum():
    # just above 1-phase minimum -> 6 A
    p = mk().plan(ob(soc=90, grid=-1400))
    assert p.charge and p.amp == 6 and p.phases == 1


# ---- phase switching --------------------------------------------------
def test_phase_up_1_to_3():
    p = mk(min_phase_dwell_s=300)
    p.plan(ob(now=10000, soc=90, grid=-2000))            # -> 1-phase, switch ts=10000
    up = p.plan(ob(now=10400, soc=90, grid=-6000, cur_phases=1))  # dwell passed, big surplus
    assert up.phases == 3 and up.charge


def test_phase_down_3_to_1():
    p = mk(min_phase_dwell_s=300)
    p.plan(ob(now=10000, soc=90, grid=-6000))            # -> 3-phase
    dn = p.plan(ob(now=10400, soc=90, grid=-3000, cur_phases=3))  # surplus below 3ph min
    assert dn.phases == 1


def test_phase_switch_blocked_by_dwell():
    p = mk(min_phase_dwell_s=300)
    p.plan(ob(now=10000, soc=90, grid=-2000))            # -> 1-phase at 10000
    same = p.plan(ob(now=10100, soc=90, grid=-9000, cur_phases=1))  # only 100s later
    assert same.phases == 1   # not enough dwell to step up


# ---- anti-flap dwell --------------------------------------------------
def test_restart_blocked_by_min_pause():
    p = mk(min_pause_s=120)
    p.plan(ob(now=10000, mode=EvMode.SOLAR, soc=90, grid=-6000))  # charging
    p.plan(ob(now=10010, mode=EvMode.OFF))                        # hard off -> ts=10010
    again = p.plan(ob(now=10050, mode=EvMode.SOLAR, soc=90, grid=-6000))  # 40s < 120
    assert again.charge is False and again.state == "hold"


def test_min_on_time_holds_when_bridge_unavailable():
    # below the bridge floor, a soft dip within min on-time still rides briefly on grid
    # (no battery bridge, no hard import) -> the legacy "hold" path
    p = mk(bridge_floor_soc=50, min_charge_s=120, import_stop_w=400)
    p.plan(ob(now=10000, soc=90, grid=-6000))            # charging
    hold = p.plan(ob(now=10030, soc=45, grid=-50))       # below floor, gentle dip, 30s in
    assert hold.charge is True and hold.state == "hold" and not hold.bridge_active


# ---- battery bridge ---------------------------------------------------
def test_bridge_holds_through_dip():
    # surplus drops while charging -> batteries bridge the car instead of importing
    p = mk(bridge_grace_s=180, bridge_floor_soc=50)
    p.plan(ob(now=10000, soc=90, grid=-6000))            # charging
    dip = p.plan(ob(now=10030, soc=60, grid=-100))       # surplus gone 30s later
    assert dip.charge is True and dip.amp == 6 and dip.state == "bridge" and dip.bridge_active


def test_bridge_charges_even_when_importing():
    # the trigger moment IS an import (that's why surplus dropped); bridge anyway so the
    # battery manager can pick up the car next tick
    p = mk(bridge_grace_s=180, bridge_floor_soc=50, import_stop_w=400)
    p.plan(ob(now=10000, soc=90, grid=-6000))            # charging
    b = p.plan(ob(now=10030, soc=85, grid=1500))         # importing 1500 W
    assert b.charge is True and b.state == "bridge" and b.bridge_active


def test_bridge_not_when_not_charging():
    # never STARTS a bridge if we weren't already charging
    p = mk(bridge_grace_s=180, bridge_floor_soc=50)
    out = p.plan(ob(now=10000, soc=90, grid=-100))       # insufficient surplus, idle
    assert out.charge is False and out.state == "waiting" and not out.bridge_active


def test_bridge_stops_below_floor():
    # once fleet SOC reaches the bridge floor, stop instead of draining further
    p = mk(bridge_grace_s=180, bridge_floor_soc=50, import_stop_w=400)
    p.plan(ob(now=10000, soc=90, grid=-6000))            # charging
    stop = p.plan(ob(now=10030, soc=45, grid=1500))      # below floor + importing -> stop
    assert stop.charge is False and not stop.bridge_active


def test_bridge_expires_after_grace():
    p = mk(bridge_grace_s=120, bridge_floor_soc=50, min_charge_s=120, import_stop_w=400)
    p.plan(ob(now=10000, soc=90, grid=-6000))            # charging, start ts=10000
    b = p.plan(ob(now=10030, soc=85, grid=-100))         # bridge starts, dropped_at=10030
    assert b.state == "bridge"
    done = p.plan(ob(now=10200, soc=82, grid=-100))      # 170s after drop > grace 120
    assert done.charge is False and not done.bridge_active


def test_bridge_recovers_and_resets_timer():
    p = mk(bridge_grace_s=180, bridge_floor_soc=50)
    p.plan(ob(now=10000, soc=90, grid=-6000))            # charging
    b = p.plan(ob(now=10030, soc=85, grid=-100))         # bridge, dropped_at=10030
    assert b.bridge_active
    rec = p.plan(ob(now=10060, soc=85, grid=-6000))      # surplus back -> normal solar
    assert rec.state == "solar" and rec.charge and not rec.bridge_active
    # a fresh dip much later still gets a full grace window (timer was reset)
    again = p.plan(ob(now=10400, soc=85, grid=-100))
    assert again.state == "bridge" and again.bridge_active


# ---- zero-grid startup (battery_charge_w) ----------------------------
# When the battery manager is zeroing the grid, grid_w ≈ 0 even with lots of solar.
# The EV planner must use battery_charge_w to see that surplus and start the car.

def ob_zg(battery_charge_w=3000.0, soc=85.0, now=10000.0):
    """Zero-grid scenario: grid≈0, batteries absorbing all surplus."""
    return EvObservation(
        now=now, mode=EvMode.SOLAR, grid_w=0.0, car_power_w=0.0,
        battery_soc=soc, price=None, cheap_target=TARGET_BOTH,
        car_connected=True, car_done=False, max_amp=16, cur_amp=0, cur_phases=1,
        battery_charge_w=battery_charge_w,
    )


def test_zerogrid_starts_car_when_batteries_above_reserve():
    # batteries at 85% (> 80% reserve), absorbing 3000W → car should start
    p = mk(reserve_soc=80.0)
    plan = p.plan(ob_zg(battery_charge_w=3000.0, soc=85.0))
    assert plan.charge is True and plan.state == "solar", (
        f"expected solar charge, got {plan.state}: {plan.reason}"
    )


def test_zerogrid_waits_when_batteries_below_reserve():
    # batteries at 60% (< 80% reserve): surplus should refill batteries first
    p = mk(reserve_soc=80.0)
    plan = p.plan(ob_zg(battery_charge_w=3000.0, soc=60.0))
    assert plan.charge is False and plan.state == "waiting", (
        f"expected waiting, got {plan.state}: {plan.reason}"
    )


def test_zerogrid_insufficient_surplus_stays_off():
    # only 500W absorbed by batteries: below 1-phase 6A minimum (1380W)
    p = mk(reserve_soc=80.0)
    plan = p.plan(ob_zg(battery_charge_w=500.0, soc=90.0))
    assert plan.charge is False


def test_zerogrid_amp_computed_from_available():
    # 2300W available (all battery absorption, grid=0): should get 6A on 1-phase (10A)
    p = mk(reserve_soc=80.0)
    plan = p.plan(ob_zg(battery_charge_w=2300.0, soc=90.0))
    assert plan.charge is True and plan.phases == 1
    assert plan.amp == min(16, max(6, round(2300 / 230))), plan.amp  # ~10A


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} EV planner tests passed ✓")
