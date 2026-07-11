from astrameter.config.logger import logger
from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class TransformedPowermeter(PowermeterWrapper):
    """
    A wrapper around a powermeter that applies a linear transformation
    (multiplier and offset) to each returned power value.

    Per-value multiplier is applied directly (``value * multiplier``); it is a
    per-reading ratio, so a single ``POWER_MULTIPLIER`` scales every phase.

    The **offset** is additive watts. A *single* ``POWER_OFFSET`` is treated as
    a **total** adjustment and spread evenly across the phases, so the summed
    grid reading (what active control targets) shifts by exactly that value —
    not by Nx the value on an N-phase meter. A *per-phase list* is taken
    literally: each value applies to its phase (use this for per-phase
    calibration). Single-phase meters are unaffected either way.
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        offsets: list[float],
        multipliers: list[float],
    ) -> None:
        if not offsets:
            raise ValueError("offsets must be a non-empty list")
        if not multipliers:
            raise ValueError("multipliers must be a non-empty list")
        super().__init__(wrapped_powermeter)
        self.offsets = offsets
        self.multipliers = multipliers
        self._offsets_mismatch_warned = False
        self._multipliers_mismatch_warned = False

    def _apply_transform(self, values: list[float]) -> list[float]:
        # A single offset is a *total* watts adjustment spread evenly across the
        # phases (so the summed reading shifts by exactly that value); a per-phase
        # list is applied literally, one value per phase.
        single_offset = len(self.offsets) == 1
        n = len(values)
        result = []
        for i, value in enumerate(values):
            multiplier = self.multipliers[i % len(self.multipliers)]
            if single_offset:
                offset = self.offsets[0] / n if n else 0.0
            else:
                offset = self.offsets[i % len(self.offsets)]
            result.append(value * multiplier + offset)

        if len(self.offsets) > 1 and len(self.offsets) != len(values):
            if not self._offsets_mismatch_warned:
                logger.warning(
                    "POWER_OFFSET has %d values but powermeter returned %d phases",
                    len(self.offsets),
                    len(values),
                )
                self._offsets_mismatch_warned = True
        else:
            self._offsets_mismatch_warned = False

        if len(self.multipliers) > 1 and len(self.multipliers) != len(values):
            if not self._multipliers_mismatch_warned:
                logger.warning(
                    "POWER_MULTIPLIER has %d values but powermeter returned %d phases",
                    len(self.multipliers),
                    len(values),
                )
                self._multipliers_mismatch_warned = True
        else:
            self._multipliers_mismatch_warned = False

        return result

    async def get_powermeter_watts(self) -> list[float]:
        values = await self.wrapped_powermeter.get_powermeter_watts()
        return self._apply_transform(values)
