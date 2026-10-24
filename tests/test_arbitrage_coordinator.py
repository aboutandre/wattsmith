"""ArbitrageCoordinator: retry sooner while there are no tariff prices (hel-143). No HA.

After a restart tibber.get_prices can fail or come back empty. The tick then has no
priced bucket, the plan reads "no forecast" and nothing happens until the next tick.
That used to be 5 min away; now the coordinator polls on a short back-off until
prices are back.

Run: pytest tests/test_arbitrage_coordinator.py
"""
import asyncio
import importlib.util
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

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
        self.update_interval = update_interval


_uc.DataUpdateCoordinator = DataUpdateCoordinator
sys.modules["homeassistant.helpers.update_coordinator"] = _uc

_util = ModuleType("homeassistant.util")
_dt = ModuleType("homeassistant.util.dt")
_dt.now = lambda: datetime.now(timezone.utc)
_dt.utcnow = lambda: datetime.now(timezone.utc)
_dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
_util.dt = _dt
sys.modules["homeassistant.util"] = _util
sys.modules["homeassistant.util.dt"] = _dt

# ── load the real wattsmith modules; fake the battery bridge ─────────────────
_base = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith"
_pkg = ModuleType("wattsmith")
_pkg.__path__ = [str(_base)]
sys.modules["wattsmith"] = _pkg

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


for _m in ("const", "economics", "settings", "arbitrage", "load_profile", "soc_drift"):
    _load(_m)
_coord = _load("arbitrage_coordinator")
ArbitrageCoordinator = _coord.ArbitrageCoordinator
TICK_S = _coord.ARBITRAGE_TICK_S
LOGGER_NAME = _coord.__name__


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeServices:
    """tibber.get_prices: a queue of outcomes, each an exception or a response."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        assert (domain, service) == ("tibber", "get_prices")
        self.calls += 1
        out = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(out, Exception):
            raise out
        return out


def _prices_resp(hours: int = 40) -> dict:
    """What tibber.get_prices returns: hourly prices from the start of this hour."""
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return {"prices": {"Home": [
        {"start_time": (start + timedelta(hours=h)).isoformat(), "price": 0.14 + 0.01 * (h % 5)}
        for h in range(hours)
    ]}}


class FakeBridge:
    def read_all(self):
        return [SimpleNamespace(battery_id="b1", soc=11.0, capacity=5120.0, power=0, available=True)]


def _coordinator(*outcomes) -> ArbitrageCoordinator:
    hass = SimpleNamespace(services=FakeServices(*outcomes), data={}, states=MagicMock())
    hass.states.get = lambda _eid: None
    entry = SimpleNamespace(entry_id="e1", options={})
    c = ArbitrageCoordinator(hass, entry)
    c.bridge = FakeBridge()
    return c


def _interval_s(c) -> float:
    return c.update_interval.total_seconds()


def _run(coro):
    return asyncio.run(coro)


# ── the schedule ─────────────────────────────────────────────────────────────

def test_retry_delay_backs_off_and_stops_at_the_normal_interval():
    delay = _coord.price_retry_delay_s
    assert [delay(n, 300.0) for n in range(0, 9)] == [300, 15, 30, 60, 120, 240, 300, 300, 300]
    assert delay(10_000, 300.0) == 300.0          # no float overflow on a long outage
    assert delay(1, 10.0) == 10.0                 # never longer than the normal interval


# ── the coordinator ──────────────────────────────────────────────────────────

def test_a_tick_without_prices_polls_again_in_seconds_not_minutes():
    c = _coordinator(RuntimeError("Service tibber.get_prices not found"))
    assert _interval_s(c) == TICK_S
    data = _run(c._async_update_data())
    assert data["horizon_buckets"] == 0           # "no forecast", as seen live on 2026-10-24
    assert _interval_s(c) == 15.0


def test_the_interval_backs_off_while_prices_stay_away_and_resets_when_they_return():
    c = _coordinator(RuntimeError("not ready"), {"prices": {}}, RuntimeError("not ready"),
                     _prices_resp())
    seen = []
    for _ in range(3):
        _run(c._async_update_data())
        seen.append(_interval_s(c))
    assert seen == [15.0, 30.0, 60.0]

    data = _run(c._async_update_data())
    assert data["horizon_buckets"] > 0
    assert _interval_s(c) == TICK_S
    assert c._price_misses == 0


def test_empty_and_uncovering_prices_count_as_missing():
    # an empty answer, and prices that all lie in the past: no bucket covers "now"
    old = datetime.now(timezone.utc) - timedelta(days=3)
    stale = {"prices": {"Home": [{"start_time": (old + timedelta(hours=h)).isoformat(), "price": 0.2}
                                 for h in range(24)]}}
    for outcome in ({"prices": {}}, {}, stale):
        c = _coordinator(outcome)
        data = _run(c._async_update_data())
        assert data["horizon_buckets"] == 0
        assert _interval_s(c) == 15.0, outcome


def test_a_healthy_tick_keeps_the_normal_interval():
    c = _coordinator(_prices_resp())
    data = _run(c._async_update_data())
    assert data["horizon_buckets"] > 0
    assert _interval_s(c) == TICK_S


def test_prices_problem_names_the_cause():
    c = _coordinator(RuntimeError("Service tibber.get_prices not found"), {"prices": {}}, _prices_resp())
    assert _run(c._prices()) == []
    assert "not found" in c._price_problem
    assert _run(c._prices()) == []
    assert "no prices" in c._price_problem
    assert _run(c._prices())
    assert c._price_problem is None


def test_log_is_quiet_then_warns_once_then_says_when_prices_are_back(caplog):
    c = _coordinator(RuntimeError("not ready"), RuntimeError("not ready"), RuntimeError("not ready"),
                     RuntimeError("not ready"), RuntimeError("not ready"), _prices_resp())
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for _ in range(6):
            _run(c._async_update_data())
    miss = [r for r in caplog.records if "no priced bucket" in r.getMessage()]
    assert [r.levelno for r in miss] == [logging.INFO, logging.DEBUG, logging.DEBUG,
                                         logging.WARNING, logging.DEBUG]
    assert "not ready" in miss[0].getMessage()
    back = [r for r in caplog.records if "are back" in r.getMessage()]
    assert len(back) == 1 and back[0].levelno == logging.INFO
    assert "5 empty tick" in back[0].getMessage()
