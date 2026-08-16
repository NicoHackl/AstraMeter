"""Marstek B2500 (HMJ) DC-output steering controller.

The B2500 is **DC-coupled** (PV/DC in, DC out to one or two external
microinverters), so it steers its **DC output power** per channel rather than an
AC inverter setpoint. The controller is integer-only and built from a
meter-derived setpoint feeding a per-channel hysteresis regulator — none of the
Venus float gain table, ``sqrt`` step, or spike filter apply. It is documented in
``docs/ct002-ct003-protocol.md`` ("B2500-class (HMJ) DC-output steering").

``cmd`` is an internal command unit, not watts: this model maps it to output via
``(cmd - 5) * 10 / 59``, so a ±100 ``cmd`` step moves the output by ~17 W per
cycle. The loop holds while the measured output is within a ±10 W deadband of the
setpoint, otherwise nudges ``cmd`` by ±100 — a bounded integrator. **See the
audit status below before treating any of that as device behaviour.**

Each channel also has a **minimum output** (``MIN_CHANNEL_OUTPUT_W``): commanded
below it the channel simply stays off, so the unit cannot deliver less than
roughly twice that. This makes a small command unexecutable rather than slow —
a control loop that only ever asks for less than the minimum gets no response at
all, and reads as a dead battery rather than a lagging one.

The setpoint is **incremental** (``setpoint = output + 0.9 * grid``), so the loop
integrates the grid toward zero (fixed point ``output = load``); a proportional
``0.9 * grid`` would droop and never null it.

SOC and temperature are handled by a *separate* BMS (charge-current derating,
cell-voltage limits) and are **not** part of this steering loop.

Audit status (2026-08) — **this model does not match the firmware**
------------------------------------------------------------------
Two independent analyses of ``HMJ-2/V118`` (`tomquist/hm2500
<https://github.com/tomquist/hm2500>`_) agree that the loop modelled below is
not the one the device runs. Kept as-is for now because replacing it is a
rewrite, not a patch — but nothing here should be cited as device behaviour:

* **The hysteresis loop is unreachable.** The ±10 W hold band and ±100 command
  step are real instructions (0x0800ca1e), but they sit in **state 4** of the
  channel state machine, entered only from state 3 — and no instruction in the
  image ever writes state 3 to that struct (verified: the only store of ``3`` to
  a ``+0x22`` field targets an unrelated struct at 0x2000ee71).
* **The live path is a PV-curve emulation.** State 2 builds a piecewise-linear
  panel I-V characteristic whose maximum-power point equals the requested watts,
  then each cycle solves the load line against the channel's *measured* port
  voltage and current. Below 16 V at the port it does not touch the DAC at all;
  below a measured ~800 mA it commands a fixed start-up kick.
* **The command is milliamps driving a DAC**, not watts:
  ``dac = zero_offset[ch] + (cmd_mA - 5) * 10 / 59``, with the measured current
  its exact inverse. So ``(cmd-5)*10/59`` is a DAC code, and the ±100 step is
  ~5 W/s at the port rather than the ~17 W/cycle assumed here.
* **The setpoint is not** ``output + 0.9 * grid``. The CT reading drives an
  aggregate setpoint through a bracketed search railed at ``[pmin, p]``, which
  is then split per channel.

What survives: the device does have a per-channel floor, and a small command
does go unexecuted — which is what the field logs in issue #600 show and what
this model is used for. The mechanism is wrong; the observable is not.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["B2500SteeringController"]

DEADBAND_W = 10  # hold while abs(power - setpoint) <= 10 W
CMD_STEP = 100  # internal command step per cycle (~17 W of output)
CMD_FLOOR = 5  # output = (cmd - CMD_FLOOR) * CAL_NUM / CAL_DEN
CAL_NUM, CAL_DEN = 10, 59  # command -> output (watts) calibration
APPROACH_NUM, APPROACH_DEN = 9, 10  # correct 90% of the residual grid per cycle
# Minimum output one DC channel can physically deliver (W). The channel is a
# hard on/off below this: commanded under it, the output stays de-energized
# rather than delivering a small trickle, so the unit as a whole has a ~2x
# minimum.
#
# **This is a property of the paired inverter, not of the battery.** The
# firmware calls it ``pmin`` and prints it with its own name
# (``"id=%d,s=%d,c=%d,i=%d,p=%d,pmin=%d,adjust_time=%d"``); it lives in a
# CRC-checked config block written from the app, and its default is derived from
# the inverter ``id``: **40 W** for ids in [5000,5500), **80 W** for everything
# else (0x0800e80c-0x0800e882 in HMJ-2 V118). The per-channel floor is
# ``pmin / 2``, so 20 W or 40 W per output depending on the inverter.
#
# The value below is therefore a *scenario knob* standing in for the common
# 80 W case, not a device constant. Anything that depends on how small a command
# a given unit ignores must be parameterised — the trap band in issue #600 is
# device-specific for exactly this reason.
MIN_CHANNEL_OUTPUT_W = 40


@dataclass
class B2500SteeringController:
    """One DC output channel's steering state. Call :meth:`step` per poll cycle.

    The B2500 has two independent outputs; use one controller per channel.
    """

    cmd: int = 60  # internal command unit (not watts)
    # Below this the channel cannot be energized at all (see
    # ``MIN_CHANNEL_OUTPUT_W``). ``0`` models a channel with no such floor.
    min_output: int = MIN_CHANNEL_OUTPUT_W
    # Output ceiling (W). The command is clamped so its output never runs past
    # this — anti-windup. On real hardware the measured-output feedback
    # saturates at the inverter limit, so the command can't wind up; a
    # watt-domain model needs the clamp explicitly, or the integrator runs away
    # whenever the physical output is capped (and recovers only ~17 W/cycle).
    max_output: int = 2500

    def output(self) -> int:
        """The DC output power (W) the current command maps to.

        Mirrors the firmware calibration ``(cmd - 5) * 10 / 59`` in 16-bit
        unsigned arithmetic (so the ``cmd < 5`` underflow matches the device; in
        normal operation ``cmd`` stays well above the floor).
        """
        r = ((self.cmd - CMD_FLOOR) & 0xFFFFFFFF) * CAL_NUM & 0xFFFFFFFF
        return (r // CAL_DEN) & 0xFFFF

    def regulate(self, setpoint: int, power: int) -> int:
        """Advance one hysteresis cycle toward *setpoint*; return the new output.

        *power* is the channel's measured output power (W). Holds within the
        ±10 W deadband, else slews ``cmd`` by ±100, clamped so the output stays
        within ``[0, max_output]`` (anti-windup).

        A setpoint below ``min_output`` de-energizes the channel outright: the
        output goes to 0 and ``cmd`` is reset to the floor rather than left to
        wind up. Resetting matters as much as the gate does — a held command
        would keep integrating a sub-minimum setpoint cycle after cycle and
        eventually cross the threshold on its own, so an under-minimum command
        would start the channel after a delay instead of never. On the device it
        never starts, however long the command is repeated.
        """
        if setpoint < self.min_output:
            self.cmd = CMD_FLOOR
            return 0
        if power > setpoint + DEADBAND_W:
            self.cmd = (self.cmd - CMD_STEP) & 0xFFFF
        elif power < setpoint - DEADBAND_W:
            self.cmd = (self.cmd + CMD_STEP) & 0xFFFF
        cmd_ceiling = CMD_FLOOR + self.max_output * CAL_DEN // CAL_NUM
        if self.cmd > cmd_ceiling:
            self.cmd = cmd_ceiling
        return self.output()

    def step(self, grid: int, power: int, max_power: int) -> int:
        """Full per-cycle pass: form the incremental setpoint, then regulate.

        *grid* is the residual grid power (positive = import), *power* the
        channel's measured output, *max_power* the output envelope. The setpoint
        is ``power + 0.9 * grid`` clamped to ``[0, max_power]`` — incremental, so
        a sustained import winds the output up until the grid is nulled (rather
        than parking at 90% of the residual). The B2500 has no AC input, so the
        setpoint never goes negative: a surplus winds the output down to idle.

        A residual small enough to keep the setpoint under ``min_output`` leaves
        the channel off, however long it persists.
        """
        setpoint = power + int(grid) * APPROACH_NUM // APPROACH_DEN
        setpoint = max(0, min(setpoint, int(max_power)))
        return self.regulate(setpoint, power)
