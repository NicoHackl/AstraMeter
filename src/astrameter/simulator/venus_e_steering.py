"""Marstek Venus E (VNSE3-0) self-consumption steering controller.

The Venus E does **not** run the HMG-50 float ramp
(:mod:`astrameter.simulator.firmware_steering`). Its CT-following loop is the
same integer proportional integrator the Venus D runs
(:mod:`astrameter.simulator.venus_d_steering`), preceded by an
input-conditioning gate of its own. This module is the gate plus the share
split; the integrator arithmetic is imported rather than copied, because the
two devices run the *same* law.

Provenance
----------
Read out of ``VNSE3-0/Control`` firmware images in the marstek-firmware-archive
(`rweijnen/marstek-firmware-archive
<https://github.com/rweijnen/marstek-firmware-archive>`_), which are flat
Cortex-M images based at ``0x08000000``. Addresses below are from **v150**
(``VNSEE3-0_app_0150_0804_151249.bin``); the same code, instruction for
instruction, is in v144, v148 and v1476 (only the addresses move), so the law
is not version-specific:

===============================  ==========  ==================================
what                             v150 addr   notes
===============================  ==========  ==================================
CT task (bucket select, split)   0x08029c88  reads the phase / combined bucket
share split + validity           0x08004218  ``g = g / nb`` (signed ``sdiv``)
input-conditioning gate          0x08023d2c  modelled by :meth:`_gate`
the integrator                   0x08029ecc  the Venus D law, see below
loop gain (``ctrl_ratio``)       0x08006288  clamp 30..100 else 100, then x0.01
setpoint (persistent)            0x20000290  int32 in RAM
===============================  ==========  ==================================

Two facts about the integrator are worth stating explicitly, because they are
what distinguishes this device from a B2500 (:mod:`b2500_steering`) and they
were verified rather than assumed:

* **It integrates its own stored setpoint, never its measured output.** The
  update at 0x08029ecc reads ``0x20000290``, adds the correction and writes it
  back. Every other write to that address in the image (0x08012f26, 0x08027cec,
  0x08029c9a, 0x08029d92, 0x0802ab90) stores a plain zero — the reset paths for
  a lost CT, a mode change or an invalid reading. So a command the device
  cannot execute still accumulates: a repeated 30 W reading walks the setpoint
  25 -> 50 -> 75 W until the unit starts. A B2500, whose setpoint is
  ``measured_output + 0.9 * grid``, cannot do this.
* **The float gain table is absent.** None of the eleven HMG-50 gain constants
  (410.35, 350.41, 180.30, 60.02, 50.12, 50.23, 50.10, 50.21, 100.01, 200.02,
  400.40) appears anywhere in any VNSE3-0 image, while all eleven appear in all
  ten archived HMG-50 images. The ramp law is simply not in this firmware.

The branch selector
-------------------
The integrator's per-step branch is selected by a signed 16-bit field of the
device status struct (``0x20014e94 + 0x18``). The same field is what the gate
tests for "am I running" and what it diffs for the spike filter, which is what
identifies it: a spike filter reading *"the grid jumped >50 W while this value
barely moved"* is only meaningful if the value is the device's **own output** —
were it the device's own grid reading the two conditions would contradict each
other. The HMG-50 gate has the identical shape (0x0801e420 in v156) around a
value its own documentation calls ``out``.

:mod:`venus_d_steering` calls the corresponding argument ``measured_grid``. On
this device it is the own-output power, so that is what :meth:`step` passes.
Whether the VNSD-0 agrees is *unverified* — no VNSD-0 image is archived — so
that module is deliberately left as it is. At the default ``ctrl_ratio`` of 100
the two readings pick different branches only in the mixed-sign quadrants, and
differ there by the 5 W step bias.

Cadence
-------
The CT task waits on a queue and runs one regulation step per received CT
response, which is how :class:`~astrameter.simulator.battery.BatterySimulator`
drives it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .venus_d_steering import DEFAULT_CTRL_RATIO, VenusDSteeringController

__all__ = [
    "DEADBAND_W",
    "SMALL_IMPORT_HOLD_W",
    "SPIKE_JUMP_W",
    "SPIKE_OWN_DELTA_W",
    "VenusESteeringController",
]

# Input-conditioning thresholds (0x08023d2c). The deadband is ±10 W, where the
# HMG-50 uses ±20 W; the spike thresholds are the same on both.
DEADBAND_W = 10
SPIKE_JUMP_W = 50
SPIKE_OWN_DELTA_W = 20
SMALL_IMPORT_HOLD_W = 10


def _share_split(g: float, device_count: int) -> int:
    """Divide the bucket value across the batteries sharing it (0x080042c6).

    Signed division truncating toward zero, matching the firmware's ``sdiv``.
    """
    nb = max(1, int(device_count))
    g = int(g)
    if nb > 1:
        g = int(g / nb)
    return g


@dataclass
class VenusESteeringController:
    """One Venus E's steering state. Call :meth:`step` per CT response.

    ``setpoint`` is the commanded inverter power in the device's own
    convention: **positive = discharge**, negative = charge.
    """

    setpoint: int = 0
    ctrl_ratio: int = DEFAULT_CTRL_RATIO
    # Gate baselines and the spike filter's one-shot flag (0x200002ec /
    # 0x200002f0 / 0x200002f4).
    prev_g: int = 0
    prev_out: int = 0
    spike_pending: bool = False

    def step(
        self,
        g: float,
        hi: float,
        lo: float,
        *,
        out: float = 0.0,
        device_count: int = 1,
    ) -> int:
        """Advance one regulation cycle for bucket value *g*; return the setpoint.

        *g* is the selected bucket (this phase, or the combined bucket), already
        net of ``grid_standard``, **positive = importing**. *hi* / *lo* are the
        discharge (positive) / charge (negative) limits. *out* is the battery's
        own measured output power (positive = discharging). *device_count* is
        the ``*_chrg_nb`` count for the bucket: it divides *g*, disables the
        spike filter, and widens the final deadband from ±11 W to ±15 W.

        A sample the gate holds leaves the setpoint — and the integrator's state
        — untouched, exactly as the firmware's early return does.
        """
        g_i = _share_split(g, device_count)
        if not self._gate(g_i, int(out), device_count):
            return self.setpoint
        # The integrator carries no state beyond the setpoint and the gain, so
        # driving a fresh one keeps the shared arithmetic single-sourced.
        core = VenusDSteeringController(
            setpoint=self.setpoint, ctrl_ratio=self.ctrl_ratio
        )
        self.setpoint = core.step(
            g_i, hi, lo, measured_grid=int(out), phase_count=device_count
        )
        return self.setpoint

    def _gate(self, g: int, out: int, device_count: int) -> bool:
        """The firmware's pre-integrator gate (0x08023d2c); ``True`` ⇒ integrate.

        Three holds, in order: a >50 W spike the own output cannot explain, a
        ±10 W deadband that applies only while the unit is at rest, and a hold on
        a small residual import. The spike filter is a **one-shot** — the sample
        after a skipped one is forced through, whether or not it still looks like
        a spike — and it is skipped entirely when several batteries share the
        bucket, which is also when the baselines stop advancing. The firmware
        additionally requires the run mode to be in 1..6 and not 2; a
        CT-following unit always is, so that test is not modelled.
        """
        if device_count <= 1:
            d_out = abs(out - self.prev_out)
            d_g = abs(g - self.prev_g)
            self.prev_g = g
            self.prev_out = out
            spike = (
                abs(g) > DEADBAND_W and d_g > SPIKE_JUMP_W and d_out < SPIKE_OWN_DELTA_W
            )
            if spike and not self.spike_pending:
                self.spike_pending = True
                return False
            if self.spike_pending:
                # One-shot: the next sample runs, skipping the holds below.
                self.spike_pending = False
                return True
        self.spike_pending = False
        if abs(g) < DEADBAND_W and out < 1:
            return False
        return not 0 <= g < SMALL_IMPORT_HOLD_W
