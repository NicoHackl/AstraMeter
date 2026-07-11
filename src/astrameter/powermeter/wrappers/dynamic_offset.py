from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class DynamicOffsetPowermeter(PowermeterWrapper):
    """Adds a single, runtime-adjustable offset (watts) to every phase.

    Unlike :class:`TransformedPowermeter` — whose ``POWER_OFFSET`` /
    ``POWER_MULTIPLIER`` are fixed at startup from config — this wrapper's offset
    is meant to be changed live over MQTT (see MQTT Insights
    ``{base}/powermeter/{pm}/offset/set``). It is applied **in addition to** the
    static transform: with a static ``POWER_OFFSET`` of ``-50`` and a live offset
    of ``500`` the effective per-phase offset is ``450``.

    The offset shifts the value the *control loop* sees, exactly like
    ``POWER_OFFSET``; the raw reading (:meth:`get_powermeter_watts_raw`, used by
    consumers that must match the physical meter) is passed through untouched.
    """

    # Marker so consumers (e.g. the MQTT Insights service) can locate this
    # wrapper inside a powermeter's decorator chain without importing the class.
    is_dynamic_offset = True

    def __init__(self, wrapped_powermeter: Powermeter, offset: float = 0.0) -> None:
        super().__init__(wrapped_powermeter)
        self._offset = float(offset)

    @property
    def offset(self) -> float:
        return self._offset

    def set_offset(self, offset: float) -> None:
        """Set the live additive offset (watts) applied to every phase."""
        self._offset = float(offset)

    async def get_powermeter_watts(self) -> list[float]:
        values = await self.wrapped_powermeter.get_powermeter_watts()
        if self._offset == 0.0:
            return values
        return [value + self._offset for value in values]
