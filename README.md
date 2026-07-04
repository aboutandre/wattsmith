# Wattsmith

**The standalone home-energy orchestration brain for Home Assistant.**

Wattsmith is the opinionated control layer for a self-owned, fully local energy
system: solar + a fleet of batteries + an EV charger. It reads the live picture
from your existing Home Assistant sensors and dispatches setpoints to the
batteries through the Marstek device integration's HA **services** — so it has
**no code dependency** on the device layer, only a runtime, service-call one.

It is a deliberate split: the [Marstek Venus E base integration][base] owns the
per-battery Local API device (sensors + `set_passive_mode`/`set_mode` services);
Wattsmith owns all the decisions.

## What it does

- **Zero-grid dispatch** — a PD control loop drives net grid power to ~0 by
  charging/discharging the battery fleet, split across batteries by state of
  charge, with safety fallbacks (grid-staleness, per-battery health, bad-cycle
  watchdog → SAFE).
- **EV charging** — PV-surplus priority cascade, cheap-window grid charging,
  automatic 1↔3-phase switching, and a battery-bridge for brief PV dips.
  Charger brands are pluggable **wallbox drivers** (currently: go-e via its
  local HTTP API); the planner and coordinator are brand-agnostic.
- **Adaptive PV charging** — reactive gate that holds the battery fleet at its
  normal SOC cap through the day, then opens to the ceiling only when remaining
  forecast surplus ≤ headroom, so the fill to 100% lands on the last hours of
  sun (zero feed-in, zero evening import).

## Architecture

```
Home Assistant
├── hacs_marstek_venus_e   (base: per-battery Local API device + services)
│        ▲ reads SOC/power/cap via sensor entities
│        ▼ dispatches setpoints via hass.services (set_passive_mode, target=device)
└── wattsmith              (this: the brain — manager, planner, controller,
                            safety, EV coordinator, adaptive charging + all
                            site configuration)
```

The pure decision modules carry no Home Assistant dependency and are unit-tested
in `tests/`:

| Module | Responsibility |
|---|---|
| `controller.py` | Zero-grid PD control + per-battery SOC-split dispatch |
| `safety.py` | Safety gates and SAFE-state snapshot |
| `planner.py` | Energy-manager decision logic |
| `ev_planner.py` | EV charge planning (cascade, cheap window, phase switching) |
| `adaptive.py` | Adaptive PV ceiling gate (pure function, no HA) |
| `validate_config.py` | Cross-value config sanity checks (reserve↔cap clamp, phase thresholds, …) |
| `wallbox.py` | Brand-agnostic wallbox contract: `WallboxDriver`, normalized state, drift reconcile |
| `wallbox_goe.py` | go-e driver (local HTTP API v2: frc/psm/car/nrg/cll, acs/trx auth) |
| `binary_sensor.py` | `is_reserve_conflict()` helper (EV reserve vs max-SOC) |
| `battery_bridge.py` | HA-boundary: discovers base batteries, reads states, calls services |

### Wallbox drivers

Everything charger-brand-specific lives behind `wallbox.WallboxDriver` (read
actual state / apply command / release). The EV coordinator **reconciles the
charger to the plan every tick** — it never assumes the hardware remembers its
last command (the go-e provably resets its force state on unplug, restart and
by its own internal logic). Supporting a new charger brand = one new
`wallbox_<brand>.py` module implementing three methods; the planner,
coordinator, entities and tests are untouched. The driver also supplies charger
power and car state, so **no separate charger integration is required**.

## Install

Wattsmith requires the [Marstek Venus E base integration][base] to already be
installed and configured (one config entry per physical battery).

### HACS (recommended)

1. In HACS → **Custom repositories** → add `https://github.com/aboutandre/wattsmith`
   (category: Integration).
2. Install **Wattsmith** from HACS.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add integration → Wattsmith**.

### Manual

Copy `custom_components/wattsmith/` into your HA `custom_components/` directory
and restart.

## Configure

The config flow asks for the key sensor entity IDs and operating parameters.
All tunable numbers can be changed later via **Configure** (no restart needed).

| Field | Example entity | Notes |
|---|---|---|
| Grid power sensor | `sensor.shellypro3em_power` | + = import, − = export |
| Wallbox (go-e) IP | `192.168.x.x` | enables EV control; the driver also reads charger power, car state and hardware amp limits directly |
| Tibber price sensor | `sensor.tibber_current_price` | cheap-window charging |
| Solcast remaining sensor | `sensor.solcast_forecast_remaining_today` | kWh; enables adaptive charging |
| House consumption sensor | `sensor.house_consumption_power` | feeds the baseline learner (default shown) |
| EV charger power sensor | *(optional)* | fallback only — used when the wallbox driver can't be read |
| Car state sensor | *(optional)* | fallback only — same |

> **Migrating from an external charger integration** (e.g. `goecharger_api2`):
> since v0.7.0 Wattsmith reads charger power + car state itself through the
> wallbox driver, in the same HTTP call it already makes to reconcile the
> charger. Once you've verified a charge cycle on v0.7.0, you can clear the two
> fallback sensor fields and uninstall the external integration —
> `sensor.wattsmith_ev_ev_power` replaces its power entity (recorder history
> included).

The energy manager starts **disabled** on first install (safety — prevents a
two-brain conflict if you're migrating from another controller). Enable via the
**Zero-Grid Control** switch once you've verified the base integration is
working.

## Entities

### Zero-grid manager

| Entity | Type | Description |
|---|---|---|
| `switch.wattsmith_zero_grid_control` | switch | Master enable/disable |
| `sensor.wattsmith_status` | sensor | Current state (SAFE / ACTIVE / HOLD / …) |
| `sensor.wattsmith_total_battery_command` | sensor | Sum of per-battery setpoints (W) |
| `sensor.wattsmith_grid_power_seen` | sensor | Raw grid reading used this tick |
| `number.wattsmith_target_grid_power` | number | Setpoint (default −50 W) |
| `number.wattsmith_proportional_gain_kp` | number | PD proportional gain |
| `number.wattsmith_derivative_gain_kd` | number | PD derivative gain |
| `number.wattsmith_deadband` | number | No-action band (W) |
| `number.wattsmith_minimum_soc` | number | Battery floor (don't discharge below) |
| `number.wattsmith_maximum_charge_soc` | number | Normal cap (adaptive overrides up) |
| `number.wattsmith_max_battery_power` | number | Per-fleet setpoint ceiling (W) |

### Adaptive PV charging

| Entity | Type | Description |
|---|---|---|
| `switch.wattsmith_adaptive_charging` | switch | Enable/disable |
| `sensor.wattsmith_adaptive_status` | sensor | `disabled` / `inactive` / `holding_at_cap` / `charging_to_ceiling` |
| `sensor.wattsmith_adaptive_effective_max_soc` | sensor | SOC cap used this tick (%) |
| `sensor.wattsmith_adaptive_fleet_headroom` | sensor | kWh left to ceiling |
| `sensor.wattsmith_adaptive_learned_baseline` | sensor | Per-(weekday, hour) learned house load (W) for this moment; falls back to `adaptive_baseline_load` while learning |
| `sensor.wattsmith_adaptive_learned_slots` | sensor | How many (weekday, hour) slots have enough data (0–168) |
| `number.wattsmith_adaptive_ceiling_soc` | number | Target SOC when gate opens (default 100%) |
| `number.wattsmith_adaptive_baseline_load` | number | Fallback house load (W) until learner accumulates data |
| `number.wattsmith_adaptive_forecast_derate` | number | Forecast confidence factor (0–1) |

### EV coordinator

| Entity | Type | Description |
|---|---|---|
| `select.wattsmith_ev_ev_charging_mode` | select | `off` / `solar` / `solar_cheap` / `fast` |
| `sensor.wattsmith_ev_ev_state` | sensor | Coordinator state |
| `sensor.wattsmith_ev_ev_reason` | sensor | Human-readable reason for current state |
| `sensor.wattsmith_ev_ev_power` | sensor | Actual charger power (W), read from the wallbox driver |
| `sensor.wattsmith_ev_ev_target_power` | sensor | Requested charge power (W) |
| `sensor.wattsmith_ev_ev_charge_current` | sensor | Current commanded (A) |
| `sensor.wattsmith_ev_ev_phases` | sensor | Phase count (1 or 3) |
| `number.wattsmith_ev_ev_reserve_soc` | number | Fleet SOC gate before solar charging starts (clamped to Max Charge SOC) |
| `number.wattsmith_ev_ev_cheap_price` | number | Price threshold for cheap-window charging |
| `select.wattsmith_ev_ev_cheap_price_target` | select | `none` / `car` — what the cheap window charges |

### Config-conflict guard

| Entity | Type | Description |
|---|---|---|
| `binary_sensor.wattsmith_ev_reserve_max_charge_soc_conflict` | binary_sensor (problem) | On when EV Reserve SOC > Max Charge SOC. The effective reserve is auto-clamped to the cap (the car still solar-charges); the sensor flags that the configured value is misleading |

Additionally, `sensor.wattsmith_status` carries a `config_warnings` attribute:
every cross-value clash found by `validate_config.check_config()` (reserve vs
cap, min vs max SOC, adaptive ceiling vs cap, phase up vs down thresholds,
bridge floor vs reserve, baseline-learner starvation) — empty list = all clear.
Warnings are also logged whenever they change.

## Control model

### Zero-grid

Every `MANAGER_TICK_S` (default 3 s) the manager:

1. Reads the grid sensor (skips if stale).
2. Runs the PD controller → a signed fleet setpoint (W).
3. Splits the setpoint across batteries proportionally by available headroom
   (discharge → high-SOC batteries first; charge → low-SOC batteries first).
4. Calls `hacs_marstek_venus_e.set_passive_mode` targeted at each battery's
   device. The service returns a per-battery ack; missed acks are counted and
   logged.
5. Falls back to SAFE (releases all batteries to Auto) on: grid sensor stale
   > `MANAGER_GRID_MAX_AGE_S`, no batteries found, bad-cycle watchdog.

### Adaptive PV charging gate

Each tick, `adaptive.plan_adaptive_ceiling()` computes:

```
headroom_wh      = fleet_capacity_wh × (ceiling_soc − cap_soc) / 100
remaining_pv_wh  = solcast_remaining_kWh × 1000 × forecast_derate
                   − baseline_load_w × hours_to_sunset
open_gate        = (fleet_soc > cap_soc + 0.5)  # already climbing → latch open
                   OR remaining_pv_wh ≤ headroom_wh
```

`baseline_load_w` is the **learned** per-(weekday, hour-of-day) average from
`sensor.house_consumption_power` (rolling 90-day window, 5-min debounce, ≥5
samples/slot before trusted). This captures weekly patterns: Monday 9am vs
Saturday 9am are separate slots. While learning, falls back to the configured
`adaptive_baseline_load` constant. Data persists across restarts in
`.storage/wattsmith_learned_baseline.json`. Sensor
`sensor.wattsmith_adaptive_learned_slots` shows coverage (target: 168 — all
7 weekdays × 24 hours).

While the gate is closed, the effective max SOC is `max_battery_soc` (the
normal cap). When it opens, the effective max SOC becomes `adaptive_ceiling_soc`
(default 100%). The latch (`fleet_soc > cap_soc + 0.5`) keeps the gate open
once the batteries start climbing past the cap — forecast wobble can't close it
mid-fill.

### EV cascade

In `solar` mode the priority order is:
1. House loads (always served first).
2. Batteries → up to `ev_reserve_soc`.
3. EV charging (PV surplus above reserve).
4. Batteries → up to 100% alongside the EV.

`solar_cheap` mode follows the same cascade, but additionally charges the car
at full power whenever the Tibber price is at or below `cheap_price_threshold`
(and the cheap-price target includes the car). `fast` charges at maximum power
regardless of price or PV.

## Development

```bash
# All tests run without Home Assistant or network:
python3 tests/test_controller.py       # 10 tests  (pure)
python3 tests/test_safety.py           #  5 tests  (pure)
python3 tests/test_planner.py          # 25 tests  (pure)
python3 tests/test_ev_planner.py       # 29 tests  (pure)
python3 tests/test_adaptive.py         # 15 tests  (pure)
python3 tests/test_baseline_learner.py # 22 tests  (learner + persistence)
python3 tests/test_validate_config.py  # 11 tests  (cross-value config checks)
python3 tests/test_wallbox.py          # 21 tests  (driver contract + go-e parsing/params)
python3 tests/test_binary_sensor.py    #  5 tests
python3 tests/test_battery_bridge.py   # 22 tests  (HA-boundary; mocked registry)
python3 tests/test_ev_coordinator.py   # 11 tests  (tick orchestration; faked driver/hass)
python3 tests/test_manager_tick.py     # 10 tests  (tick orchestration; faked bridge/hass)
# Total: 186 tests
```

All settings (polling intervals, PD gains, SOC defaults, EV parameters) live in
`custom_components/wattsmith/settings.py` — the single source of truth for
tunable dials.

## License

MIT — see [LICENSE](LICENSE).

[base]: https://github.com/aboutandre/hacs_marstek_venus_e
