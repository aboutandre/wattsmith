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
- **EV charging (go-e)** — PV-surplus priority cascade, cheap-window grid
  charging, automatic 1↔3-phase switching, and a battery-bridge for brief PV
  dips.
- **Adaptive PV charging** *(planned)* — predictively times the fill to 100% on
  the day's last rays for zero feed-in and zero import.

## Architecture

```
Home Assistant
├── hacs_marstek_venus_e   (base: per-battery Local API device + services)
│        ▲ reads SOC/power/cap via sensor entities
│        ▼ dispatches setpoints via hass.services (set_passive_mode, target=device)
└── wattsmith              (this: the brain — manager, planner, controller,
                            safety, EV coordinator + all site configuration)
```

The pure decision modules carry no Home Assistant dependency and are unit
tested in `tests/`:

| Module | Responsibility |
|---|---|
| `controller.py` | Zero-grid PD control + per-battery SOC-split dispatch |
| `safety.py` | Safety gates and SAFE-state snapshot |
| `planner.py` | Energy-manager decision logic |
| `ev_planner.py` | EV charge planning (cascade, cheap window, phase switching) |

## Status

🚧 **Phase 1 (scaffold).** The integration installs and the pure modules are
lifted and green. The control coordinators, entities, and the service-call I/O
wiring to the base integration land next. See the project's `todos/hel-108..111`.

## Development

```bash
# Pure logic tests (no Home Assistant required):
python3 tests/test_controller.py
python3 tests/test_safety.py
python3 tests/test_planner.py
python3 tests/test_ev_planner.py
```

## License

MIT — see [LICENSE](LICENSE).

[base]: https://github.com/aboutandre/hacs_marstek_venus_e
