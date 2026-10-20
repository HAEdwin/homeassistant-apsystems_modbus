"""Tests for the inverter output switch: turning all inverters off and on."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import datetime, timedelta
import logging
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection, WriteEvent
import pytest

from homeassistant.components import persistent_notification
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.apsystems_modbus.coordinator import EcuCoordinator
from custom_components.apsystems_modbus.ecu import (
    EXPORT_LIMIT_ADDRESS,
    GRID_CONNECTION_ADDRESS,
)

ENTITY = "switch.apsystems_ecu_inverter_output"
START = datetime(2026, 10, 20, 11, 1, 30, tzinfo=dt_util.UTC)
"""Just after a poll, between two ECU rounds."""


@pytest.fixture(autouse=True)
def daylight() -> Generator[MagicMock]:
    """Make it day unless a test says otherwise."""
    with patch(
        "custom_components.apsystems_modbus.inverter_output.sun.is_up",
        return_value=True,
    ) as is_up:
        yield is_up


async def setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> EcuCoordinator:
    freezer.move_to(START)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data


def conn_writes(mock_ecu: MockModbusConnection, unit_id: int = 1) -> list[int]:
    """Collect the values written to Conn on one unit."""
    values: list[int] = []

    def take(event: WriteEvent) -> None:
        if event.address == GRID_CONNECTION_ADDRESS:
            assert len(event.values) == 1
            values.append(event.values[0])

    mock_ecu.for_unit(unit_id).on_write(take)
    return values


async def switch(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: str) -> None:
    """Move the clock to a time today (UTC) and let the timers fire."""
    hour, minute, second = (int(part) for part in when.split(":"))
    freezer.move_to(START.replace(hour=hour, minute=minute, second=second))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_on_by_default_and_nothing_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    writes = conn_writes(mock_ecu)
    await setup_entry(hass, mock_config_entry, freezer)
    await at(hass, freezer, "11:05:00")

    assert hass.states.get(ENTITY).state == STATE_ON
    assert writes == []


async def test_off_writes_at_once_and_before_every_round(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await switch(hass, SERVICE_TURN_OFF)
    assert hass.states.get(ENTITY).state == STATE_OFF
    assert writes == [0]

    await at(hass, freezer, "11:05:00")
    await at(hass, freezer, "11:07:30")
    await at(hass, freezer, "11:10:00")
    assert writes == [0, 0, 0]
    # Unit 2 never gets anything: one write on the lead covers the ECU.
    assert conn_writes(mock_ecu, 2) == []


async def test_on_writes_one_once_and_stops(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await switch(hass, SERVICE_TURN_OFF)
    await switch(hass, SERVICE_TURN_ON)
    await at(hass, freezer, "11:05:00")
    await at(hass, freezer, "11:10:00")

    assert hass.states.get(ENTITY).state == STATE_ON
    assert writes == [0, 1]


async def test_same_choice_again_writes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await switch(hass, SERVICE_TURN_ON)
    await switch(hass, SERVICE_TURN_OFF)
    await switch(hass, SERVICE_TURN_OFF)

    assert writes == [0]


async def test_clock_write_skipped_right_after_a_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    freezer.move_to(START.replace(minute=4, second=55))
    await switch(hass, SERVICE_TURN_OFF)
    await at(hass, freezer, "11:05:00")
    assert writes == [0]

    await at(hass, freezer, "11:10:00")
    assert writes == [0, 0]


async def test_late_clock_write_is_skipped_and_caught_up(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    # Home Assistant was busy: the write of 11:05 runs only at 11:05:07,
    # in the ECU round, where it would do nothing.
    await at(hass, freezer, "11:05:07")
    assert writes == []
    assert coordinator.inverter_output.catch_up

    # The next healthy poll sends it.
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [0]
    assert not coordinator.inverter_output.catch_up


async def test_no_write_at_night_then_after_the_first_poll_by_day(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    daylight: MagicMock,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)
    daylight.return_value = False

    await switch(hass, SERVICE_TURN_OFF)  # No error at night.
    await at(hass, freezer, "11:05:00")
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY).state == STATE_OFF
    assert writes == []

    daylight.return_value = True
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [0]


async def test_slow_ecu_raises_and_the_next_healthy_poll_sends_it(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)
    coordinator.poll_healthy = False

    with pytest.raises(HomeAssistantError) as err:
        await switch(hass, SERVICE_TURN_OFF)
    assert err.value.translation_key == "inverter_output_pending"
    assert hass.states.get(ENTITY).state == STATE_OFF

    # The clock write waits too while the ECU struggles.
    await at(hass, freezer, "11:05:00")
    assert writes == []

    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.poll_healthy
    assert writes == [0]


async def test_unanswered_write_raises_and_frees_the_link_later(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(GRID_CONNECTION_ADDRESS, ModbusTimeoutError())

    with pytest.raises(HomeAssistantError):
        await switch(hass, SERVICE_TURN_OFF)
    assert coordinator.link_busy
    assert coordinator.inverter_output.last_result == "ModbusTimeoutError"

    unit.fail_write(GRID_CONNECTION_ADDRESS, None)
    writes = conn_writes(mock_ecu)
    # The next poll starts on a fresh link and then sends it.
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator.link_busy
    assert writes == [0]


async def test_failed_on_only_warns(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    mock_ecu.for_unit(1).fail_write(GRID_CONNECTION_ADDRESS, ModbusTimeoutError())

    await switch(hass, SERVICE_TURN_ON)

    assert hass.states.get(ENTITY).state == STATE_ON
    assert "Could not send inverter output on" in caplog.text


async def test_switch_never_touches_the_power_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    addresses: list[int] = []
    mock_ecu.for_unit(1).on_write(lambda event: addresses.append(event.address))

    await switch(hass, SERVICE_TURN_OFF)
    await at(hass, freezer, "11:05:00")
    await switch(hass, SERVICE_TURN_ON)

    assert EXPORT_LIMIT_ADDRESS not in addresses
    assert set(addresses) == {GRID_CONNECTION_ADDRESS}


async def test_off_survives_a_reload(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(ENTITY).state == STATE_OFF
    # The first poll after the start sends it, then the clock again.
    assert writes == [0]
    await at(hass, freezer, "11:05:00")
    assert writes == [0, 0]


async def test_unload_stops_the_clock(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await at(hass, freezer, "11:05:00")

    assert writes == []


async def test_removal_turns_the_output_on(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert writes == [1]


async def test_removal_with_the_output_on_writes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert writes == []


async def test_removal_tells_the_user_when_the_ecu_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert any(
        "Inverter output was off" in n["message"] for n in notifications.values()
    )


async def test_debug_log_shows_the_writes(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    caplog.set_level(
        logging.DEBUG, logger="custom_components.apsystems_modbus.inverter_output"
    )

    await switch(hass, SERVICE_TURN_OFF)
    freezer.move_to(START.replace(minute=5, second=7))
    async_fire_time_changed(hass, START.replace(minute=5) + timedelta(seconds=7))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert "Writing grid connection register 40188 = 0" in caplog.text
    assert "answered after" in caplog.text
    assert "too late for the ECU round" in caplog.text


async def test_failed_clock_write_is_caught_up(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(GRID_CONNECTION_ADDRESS, ModbusTimeoutError())

    await at(hass, freezer, "11:05:00")
    assert coordinator.inverter_output.catch_up

    unit.fail_write(GRID_CONNECTION_ADDRESS, None)
    writes = conn_writes(mock_ecu)
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [0]


async def test_quick_off_and_on_ends_on(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)
    output = coordinator.inverter_output

    # On is chosen while off is still being written: on comes last.
    await asyncio.gather(output.async_set(False), output.async_set(True))
    await at(hass, freezer, "11:05:00")

    assert output.enabled
    assert writes == [0, 1]
