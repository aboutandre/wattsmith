"""Unit tests for the wallbox abstraction + the go-e driver.

Covers:
  - wallbox.needs_write — the per-tick drift reconcile (successor of the frc
    drift fix; the go-e provably loses its force state, see API issue #117),
  - wallbox_goe parsing (frc/psm/car/nrg/cll → normalized WallboxState),
  - wallbox_goe.apply param building incl. the acs=1 trx authorization.

Pure logic + a fake aiohttp session — no Home Assistant, no network.

Run directly:   python3 tests/test_wallbox.py
Or with pytest: pytest tests/test_wallbox.py
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

# ── stub aiohttp + the wattsmith package deps before loading the modules ─────
sys.modules["aiohttp"] = MagicMock()

_pkg = ModuleType("wattsmith")
sys.modules.setdefault("wattsmith", _pkg)
_settings = ModuleType("wattsmith.settings")
_settings.EV_GOE_TIMEOUT_S = 5.0
sys.modules["wattsmith.settings"] = _settings


def _load(name: str):
    path = Path(__file__).resolve().parents[1] / "custom_components" / "wattsmith" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"wattsmith.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "wattsmith"
    sys.modules[f"wattsmith.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_wallbox = _load("wallbox")
_goe = _load("wallbox_goe")

WallboxState = _wallbox.WallboxState
needs_write = _wallbox.needs_write
FORCE_ON, FORCE_OFF, FORCE_NEUTRAL = _wallbox.FORCE_ON, _wallbox.FORCE_OFF, _wallbox.FORCE_NEUTRAL
GoeWallbox = _goe.GoeWallbox


# ── needs_write: the drift reconcile ─────────────────────────────────────────

def test_off_in_sync_no_write():
    assert needs_write(False, 0, 1, WallboxState(force=FORCE_OFF)) is False


def test_off_but_neutral_drift_writes():
    # THE incident class: charger drifted to neutral (= charge by default)
    assert needs_write(False, 0, 1, WallboxState(force=FORCE_NEUTRAL)) is True


def test_off_but_forced_on_writes():
    assert needs_write(False, 0, 1, WallboxState(force=FORCE_ON, amp=16, phases=3)) is True


def test_unreadable_charger_always_writes():
    assert needs_write(False, 0, 1, None) is True
    assert needs_write(True, 8, 3, None) is True


def test_charge_in_sync_no_write():
    st = WallboxState(force=FORCE_ON, amp=10, phases=3)
    assert needs_write(True, 10, 3, st) is False


def test_charge_amp_mismatch_writes():
    st = WallboxState(force=FORCE_ON, amp=6, phases=3)
    assert needs_write(True, 10, 3, st) is True


def test_charge_phase_mismatch_writes():
    st = WallboxState(force=FORCE_ON, amp=10, phases=1)
    assert needs_write(True, 10, 3, st) is True


def test_charge_unknown_phases_tolerated():
    # psm=0 (Auto) → phases None: don't rewrite every tick over an unknowable
    st = WallboxState(force=FORCE_ON, amp=10, phases=None)
    assert needs_write(True, 10, 3, st) is False


def test_charge_unknown_force_writes():
    assert needs_write(True, 10, 3, WallboxState(force=None, amp=10, phases=3)) is True


# ── go-e parsing helpers ─────────────────────────────────────────────────────

def test_parse_force_codes():
    assert _goe._FORCE_BY_FRC == {0: FORCE_NEUTRAL, 1: FORCE_OFF, 2: FORCE_ON}


def test_parse_phases_from_psm():
    assert _goe._phases_from_psm(1) == 1
    assert _goe._phases_from_psm(2) == 3
    assert _goe._phases_from_psm(0) is None   # Auto
    assert _goe._phases_from_psm(None) is None


def test_parse_power_from_nrg():
    nrg = [230.0] * 11 + [1208.05, 95.4]
    assert _goe._power_from_nrg(nrg) == 1208.05
    assert _goe._power_from_nrg(None) is None
    assert _goe._power_from_nrg([1, 2]) is None


def test_parse_max_amp_from_cll():
    assert _goe._max_amp_from_cll({"currentLimitMax": 16, "cableCurrentLimit": 32}) == 16
    assert _goe._max_amp_from_cll({"adapterCurrentLimit": 20}) == 20
    assert _goe._max_amp_from_cll({}) is None
    assert _goe._max_amp_from_cll(None) is None


def test_car_state_allowlist():
    # 1=idle → disconnected; 2/3/4 → connected, not done; unknown → (None, None)
    assert _goe._CAR_STATES[1] == (False, False)
    assert _goe._CAR_STATES[2] == (True, False)
    assert _goe._CAR_STATES[4] == (True, False)   # Finished: BMS-safe restart
    assert _goe._CAR_STATES.get(99, (None, None)) == (None, None)


# ── driver read/apply against a fake HTTP session ────────────────────────────

class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Records get() calls; returns queued responses."""

    def __init__(self, payload=None, status=200):
        self.calls: list[tuple[str, dict]] = []
        self._payload = payload
        self._status = status

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self._status, self._payload)


def _run(coro):
    return asyncio.run(coro)


def test_read_normalizes_full_status():
    session = _FakeSession(payload={
        "frc": 1, "psm": 1, "amp": 6, "car": 4,
        "nrg": [0] * 11 + [1208.0], "cll": {"currentLimitMax": 16},
        "acs": 0, "trx": None,
    })
    st = _run(GoeWallbox("192.0.2.1", session).read())
    assert st.force == FORCE_OFF and st.amp == 6 and st.phases == 1
    assert st.power_w == 1208.0 and st.connected is True and st.done is False
    assert st.max_amp == 16
    url, params = session.calls[0]
    assert url.endswith("/api/status") and "frc" in params["filter"]


def test_read_unreachable_returns_none():
    session = _FakeSession(status=500)
    assert _run(GoeWallbox("192.0.2.1", session).read()) is None


def test_apply_off_sends_only_frc1():
    session = _FakeSession(payload={})
    ok = _run(GoeWallbox("192.0.2.1", session).apply(False, 0, 1))
    assert ok is True
    url, params = session.calls[0]
    assert url.endswith("/api/set") and params == {"frc": "1"}


def test_apply_charge_sends_full_params():
    session = _FakeSession(payload={})
    _run(GoeWallbox("192.0.2.1", session).apply(True, 10, 3))
    _url, params = session.calls[0]
    assert params == {"frc": "2", "amp": "10", "psm": "2"}


def test_apply_authorizes_when_acs_wait():
    # charger in acs=1 (Wait) with no open transaction → apply(charge) adds trx=0
    session = _FakeSession(payload={
        "frc": 0, "psm": 1, "amp": 6, "car": 3, "nrg": [0] * 12,
        "cll": {}, "acs": 1, "trx": None,
    })
    box = GoeWallbox("192.0.2.1", session)
    _run(box.read())                      # learn acs/trx
    _run(box.apply(True, 6, 1))
    _url, params = session.calls[-1]
    assert params == {"frc": "2", "amp": "6", "psm": "1", "trx": "0"}


def test_apply_no_trx_when_acs_open():
    session = _FakeSession(payload={
        "frc": 0, "psm": 1, "amp": 6, "car": 3, "nrg": [0] * 12,
        "cll": {}, "acs": 0, "trx": None,
    })
    box = GoeWallbox("192.0.2.1", session)
    _run(box.read())
    _run(box.apply(True, 6, 1))
    _url, params = session.calls[-1]
    assert "trx" not in params


def test_release_sends_neutral():
    session = _FakeSession(payload={})
    _run(GoeWallbox("192.0.2.1", session).release())
    _url, params = session.calls[0]
    assert params == {"frc": "0"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)} wallbox tests passed ✓")
