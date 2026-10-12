"""Tests for setup, unload and the poll budget."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from modbus_connection import ModbusConnectionError, ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.apsystems_modbus.const import (
    CONF_INVERTERS,
    CONF_MODEL,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
    ROUND_PERIOD,
)
from custom_components.apsystems_modbus.coordinator import (
    EcuCoordinator,
    new_address_issue_id,
    round_interval,
    settle_time,
    until_next_poll,
)

from .conftest import INVERTERS, add_inverter, set_float32


@pytest.fixture(autouse=True)
def no_platforms() -> None:
    """Entities are tested separately; keep setup to the coordinator here."""
    with patch("custom_components.apsystems_modbus.PLATFORMS", []):
        yield


def assert_next_poll_after_a_round(
    coordinator: EcuCoordinator, interval: int | None = None
) -> None:
    """The next poll is planned just after an ECU round, within ``interval``."""
    interval = interval or coordinator.interval
    assert coordinator.update_interval is not None
    due = dt_util.utcnow() + coordinator.update_interval
    phase = (due.timestamp() - coordinator.settle) % interval
    assert min(phase, interval - phase) < 1
    assert timedelta(seconds=60) <= coordinator.update_interval
    assert coordinator.update_interval <= timedelta(seconds=interval + 60)


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_first_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    data = mock_config_entry.runtime_data.data
    assert data.available == {1, 2}
    assert data.power == pytest.approx(204.0 + 506.0)
    assert data.energy_today == pytest.approx(554.08 + 541.79, abs=0.01)
    assert data.online == 2
    assert data.last_change is not None


async def test_setup_only_waits_for_one_read(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    unit_2 = mock_ecu.for_unit(2)
    unit_2.fail_requests(ModbusTimeoutError())

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.data.available == {1}
    assert mock_config_entry.runtime_data.data.online == 1


async def test_silent_ecu_retries_setup(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_silent_first_inverter_does_not_block_setup(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    coordinator = mock_config_entry.runtime_data
    assert coordinator.lead.unit_id == 2
    assert coordinator.data.available == {2}


async def test_refused_ecu_retries_setup(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_ecu.for_unit(1).fail_requests(ModbusConnectionError("refused"))

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unsupported_layout_is_a_setup_error(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_ecu.for_unit(1).holding[40070] = 103

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_unload(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_timeout_budget_skips_rest_and_backs_off(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    add_inverter(mock_ecu, 3, "703000000003")
    add_inverter(mock_ecu, 4, "703000000004")
    entry = MockConfigEntry(
        domain="apsystems_modbus",
        unique_id="704000000001",
        data={
            "host": "192.0.2.10",
            "port": 502,
            CONF_INVERTERS: [
                *INVERTERS,
                {
                    CONF_UNIT_ID: 3,
                    CONF_SERIAL_NUMBER: "703000000003",
                    CONF_MODEL: "DS3",
                },
                {
                    CONF_UNIT_ID: 4,
                    CONF_SERIAL_NUMBER: "703000000004",
                    CONF_MODEL: "DS3",
                },
            ],
        },
    )
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.data.available == {1, 2, 3, 4}

    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(4).read_events.clear()
    await coordinator.async_refresh()

    # Units 2 and 3 timed out, so unit 4 was not even asked this cycle.
    assert coordinator.data.available == {1}
    assert mock_ecu.for_unit(4).read_events == []
    # A failed cycle is retried just after the next round.
    assert_next_poll_after_a_round(coordinator, ROUND_PERIOD)
    # The daily total keeps its last complete value instead of dipping.
    assert coordinator.data.energy_today == pytest.approx(554.08 + 3 * 541.79, abs=0.05)

    # Still silent: still every round, no longer waits.
    for _ in range(3):
        await coordinator.async_refresh()
        assert_next_poll_after_a_round(coordinator, ROUND_PERIOD)

    mock_ecu.for_unit(2).fail_requests(None)
    mock_ecu.for_unit(3).fail_requests(None)
    await coordinator.async_refresh()
    assert coordinator.data.available == {1, 2, 3, 4}
    assert_next_poll_after_a_round(coordinator)


def new_address_issue(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        "apsystems_modbus", new_address_issue_id(entry.entry_id)
    )


async def test_replaced_inverter_keeps_counting(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_inverter(mock_ecu, 2, "703000009999")  # a new inverter on address 2

    await setup_entry(hass, mock_config_entry)
    await mock_config_entry.runtime_data.async_refresh()

    assert mock_config_entry.runtime_data.data.available == {1, 2}
    assert caplog.text.count("Unit 2: a new inverter answers") == 1
    assert new_address_issue(hass, mock_config_entry) is None


async def test_replaced_inverter_keeps_the_lifetime(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    before = coordinator.ledger.lifetime

    # The new inverter starts its own daily counter low.
    add_inverter(mock_ecu, 2, "703000009999")
    set_float32(mock_ecu.for_unit(2), ENERGY_TODAY, 10.0)
    for _ in range(2):
        await coordinator.async_refresh()
        assert coordinator.ledger.lifetime >= before
    kept = coordinator.ledger.lifetime

    # Once it runs up, all it produced today counts, from 0.
    set_float32(mock_ecu.for_unit(2), ENERGY_TODAY, 50.0)
    await coordinator.async_refresh()
    assert coordinator.ledger.lifetime == pytest.approx(kept + 50.0)


async def test_changed_address_is_held_back_and_reported(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    add_inverter(mock_ecu, 2, "704000000001")  # unit 1's inverter on address 2

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.data.available == {1}
    issue = new_address_issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.translation_placeholders == {
        "id": "704000000001",
        "old": "1",
        "new": "2",
    }

    # Reconfiguring reloads the entry; with the addresses right it stays away.
    add_inverter(mock_ecu, 2, "703000000002")
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert new_address_issue(hass, mock_config_entry) is None
    assert mock_config_entry.runtime_data.data.available == {1, 2}


async def test_connection_lost_marks_update_failed(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    mock_ecu.for_unit(1).fail_requests(ModbusConnectionError("gone"))

    await coordinator.async_refresh()
    assert coordinator.last_update_success is False

    mock_ecu.for_unit(1).fail_requests(None)
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True


async def test_write_lock_lets_write_go_between_inverters(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    async with coordinator.write_lock:
        refresh = asyncio.ensure_future(coordinator.async_refresh())
        await asyncio.sleep(0)
        assert not refresh.done()
    await refresh
    assert coordinator.last_update_success is True


async def test_interval_recovers_while_one_inverter_stays_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    # The whole ECU stops answering: it is tried again every round.
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    assert_next_poll_after_a_round(coordinator, ROUND_PERIOD)

    # The ECU is back, but unit 2 stays broken: normal polling resumes anyway.
    mock_ecu.for_unit(1).fail_requests(None)
    await coordinator.async_refresh()
    assert coordinator.data.available == {1}
    assert_next_poll_after_a_round(coordinator)


async def test_daily_total_keeps_counting_without_one_inverter(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    before = coordinator.data.energy_today

    # Unit 2 drops out for good; unit 1 keeps producing.
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    set_float32(mock_ecu.for_unit(1), 40154, 600.0)
    await coordinator.async_refresh()

    assert coordinator.data.available == {1}
    assert coordinator.data.energy_today == pytest.approx(600.0 + 541.79, abs=0.01)
    assert coordinator.data.energy_today > before


async def test_first_poll_reads_identity_only_once(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    # Unit 2 only gets the two polling reads; the layout is checked on unit 1.
    spans = sorted((e.address, e.count) for e in mock_ecu.for_unit(2).read_events)
    assert spans == [(40052, 60), (40124, 114)]
    assert any(e.address == 40000 for e in mock_ecu.for_unit(1).read_events)


async def test_impossible_power_reading_is_not_counted(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    set_float32(mock_ecu.for_unit(2), 40144, 3.0e9)
    await setup_entry(hass, mock_config_entry)

    coordinator = mock_config_entry.runtime_data
    assert coordinator.data.power == pytest.approx(204.0)
    assert coordinator.ledger.peak_power == pytest.approx(204.0)


async def test_totals_reach_storage(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    await setup_entry(hass, mock_config_entry)
    key = "apsystems_modbus.704000000001"

    # Written shortly after a poll ...
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["reported"] == pytest.approx(1095.87, abs=0.01)

    # ... and right away when the integration unloads.
    mock_config_entry.runtime_data.ledger.record_power(9999)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert hass_storage[key]["data"]["peak_power"] == 9999


ENERGY_TODAY = 40154
FROZEN = "2026-10-12 10:01:25+00:00"


@pytest.mark.parametrize(
    ("now", "interval", "expected"),
    [
        # Eight inverters: polls at :x1:02, :x6:02 and so on.
        ("2026-10-12 10:01:25+00:00", 300, timedelta(minutes=4, seconds=37)),
        # Too close to the next moment: the one after that.
        ("2026-10-12 10:05:30+00:00", 300, timedelta(minutes=5, seconds=32)),
        ("2026-10-12 10:00:50+00:00", 300, timedelta(minutes=5, seconds=12)),
        # Every second or third round: at 10:11:02 and 10:16:02.
        ("2026-10-12 10:01:25+00:00", 600, timedelta(minutes=9, seconds=37)),
        ("2026-10-12 10:01:25+00:00", 900, timedelta(minutes=14, seconds=37)),
    ],
)
def test_next_poll_comes_just_after_a_round(
    now: str, interval: int, expected: timedelta
) -> None:
    moment = dt_util.parse_datetime(now)
    assert moment is not None
    assert until_next_poll(moment, interval, settle_time(8)) == expected


@pytest.mark.parametrize(("inverters", "seconds"), [(2, 38), (8, 62), (80, 280)])
def test_more_inverters_wait_longer_but_before_the_next_round(
    inverters: int, seconds: int
) -> None:
    assert settle_time(inverters) == seconds


@pytest.mark.parametrize(
    ("stored", "seconds"),
    [(60, 300), (120, 300), (300, 300), (600, 600), (900, 900), (1800, 900)],
)
def test_old_poll_intervals_become_whole_rounds(stored: int, seconds: int) -> None:
    assert round_interval(stored) == seconds


async def test_first_poll_at_once_then_after_each_round(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN)
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    # Two inverters: 38 s into the round, so the next poll is at 10:05:38.
    assert coordinator.data is not None
    assert coordinator.update_interval == timedelta(minutes=4, seconds=13)


async def test_polls_before_the_ecu_refreshed_are_counted(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    # The first poll after a start has nothing to compare with.
    assert coordinator.daytime_polls == 0

    await coordinator.async_refresh()
    assert coordinator.daytime_polls == 1
    assert coordinator.polls_without_news == 1
    assert coordinator.late_inverters == {}

    # Unit 1 has new values, unit 2 not yet.
    set_float32(mock_ecu.for_unit(1), ENERGY_TODAY, 600.0)
    await coordinator.async_refresh()
    assert coordinator.daytime_polls == 2
    assert coordinator.polls_without_news == 1
    assert coordinator.late_inverters == {2: 1}


async def test_night_polls_are_not_counted(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    for unit_id in (1, 2):
        set_float32(mock_ecu.for_unit(unit_id), 40144, 0.0)
        set_float32(mock_ecu.for_unit(unit_id), 40146, 0.0)
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    await coordinator.async_refresh()
    assert coordinator.daytime_polls == 0
    assert coordinator.polls_without_news == 0


async def test_one_inverter_left_at_1_w_at_night_is_not_daytime(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    # At night the ECU often keeps one inverter at 1 W, but with 0 Hz.
    for unit_id in (1, 2):
        set_float32(mock_ecu.for_unit(unit_id), 40144, 0.0)
        set_float32(mock_ecu.for_unit(unit_id), 40146, 0.0)
    set_float32(mock_ecu.for_unit(2), 40144, 1.0)
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.data.online == 1

    await coordinator.async_refresh()
    assert coordinator.daytime_polls == 0
    assert coordinator.polls_without_news == 0


async def test_a_long_interval_still_retries_every_round(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    entry = MockConfigEntry(
        domain="apsystems_modbus",
        title="APsystems ECU",
        unique_id="704000000001",
        data={"host": "192.0.2.10", "port": 502, CONF_INVERTERS: INVERTERS},
        options={CONF_SCAN_INTERVAL: 900},
    )
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    assert_next_poll_after_a_round(coordinator, 900)

    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    assert_next_poll_after_a_round(coordinator, ROUND_PERIOD)

    # Answering again: back to the chosen interval.
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)
    await coordinator.async_refresh()
    assert_next_poll_after_a_round(coordinator, 900)
