"""Venus E (VNSE3-0) steering: gate + integrator, pinned to the firmware.

Every expectation here traces to the disassembly cited in
:mod:`astrameter.simulator.venus_e_steering` (VNSE3-0 Control v150, flat
Cortex-M image based at 0x08000000): the gate at 0x08023d2c, the share split at
0x080042c6 and the integrator at 0x08029ecc.
"""

from __future__ import annotations

from .venus_d_steering import VenusDSteeringController
from .venus_e_steering import VenusESteeringController


def _run(steps, **kwargs):
    """Feed ``(g, out)`` pairs to a fresh controller; return the setpoints."""
    c = VenusESteeringController(**kwargs)
    return [c.step(g, 2500.0, -2500.0, out=out) for g, out in steps]


# ---------------------------------------------------------------------------
# The integrator accumulates on its own setpoint
# ---------------------------------------------------------------------------


def test_small_sustained_import_walks_the_setpoint_up() -> None:
    """The behaviour the whole model turns on (issue #600 follow-up).

    A 30 W reading is far below what the inverter will start for, and the unit
    reports 0 W throughout — yet the stored setpoint climbs by ``g - 5`` every
    cycle, because the update never consults the measured output. A B2500 in the
    same situation stays at 0 W forever.
    """
    assert _run([(30, 0)] * 6) == [25, 50, 75, 100, 125, 150]


def test_gain_scales_the_step() -> None:
    """``ctrl_ratio`` is a percentage; 50 % halves the per-cycle step.

    ``prev_g`` is seeded so the first sample isn't eaten by the spike filter —
    see :func:`test_cold_start_spike_filters_the_first_large_reading`.
    """
    assert _run([(100, 0)] * 3, ctrl_ratio=50, prev_g=100) == [45, 90, 135]


def test_out_of_range_ratio_falls_back_to_unity() -> None:
    """The device forces anything outside 30..100 back to 100 (0x08006288)."""
    assert _run([(100, 0)] * 2, ctrl_ratio=7, prev_g=100) == [95, 190]
    assert _run([(100, 0)] * 2, ctrl_ratio=140, prev_g=100) == [95, 190]


def test_cold_start_spike_filters_the_first_large_reading() -> None:
    """The gate's baselines are zero-initialised globals in the firmware.

    So the first reading after a boot that is more than 50 W away from zero
    looks exactly like a spike and is held — once.
    """
    assert _run([(100, 0)] * 3) == [0, 95, 190]


def test_setpoint_clamps_to_the_power_envelope() -> None:
    c = VenusESteeringController()
    for _ in range(20):
        c.step(500, 800.0, -800.0, out=0)
    assert c.setpoint == 800
    for _ in range(40):
        c.step(-500, 800.0, -800.0, out=0)
    assert c.setpoint == -800


# ---------------------------------------------------------------------------
# The input-conditioning gate
# ---------------------------------------------------------------------------


def test_deadband_holds_a_small_reading_only_while_at_rest() -> None:
    """±10 W deadband, conditioned on the unit's own output (not on the grid).

    A small reading is dropped outright while the unit is at rest, and reaches
    the integrator once it is producing. Both runs first wind the setpoint up to
    50 W — with the unit still reporting 0 W, since a real one takes seconds to
    start — so the difference shows in the setpoint rather than being hidden by
    the final ±11 W park.
    """

    def wind_up_then(last_out: float) -> list[int]:
        c = VenusESteeringController(prev_g=30)
        out = [c.step(30, 2500.0, -2500.0, out=0) for _ in range(2)]
        return [*out, c.step(-8, 2500.0, -2500.0, out=last_out)]

    assert wind_up_then(0) == [25, 50, 50]  # at rest: dropped by the gate
    assert wind_up_then(200) == [25, 50, 42]  # producing: integrated

    # And from rest, a small export never accumulates at all.
    assert _run([(-8, 0)] * 4) == [0, 0, 0, 0]


def test_small_import_hold_applies_even_while_producing() -> None:
    """A residual import under 10 W is held whatever the unit is doing."""
    assert _run([(9, 500)] * 3) == [0, 0, 0]
    assert _run([(9, 0)] * 3) == [0, 0, 0]


def test_final_deadband_parks_a_sub_11w_setpoint() -> None:
    """A reading that passes the gate can still land inside the ±11 W park.

    -9 W while producing integrates to a -9 W setpoint, which the final
    deadband (both |setpoint| and the reading under 11) then zeroes.
    """
    assert _run([(-9, 500)] * 2) == [0, 0]


def test_spike_filter_is_a_one_shot() -> None:
    """A >50 W jump the own output cannot explain is skipped exactly once.

    The second sample is forced through even though it still looks like a spike
    relative to nothing having moved — the firmware clears its flag and runs.
    """
    c = VenusESteeringController()
    assert c.step(20, 2500.0, -2500.0, out=0) == 15  # baseline
    assert c.step(400, 2500.0, -2500.0, out=0) == 15  # spike: held
    assert c.step(400, 2500.0, -2500.0, out=0) == 410  # one-shot expired


def test_spike_needs_the_own_output_to_have_stayed_still() -> None:
    """A jump the unit's own ramp explains is not a spike."""
    c = VenusESteeringController()
    c.step(20, 2500.0, -2500.0, out=0)
    # Own output moved 300 W between samples, so the grid jump is our own doing.
    assert c.step(400, 2500.0, -2500.0, out=300) == 410


def test_a_held_sample_leaves_the_integrator_untouched() -> None:
    """The firmware returns before the update; state must not drift."""
    c = VenusESteeringController()
    c.step(30, 2500.0, -2500.0, out=0)
    before = (c.setpoint, c.ctrl_ratio)
    c.step(4, 2500.0, -2500.0, out=0)  # small-import hold
    assert (c.setpoint, c.ctrl_ratio) == before


# ---------------------------------------------------------------------------
# The bucket's device count
# ---------------------------------------------------------------------------


def test_device_count_splits_the_bucket_value() -> None:
    """``g = g / nb`` (signed, truncating) before anything else."""
    c = VenusESteeringController()
    assert c.step(300, 2500.0, -2500.0, out=0, device_count=3) == 95  # 100 - 5
    c = VenusESteeringController()
    assert c.step(-300, 2500.0, -2500.0, out=0, device_count=3) == -100


def test_device_count_widens_the_final_deadband() -> None:
    """±11 W alone, ±15 W when the bucket is shared.

    Both cases integrate the same 13 W to an 8 W setpoint. Alone, the reading
    (13) is outside ±11 so the setpoint stands; shared, both the setpoint and
    the reading are inside ±15 and the device parks at zero.
    """
    solo = VenusESteeringController()
    assert solo.step(13, 2500.0, -2500.0, out=100, device_count=1) == 8
    shared = VenusESteeringController()
    assert shared.step(26, 2500.0, -2500.0, out=100, device_count=2) == 0


def test_shared_bucket_disables_the_spike_filter() -> None:
    c = VenusESteeringController()
    c.step(20, 2500.0, -2500.0, out=0, device_count=2)
    # Same jump that a solo unit would hold once.
    assert c.step(400, 2500.0, -2500.0, out=0, device_count=2) != 15


# ---------------------------------------------------------------------------
# Relationship to the Venus D
# ---------------------------------------------------------------------------


def test_integrator_matches_the_venus_d_law_exactly() -> None:
    """Same arithmetic, verified against the same firmware family.

    Fed samples the gate passes, the Venus E must track a bare Venus D
    controller step for step — the E adds a gate and a share split, nothing else.
    """
    e = VenusESteeringController()
    d = VenusDSteeringController()
    for g in (500, 500, -300, -300, 120, 40, -60, 500):
        got = e.step(g, 2500.0, -2500.0, out=g)
        want = d.step(g, 2500.0, -2500.0, measured_grid=g)
        assert got == want, f"diverged at g={g}: {got} != {want}"
