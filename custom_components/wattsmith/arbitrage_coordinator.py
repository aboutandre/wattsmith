"""Arbitrage coordinator — the HA I/O shell around the pure arbitrage planner.

Advisory by default (Phase 3): fetches confirmed tariff prices + PV forecast +
learned load, builds 15-min buckets, runs the pure planner, and exposes the
plan + economics. It NEVER commands anything itself — the manager reads the
public surface (charge_floor_soc / hold_floor_soc) and only acts on it when the
Arbitrage switch is on (Phase 4). Every tick writes the advisory decision into
the history DB's arb_reason / grid_charge_wh columns for later validation.

Slow cadence (a few minutes): prices/forecast change on the order of 15 min.
Like the other coordinators, the tick never raises — any failure yields an
inert plan (no charge, no hold) so a forecast hiccup can't affect dispatch.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .arbitrage import (
    BatteryModel,
    Econ,
    build_buckets,
    plan_arbitrage,
    pv_slots_from_detailed,
    solcast_forecast_entities,
)
from .battery_bridge import BatteryBridge
from .const import (
    CONF_ARBITRAGE_ENABLED,
    CONF_ARBITRAGE_PV_CONFIDENCE,
    CONF_BATTERY_CONFIG,
    CONF_CALIBRATION_ENABLED,
    CONF_CALIBRATION_GRID,
    CONF_CALIBRATION_MAX_DAYS,
    CONF_CALIBRATION_THRESHOLD_PTS,
    CONF_ETA_OVERRIDE,
    CONF_FORECAST_MARGIN_PCT,
    CONF_IMPORT_POWER_CAP_W,
    CONF_MAX_BATTERY_SOC,
    CONF_MIN_ARBITRAGE_MARGIN_CT,
    CONF_MIN_SOC,
    CONF_SOLCAST_FORECAST_SENSOR,
    CONF_TIBBER_SENSOR,
    CONF_WEAR_COST_CT,
    DOMAIN,
)
from .economics import (
    fleet_wear_cost_ct,
    parse_eta_override,
    wear_cost_ct_per_kwh,
)
from .settings import (
    ARBITRAGE_HORIZON_H,
    DEFAULT_ARBITRAGE_PV_CONFIDENCE,
    DEFAULT_BATTERY_COST_EUR,
    DEFAULT_FORECAST_MARGIN_PCT,
    DEFAULT_ETA_SEED,
    DEFAULT_EXPECTED_CYCLES,
    DEFAULT_MAX_BATTERY_SOC,
    DEFAULT_MAX_BATTERY_POWER,
    DEFAULT_MIN_ARBITRAGE_MARGIN_CT,
    DEFAULT_MIN_SOC,
    CALIBRATION_GRID_EXTRA_PTS,
    CALIBRATION_LOOKAHEAD_BUCKETS,
    DEFAULT_CALIBRATION_ENABLED,
    DEFAULT_CALIBRATION_GRID,
    DEFAULT_CALIBRATION_MAX_DAYS,
    DEFAULT_CALIBRATION_THRESHOLD_PTS,
    DRIFT_HISTORY_DAYS,
    DRIFT_MAX_GAP_BUCKETS,
    DRIFT_MIN_WINDOWS,
    DRIFT_STATE_DAYS,
    ETA_MIN_WINDOWS,
    ETA_REFRESH_S,
    ETA_VALID_RANGE,
)
from .soc_drift import (
    CalibrationPlan,
    DriftFit,
    battery_drift_now,
    fit_drift_rate,
    fit_eta_standby,
    full_to_full_windows,
    plan_calibration,
    reset_events,
)

_LOGGER = logging.getLogger(__name__)

ARBITRAGE_TICK_S = 300.0  # 5 min — prices/forecast move on ~15-min timescales


class ArbitrageCoordinator(DataUpdateCoordinator):
    """Computes the advisory arbitrage plan + economics for the fleet."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_arbitrage",
            update_interval=timedelta(seconds=ARBITRAGE_TICK_S),
        )
        self.entry = entry
        self.bridge = BatteryBridge(hass)
        self._measured_eta: float | None = None
        # last history refresh (None until the first one lands — the DB starts after us)
        self._eta_measured_at: float | None = None
        self._eta_detail: dict[str, Any] = {}
        # SOC drift model (hel-134): per-battery rates + the fleet fallback
        self._drift_fits: dict[str, DriftFit] = {}
        self._fleet_drift: DriftFit | None = None
        self._calibration: CalibrationPlan | None = None

    # ---- public surface (read by the manager, gated by the switch) ------
    @property
    def enabled(self) -> bool:
        return bool(self.entry.options.get(CONF_ARBITRAGE_ENABLED, False))

    @property
    def charge_floor_soc(self) -> float | None:
        """Grid-charge target SOC for this window, or None (advisory/off)."""
        if not self.enabled or not self.data:
            return None
        return self.data.get("target_soc")

    @property
    def calibration_open_ceiling(self) -> bool:
        """True while a calibration full charge is due (manager lifts the ceiling to 100%)."""
        return bool(self._calibration and self._calibration.open_ceiling)

    @property
    def hold_floor_soc(self) -> float | None:
        """Protect-until SOC (discharge hold), or None."""
        if not self.enabled or not self.data:
            return None
        return self.data.get("hold_floor_soc")

    def set_measured_eta(self, eta: float | None) -> None:
        self._measured_eta = eta

    @property
    def eta_override(self) -> float | None:
        """The UI/options override as a fraction, or None (= use the measurement)."""
        return parse_eta_override(self.entry.options.get(CONF_ETA_OVERRIDE))

    # ---- economics inputs ----------------------------------------------
    def _eta(self) -> tuple[float, str]:
        override = self.eta_override
        if override is not None:
            return override, "override"
        if self._measured_eta is not None:
            return self._measured_eta, "measured"
        return DEFAULT_ETA_SEED, "seed"

    def _recorder_query(self):
        recorder = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id + "_history")
        query = getattr(recorder, "async_query_range", None)
        return query if callable(query) else None

    async def _async_refresh_history(self) -> None:
        """Re-fit round-trip η and the per-battery SOC drift rates from the DB.

        Both come from full-to-full windows (soc_drift): between two BMS resets the
        true SOC is 100% at both ends, so the energy balance is exact and the size
        of each reset measures the drift. Keeps previous values on thin data or an
        implausible η, and never raises.
        """
        query = self._recorder_query()
        if query is None:
            return                      # DB not up yet (starts after us) or disabled
        import time
        now = time.time()
        self._eta_measured_at = now     # a failed attempt waits for the next refresh too
        try:
            rows = await query(int(now - DRIFT_HISTORY_DAYS * 86400), int(now),
                               table="battery_bucket", limit=DRIFT_HISTORY_DAYS * 96 * 16)
            windows = full_to_full_windows(rows, max_gap_buckets=DRIFT_MAX_GAP_BUCKETS)
            # η needs the exact energy balance: only gap-free windows
            fit = fit_eta_standby([w for w in windows if w.missing_buckets == 0],
                                  min_windows=ETA_MIN_WINDOWS)
            by_bat: dict[str, list] = {}
            for w in windows:
                by_bat.setdefault(w.battery_id, []).append(w)
            self._drift_fits = {b: f for b, ws in by_bat.items()
                                if (f := fit_drift_rate(ws, min_windows=DRIFT_MIN_WINDOWS))}
            self._fleet_drift = fit_drift_rate(windows, min_windows=DRIFT_MIN_WINDOWS)
        except Exception as err:  # noqa: BLE001 - measurement must never break the tick
            _LOGGER.warning("arbitrage: history refresh failed: %s", err)
            return
        # log every BMS reset in the window (idempotent) so the drift model can be
        # checked against reality over time — best effort, never breaks the refresh
        recorder = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id + "_history")
        logger = getattr(recorder, "async_record_calibration_events", None)
        if callable(logger):
            try:
                await logger(reset_events(windows, min_windows=DRIFT_MIN_WINDOWS))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("arbitrage: logging calibration events failed: %s", err)
        lo, hi = ETA_VALID_RANGE
        valid = fit is not None and lo <= fit.eta <= hi
        self._eta_detail = {
            "eta_measured": round(fit.eta, 4) if fit else None,
            "eta_measured_valid": valid,
            "eta_standby_w": round(fit.standby_w, 1) if fit else None,
            "eta_windows": fit.windows if fit else 0,
            "eta_window_days": DRIFT_HISTORY_DAYS,
            "eta_measured_at": dt_util.utc_from_timestamp(now).isoformat(),
        }
        if valid:
            self._measured_eta = fit.eta
        elif fit is not None:
            _LOGGER.warning("arbitrage: measured η %.3f outside %s — keeping %s",
                            fit.eta, ETA_VALID_RANGE, self._measured_eta or DEFAULT_ETA_SEED)

    async def _async_calibration(self, bat: BatteryModel, buckets, eta: float) -> dict[str, Any]:
        """Predict each battery's SOC drift now and decide on a calibration charge."""
        opts = self.entry.options
        query = self._recorder_query()
        states = self.bridge.read_all()
        import time
        now = time.time()
        if query is None:
            # history DB not up yet (it starts after us) or disabled: we cannot know
            # when the batteries were last full, so decide nothing rather than guess
            self._calibration = None
            return {"calibration_status": "unknown",
                    "calibration_reason": "history DB not available yet",
                    "calibration_batteries": {}}
        rows = await query(int(now - DRIFT_STATE_DAYS * 86400), int(now),
                           table="battery_bucket", limit=DRIFT_STATE_DAYS * 96 * 16)
        drift = battery_drift_now(rows, self._drift_fits, self._fleet_drift,
                                  {s.battery_id: s.soc for s in states}, now)
        per_bucket = bat.charge_power_w * 0.25
        cap_w = float(opts.get(CONF_IMPORT_POWER_CAP_W, 0) or 0)
        if cap_w > 0:
            per_bucket = min(per_bucket, cap_w * 0.25)
        need = bat.capacity_wh * max(0.0, 100.0 - bat.soc_pct) / 100.0 / max(eta, 0.5) ** 0.5
        self._calibration = plan_calibration(
            drift, now,
            enabled=bool(opts.get(CONF_CALIBRATION_ENABLED, DEFAULT_CALIBRATION_ENABLED)),
            threshold_pts=float(opts.get(CONF_CALIBRATION_THRESHOLD_PTS, DEFAULT_CALIBRATION_THRESHOLD_PTS)),
            max_days=float(opts.get(CONF_CALIBRATION_MAX_DAYS, DEFAULT_CALIBRATION_MAX_DAYS)),
            grid_enabled=bool(opts.get(CONF_CALIBRATION_GRID, DEFAULT_CALIBRATION_GRID)),
            grid_extra_pts=CALIBRATION_GRID_EXTRA_PTS,
            prices_ct=[b.price_ct for b in buckets],
            need_wh=need, per_bucket_wh=per_bucket,
            lookahead=CALIBRATION_LOOKAHEAD_BUCKETS,
        )
        per_battery = {}
        for bid, d in sorted(drift.items()):
            fit = self._drift_fits.get(bid) or self._fleet_drift
            per_battery[bid[-4:]] = {
                "days_since_full": (round((now - d.last_full_ts) / 86400.0, 1)
                                    if d.last_full_ts is not None else None),
                "discharged_kwh": round(d.discharged_wh / 1000.0, 2),
                "predicted_drift_pts": (round(d.predicted_pts, 1)
                                        if d.predicted_pts is not None else None),
                "drift_pts_per_kwh": round(fit.pts_per_kwh, 2) if fit else None,
            }
        return {"calibration_status": self._calibration.status,
                "calibration_reason": self._calibration.reason,
                "calibration_batteries": per_battery}

    def _wear_ct(self) -> float:
        configured = self.entry.options.get(CONF_WEAR_COST_CT)
        if configured not in (None, ""):
            return float(configured)
        per_battery = []
        batt_cfg = self.entry.options.get(CONF_BATTERY_CONFIG) or {}
        for st in self.bridge.read_all():
            if not st.capacity:
                continue
            b = batt_cfg.get(st.battery_id, {})
            # fall back to the conservative fleet default when a battery has no
            # configured cost — never treat wear as 0 (that reads as "free" and
            # makes arbitrage over-aggressive).
            cost = float(b.get("cost_eur") or DEFAULT_BATTERY_COST_EUR)
            cycles = float(b.get("expected_cycles") or DEFAULT_EXPECTED_CYCLES)
            per_battery.append((cost, cycles, float(st.capacity)))
        w = fleet_wear_cost_ct(per_battery)
        return w if w is not None else 0.0

    def _fleet_model(self) -> BatteryModel | None:
        states = self.bridge.read_all()
        socs = [(s.soc, s.capacity) for s in states if s.soc is not None and s.capacity]
        if not socs:
            return None
        cap = sum(c for _s, c in socs)
        soc = sum(s * c for s, c in socs) / cap
        min_soc = float(self.entry.options.get(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        max_soc = (100.0 if self.calibration_open_ceiling
                   else float(self.entry.options.get(CONF_MAX_BATTERY_SOC, DEFAULT_MAX_BATTERY_SOC)))
        charge_power = DEFAULT_MAX_BATTERY_POWER * len(states)
        return BatteryModel(soc_pct=soc, capacity_wh=cap, min_soc=min_soc,
                            max_soc=max_soc, charge_power_w=charge_power)

    # ---- forecast inputs ------------------------------------------------
    async def _prices(self) -> list[tuple[float, float]]:
        """Confirmed tariff prices as [(start_epoch, EUR/kWh)]. Tibber get_prices.

        MUST pass `end` — get_prices with no args returns TODAY ONLY, which caps
        the horizon at midnight and defeats overnight arbitrage. `end` = now +
        horizon pulls tomorrow's published curve (its evening peak is what makes
        overnight grid-charging worthwhile).
        """
        end = (dt_util.now() + timedelta(hours=ARBITRAGE_HORIZON_H)).isoformat()
        try:
            resp = await self.hass.services.async_call(
                "tibber", "get_prices", {"end": end},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("arbitrage: get_prices failed: %s", err)
            return []
        # in-process HA returns the service_response directly; be robust to a wrapper
        resp = (resp or {}).get("service_response", resp) or {}
        out: list[tuple[float, float]] = []
        homes = resp.get("prices", {})
        for series in homes.values() if isinstance(homes, dict) else []:
            for p in series or []:
                start = p.get("start_time") or p.get("startsAt")
                price = p.get("price") if p.get("price") is not None else p.get("total")
                if start is None or price is None:
                    continue
                try:
                    dt = datetime.fromisoformat(str(start))
                    out.append((dt.timestamp(), float(price)))
                except (ValueError, TypeError):
                    continue
        return out

    def _pv_by_slot(self) -> dict[int, float]:
        """PV forecast per 15-min slot across today AND tomorrow (hel-131)."""
        periods: list = []
        for eid in solcast_forecast_entities(self.entry.options.get(CONF_SOLCAST_FORECAST_SENSOR) or ""):
            st = self.hass.states.get(eid)
            if st is None or not st.attributes:
                continue
            p = st.attributes.get("detailedForecast") or st.attributes.get("detailedHourly")
            if isinstance(p, list):
                periods.extend(p)
        confidence = self.entry.options.get(
            CONF_ARBITRAGE_PV_CONFIDENCE, DEFAULT_ARBITRAGE_PV_CONFIDENCE)
        return pv_slots_from_detailed(periods, confidence)

    def _load_by_hour(self) -> list[float] | None:
        mgr = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        learner = getattr(mgr, "_learner", None)
        baseline = getattr(mgr, "adaptive_baseline_w", 500.0)
        if learner is None:
            return None
        # W per hour -> Wh per 15-min bucket
        return [learner.baseline_for_slot(datetime.now().weekday(), h, baseline) * 0.25
                for h in range(24)]

    # ---- the (advisory) tick -------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        import time
        if self._eta_measured_at is None or time.time() - self._eta_measured_at >= ETA_REFRESH_S:
            await self._async_refresh_history()
        try:
            bat = self._fleet_model()
            if bat is None:
                return self._inert("no batteries")
            eta, eta_src = self._eta()
            wear = self._wear_ct()
            econ = Econ(
                eta=eta, wear_ct=wear,
                min_margin_ct=float(self.entry.options.get(
                    CONF_MIN_ARBITRAGE_MARGIN_CT, DEFAULT_MIN_ARBITRAGE_MARGIN_CT)),
                import_cap_w=float(self.entry.options.get(CONF_IMPORT_POWER_CAP_W, 0) or 0),
                forecast_margin_frac=float(self.entry.options.get(
                    CONF_FORECAST_MARGIN_PCT, DEFAULT_FORECAST_MARGIN_PCT)) / 100.0,
            )
            prices = await self._prices()
            pv = self._pv_by_slot()
            buckets = build_buckets(
                time.time(), prices, pv, self._load_by_hour(),
                horizon_h=ARBITRAGE_HORIZON_H,
            )
            plan = plan_arbitrage(buckets, bat, econ)
            try:
                calib = await self._async_calibration(bat, buckets, eta)
            except Exception as err:  # noqa: BLE001 - calibration must never break arbitrage
                _LOGGER.warning("arbitrage: calibration planning failed: %s", err)
                self._calibration = None
                calib = {"calibration_status": "error", "calibration_reason": str(err),
                         "calibration_batteries": {}}
            if self._calibration and self._calibration.grid_charge_now:
                # finish the calibration from the grid: charge to 100% this bucket
                plan = replace(plan, grid_charge_now_wh=bat.charge_power_w * 0.25, target_soc=100.0,
                               reason=f"calibration: {self._calibration.reason}")
            self._write_advisory(plan, eta, wear)
            return {
                **calib,
                "grid_charge_now_wh": plan.grid_charge_now_wh,
                "target_soc": plan.target_soc,
                "hold_floor_soc": plan.hold_floor_soc,
                "profitable_deficit_wh": plan.profitable_deficit_wh,
                "reason": plan.reason,
                "eta": eta, "eta_source": eta_src, "wear_ct": wear,
                "eta_override": self.eta_override, **self._eta_detail,
                "horizon_buckets": len(buckets),
                # last PV slot the forecast covers — if the priced horizon runs past
                # it, the planner is reading those hours as 0 W of sun
                "pv_forecast_until": (dt_util.utc_from_timestamp(max(pv) + 900).isoformat()
                                      if pv else None),
                "enabled": self.enabled,
            }
        except Exception as err:  # noqa: BLE001 - advisory tick must never raise
            _LOGGER.exception("arbitrage tick failed: %s", err)
            return self._inert(str(err))

    def _inert(self, reason: str) -> dict[str, Any]:
        return {
            "grid_charge_now_wh": 0.0, "target_soc": None, "hold_floor_soc": None,
            "profitable_deficit_wh": 0.0, "reason": reason,
            "eta": None, "eta_source": None, "wear_ct": None,
            "eta_override": self.eta_override, **self._eta_detail,
            "horizon_buckets": 0, "enabled": self.enabled,
            "calibration_status": self._calibration.status if self._calibration else None,
        }

    def _write_advisory(self, plan, eta: float, wear: float) -> None:
        """Best-effort stamp the current bucket's advisory columns in the DB."""
        rec = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id + "_history")
        writer = getattr(rec, "note_advisory", None)
        if callable(writer):
            writer(plan.grid_charge_now_wh, plan.reason, eta, wear / 100.0)
        note = getattr(rec, "note_calibration", None)
        if callable(note):
            note(self._calibration.status if self._calibration else None)
