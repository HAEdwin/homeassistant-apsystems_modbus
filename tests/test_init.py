"""Tests for setup, unload and the poll budget."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from modbus_connection import ModbusConnectionError, ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
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
    MAX_BACKOFF,
    RETRY_AFTER_FAILURE,
)

from .conftest import INVERTERS, add_inverter, set_float32


@pytest.fixture(autouse=True)
def no_platforms() -> None:
    """Entities are tested separately; keep setup to the coordinator here."""
    with patch("custom_components.apsystems_modbus.PLATFORMS", []):
        yield


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
    base_interval = coordinator.update_interval

    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(4).read_events.clear()
    await coordinator.async_refresh()

    # Units 2 and 3 timed out, so unit 4 was not even asked this cycle.
    assert coordinator.data.available == {1}
    assert mock_ecu.for_unit(4).read_events == []
    # The first failure is retried soon: the ECU may just be restarting.
    assert coordinator.update_interval == RETRY_AFTER_FAILURE
    # The daily total keeps its last complete value instead of dipping.
    assert coordinator.data.energy_today == pytest.approx(554.08 + 3 * 541.79, abs=0.05)

    # Still silent: from then on less and less often.
    await coordinator.async_refresh()
    assert coordinator.update_interval == RETRY_AFTER_FAILURE * 2
    await coordinator.async_refresh()
    assert coordinator.update_interval == RETRY_AFTER_FAILURE * 4
    await coordinator.async_refresh()
    assert coordinator.update_interval == MAX_BACKOFF
    await coordinator.async_refresh()
    assert coordinator.update_interval == MAX_BACKOFF

    mock_ecu.for_unit(2).fail_requests(None)
    mock_ecu.for_unit(3).fail_requests(None)
    await coordinator.async_refresh()
    assert coordinator.data.available == {1, 2, 3, 4}
    assert coordinator.update_interval == base_interval


async def test_replaced_inverter_is_reported_and_held_back(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    add_inverter(mock_ecu, 2, "703000009999")  # another inverter on address 2

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.data.available == {1}


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
    base_interval = coordinator.update_interval

    # The whole ECU stops answering: the interval backs off.
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    assert coordinator.update_interval == RETRY_AFTER_FAILURE

    # The ECU is back, but unit 2 stays broken: normal polling resumes anyway.
    mock_ecu.for_unit(1).fail_requests(None)
    await coordinator.async_refresh()
    assert coordinator.data.available == {1}
    assert coordinator.update_interval == base_interval


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
