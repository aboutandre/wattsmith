"""Unit tests for battery_bridge.py — Wattsmith's decoupling linchpin.

Tests the pure helpers (_uid, _resp_ok), battery discovery logic, sensor
state reading, and service dispatch — all via mocked HA objects (no real
Home Assistant installation required).

Run directly:   python3 tests/test_battery_bridge.py
Or with pytest: pytest tests/test_battery_bridge.py
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

# ── stub the homeassistant package tree before the module is imported ────────
# entity_registry must be the same object referenced from helpers.entity_registry
# AND from sys.modules["homeassistant.helpers.entity_registry"] — Python's import
# machinery resolves `from homeassistant.helpers import entity_registry as er`
# by looking up the attribute on the helpers module object, NOT by key in
# sys.modules, so both must point to the same mock.
_er_mock = MagicMock()
_helpers_mock = MagicMock()
_helpers_mock.entity_registry = _er_mock

sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = _helpers_mock
sys.modules["homeassistant.helpers.entity_registry"] = _er_mock

# ── stub the wattsmith package and const relative import ─────────────────────
_pkg = ModuleType("wattsmith")
sys.modules.setdefault("wattsmith", _pkg)

_const = ModuleType("wattsmith.const")
_const.BASE_DOMAIN = "hacs_marstek_venus_e"
_const.BASE_BATTERY_MARKER_SENSOR = "battery_state_of_charge"
_const.BASE_SENSOR_SOC = "battery_state_of_charge"
_const.BASE_SENSOR_POWER = "grid_power"
_const.BASE_SENSOR_CAPACITY = "battery_capacity"
_const.BASE_SVC_SET_PASSIVE_MODE = "set_passive_mode"
_const.BASE_SVC_SET_MODE = "set_mode"
sys.modules["wattsmith.const"] = _const

# ── load battery_bridge ───────────────────────────────────────────────────────
_path = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "wattsmith"
    / "battery_bridge.py"
)
_spec = importlib.util.spec_from_file_location("wattsmith.battery_bridge", _path)
_bb = importlib.util.module_from_spec(_spec)
_bb.__package__ = "wattsmith"
sys.modules["wattsmith.battery_bridge"] = _bb
_spec.loader.exec_module(_bb)

BatteryBridge = _bb.BatteryBridge
BatteryHandle = _bb.BatteryHandle
BatteryState = _bb.BatteryState
discover_batteries = _bb.discover_batteries
_uid = _bb._uid
_resp_ok = _bb._resp_ok

# ── test constants (mirrors the const stub) ───────────────────────────────────
BASE = "hacs_marstek_venus_e"
MARKER = "battery_state_of_charge"
POWER_S = "grid_power"
CAP_S = "battery_capacity"


# ── helpers ────────────────────────────────────────────────────────────────────

class _Ent:
    """Minimal fake entity registry entry."""
    def __init__(self, platform, entry_id, unique_id, entity_id, device_id=None):
        self.platform = platform
        self.config_entry_id = entry_id
        self.unique_id = unique_id
        self.entity_id = entity_id
        self.device_id = device_id


def _battery_ents(entry_id: str, device_id: str | None) -> list[_Ent]:
    """The three sensor entities a base battery config entry produces."""
    def uid(s):
        return f"{BASE}_{entry_id}_{s}"
    return [
        _Ent(BASE, entry_id, uid(MARKER),  f"sensor.{entry_id}_soc",   device_id),
        _Ent(BASE, entry_id, uid(POWER_S), f"sensor.{entry_id}_power", device_id),
        _Ent(BASE, entry_id, uid(CAP_S),   f"sensor.{entry_id}_cap",   device_id),
    ]


class _FakeReg:
    """Fake entity registry (dict-backed)."""
    def __init__(self, ents):
        self.entities = {e.entity_id: e for e in ents}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)


def _make_hass(ents, state_map=None):
    """Mock hass backed by a given entity list and optional state values."""
    hass = MagicMock()
    reg = _FakeReg(ents)
    _er_mock.async_get.return_value = reg  # er.async_get(hass) returns our registry

    state_map = state_map or {}

    def _get(entity_id):
        if entity_id not in state_map:
            return None
        s = MagicMock()
        s.state = state_map[entity_id]
        return s

    hass.states.get.side_effect = _get
    return hass


# ══════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ══════════════════════════════════════════════════════════════════════════════

def test_uid_format():
    assert _uid("abc", "battery_state_of_charge") == "hacs_marstek_venus_e_abc_battery_state_of_charge"


def test_resp_ok_true():
    assert _resp_ok({"results": {"e": {"ok": True}}}) is True


def test_resp_ok_false():
    assert _resp_ok({"results": {"e": {"ok": False}}}) is False


def test_resp_ok_empty_results():
    assert _resp_ok({"results": {}}) is False


def test_resp_ok_none():
    assert _resp_ok(None) is False


def test_resp_ok_missing_key():
    assert _resp_ok({}) is False


# ══════════════════════════════════════════════════════════════════════════════
# discover_batteries
# ══════════════════════════════════════════════════════════════════════════════

def test_discover_finds_one_battery():
    hass = _make_hass(_battery_ents("entry_a", "device_a"))
    handles = discover_batteries(hass)
    assert len(handles) == 1
    h = handles[0]
    assert h.battery_id == "entry_a"
    assert h.device_id == "device_a"
    assert h.soc_entity == "sensor.entry_a_soc"
    assert h.power_entity == "sensor.entry_a_power"
    assert h.capacity_entity == "sensor.entry_a_cap"


def test_discover_finds_two_batteries():
    ents = _battery_ents("entry_a", "device_a") + _battery_ents("entry_b", "device_b")
    handles = discover_batteries(_make_hass(ents))
    assert {h.battery_id for h in handles} == {"entry_a", "entry_b"}


def test_discover_skips_entry_without_soc_marker():
    # e.g. a leftover manager entry that exposes only non-battery sensors
    non_bat = _Ent(BASE, "mgr_entry", f"{BASE}_mgr_entry_something_else",
                   "sensor.mgr_something", "device_mgr")
    handles = discover_batteries(_make_hass([non_bat]))
    assert handles == []


def test_discover_skips_wrong_platform():
    ent = _Ent("other_integration", "entry_x", f"{BASE}_entry_x_{MARKER}",
               "sensor.soc_x", "device_x")
    handles = discover_batteries(_make_hass([ent]))
    assert handles == []


def test_discover_skips_when_no_device_id():
    # SOC entity has device_id=None → _device_id_for returns None → skip
    handles = discover_batteries(_make_hass(_battery_ents("entry_nd", None)))
    assert handles == []


# ══════════════════════════════════════════════════════════════════════════════
# BatteryBridge.read_all
# ══════════════════════════════════════════════════════════════════════════════

def test_read_all_happy_path():
    hass = _make_hass(
        _battery_ents("entry_a", "device_a"),
        {
            "sensor.entry_a_soc": "75.0",
            "sensor.entry_a_power": "-500",
            "sensor.entry_a_cap": "5120.0",
        },
    )
    states = BatteryBridge(hass).read_all()
    assert len(states) == 1
    s = states[0]
    assert s.soc == 75.0
    assert s.power == -500
    assert s.capacity == 5120.0
    assert s.available is True


def test_read_all_unavailable_soc():
    hass = _make_hass(
        _battery_ents("entry_a", "device_a"),
        {"sensor.entry_a_soc": "unavailable"},
    )
    s = BatteryBridge(hass).read_all()[0]
    assert s.available is False
    assert s.soc is None


def test_read_all_missing_state():
    # hass.states.get returns None for every entity → no numeric values
    hass = _make_hass(_battery_ents("entry_a", "device_a"), {})
    s = BatteryBridge(hass).read_all()[0]
    assert s.available is False
    assert s.power == 0
    assert s.capacity is None


def test_read_all_non_numeric_state():
    hass = _make_hass(
        _battery_ents("entry_a", "device_a"),
        {"sensor.entry_a_soc": "not_a_number", "sensor.entry_a_power": "300"},
    )
    s = BatteryBridge(hass).read_all()[0]
    assert s.soc is None
    assert s.available is False


# ══════════════════════════════════════════════════════════════════════════════
# BatteryBridge.set_passive
# ══════════════════════════════════════════════════════════════════════════════

async def test_set_passive_calls_correct_service():
    hass = MagicMock()
    hass.services.async_call = AsyncMock(
        return_value={"results": {"entry_a": {"ok": True}}}
    )
    result = await BatteryBridge(hass).set_passive(
        {"entry_a": 800}, {"entry_a": "device_a"}, cd_time=10
    )
    assert result == {"entry_a": True}
    args = hass.services.async_call.call_args[0]
    assert args[0] == BASE
    assert args[1] == "set_passive_mode"
    assert args[2]["power"] == 800
    assert args[2]["cd_time"] == 10


async def test_set_passive_nack_returns_false():
    hass = MagicMock()
    hass.services.async_call = AsyncMock(
        return_value={"results": {"e": {"ok": False}}}
    )
    result = await BatteryBridge(hass).set_passive({"e": 100}, {"e": "dev_e"}, 0)
    assert result == {"e": False}


async def test_set_passive_service_exception_returns_false():
    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("UDP gone"))
    result = await BatteryBridge(hass).set_passive({"e": 100}, {"e": "dev_e"}, 0)
    assert result == {"e": False}


async def test_set_passive_skips_battery_not_in_device_map():
    # setpoints has a battery_id that isn't in device_by_id → no service call
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    result = await BatteryBridge(hass).set_passive({"entry_a": 100}, {}, 0)
    assert result == {}
    hass.services.async_call.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# BatteryBridge.release_all
# ══════════════════════════════════════════════════════════════════════════════

async def test_release_all_sends_auto_to_each_device():
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    await BatteryBridge(hass).release_all(["dev_a", "dev_b"])
    assert hass.services.async_call.await_count == 2
    for call in hass.services.async_call.call_args_list:
        assert call[0][1] == "set_mode"
        assert call[0][2]["mode"] == "Auto"


async def test_release_all_tolerates_service_exception():
    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("gone"))
    await BatteryBridge(hass).release_all(["dev_a"])  # must not raise


async def test_release_all_empty_list_makes_no_calls():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    await BatteryBridge(hass).release_all([])
    hass.services.async_call.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _sync, _async = [], []
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_"):
            continue
        (_async if asyncio.iscoroutinefunction(_fn) else _sync).append((_name, _fn))

    count = 0
    for name, fn in _sync:
        fn()
        print(f"  PASS {name}")
        count += 1
    for name, fn in _async:
        asyncio.run(fn())
        print(f"  PASS {name}")
        count += 1

    print(f"\n{count} battery_bridge tests passed ✓")
