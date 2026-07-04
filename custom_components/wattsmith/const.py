"""Constants for the Wattsmith energy brain.

Identity + config keys only. All tunable defaults (timings, gains, thresholds,
SOC limits, EV/adaptive params) live in settings.py — the single source of truth
for the dials.
"""
from typing import Final

DOMAIN: Final = "wattsmith"

# ---------------------------------------------------------------------------
# Base Marstek device integration (Wattsmith dispatches to it via HA services;
# NO code import). These mirror the base's public contract — its service names
# and the deterministic unique_id suffixes of the per-battery sensors.
# ---------------------------------------------------------------------------
BASE_DOMAIN: Final = "hacs_marstek_venus_e"
BASE_SVC_SET_MODE: Final = "set_mode"
BASE_SVC_SET_PASSIVE_MODE: Final = "set_passive_mode"

# Base battery sensor_ids (unique_id = f"{BASE_DOMAIN}_{entry_id}_{sensor_id}").
BASE_SENSOR_SOC: Final = "battery_state_of_charge"   # attr bat_soc, %
BASE_SENSOR_POWER: Final = "grid_power"              # attr ongrid_power, W (+=discharge)
BASE_SENSOR_CAPACITY: Final = "battery_capacity"     # attr bat_cap, Wh

# A battery config entry is recognised by exposing the SOC sensor — this is how
# Wattsmith tells real batteries apart from any other base-domain device.
BASE_BATTERY_MARKER_SENSOR: Final = BASE_SENSOR_SOC

# ---------------------------------------------------------------------------
# Config / options keys (defaults for these live in settings.py)
# ---------------------------------------------------------------------------
# Energy Manager (zero-grid multi-battery coordination)
CONF_GRID_SENSOR: Final = "grid_sensor"        # HA entity_id, + = import
CONF_EV_SENSOR: Final = "ev_sensor"            # HA entity_id, EV charger power (excluded)
CONF_TARGET_GRID_W: Final = "target_grid_w"
CONF_KP: Final = "kp"
CONF_KD: Final = "kd"
CONF_DEADBAND_W: Final = "deadband_w"
CONF_MIN_SOC: Final = "min_soc"
CONF_MAX_BATTERY_SOC: Final = "max_battery_soc"
CONF_MAX_STEP_W: Final = "max_step_w"
CONF_DIRECTION_HYSTERESIS_W: Final = "direction_hysteresis_w"

# Adaptive PV charging
CONF_ADAPTIVE_ENABLED: Final = "adaptive_enabled"
CONF_ADAPTIVE_CEILING_SOC: Final = "adaptive_ceiling_soc"
CONF_ADAPTIVE_BASELINE_W: Final = "adaptive_baseline_w"
CONF_ADAPTIVE_FORECAST_DERATE: Final = "adaptive_forecast_derate"
CONF_SOLCAST_REMAINING_SENSOR: Final = "solcast_remaining_sensor"  # HA entity_id, remaining PV today
CONF_SUN_SENSOR: Final = "sun_sensor"                             # HA entity_id, default sun.sun

# Manager: house-consumption input for the baseline learner (options-repointable;
# the default entity id lives in settings.py)
CONF_HOUSE_CONSUMPTION_SENSOR: Final = "house_consumption_sensor"

# EV Coordinator (wallbox control — brand selected via CONF_WALLBOX_TYPE,
# specifics implemented by the matching wallbox_* driver module)
CONF_WALLBOX_TYPE: Final = "wallbox_type"
WALLBOX_TYPE_GOE: Final = "goe"
CONF_GOE_IP: Final = "goe_ip"      # driver host/IP (key name kept for compat)
CONF_EV_MODE: Final = "ev_mode"
CONF_RESERVE_SOC: Final = "reserve_soc"
CONF_CHEAP_PRICE_THRESHOLD: Final = "cheap_price_threshold"
CONF_CHEAP_TARGET: Final = "cheap_target"
CONF_TIBBER_SENSOR: Final = "tibber_sensor"
CONF_CAR_STATE_SENSOR: Final = "car_state_sensor"
CONF_PHASE_UP_W: Final = "phase_up_w"
CONF_PHASE_DOWN_W: Final = "phase_down_w"
CONF_BRIDGE_GRACE_S: Final = "bridge_grace_s"      # how long batteries bridge the car after surplus drops
CONF_BRIDGE_FLOOR_SOC: Final = "bridge_floor_soc"  # stop bridging once fleet SOC falls to this

# ---------------------------------------------------------------------------
# History DB + arbitrage economics (Part A/B of the arbitrage spec)
# ---------------------------------------------------------------------------
# Extra logged inputs (options-configurable HA entity ids)
CONF_PV_SENSOR: Final = "pv_sensor"                     # actual PV production (W)
CONF_SOLCAST_FORECAST_SENSOR: Final = "solcast_forecast_sensor"  # detailed per-slot forecast
CONF_WEATHER_SENSOR: Final = "weather_sensor"           # weather.* for outdoor temperature
CONF_EXPORT_PRICE: Final = "export_price"               # feed-in tariff EUR/kWh (opportunity cost)

# History DB
CONF_HISTORY_ENABLED: Final = "history_enabled"
CONF_HISTORY_DB_PATH: Final = "history_db_path"         # blank -> <config>/wattsmith/history.db
CONF_HISTORY_RETENTION_DAYS: Final = "history_retention_days"  # 0 = keep forever

# Per-battery economics (dict keyed by battery_id, values: cost_eur/expected_cycles/…)
CONF_BATTERY_CONFIG: Final = "battery_config"

# Arbitrage brain
CONF_ARBITRAGE_ENABLED: Final = "arbitrage_enabled"
CONF_WEAR_COST_CT: Final = "wear_cost_ct"              # EUR-cent/kWh delivered; blank -> derived
CONF_ETA_OVERRIDE: Final = "eta_override"              # manual round-trip η (0-1); blank -> measured
CONF_MIN_ARBITRAGE_MARGIN_CT: Final = "min_arbitrage_margin_ct"
CONF_IMPORT_POWER_CAP_W: Final = "import_power_cap_w"   # 0 = no cap
