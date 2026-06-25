"""Zero-grid multi-battery coordination controller.

Pure control logic — NO Home Assistant or I/O dependencies, so it can be unit-tested
and validated against live hardware in isolation. The HA "Energy Manager" config entry
wires this to a grid sensor (input) and the per-battery coordinators (output).

Sign conventions (consistent throughout):
  grid_power  : + = importing from grid,      - = exporting to grid
  battery_power / setpoint : + = discharging, - = charging   (matches Marstek ES.GetMode
                            ongrid_power and ES.SetMode passive_cfg.power)

Goal: drive grid_power to `target_grid_w` (default slightly negative = tiny export buffer)
by commanding a total battery power and splitting it across batteries by SOC.

Control law (incremental PD on the grid error):
  error = grid_power - target_grid_w        # + error  => importing too much => discharge more
  command_total += kp*error + kd*(error - prev_error)
  command_total = clamp(command_total, -charge_capacity, +discharge_capacity)

Because a 1 W change in battery power moves grid power ~1 W the other way, the incremental
form drives steady-state error to zero even with kp<1, and kp<1 + kd damps overshoot/noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BatteryState:
    """Live state of one battery (inputs to the controller)."""

    id: str
    soc: float                 # %
    power: int = 0             # current W, + = discharging
    min_soc: float = 11.0      # don't discharge below this
    max_soc: float = 100.0     # don't charge above this
    max_power: int = 2500      # per-battery W limit (charge and discharge)


@dataclass
class ControllerConfig:
    """Tunable parameters (exposed in HA as number/switch entities).

    Defaults mirror ffunes's proven values: a GENTLE ramp (max_step_w) and a
    direction-hysteresis band are what keep the grid from yo-yoing on spikes and
    flip-flopping charge<->discharge near zero.
    """

    target_grid_w: int = -50   # aim slightly into export to guarantee ~zero import
    kp: float = 0.65
    kd: float = 0.2
    deadband_w: int = 40       # ignore tiny grid errors to avoid jitter
    max_step_w: int = 800      # max change of total command per cycle (gentle ramp)
    direction_hysteresis_w: int = 60  # must exceed this to FLIP charge<->discharge


@dataclass
class ZeroGridController:
    """Incremental-PD zero-grid controller with SOC-based multi-battery split."""

    config: ControllerConfig = field(default_factory=ControllerConfig)
    _command_total: float = 0.0   # last total battery command (W, + = discharge)
    _prev_error: float = 0.0
    _last_sign: int = 0           # sign of last non-zero output (for direction hysteresis)

    def reset(self) -> None:
        self._command_total = 0.0
        self._prev_error = 0.0
        self._last_sign = 0

    def update(self, grid_power: float, batteries: list[BatteryState]) -> dict[str, int]:
        """Compute per-battery setpoints (W, + = discharge) for this tick."""
        cfg = self.config
        error = grid_power - cfg.target_grid_w

        if abs(error) <= cfg.deadband_w:
            # Within deadband: HOLD the current command (no change, no derivative kick),
            # but re-split in case SOC shifted. Reset prev_error so the next out-of-band
            # cycle computes a clean derivative (avoids a kick when leaving the band).
            self._prev_error = 0.0
            return self._split(self._command_total, batteries)

        delta = cfg.kp * error + cfg.kd * (error - self._prev_error)
        # Ramp limit
        delta = _clamp(delta, -cfg.max_step_w, cfg.max_step_w)

        command = self._command_total + delta

        # Capacity limits depend on which batteries can charge/discharge right now
        discharge_cap = sum(
            b.max_power for b in batteries if b.soc > b.min_soc
        )
        charge_cap = sum(
            b.max_power for b in batteries if b.soc < b.max_soc
        )
        command = _clamp(command, -charge_cap, discharge_cap)

        # Direction hysteresis: don't FLIP charge<->discharge for small corrections.
        # If the command would reverse direction vs the last non-zero output and is
        # below the threshold, hold at 0 (idle) instead of flip-flopping near zero.
        new_sign = 1 if command > 0 else (-1 if command < 0 else 0)
        if (self._last_sign != 0 and new_sign != 0 and new_sign != self._last_sign
                and abs(command) < cfg.direction_hysteresis_w):
            command = 0.0
            new_sign = 0

        self._command_total = command
        self._prev_error = error
        self._last_sign = new_sign

        return self._split(command, batteries)

    @staticmethod
    def _split(command: float, batteries: list[BatteryState]) -> dict[str, int]:
        """Split a total command across batteries by SOC, respecting caps.

        Discharge (command > 0): favor HIGH soc (weight = soc - min_soc).
        Charge   (command < 0): favor LOW soc  (weight = max_soc - soc).
        Water-fills so per-battery caps and SOC limits are respected and the
        remainder is redistributed to batteries with headroom.
        """
        out = {b.id: 0 for b in batteries}
        if not batteries or abs(command) < 1:
            return out

        discharging = command > 0
        remaining = abs(command)

        # eligible batteries + their weights and per-battery caps
        pool = []
        for b in batteries:
            if discharging and b.soc > b.min_soc:
                weight = b.soc - b.min_soc
                cap = b.max_power
            elif not discharging and b.soc < b.max_soc:
                weight = b.max_soc - b.soc
                cap = b.max_power
            else:
                continue
            if weight > 0 and cap > 0:
                pool.append([b.id, weight, cap, 0])  # id, weight, cap, assigned

        # Water-filling: distribute by weight, spill over caps, repeat
        for _ in range(len(pool) + 1):
            if remaining < 1 or not pool:
                break
            active = [p for p in pool if p[3] < p[2]]  # not yet capped
            if not active:
                break
            total_w = sum(p[1] for p in active)
            if total_w <= 0:
                break
            assigned_any = False
            for p in active:
                share = remaining * (p[1] / total_w)
                room = p[2] - p[3]
                add = min(share, room)
                if add > 0:
                    p[3] += add
                    assigned_any = True
            # recompute remaining from total assigned
            assigned_total = sum(p[3] for p in pool)
            remaining = abs(command) - assigned_total
            if not assigned_any:
                break

        sign = 1 if discharging else -1
        for pid, _w, _cap, assigned in pool:
            out[pid] = int(round(sign * assigned))
        return out


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
