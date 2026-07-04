"""Unit tests for the EV coordinator tick — the HA I/O shell.

All three production incidents to date (zero-grid deadlock, car=4 restart
block, frc drift) lived in the coordinator layer, not the tested planner —
these tests close that gap. The wallbox driver, battery bridge and hass are
faked; the real EvChargePlanner runs inside.

Run directly:   python3 tests/test_ev_coordinator.py
Or with pytest: pytest tests/test_ev_coordinator.py
"""
import asyncio
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# ── stub homeassistant + aiohttp ─────────────────────────────────────────────
sys.modules["aiohttp"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.aiohttp_client"] = MagicMock()

_uc = ModuleType("homeassistant.helpers.update_coordinator")


class DataUpdateCoordinator:  # real class: subclassed + super().__init__ called
    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.data = None


_uc.DataUpdateCoordinator = DataUpdateCoordinator
sys.modules["homeassistant.helpers.update_coordinator"] = _uc

# ── load the real wattsmith modules (settings/const/planner/wallbox chain) ───
_pkg = ModuleType("wattsmith")
sys.modules["wattsmith"] = _pkg
_base = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"wattsmith.{name}", _base / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "wattsmith"
    sys.modules[f"wattsmith.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# battery_bridge pulls the entity registry — fake it entirely
_bb = ModuleType("wattsmith.battery_bridge")
_bb.BatteryBridge = MagicMock
sys.modules["wattsmith.battery_bridge"] = _bb

_load("const")
_load("settings")
_load("validate_config")
_load("ev_planner")
_load("wallbox")
_load("wallbox_goe")
_evc = _load("ev_coordinator")

EvCoordinator = _evc.EvCoordinator
_wallbox = sys.modules["wattsmith.wallbox"]
WallboxState = _wallbox.WallboxState
FORCE_ON, FORCE_OFF, FORCE_NEUTRAL = _wallbox.FORCE_ON, _wallbox.FORCE_OFF, _wallbox.FORCE_NEUTRAL


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeDriver:
    """Scriptable WallboxDriver: queue of read() states + recorded applies."""

    def __init__(self, states=None):
        self.states = list(states or [])
        self.applies: list[tuple[bool, int, int]] = []
        self.released = 0

    async def read(self):
        if not self.states:
            return None
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def apply(self, charge, amp, phases):
        self.applies.append((charge, amp, phases))
        return True

    async def release(self):
        self.released += 1


class FakeBatteryState(SimpleNamespace):
    pass


def _bat(soc=90.0, power=-2000, available=True):
    return FakeBatteryState(soc=soc, power=power, available=available)


class FakeHass:
    def __init__(self, states: dict | None = None):
        self._states = states or {}
        self.data = {}

    @property
    def states(self):
        outer = self

        class _S:
            def get(self, entity_id):
                v = outer._states.get(entity_id)
                if v is None:
                    return None
                return SimpleNamespace(state=str(v))

        return _S()


def make_coord(options=None, states=None, batteries=None, driver=None):
    entry = SimpleNamespace(
        options=options or {}, data={}, entry_id="test_entry",
        title="Wattsmith",
    )
    coord = EvCoordinator(FakeHass(states), entry)
    coord.bridge = SimpleNamespace(read_all=lambda: batteries if batteries is not None else [])
    coord._driver = driver
    return coord


def _tick(coord):
    return asyncio.run(coord._async_update_data())


BASE_OPTIONS = {
    "goe_ip": "192.0.2.1",
    "grid_sensor": "sensor.grid",
    "tibber_sensor": "sensor.price",
    "ev_mode": "solar",
}


# ── tests ────────────────────────────────────────────────────────────────────

def test_off_plan_reasserts_on_neutral_drift():
    # THE incident: planner says waiting/off, charger drifted to neutral
    driver = FakeDriver([WallboxState(force=FORCE_NEUTRAL, amp=6, phases=1,
                                      power_w=0.0, connected=True, done=False)])
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": 100.0}, [_bat(soc=60.0)], driver)
    data = _tick(coord)
    assert data["state"] == "waiting"
    assert driver.applies == [(False, 0, 1)]   # frc re-asserted to off


def test_off_plan_in_sync_no_write():
    driver = FakeDriver([WallboxState(force=FORCE_OFF, amp=6, phases=1,
                                      power_w=0.0, connected=True, done=False)])
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": 100.0}, [_bat(soc=60.0)], driver)
    _tick(coord)
    assert driver.applies == []   # already off — don't hammer the charger


def test_unreachable_wallbox_asserts_blind():
    driver = FakeDriver([])       # read() → None
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": 100.0}, [_bat(soc=60.0)], driver)
    data = _tick(coord)
    assert data["wallbox_reachable"] is False
    assert driver.applies == [(False, 0, 1)]


def test_driver_power_feeds_planner_and_status():
    # charger reports 1200 W; no external EV power sensor configured
    driver = FakeDriver([WallboxState(force=FORCE_ON, amp=6, phases=1,
                                      power_w=1200.0, connected=True, done=False)])
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": 0.0}, [_bat(soc=90.0)], driver)
    data = _tick(coord)
    assert data["ev_power_w"] == 1200.0
    assert coord.ev_power_recent() == 1200.0   # manager-facing surface


def test_solar_charge_uses_driver_max_amp():
    # 4000 W export, 1 phase (below the 4500 W phase-up threshold):
    # raw amp = 4000/230 ≈ 17 → clamps to the driver-reported 13 A limit
    driver = FakeDriver([WallboxState(force=FORCE_OFF, amp=6, phases=1,
                                      power_w=0.0, connected=True, done=False,
                                      max_amp=13)])
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": -4000.0}, [_bat(soc=90.0, power=0)], driver)
    data = _tick(coord)
    assert data["state"] == "solar" and data["charge"] is True
    assert driver.applies and driver.applies[0] == (True, 13, 1)


def test_car_state_grace_rides_out_blip():
    st = WallboxState(force=FORCE_ON, amp=6, phases=1, power_w=1400.0,
                      connected=True, done=False)
    driver = FakeDriver([st])
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": -2000.0}, [_bat(soc=90.0, power=0)], driver)
    _tick(coord)                                   # establishes car cache
    # next tick: wallbox unreadable, no fallback sensor → within grace = still connected
    connected, done = coord._resolve_car_state(None, coord._car_cache_ts + 10.0)
    assert (connected, done) == (True, False)
    # past the grace window → fails safe to disconnected
    connected, _ = coord._resolve_car_state(None, coord._car_cache_ts + 999.0)
    assert connected is False


def test_unknown_car_state_falls_back_to_sensor():
    # driver returns connected=None (unknown car code) → sensor fallback decides
    st = WallboxState(force=FORCE_OFF, connected=None, done=None, power_w=0.0)
    driver = FakeDriver([st])
    options = {**BASE_OPTIONS, "car_state_sensor": "sensor.car"}
    coord = make_coord(options, {"sensor.grid": 100.0, "sensor.car": "Charging"},
                       [_bat(soc=60.0)], driver)
    data = _tick(coord)
    assert data["car_connected"] is True


def test_reserve_clamped_to_battery_cap():
    # reserve 90 but Maximum Charge SOC 80 → effective reserve 80, so a fleet
    # at 80 may charge the car (pre-fix: waited forever for an unreachable 90)
    options = {**BASE_OPTIONS, "reserve_soc": 90.0, "max_battery_soc": 80.0}
    coord = make_coord(options)
    assert coord._planner.config.reserve_soc == 80.0


def test_tick_error_fails_safe():
    driver = FakeDriver([WallboxState(force=FORCE_ON, amp=6, phases=1,
                                      power_w=1000.0, connected=True, done=False)])
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": 0.0}, None, driver)
    coord.bridge = SimpleNamespace(read_all=MagicMock(side_effect=RuntimeError("boom")))
    data = _tick(coord)
    assert data["state"] == "error"
    assert coord.bridge_active is False


def test_no_driver_observation_only():
    coord = make_coord({"grid_sensor": "sensor.grid", "ev_mode": "solar"},
                       {"sensor.grid": 100.0}, [_bat(soc=60.0)], driver=None)
    data = _tick(coord)
    assert data["goe_configured"] is False
    assert coord.wallbox_configured is False


def test_solar_reserve_surface_for_manager():
    driver = FakeDriver([WallboxState(force=FORCE_OFF, amp=6, phases=1,
                                      power_w=0.0, connected=True, done=False)])
    coord = make_coord(BASE_OPTIONS, {"sensor.grid": -5060.0}, [_bat(soc=90.0, power=0)], driver)
    coord.data = _tick(coord)
    assert coord.data["state"] == "solar"
    assert coord.solar_reserve_soc == coord._planner.config.reserve_soc
    coord.data = {"state": "waiting"}
    assert coord.solar_reserve_soc is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} EV coordinator tests passed ✓")
