"""go-e Charger driver — the only module that knows go-e specifics.

Implements wallbox.WallboxDriver over the go-e local HTTP API v2
(https://github.com/goecharger/go-eCharger-API-v2). Everything go-e-flavoured
is contained here: frc/psm/car codes, the nrg power array, cll hardware limits,
and the acs/trx access-control handshake.

go-e key semantics used:
  frc  forceState: 0=Neutral (charger's own logic), 1=Off, 2=On
  psm  phase switch mode: 0=Auto, 1=1-phase, 2=3-phase
  car  1=Idle(no car), 2=Charging, 3=Connected/waiting, 4=Finished(prev session)
  nrg  power array; index 11 = total charging power (W)
  cll  current limits; currentLimitMax = effective hardware/cable/adapter cap
  acs  access control: 0=Open, 1=Wait(needs authorization), 2=EVCMS
  trx  transaction: null=none, 0=authorized without card
  modelStatus  the charger's own "why (not) charging" reason enum

Reliability notes (firmware-confirmed, go-e API issue #117):
  - frc resets to 0 on car unplug (BY DESIGN), on restart, and sporadically.
    Never trust it to persist; the coordinator reconciles every tick.
  - With acs=1 the charger is default-deny: frc=0 does NOT charge without an
    authorized transaction. If the user enables acs=1, this driver authorizes
    (trx=0) when charging is wanted — the firmware-sanctioned way to keep an
    external brain in charge even across HA outages. With acs=0 (default) the
    per-tick frc reconcile is the only guard while HA is running.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .settings import EV_GOE_TIMEOUT_S
from .wallbox import FORCE_NEUTRAL, FORCE_OFF, FORCE_ON, WallboxState

_LOGGER = logging.getLogger(__name__)

_STATUS_FILTER = "frc,psm,amp,car,nrg,cll,acs,trx"

_FORCE_BY_FRC = {0: FORCE_NEUTRAL, 1: FORCE_OFF, 2: FORCE_ON}
_FRC_BY_FORCE = {v: k for k, v in _FORCE_BY_FRC.items()}

# car code → (connected, done). Explicit allowlist (F-06): anything not listed
# maps to (None, None) = unknown, and the coordinator fails safe instead of
# guessing that a novel/error state means "car ready to charge".
_CAR_STATES: dict[int, tuple[bool, bool]] = {
    1: (False, False),  # Idle / no car
    2: (True, False),   # Charging
    3: (True, False),   # Connected, waiting
    # 4 = Finished: the PREVIOUS session ended but the car is still plugged in.
    # Treated as connected-and-not-done so solar charging can restart; the
    # car's own BMS refuses power if it is genuinely full.
    4: (True, False),
}


class GoeWallbox:
    """WallboxDriver for a go-e Charger on the local network."""

    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        self._host = host
        self._session = session
        self._acs: int | None = None   # last-seen access-control mode
        self._trx: int | None = None   # last-seen transaction state

    # ---- WallboxDriver ---------------------------------------------------

    async def read(self) -> WallboxState | None:
        data = await self._get("/api/status", {"filter": _STATUS_FILTER})
        if data is None:
            return None
        car = _as_int(data.get("car"))
        connected, done = _CAR_STATES.get(car, (None, None))
        if connected is None and car is not None:
            _LOGGER.warning("go-e reported unknown car state %s — treating as unknown", car)
        self._acs = _as_int(data.get("acs"))
        self._trx = _as_int(data.get("trx"))
        return WallboxState(
            force=_FORCE_BY_FRC.get(_as_int(data.get("frc"))),
            amp=_as_int(data.get("amp")),
            phases=_phases_from_psm(_as_int(data.get("psm"))),
            power_w=_power_from_nrg(data.get("nrg")),
            connected=connected,
            done=done,
            max_amp=_max_amp_from_cll(data.get("cll")),
        )

    async def apply(self, charge: bool, amp: int, phases: int) -> bool:
        params: dict[str, str] = {"frc": str(_FRC_BY_FORCE[FORCE_ON if charge else FORCE_OFF])}
        if charge:
            params["amp"] = str(int(amp))
            params["psm"] = "1" if phases == 1 else "2"
            # acs=1 (Wait) default-deny: charging also needs an authorized
            # transaction. Authorize alongside the force-on; harmless if the
            # transaction is already open. Never auto-revoke: frc=off already
            # blocks, and the transaction expires on unplug (firmware behavior).
            if self._acs == 1 and self._trx is None:
                params["trx"] = "0"
        return await self._set(params)

    async def release(self) -> None:
        """Hand control back to the charger's own logic (frc → Neutral)."""
        await self._set({"frc": str(_FRC_BY_FORCE[FORCE_NEUTRAL])})

    # ---- HTTP ------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any] | None:
        try:
            timeout = aiohttp.ClientTimeout(total=EV_GOE_TIMEOUT_S)
            url = f"http://{self._host}{path}"
            async with self._session.get(url, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    _LOGGER.debug("go-e %s HTTP %s", path, resp.status)
                    return None
                return await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001 — any transport failure = unreadable
            _LOGGER.debug("go-e %s failed: %s", path, err)
            return None

    async def _set(self, params: dict[str, str]) -> bool:
        result = await self._get("/api/set", params)
        if result is None:
            _LOGGER.warning("go-e command failed (%s)", params)
            return False
        _LOGGER.debug("go-e set %s → %s", params, result)
        return True


# ---- parsing helpers (pure, unit-tested) ----------------------------------

def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _phases_from_psm(psm: int | None) -> int | None:
    """psm 1 → 1-phase, psm 2 → 3-phase, psm 0 (Auto) / unknown → None."""
    if psm == 1:
        return 1
    if psm == 2:
        return 3
    return None


def _power_from_nrg(nrg: Any) -> float | None:
    """nrg[11] = total charging power in W."""
    try:
        return float(nrg[11])
    except (TypeError, ValueError, IndexError):
        return None


def _max_amp_from_cll(cll: Any) -> int | None:
    """The effective hardware limit: go-e reports currentLimitMax (already the
    min over cable/adapter/temperature limits). Fall back to adapterCurrentLimit."""
    if not isinstance(cll, dict):
        return None
    for key in ("currentLimitMax", "adapterCurrentLimit", "cableCurrentLimit"):
        v = _as_int(cll.get(key))
        if v is not None and v > 0:
            return v
    return None
