from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class DynamicOffsetPowermeter(PowermeterWrapper):
    """Adds a single, runtime-adjustable offset (watts) to every phase.

    Unlike :class:`TransformedPowermeter` — whose ``POWER_OFFSET`` /
    ``POWER_MULTIPLIER`` are fixed at startup from config — this wrapper's offset
    is meant to be changed live over MQTT (see MQTT Insights
    ``{base}/powermeter/{pm}/offset/set``). It is applied **in addition to** the
    static ``POWER_OFFSET``: with a static ``-50`` and a live ``500`` the summed
    grid reading shifts by ``450``.

    The offset is a **total** watts adjustment: it is spread evenly across the
    phases so the summed grid reading (what active control targets) shifts by
    exactly the offset — not by Nx on an N-phase meter. It shifts the value the
    *control loop* sees; the raw reading (:meth:`get_powermeter_watts_raw`, used
    by consumers that must match the physical meter) is passed through untouched.
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
        if self._offset == 0.0 or not values:
            return values
        share = self._offset / len(values)
        return [value + share for value in values]
