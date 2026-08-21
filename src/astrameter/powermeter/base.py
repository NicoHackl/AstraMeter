# Powermeter classes
import asyncio
from collections.abc import Callable


def stream_fresh(
    last_monotonic: float | None,
    max_age: float,
    clock: Callable[[], float],
) -> bool:
    """Freshness check shared by cadence-based push powermeters.

    Returns ``False`` if nothing has been received yet; ``True`` when
    ``max_age <= 0`` (freshness disabled); otherwise ``True`` only while the
    last message is no older than ``max_age`` seconds.
    """
    if last_monotonic is None:
        return False
    if max_age <= 0:
        return True
    return (clock() - last_monotonic) <= max_age


class SampleGate:
    """Tracks whether a pushed sample is still unread by the control loop.

    ``wait_for_next_message`` means "never serve the same sample twice", not
    "always block for one more push".  Clearing an :class:`asyncio.Event` on
    entry does the latter: a sample that arrived moments ago is thrown away and
    the caller waits out a whole source interval before it can answer.  Against
    a battery polling every 4 s and a source pushing every ~2.4 s that cost
    1-2 s of latency on *every* reply -- and a late reply makes the battery skip
    its next poll, which is what a user sees as an unstable meter connection.

    So count the pushes and only block when the counter has not moved since the
    last read.  A stalled source still blocks for the full timeout, so the
    "hold the last known value" path is unchanged.

    The gate wraps the powermeter's existing event rather than owning a private
    one, so ``wait_for_message`` and tests that set the event directly keep
    working.
    """

    def __init__(self, event: "asyncio.Event | None" = None) -> None:
        self.event = event if event is not None else asyncio.Event()
        self._seq = 0
        self._seen = 0

    def mark(self) -> None:
        """Record a freshly pushed sample and wake any waiter."""
        self._seq += 1
        self.event.set()

    def reset(self) -> None:
        """Forget any unread sample (restart, reconnect, cleared values)."""
        self._seen = self._seq
        self.event.clear()

    def take_pending(self) -> bool:
        """Consume an already-arrived sample; ``False`` when there is none."""
        if self._seq == self._seen:
            return False
        self._seen = self._seq
        return True

    async def wait(self, timeout: float) -> None:
        """Return at once if a sample is unread, otherwise await the next one.

        Raises :class:`asyncio.TimeoutError`; callers translate that into their
        own source-specific ``TimeoutError`` message.
        """
        if self.take_pending():
            return
        self.event.clear()
        await asyncio.wait_for(self.event.wait(), timeout=timeout)
        self._seen = self._seq


class Powermeter:
    # Labels the powermeter's diagnostic device in MQTT Insights. Set by the
    # outermost HealthTrackingPowermeter wrapper to the config section name.
    name: str = ""

    async def get_powermeter_watts(self) -> list[float]:
        raise NotImplementedError()

    async def get_powermeter_watts_raw(self) -> list[float]:
        """Per-phase watts before section/global processing wrappers.

        Used when a consumer (e.g. Marstek MQTT display) should match the physical
        meter while control still uses :meth:`get_powermeter_watts`. Defaults to
        the same values as :meth:`get_powermeter_watts` for sources with no inner
        pipeline.
        """
        return await self.get_powermeter_watts()

    def stream_online(self) -> bool | None:
        """Health hook for the MQTT Insights "Online" diagnostic sensor.

        ``None`` (the default) means "don't know" — used by pull/polling
        powermeters; the health loop falls back to reusing the control loop's
        last read or, when idle, a single bounded probe. Push powermeters
        override this to report their own connection/validity state with no
        I/O.
        """
        return None

    async def wait_for_message(self, timeout=5):
        pass

    async def wait_for_next_message(self, timeout=5):
        """Block until a *new* measurement arrives (push-based powermeters).

        Unlike ``wait_for_message`` (which returns immediately once data has
        been received *at least once*), this method waits for the *next*
        update, ensuring callers always get fresh data.  Polling-based
        powermeters leave the default no-op.
        """

    # --- Lifecycle (no-op by default, override for push-based powermeters) ---

    async def start(self):
        pass

    async def stop(self):
        pass

    def reset(self):
        pass
