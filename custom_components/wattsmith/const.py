"""Constants for the Wattsmith energy brain."""
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
# Energy Manager (zero-grid multi-battery coordination)
# ---------------------------------------------------------------------------
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

DEFAULT_TARGET_GRID_W: Final = -50
DEFAULT_KP: Final = 0.65
DEFAULT_KD: Final = 0.2
DEFAULT_DEADBAND_W: Final = 40
DEFAULT_MIN_SOC: Final = 11
DEFAULT_MAX_BATTERY_SOC: Final = 100
DEFAULT_MAX_STEP_W: Final = 800
DEFAULT_DIRECTION_HYSTERESIS_W: Final = 60
DEFAULT_MAX_BATTERY_POWER: Final = 2500

# Control timing
MANAGER_TICK_S: Final = 3.0          # control loop period (proven cadence from live test)
MANAGER_CD_TIME_S: Final = 10        # passive setpoint auto-revert (> tick)
MANAGER_GRID_MAX_AGE_S: Final = 20.0 # grid sample older than this -> SAFE
MANAGER_BATTERY_FAIL_THRESHOLD: Final = 3
MANAGER_CYCLE_FAIL_THRESHOLD: Final = 3
MANAGER_DEGRADED_THRESHOLD: Final = 3  # consecutive missed acks before flagging "degraded"
MANAGER_RESEND_S: Final = 7.0          # re-arm setpoints only if last send older than this (< cd_time)

# ---------------------------------------------------------------------------
# EV Coordinator (go-e local-API control)
# ---------------------------------------------------------------------------
CONF_GOE_IP: Final = "goe_ip"
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

DEFAULT_RESERVE_SOC: Final = 80.0
DEFAULT_CHEAP_PRICE_THRESHOLD: Final = 0.10
DEFAULT_EV_MODE: Final = "solar"
DEFAULT_CHEAP_TARGET: Final = "car"
DEFAULT_PHASE_UP_W: Final = 4500.0
DEFAULT_PHASE_DOWN_W: Final = 4140.0
DEFAULT_BRIDGE_GRACE_S: Final = 180.0
DEFAULT_BRIDGE_FLOOR_SOC: Final = 50.0

EV_TICK_S: Final = 15.0
EV_GOE_TIMEOUT_S: Final = 5.0
