"""Tests for the power limit: writing, showing and keeping the ECU aligned."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from freezegun.api import FrozenDateTimeFactory
from modbus_connection import ModbusConnectionError, ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit, WriteEvent
import pytest

from homeassistant.components import persistent_notification
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.apsystems_modbus.const import (
    CONF_INVERTERS,
    CONF_MODEL,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
)
from custom_components.apsystems_modbus.coordinator import EcuCoordinator
from custom_components.apsystems_modbus.ecu import (
    EXPORT_LIMIT_ADDRESS,
    EXPORT_LIMIT_ENABLED_ADDRESS,
    GRID_CONNECTION_ADDRESS,
    Inverter,
)
from custom_components.apsystems_modbus.power_limit import (
    REFRESH_DELAY,
    REFRESH_REPEAT,
    REWRITE_INTERVAL,
    STEP_HOLD,
    effective_limit,
)

from .conftest import INVERTERS, add_inverter, set_float32

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


def entry_with_four_inverters(mock_ecu: MockModbusConnection) -> MockConfigEntry:
    """A config entry for four inverters, so a poll can be cut short."""
    extra = []
    for unit_id in (3, 4):
        serial = f"70300000000{unit_id}"
        add_inverter(mock_ecu, unit_id, serial)
        extra.append(
            {CONF_UNIT_ID: unit_id, CONF_SERIAL_NUMBER: serial, CONF_MODEL: "DS3"}
        )
    return MockConfigEntry(
        domain="apsystems_modbus",
        title="APsystems ECU",
        unique_id="704000000001",
        data={"host": "192.0.2.10", "port": 502, CONF_INVERTERS: [*INVERTERS, *extra]},
    )


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


async def test_pending_is_retried_while_one_inverter_stays_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)

    # A broken inverter that never answers does not hold the retry up.
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()

    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.pending is False


async def test_pending_is_not_retried_in_a_poll_cut_short(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = entry_with_four_inverters(mock_ecu)
    coordinator = await setup_entry(hass, entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)
    caplog.clear()

    # Unit 1 answers, but the ECU struggles: the cycle is cut short.
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()

    assert coordinator.data.cut_short is True
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


async def test_reload_keeps_the_choice_and_refreshes_it(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)

    writes = record_writes(mock_ecu.for_unit(1))
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert attributes_of(hass)["desired"] == 30
    # The ECU may have restarted meanwhile: the choice is sent again at once.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


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


async def test_restore_waits_for_a_poll_not_cut_short(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
) -> None:
    entry = entry_with_four_inverters(mock_ecu)
    coordinator = await setup_entry(hass, entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # The ECU lost the limit while it struggles: no write yet.
    set_ecu(unit, 100, 48)
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert writes == []
    assert state_of(hass) == "100"

    # The next poll that is not cut short restores it.
    mock_ecu.for_unit(2).fail_requests(None)
    mock_ecu.for_unit(3).fail_requests(None)
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


async def test_refresh_sends_the_limit_again_via_one_percent_less(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    freezer.tick(REFRESH_DELAY - timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes == []

    freezer.tick(timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.refreshing is True
    assert state_of(hass) == "30"

    # The in-between value stays one ECU round, and is never shown.
    freezer.tick(STEP_HOLD - timedelta(seconds=1))
    await coordinator.async_refresh()
    assert len(writes) == 2
    assert state_of(hass) == "30"

    freezer.tick(timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes[2:] == [
        (EXPORT_LIMIT_ADDRESS, 300),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]
    assert coordinator.power_limit.refreshing is False
    assert coordinator.power_limit.refresh_count == 1
    assert state_of(hass) == "30"


REFRESH_AT_100 = (
    100,
    # At 100 the in-between value is 99, with the limit switched on.
    [
        (EXPORT_LIMIT_ADDRESS, 990),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 1000),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 0),
    ],
)
REFRESH_AT_0 = (
    0,
    # At 0 there is nothing below, so it is 1.
    [
        (EXPORT_LIMIT_ADDRESS, 10),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 0),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ],
)


@pytest.mark.parametrize("case", [REFRESH_AT_100, REFRESH_AT_0])
async def test_refresh_at_the_ends_of_the_range(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    case: tuple[int, list[tuple[int, int]]],
) -> None:
    percent, expected = case
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, percent)
    writes = record_writes(mock_ecu.for_unit(1))

    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()

    assert writes == expected
    assert state_of(hass) == str(percent)


async def test_two_refreshes_per_change_then_none(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 100)
    writes = record_writes(mock_ecu.for_unit(1))

    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert len(writes) == 4

    # The second one comes an hour after the first started, not earlier.
    freezer.tick(REFRESH_REPEAT - STEP_HOLD - timedelta(seconds=1))
    await coordinator.async_refresh()
    assert len(writes) == 4
    freezer.tick(timedelta(seconds=1))
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert len(writes) == 8

    for _ in range(6):
        freezer.tick(REFRESH_REPEAT)
        await coordinator.async_refresh()
    assert len(writes) == 8
    assert coordinator.power_limit.refresh_count == 2
    assert coordinator.power_limit.next_refresh is None


async def test_change_during_a_refresh_takes_over(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]

    await set_limit(hass, 60)
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()

    # Only the new choice was written; its own refreshes start afresh.
    assert writes[2:] == [
        (EXPORT_LIMIT_ADDRESS, 600),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]
    assert coordinator.power_limit.refreshing is False
    assert state_of(hass) == "60"
    freezer.tick(REFRESH_DELAY - STEP_HOLD)
    await coordinator.async_refresh()
    assert writes[4:] == [
        (EXPORT_LIMIT_ADDRESS, 590),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]


async def test_same_value_again_does_not_move_the_refresh(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    planned = coordinator.power_limit.next_refresh

    freezer.tick(timedelta(minutes=10))
    await set_limit(hass, 30)

    assert coordinator.power_limit.next_refresh == planned


async def test_no_refresh_while_an_inverter_is_offline(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    # Early morning: unit 2 answers but does not feed in yet.
    unit = mock_ecu.for_unit(2)
    set_float32(unit, 40144, 0.0)
    set_float32(unit, 40146, 0.0)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == []

    set_float32(unit, 40144, 150.0)
    set_float32(unit, 40146, 50.0)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


async def test_finishing_a_refresh_is_not_held_up_by_a_cut_short_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = entry_with_four_inverters(mock_ecu)
    coordinator = await setup_entry(hass, entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()

    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()

    assert coordinator.data.cut_short is True
    assert writes[2:] == [
        (EXPORT_LIMIT_ADDRESS, 300),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]


async def test_failed_finish_is_retried_at_the_next_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()

    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert coordinator.power_limit.refreshing is True
    assert attributes_of(hass)["pending"] is False

    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes[2:] == [
        (EXPORT_LIMIT_ADDRESS, 300),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]
    assert coordinator.power_limit.refreshing is False


async def test_silent_ecu_below_100_refreshes_once_it_answers(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    # A restart of the ECU: nothing answers for a while.
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)

    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


async def test_silent_ecu_at_100_does_not_refresh(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 100)
    writes = record_writes(mock_ecu.for_unit(1))

    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)
    await coordinator.async_refresh()

    assert writes == []


async def test_lost_connection_is_reopened_in_the_same_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))
    inverter = coordinator.inverters[1]
    original = inverter.async_poll
    calls = 0

    async def lose_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModbusConnectionError("connection reset by peer")
        return await original(*args, **kwargs)

    inverter.async_poll = lose_once  # type: ignore[method-assign]
    await coordinator.async_refresh()

    # Everything stays available, and below 100 the limit is sent again.
    assert coordinator.last_update_success is True
    assert coordinator.data.available == {1, 2}
    assert calls == 2
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


async def test_connection_lost_twice_fails_the_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    inverter = coordinator.inverters[1]

    async def always_lost(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ModbusConnectionError("connection reset by peer")

    inverter.async_poll = always_lost  # type: ignore[method-assign]
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_choice_is_restored_after_a_restart(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(ENTITY, "30"),
                {"desired": 30, "entry_id": mock_config_entry.entry_id},
            ),
        ),
    )
    set_ecu(mock_ecu.for_unit(1), 30, 49)
    writes = record_writes(mock_ecu.for_unit(1))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: EcuCoordinator = mock_config_entry.runtime_data

    # The ECU may have restarted meanwhile: the first poll sends it again.
    assert attributes_of(hass)["desired"] == 30
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.refreshing is True


async def test_factory_value_after_a_power_cut_is_put_back(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(ENTITY, "100"),
                {"desired": 100, "entry_id": mock_config_entry.entry_id},
            ),
        ),
    )
    unit = mock_ecu.for_unit(1)
    set_ecu(unit, 30, 1)  # the ECU's factory value
    writes = record_writes(unit)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: EcuCoordinator = mock_config_entry.runtime_data

    # The first poll after the start writes the remembered 100 back.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]
    assert state_of(hass) == "100"
    assert coordinator.power_limit.desired == 100


@pytest.mark.parametrize(
    "extra",
    [
        {"desired": 0, "entry_id": "an_entry_that_was_removed"},
        {"desired": 130, "entry_id": None},
        {"desired": "0", "entry_id": None},
        {"desired": True, "entry_id": None},
    ],
)
async def test_unusable_stored_choice_is_ignored(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    extra: dict,
) -> None:
    mock_config_entry.add_to_hass(hass)
    if extra["entry_id"] is None:
        extra = {**extra, "entry_id": mock_config_entry.entry_id}
    mock_restore_cache_with_extra_data(hass, ((State(ENTITY, "0"), extra),))
    writes = record_writes(mock_ecu.for_unit(1))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: EcuCoordinator = mock_config_entry.runtime_data

    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert attributes_of(hass)["desired"] is None
    assert writes == []


async def test_failed_in_between_write_is_retried_at_the_next_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == []
    assert coordinator.power_limit.refreshing is False
    assert attributes_of(hass)["pending"] is False

    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


async def test_failed_change_during_a_refresh_is_retried_at_once(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 50)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 490), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]

    # The user releases, but the ECU does not take the write.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    with pytest.raises(HomeAssistantError):
        await set_limit(hass, 100)
    assert coordinator.power_limit.refreshing is False

    # The next poll delivers it, without waiting for the refresh to end.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes[2:] == [
        (EXPORT_LIMIT_ADDRESS, 1000),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 0),
    ]
    assert coordinator.power_limit.pending is False


async def test_lost_connection_refreshes_at_most_once_an_hour(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    def silent_ecu() -> None:
        for unit_id in (1, 2):
            mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    def ecu_back() -> None:
        for unit_id in (1, 2):
            mock_ecu.for_unit(unit_id).fail_requests(None)

    silent_ecu()
    await coordinator.async_refresh()
    ecu_back()
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert len(writes) == 4

    # A network that drops the connection again soon does not cause a loop.
    freezer.tick(timedelta(minutes=10))
    silent_ecu()
    await coordinator.async_refresh()
    ecu_back()
    await coordinator.async_refresh()
    assert len(writes) == 4
