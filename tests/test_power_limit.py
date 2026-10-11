"""Tests for the power limit: writing, showing and keeping the ECU aligned."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from freezegun.api import FrozenDateTimeFactory
from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit, WriteEvent
import pytest

from homeassistant.components import persistent_notification
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.apsystems_modbus.coordinator import EcuCoordinator
from custom_components.apsystems_modbus.ecu import (
    EXPORT_LIMIT_ADDRESS,
    EXPORT_LIMIT_ENABLED_ADDRESS,
    GRID_CONNECTION_ADDRESS,
    Inverter,
)
from custom_components.apsystems_modbus.power_limit import (
    REWRITE_INTERVAL,
    effective_limit,
)

from .conftest import add_inverter

ENTITY = "number.apsystems_ecu_power_limit"


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> EcuCoordinator:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def record_writes(unit: MockModbusUnit) -> list[tuple[int, int]]:
    """Collect (address, value) of every write to a unit."""
    writes: list[tuple[int, int]] = []
    unit.on_write(lambda event: writes.append((event.address, event.values[0])))
    return writes


def count_write_attempts(unit: MockModbusUnit) -> list[int]:
    """Collect the address of every write attempt, including failing ones."""
    attempts: list[int] = []
    original = unit.write_registers

    async def write_registers(address: int, values: list[int]) -> None:
        attempts.append(address)
        await original(address, values)

    unit.write_registers = write_registers  # type: ignore[method-assign]
    return attempts


def set_ecu(unit: MockModbusUnit, percent: int, enabled_raw: int) -> None:
    """Make the ECU report a limit (percent as whole percent, flag as raw value)."""
    unit.holding[EXPORT_LIMIT_ADDRESS] = percent * 10
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = enabled_raw


async def set_limit(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: ENTITY, ATTR_VALUE: value},
        blocking=True,
    )


def notifications(hass: HomeAssistant) -> dict:
    """Return the persistent notifications currently shown."""
    return persistent_notification._async_get_or_create_notifications(hass)


def state_of(hass: HomeAssistant) -> str:
    """Return the state as a plain number string, "100" for 100 or 100.0."""
    state = hass.states.get(ENTITY)
    assert state is not None
    if state.state == STATE_UNKNOWN:
        return state.state
    return str(int(float(state.state)))


def attributes_of(hass: HomeAssistant) -> dict:
    state = hass.states.get(ENTITY)
    assert state is not None
    return state.attributes


async def test_set_below_100_writes_percent_then_enable(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert state_of(hass) == "100"
    writes = record_writes(mock_ecu.for_unit(1))

    await set_limit(hass, 30)

    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert state_of(hass) == "30"
    assert attributes_of(hass)["desired"] == 30
    assert attributes_of(hass)["pending"] is False


async def test_set_100_writes_1000_then_disable(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    await set_limit(hass, 100)

    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]
    assert state_of(hass) == "100"


async def test_other_inverters_are_never_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(2))

    await set_limit(hass, 30)

    assert writes == []


async def test_value_is_rounded_to_whole_percent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    await coordinator.power_limit.async_set(57.4)
    await coordinator.power_limit.async_set(0)
    await coordinator.power_limit.async_set(100.0)

    assert writes[0] == (EXPORT_LIMIT_ADDRESS, 570)
    assert writes[2] == (EXPORT_LIMIT_ADDRESS, 0)
    assert writes[4] == (EXPORT_LIMIT_ADDRESS, 1000)


async def test_same_value_again_does_not_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    await set_limit(hass, 30)

    assert writes == []


async def test_first_use_writes_even_when_ecu_already_reports_it(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    set_ecu(mock_ecu.for_unit(1), 30, 49)
    await setup_entry(hass, mock_config_entry)
    assert state_of(hass) == "30"
    writes = record_writes(mock_ecu.for_unit(1))

    await set_limit(hass, 30)

    assert len(writes) == 2


async def test_failed_first_write_is_pending_and_retried_at_next_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())

    with pytest.raises(HomeAssistantError) as err:
        await set_limit(hass, 30)
    assert err.value.translation_key == "power_limit_pending"
    assert writes == []
    assert state_of(hass) == "100"
    assert attributes_of(hass)["desired"] == 30
    assert attributes_of(hass)["pending"] is True
    assert "did not reach the ECU" in caplog.text

    # The ECU answers again: the next poll completes the pending write.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.pending is False
    assert coordinator.power_limit.shown == 30


async def test_pending_retry_is_rate_limited(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 30)
    writes = record_writes(unit)

    # The first retry fails as well ...
    await coordinator.async_refresh()
    assert writes == []
    assert coordinator.power_limit.pending is True

    # ... and although the ECU would accept now, the next poll does not retry.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == []

    freezer.tick(REWRITE_INTERVAL + timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.pending is False


async def test_failed_second_write_at_100_is_not_undone(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    attempts = count_write_attempts(unit)
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, ModbusTimeoutError())

    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 100)

    # The percentage landed, the flag did not: 1000 with enable is full power.
    # The flag was tried exactly twice: once plus one retry.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000)]
    assert attempts.count(EXPORT_LIMIT_ENABLED_ADDRESS) == 2
    assert coordinator.power_limit.desired == 100
    assert coordinator.power_limit.pending is True

    # The ECU reads 1000/1 back: effectively 100, so nothing is written again.
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000)]
    assert coordinator.power_limit.pending is False
    assert state_of(hass) == "100"


async def test_failed_second_write_below_100_is_completed(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, ModbusTimeoutError())

    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 30)
    # 300 without the flag does nothing; the user is told and nothing is shown.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300)]
    assert state_of(hass) == "100"

    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes[1:] == [
        (EXPORT_LIMIT_ADDRESS, 300),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]
    assert state_of(hass) == "30"


async def test_pending_is_not_retried_in_a_poll_with_timeouts(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)
    caplog.clear()

    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()

    assert writes == []
    assert coordinator.power_limit.pending is True
    assert not [
        r for r in caplog.records if r.levelno > logging.DEBUG and "limit" in r.message
    ]


async def test_pending_and_same_value_again_writes_now(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)

    await set_limit(hass, 30)

    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert attributes_of(hass)["pending"] is False


async def test_one_deviating_poll_moves_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 100)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    set_ecu(unit, 30, 1)  # the dusk glitch: factory values for one poll
    await coordinator.async_refresh()
    assert state_of(hass) == "100"
    assert writes == []

    set_ecu(unit, 100, 48)
    await coordinator.async_refresh()
    assert state_of(hass) == "100"
    assert writes == []
    assert coordinator.power_limit.deviations == 0


async def test_two_deviating_polls_show_and_restore(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # The ECU lost the setting (restart): it reports no limit twice.
    set_ecu(unit, 100, 48)
    await coordinator.async_refresh()
    assert state_of(hass) == "30"
    assert writes == []
    await coordinator.async_refresh()

    # The control followed the ECU and the choice was written back at once.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert state_of(hass) == "30"
    assert "set it back to 30 %" in caplog.text

    await coordinator.async_refresh()
    assert state_of(hass) == "30"
    assert len(writes) == 2


async def test_restore_happens_at_most_every_ten_minutes(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # A stubborn ECU that keeps reporting no limit whatever is written.
    unit.on_write(lambda event: set_ecu(unit, 100, 48))
    set_ecu(unit, 100, 48)
    for _ in range(4):
        await coordinator.async_refresh()
    assert len(writes) == 2

    freezer.tick(REWRITE_INTERVAL + timedelta(seconds=1))
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert len(writes) == 4


async def test_no_choice_means_no_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    for percent, raw in ((0, 49), (30, 1), (0, 49)):
        set_ecu(unit, percent, raw)
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert state_of(hass) == str(percent)
    assert writes == []
    assert attributes_of(hass)["desired"] is None


async def test_reload_forgets_the_choice(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: EcuCoordinator = mock_config_entry.runtime_data
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    assert attributes_of(hass)["desired"] is None
    set_ecu(unit, 100, 48)
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert writes == []
    assert state_of(hass) == "100"


async def test_stale_readback_of_a_silent_control_unit_does_not_count(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    unit.fail_requests(ModbusTimeoutError())
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = 48  # not readable anyway
    for _ in range(3):
        await coordinator.async_refresh()

    assert writes == []
    assert coordinator.power_limit.deviations == 0
    assert state_of(hass) == "30"


async def test_wrong_serial_on_control_unit_does_not_count(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    add_inverter(mock_ecu, 1, "704000009999")  # another inverter moved to address 1
    for _ in range(3):
        await coordinator.async_refresh()

    assert writes == []


async def test_latest_of_concurrent_calls_wins(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    # A poll read is in progress, so three quick changes pile up behind it.
    async with coordinator.write_lock:
        calls = [
            asyncio.create_task(coordinator.power_limit.async_set(value))
            for value in (30, 45, 60)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    await asyncio.gather(*calls)

    # The first call was already committed; the middle one is skipped.
    assert writes == [
        (EXPORT_LIMIT_ADDRESS, 300),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 600),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]
    assert coordinator.power_limit.desired == 60
    assert coordinator.power_limit.shown == 60


@pytest.mark.parametrize(
    ("percent_raw", "enabled_raw", "expected"),
    [
        (300, 1, 30),
        (300, 49, 30),
        (50, 48, 100),
        (50, 0, 100),
        (1000, 49, 100),
        (0xFFFF, 1, None),
        (300, 7, None),
    ],
)
async def test_effective_limit(
    mock_ecu: MockModbusConnection,
    percent_raw: int,
    enabled_raw: int,
    expected: int | None,
) -> None:
    unit = mock_ecu.for_unit(1)
    unit.holding[EXPORT_LIMIT_ADDRESS] = percent_raw
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = enabled_raw
    inverter = Inverter(unit, 1, checks_layout=False)
    await inverter.async_poll(("measurements",))

    assert effective_limit(inverter.measurements) == expected


async def test_unreadable_limit_keeps_the_shown_value(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = 7

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert state_of(hass) == "100"


async def test_stays_available_while_the_ecu_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert state_of(hass) == "100"

    # Setting is attempted and reports the failure instead of being skipped.
    with pytest.raises(HomeAssistantError) as err:
        await set_limit(hass, 100)
    assert err.value.translation_key == "power_limit_pending"


async def test_unknown_before_the_first_poll_but_settable(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_read(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    await setup_entry(hass, mock_config_entry)
    assert state_of(hass) == STATE_UNKNOWN

    writes = record_writes(mock_ecu.for_unit(1))
    await set_limit(hass, 40)
    assert writes == [(EXPORT_LIMIT_ADDRESS, 400), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert state_of(hass) == "40"


async def test_removal_releases_an_active_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    set_ecu(unit, 30, 49)
    writes = record_writes(unit)
    unit.read_events.clear()

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert len(unit.read_events) == 1
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]
    assert not notifications(hass)


async def test_removal_without_a_limit_only_reads(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert writes == []


async def test_removal_tries_the_next_inverter_when_the_first_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    unit_2 = mock_ecu.for_unit(2)
    set_ecu(unit_2, 30, 49)
    writes = record_writes(unit_2)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]


async def test_removal_warns_when_the_ecu_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.entry_id not in {
        e.entry_id for e in hass.config_entries.async_entries()
    }
    assert "Could not release the power limit" in caplog.text
    assert len(notifications(hass)) == 1


async def test_grid_connection_is_never_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    events: list[WriteEvent] = []
    unit.on_write(events.append)

    await set_limit(hass, 0)
    await set_limit(hass, 100)
    set_ecu(unit, 30, 1)
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert events
    assert all(event.address != GRID_CONNECTION_ADDRESS for event in events)
    # The ECU only answers "write multiple registers" (function code 16).
    assert {event.function_code for event in events} == {0x10}
    # The autouse fixture fails this test if 40188 was even attempted.


async def test_restore_waits_for_a_poll_without_timeouts(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # The ECU lost the limit while another inverter is silent: no write yet.
    set_ecu(unit, 100, 48)
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert writes == []
    assert state_of(hass) == "100"

    # The next clean poll restores it.
    mock_ecu.for_unit(2).fail_requests(None)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


async def test_whole_ecu_silent_changes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert writes == []
    assert coordinator.power_limit.deviations == 0
    assert state_of(hass) == "30"


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
async def test_non_finite_value_is_refused(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    value: float,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.power_limit.async_set(value)

    assert err.value.translation_key == "power_limit_invalid"
    assert writes == []
    assert coordinator.power_limit.desired is None


async def test_removal_does_not_write_on_an_unreadable_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = 7
    writes = record_writes(unit)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert writes == []
    assert len(notifications(hass)) == 1
