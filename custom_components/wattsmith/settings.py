"""Wattsmith — centralized tuning. Every operational dial in one place.

settings.py = the sensible defaults. Where a dial is also exposed in the config/
options flow or a number/select entity, the runtime value overrides these — these
are just the fallback. Edit one value here to change a global default; nothing
else in the codebase holds a raw tuning number.

⚠️  These dials INTERACT — keep the relationships or they fight each other:

      MANAGER_RESEND_S  <  MANAGER_CD_TIME_S
          Re-arm setpoints before they auto-revert, or the batteries briefly idle.
      MANAGER_CD_TIME_S  >  MANAGER_TICK_S
          A passive setpoint must outlive a control tick.
      MANAGER_GRID_MAX_AGE_S  >  the grid sensor's update period
          Otherwise every tick looks "stale" and the loop holds.

    Device-side polling/timeout dials live in the BASE integration's own
    settings.py (hacs_marstek_venus_e/settings.py) — keep that REQUEST_TIMEOUT_S
    well under MANAGER_TICK_S so a lost packet never starves a setpoint write.
"""
from typing import Final

# === Zero-grid control loop ==================================================
MANAGER_TICK_S: Final[float] = 3.0            # control loop period (proven cadence from live test)
MANAGER_CD_TIME_S: Final[int] = 10            # passive setpoint auto-revert (> tick)
MANAGER_GRID_MAX_AGE_S: Final[float] = 20.0   # grid sample older than this -> SAFE
MANAGER_RESEND_S: Final[float] = 7.0          # re-arm setpoints only if last send older (< cd_time)
MANAGER_BATTERY_FAIL_THRESHOLD: Final[int] = 3
MANAGER_CYCLE_FAIL_THRESHOLD: Final[int] = 3
MANAGER_DEGRADED_THRESHOLD: Final[int] = 3    # consecutive missed acks before "degraded"

# === Controller (PD) defaults ================================================
DEFAULT_TARGET_GRID_W: Final[int] = -50
DEFAULT_KP: Final[float] = 0.65
DEFAULT_KD: Final[float] = 0.2
DEFAULT_DEADBAND_W: Final[int] = 40
DEFAULT_MAX_STEP_W: Final[int] = 800
DEFAULT_DIRECTION_HYSTERESIS_W: Final[int] = 60

# === Battery limits ==========================================================
DEFAULT_MIN_SOC: Final[int] = 11
DEFAULT_MAX_BATTERY_SOC: Final[int] = 100
DEFAULT_MAX_BATTERY_POWER: Final[int] = 2500

# === Adaptive PV charging ====================================================
DEFAULT_ADAPTIVE_CEILING_SOC: Final[float] = 100.0
DEFAULT_ADAPTIVE_BASELINE_W: Final[float] = 500.0   # fallback until learner has enough data
DEFAULT_ADAPTIVE_FORECAST_DERATE: Final[float] = 0.9
DEFAULT_SUN_SENSOR: Final[str] = "sun.sun"

# Baseline learner — rolling per-hour-of-day house-load averager.
# The manager feeds it samples from HOUSE_CONSUMPTION_SENSOR each tick;
# after MIN_SAMPLES readings in a given hour it replaces the fixed baseline.
#   LEARN_WINDOW_DAYS  × 24 h × (3600/300) samples/h  =  ~2 k samples max.
ADAPTIVE_LEARN_MIN_SAMPLES: Final[int] = 5          # readings/hour before trusting learned value
HOUSE_CONSUMPTION_SENSOR: Final[str] = "sensor.house_consumption_power"

# === EV charging (go-e) ======================================================
EV_TICK_S: Final[float] = 15.0
EV_GOE_TIMEOUT_S: Final[float] = 5.0
DEFAULT_EV_MODE: Final[str] = "solar"
DEFAULT_RESERVE_SOC: Final[float] = 80.0
DEFAULT_CHEAP_PRICE_THRESHOLD: Final[float] = 0.10
DEFAULT_CHEAP_TARGET: Final[str] = "car"
DEFAULT_PHASE_UP_W: Final[float] = 4500.0
DEFAULT_PHASE_DOWN_W: Final[float] = 4140.0
DEFAULT_BRIDGE_GRACE_S: Final[float] = 180.0
DEFAULT_BRIDGE_FLOOR_SOC: Final[float] = 50.0
