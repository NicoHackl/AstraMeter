import asyncio

import pytest

from .base import SampleGate


async def test_wait_returns_immediately_for_an_unread_sample():
    gate = SampleGate()
    gate.mark()
    # timeout=0 would raise if the gate blocked at all.
    await gate.wait(0)


async def test_wait_blocks_once_the_sample_was_read():
    gate = SampleGate()
    gate.mark()
    await gate.wait(0)
    with pytest.raises(asyncio.TimeoutError):
        await gate.wait(0)


async def test_wait_wakes_on_the_next_mark():
    gate = SampleGate()

    async def _mark_later():
        await asyncio.sleep(0.01)
        gate.mark()

    task = asyncio.create_task(_mark_later())
    await gate.wait(2)
    await task
    # That mark is consumed, so the following read has to block again.
    with pytest.raises(asyncio.TimeoutError):
        await gate.wait(0)


async def test_repeated_marks_count_as_one_unread_sample():
    gate = SampleGate()
    gate.mark()
    gate.mark()
    await gate.wait(0)
    with pytest.raises(asyncio.TimeoutError):
        await gate.wait(0)


async def test_reset_drops_the_unread_sample():
    gate = SampleGate()
    gate.mark()
    gate.reset()
    with pytest.raises(asyncio.TimeoutError):
        await gate.wait(0)


async def test_wraps_an_existing_event_so_direct_sets_still_wake_waiters():
    event = asyncio.Event()
    gate = SampleGate(event)
    assert gate.event is event

    async def _set_later():
        await asyncio.sleep(0.01)
        event.set()

    task = asyncio.create_task(_set_later())
    await gate.wait(2)
    await task
