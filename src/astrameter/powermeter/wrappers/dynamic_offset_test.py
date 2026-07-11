from unittest.mock import AsyncMock, Mock

import pytest

from .dynamic_offset import DynamicOffsetPowermeter


@pytest.fixture
def mock_powermeter():
    pm = Mock()
    pm.get_powermeter_watts = AsyncMock()
    pm.get_powermeter_watts_raw = AsyncMock()
    pm.wait_for_message = AsyncMock()
    pm.wait_for_next_message = AsyncMock()
    return pm


async def test_default_offset_is_identity(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    d = DynamicOffsetPowermeter(mock_powermeter)
    assert d.offset == 0.0
    assert await d.get_powermeter_watts() == [100.0, 200.0, 300.0]


async def test_offset_applied_to_every_phase(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    d = DynamicOffsetPowermeter(mock_powermeter, offset=500.0)
    assert await d.get_powermeter_watts() == [600.0, 700.0, 800.0]


async def test_set_offset_live(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [1000.0]
    d = DynamicOffsetPowermeter(mock_powermeter)
    assert await d.get_powermeter_watts() == [1000.0]
    d.set_offset(-250.0)
    assert d.offset == -250.0
    assert await d.get_powermeter_watts() == [750.0]
    d.set_offset(0.0)
    assert await d.get_powermeter_watts() == [1000.0]


async def test_is_dynamic_offset_marker(mock_powermeter):
    d = DynamicOffsetPowermeter(mock_powermeter)
    assert getattr(d, "is_dynamic_offset", False) is True


async def test_raw_reading_is_untouched(mock_powermeter):
    """The live offset shifts control values only; raw matches the meter."""
    mock_powermeter.get_powermeter_watts.return_value = [100.0]
    mock_powermeter.get_powermeter_watts_raw.return_value = [100.0]
    d = DynamicOffsetPowermeter(mock_powermeter, offset=500.0)
    assert await d.get_powermeter_watts() == [600.0]
    assert await d.get_powermeter_watts_raw() == [100.0]


async def test_int_values_from_powermeter(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100, 200]
    d = DynamicOffsetPowermeter(mock_powermeter, offset=0.5)
    assert await d.get_powermeter_watts() == [100.5, 200.5]


async def test_empty_values(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = []
    d = DynamicOffsetPowermeter(mock_powermeter, offset=500.0)
    assert await d.get_powermeter_watts() == []


async def test_wait_for_message_passthrough(mock_powermeter):
    d = DynamicOffsetPowermeter(mock_powermeter)
    await d.wait_for_message(timeout=30)
    mock_powermeter.wait_for_message.assert_called_once_with(30)
