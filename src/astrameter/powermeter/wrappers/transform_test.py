from unittest.mock import AsyncMock, Mock

import pytest

from .transform import TransformedPowermeter


@pytest.fixture
def mock_powermeter():
    pm = Mock()
    pm.get_powermeter_watts = AsyncMock()
    pm.get_powermeter_watts_raw = AsyncMock()
    pm.wait_for_message = AsyncMock()
    pm.wait_for_next_message = AsyncMock()
    return pm


async def test_transformed_raw_ignores_transform(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [110.0, 210.0, 310.0]
    mock_powermeter.get_powermeter_watts_raw.return_value = [100.0, 200.0, 300.0]
    t = TransformedPowermeter(mock_powermeter, [30.0], [1.0])
    # Single offset 30 is a total: spread +10 per phase over 3 phases.
    assert await t.get_powermeter_watts() == pytest.approx([120.0, 220.0, 320.0])
    assert await t.get_powermeter_watts_raw() == [100.0, 200.0, 300.0]
    mock_powermeter.get_powermeter_watts_raw.assert_awaited_once()


async def test_identity_single_phase(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [500.0]
    t = TransformedPowermeter(mock_powermeter, [0.0], [1.0])
    assert await t.get_powermeter_watts() == [500.0]


async def test_identity_three_phase(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    t = TransformedPowermeter(mock_powermeter, [0.0], [1.0])
    assert await t.get_powermeter_watts() == [100.0, 200.0, 300.0]


async def test_single_offset_is_total_spread_across_phases(mock_powermeter):
    """A single POWER_OFFSET is a total watts adjustment: the summed reading
    shifts by exactly the offset (spread evenly), not by Nx on N phases."""
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    t = TransformedPowermeter(mock_powermeter, [30.0], [1.0])
    result = await t.get_powermeter_watts()
    assert result == pytest.approx([110.0, 210.0, 310.0])
    assert sum(result) == pytest.approx(600.0 + 30.0)


async def test_single_offset_single_phase_applied_once(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [1050.0]
    t = TransformedPowermeter(mock_powermeter, [50.0], [1.0])
    assert await t.get_powermeter_watts() == [1100.0]


async def test_negative_offset(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [1050.0]
    t = TransformedPowermeter(mock_powermeter, [-50.0], [1.0])
    assert await t.get_powermeter_watts() == [1000.0]


async def test_multiplier_only_broadcast(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    t = TransformedPowermeter(mock_powermeter, [0.0], [2.0])
    assert await t.get_powermeter_watts() == [200.0, 400.0, 600.0]


async def test_both_offset_and_multiplier(mock_powermeter):
    # 1050 * 0.95 + (-50) = 947.5
    mock_powermeter.get_powermeter_watts.return_value = [1050.0]
    t = TransformedPowermeter(mock_powermeter, [-50.0], [0.95])
    result = await t.get_powermeter_watts()
    assert result[0] == pytest.approx(947.5)


async def test_negative_meter_values_with_multiplier(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [-100.0]
    t = TransformedPowermeter(mock_powermeter, [0.0], [2.0])
    assert await t.get_powermeter_watts() == [-200.0]


async def test_per_phase_offsets(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    t = TransformedPowermeter(mock_powermeter, [-10.0, -20.0, -30.0], [1.0])
    assert await t.get_powermeter_watts() == [90.0, 180.0, 270.0]


async def test_per_phase_multipliers(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    t = TransformedPowermeter(mock_powermeter, [0.0], [1.05, 1.02, 1.03])
    result = await t.get_powermeter_watts()
    assert result[0] == pytest.approx(105.0)
    assert result[1] == pytest.approx(204.0)
    assert result[2] == pytest.approx(309.0)


async def test_mixed_single_offset_per_phase_multipliers(mock_powermeter):
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    # Single offset 30 → +10 per phase (total spread over 3); multipliers per phase.
    t = TransformedPowermeter(mock_powermeter, [30.0], [1.0, 2.0, 3.0])
    assert await t.get_powermeter_watts() == pytest.approx([110.0, 410.0, 910.0])


async def test_phase_count_mismatch_does_not_crash(mock_powermeter):
    """Per-phase count != returned value count should warn, not raise."""
    mock_powermeter.get_powermeter_watts.return_value = [100.0]
    t = TransformedPowermeter(mock_powermeter, [10.0, 20.0, 30.0], [1.0])
    # Should not raise; uses cyclic indexing
    result = await t.get_powermeter_watts()
    assert result == [110.0]


async def test_int_values_from_powermeter(mock_powermeter):
    """Many powermeters return int values; transform should handle them."""
    mock_powermeter.get_powermeter_watts.return_value = [100, 200]
    # Single offset 1.0 spread over 2 phases → +0.5 each.
    t = TransformedPowermeter(mock_powermeter, [1.0], [1.0])
    assert await t.get_powermeter_watts() == pytest.approx([100.5, 200.5])


async def test_wait_for_message_passthrough(mock_powermeter):
    t = TransformedPowermeter(mock_powermeter, [0.0], [1.0])
    await t.wait_for_message(timeout=30)
    mock_powermeter.wait_for_message.assert_called_once_with(30)


async def test_wait_for_message_default_timeout(mock_powermeter):
    t = TransformedPowermeter(mock_powermeter, [0.0], [1.0])
    await t.wait_for_message()
    mock_powermeter.wait_for_message.assert_called_once_with(5)


async def test_wait_for_next_message_passthrough(mock_powermeter):
    t = TransformedPowermeter(mock_powermeter, [0.0], [1.0])
    await t.wait_for_next_message(timeout=10)
    mock_powermeter.wait_for_next_message.assert_called_once_with(10)


def test_empty_offsets_raises(mock_powermeter):
    with pytest.raises(ValueError, match="offsets must be a non-empty list"):
        TransformedPowermeter(mock_powermeter, [], [1.0])


def test_empty_multipliers_raises(mock_powermeter):
    with pytest.raises(ValueError, match="multipliers must be a non-empty list"):
        TransformedPowermeter(mock_powermeter, [0.0], [])
