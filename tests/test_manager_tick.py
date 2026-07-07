"""Unit tests for the Energy Manager coordinator tick — the HA I/O shell.

manager.py was the largest untested module (audit §6); these tests cover the
orchestration the pure planners can't see: release/send execution through the
bridge, EV coupling via the coordinator's public surface, adaptive/EV-reserve
capping, config warnings, and the consumption-starvation warning.

Fakes: hass, config entry, battery bridge, sibling EV coordinator, HA storage.
The real DispatchPlanner/ZeroGridController/SafetySupervisor run inside.

Run directly:   python3 tests/test_manager_tick.py
Or with pytest: pytest tests/test_manager_tick.py
"""
import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# ── stub homeassistant ───────────────────────────────────────────────────────
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()

_uc = ModuleType("homeassistant.helpers.update_coordinator")


class DataUpdateCoordinator:
    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.data = None


_uc.DataUpdateCoordinator = DataUpdateCoordinator
sys.modules["homeassistant.helpers.update_coordinator"] = _uc

_storage = ModuleType("homeassistant.helpers.storage")


class Store:
    def __init__(self, hass, version, key):
        pass

    async def async_load(self):
        return None

    async def async_save(self, data):
        pass


_storage.Store = Store
sys.modules["homeassistant.helpers.storage"] = _storage

_util = ModuleType("homeassistant.util")
_dt = ModuleType("homeassistant.util.dt")
_dt.utcnow = lambda: datetime.now(timezone.utc)
_dt.parse_datetime = lambda s: None
_util.dt = _dt
sys.modules["homeassistant.util"] = _util
sys.modules["homeassistant.util.dt"] = _dt

# ── load real wattsmith modules; fake the battery bridge ─────────────────────
_pkg = ModuleType("wattsmith")
sys.modules["wattsmith"] = _pkg
_base = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"

_bb = ModuleType("wattsmith.battery_bridge")
_bb.BatteryBridge = MagicMock
sys.modules["wattsmith.battery_bridge"] = _bb


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"wattsmith.{name}", _base / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "wattsmith"
    sys.modules[f"wattsmith.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
_load("settings")
_load("validate_config")
_load("controller")
_load("safety")
_load("planner")
_load("adaptive")
_load("baseline_learner")
_mgr = _load("manager")

EnergyManagerCoordinator = _mgr.EnergyManagerCoordinator
DOMAIN = sys.modules["wattsmith.const"].DOMAIN


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeHass:
    def __init__(self, states: dict | None = None):
        self._states = states or {}
        self.data = {}

    @property
    def states(self):
        outer = self

        class _S:
            def get(self, entity_id):
                return outer._states.get(entity_id)

        return _S()


def _grid_state(value: float, age_s: float = 0.0):
    ts = datetime.now(timezone.utc)
    return SimpleNamespace(state=str(value), last_updated=ts, last_changed=ts)


def _bat(bid="b1", soc=50.0, power=0, available=True, capacity=5120.0, device="d1"):
    return SimpleNamespace(battery_id=bid, soc=soc, power=power,
                           available=available, capacity=capacity, device_id=device)


class FakeBridge:
    def __init__(self, batteries=None, ack=True):
        self.batteries = batteries or []
        self.ack = ack
        self.sent: list[dict] = []
        self.released = 0

    def read_all(self):
        return self.batteries

    def device_ids(self):
        return [b.device_id for b in self.batteries]

    async def set_passive(self, setpoints, device_by_id, cd_time):
        self.sent.append(dict(setpoints))
        return {bid: self.ack for bid in setpoints}

    async def release_all(self, device_ids):
        self.released += 1


def make_manager(options=None, states=None, batteries=None, ev_coord=None):
    hass = FakeHass(states)
    entry = SimpleNamespace(
        options={"enabled": True, **(options or {})},
        data={"grid_sensor": "sensor.grid"},
        entry_id="mgr_entry", title="Wattsmith",
    )
    mgr = EnergyManagerCoordinator(hass, entry)
    mgr.bridge = FakeBridge(batteries or [])
    if ev_coord is not None:
        hass.data[DOMAIN] = {"mgr_entry_ev": ev_coord}
    return mgr


def _tick(mgr):
    return asyncio.run(mgr._async_update_data())


# ── tests ────────────────────────────────────────────────────────────────────

def test_disabled_releases_once_then_idles():
    mgr = make_manager({"enabled": False}, {"sensor.grid": _grid_state(500)}, [_bat()])
    r1 = _tick(mgr)
    r2 = _tick(mgr)
    assert r1["state"] == "disabled" and r2["state"] == "disabled"
    assert mgr.bridge.released == 1   # release exactly once, then idle


def test_import_dispatches_discharge_setpoints():
    mgr = make_manager(None, {"sensor.grid": _grid_state(500)}, [_bat(soc=50.0)])
    result = _tick(mgr)
    assert result["state"] == "normal"
    assert mgr.bridge.sent and sum(mgr.bridge.sent[0].values()) > 0  # discharging
    assert result["config_warnings"] == []


def test_no_grid_sensor_goes_safe_and_releases():
    mgr = make_manager(None, {}, [_bat()])
    result = _tick(mgr)
    assert result["state"] == "safe"
    assert mgr.bridge.released == 1
    assert mgr.bridge.sent == []      # never dispatch blind


def test_ev_bridge_folds_car_into_load():
    ev = SimpleNamespace(bridge_active=True, solar_reserve_soc=None,
                         wallbox_configured=True, ev_power_recent=lambda **k: 1400.0)
    mgr = make_manager(None, {"sensor.grid": _grid_state(200)}, [_bat()], ev_coord=ev)
    result = _tick(mgr)
    assert result["ev_bridge"] is True
    assert result["ev_power"] == 0.0  # bridge: car is NOT excluded


def test_ev_power_read_from_coordinator_without_sensor():
    # no ev_sensor option — the wallbox driver reading (via the EV coordinator)
    # supplies the exclusion; this is what makes the external go-e integration optional
    ev = SimpleNamespace(bridge_active=False, solar_reserve_soc=None,
                         wallbox_configured=True, ev_power_recent=lambda **k: 1000.0)
    mgr = make_manager(None, {"sensor.grid": _grid_state(1200)}, [_bat()], ev_coord=ev)
    result = _tick(mgr)
    assert result["ev_power"] == 1000.0
    assert result["effective_grid"] == 200.0


def test_ev_solar_right_of_way_caps_battery_charging():
    ev = SimpleNamespace(bridge_active=False, solar_reserve_soc=80.0,
                         wallbox_configured=True, ev_power_recent=lambda **k: 4000.0)
    mgr = make_manager(None, {"sensor.grid": _grid_state(-100)}, [_bat(soc=85.0)], ev_coord=ev)
    _tick(mgr)
    assert mgr.planner.config.max_battery_soc == 80.0


def test_stale_ev_reading_holds():
    # wallbox configured but its power reading is stale → planner must HOLD
    # (never let batteries try to cover an unknown car draw)
    ev = SimpleNamespace(bridge_active=False, solar_reserve_soc=None,
                         wallbox_configured=True, ev_power_recent=lambda **k: None)
    mgr = make_manager(None, {"sensor.grid": _grid_state(500)}, [_bat()], ev_coord=ev)
    result = _tick(mgr)
    assert result["state"] == "hold"
    assert "EV sensor" in result["reason"]


def test_config_warnings_surface_reserve_clash():
    mgr = make_manager(
        {"goe_ip": "192.0.2.1", "reserve_soc": 90.0, "max_battery_soc": 80.0},
        {"sensor.grid": _grid_state(500)}, [_bat()],
    )
    assert any("Reserve SOC" in w for w in mgr.config_warnings)
    result = _tick(mgr)
    assert any("Reserve SOC" in w for w in result["config_warnings"])


def test_consumption_starvation_warns_once_adaptive_on():
    mgr = make_manager({"adaptive_enabled": True},
                       {"sensor.grid": _grid_state(500)}, [_bat()])
    mgr._consumption_ok_ts -= 999999           # sensor unreadable "for hours"
    result = _tick(mgr)
    assert any("baseline learner starving" in w for w in result["config_warnings"])
    # recovers when the sensor is readable again
    mgr.hass._states["sensor.house_consumption_power"] = SimpleNamespace(state="450")
    result = _tick(mgr)
    assert not any("starving" in w for w in result["config_warnings"])


def _fake_arb(enabled=True, target_soc=None, grid_charge_now_wh=0.0, hold_floor_soc=None):
    return SimpleNamespace(
        enabled=enabled,
        charge_floor_soc=target_soc,
        hold_floor_soc=hold_floor_soc,
        data={"grid_charge_now_wh": grid_charge_now_wh, "target_soc": target_soc},
    )


def test_arbitrage_grid_charge_flips_target_and_caps_soc():
    # switch on + planner wants to buy this bucket + fleet below target ->
    # grid target flips positive (import) and the charge is capped at target SOC.
    mgr = make_manager({"import_power_cap_w": 7500},
                       {"sensor.grid": _grid_state(0)}, [_bat(soc=30.0)])
    mgr.hass.data.setdefault(DOMAIN, {})["mgr_entry_arb"] = _fake_arb(
        target_soc=60.0, grid_charge_now_wh=1875.0)
    result = _tick(mgr)
    assert result["arb_charging"] is True
    assert result["target_grid_w"] == 7500                 # importing on purpose
    assert mgr.planner.config.max_battery_soc == 60.0       # capped at earmark
    assert result["command_total"] < 0                      # charging


def test_arbitrage_no_charge_when_already_at_target():
    # fleet already at/above the target -> no charge, grid target stays at base.
    mgr = make_manager({"import_power_cap_w": 7500},
                       {"sensor.grid": _grid_state(0)}, [_bat(soc=61.0)])
    mgr.hass.data.setdefault(DOMAIN, {})["mgr_entry_arb"] = _fake_arb(
        target_soc=60.0, grid_charge_now_wh=1875.0)
    result = _tick(mgr)
    assert result["arb_charging"] is False
    assert result["target_grid_w"] == -50                   # back to zero-grid base


def test_arbitrage_charge_ignored_when_switch_off():
    # advisory present but switch OFF -> never actuate (grid target stays base).
    mgr = make_manager({"import_power_cap_w": 7500},
                       {"sensor.grid": _grid_state(0)}, [_bat(soc=30.0)])
    mgr.hass.data.setdefault(DOMAIN, {})["mgr_entry_arb"] = _fake_arb(
        enabled=False, target_soc=60.0, grid_charge_now_wh=1875.0)
    result = _tick(mgr)
    assert result["arb_charging"] is False
    assert result["target_grid_w"] == -50


def test_arbitrage_charge_ignored_when_no_advisory():
    # switch on but planner isn't recommending a buy this bucket -> no actuation.
    mgr = make_manager({"import_power_cap_w": 7500},
                       {"sensor.grid": _grid_state(0)}, [_bat(soc=30.0)])
    mgr.hass.data.setdefault(DOMAIN, {})["mgr_entry_arb"] = _fake_arb(
        target_soc=30.0, grid_charge_now_wh=0.0)
    result = _tick(mgr)
    assert result["arb_charging"] is False
    assert result["target_grid_w"] == -50


def test_arbitrage_charge_never_imports_on_stale_grid():
    # SAFE must win over grid-charge: no grid sensor -> release, never import blind.
    mgr = make_manager({"import_power_cap_w": 7500}, {}, [_bat(soc=30.0)])
    mgr.hass.data.setdefault(DOMAIN, {})["mgr_entry_arb"] = _fake_arb(
        target_soc=60.0, grid_charge_now_wh=1875.0)
    result = _tick(mgr)
    assert result["state"] == "safe"
    assert mgr.bridge.sent == []          # never dispatched a charge blind


def test_tick_error_reports_error_state():
    mgr = make_manager(None, {"sensor.grid": _grid_state(500)}, [_bat()])
    mgr.bridge.read_all = MagicMock(side_effect=RuntimeError("boom"))
    result = _tick(mgr)
    assert result["state"] == "error"
    assert "boom" in result["reason"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} manager tick tests passed ✓")
