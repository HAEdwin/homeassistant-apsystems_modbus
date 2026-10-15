"""Tests for the online and grid connection binary sensors."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import set_float32


async def test_online_and_grid_connection(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    # Off the grid at night, with the 1 W the ECU may keep.
    set_float32(mock_ecu.for_unit(2), 40138, 0.0)
    set_float32(mock_ecu.for_unit(2), 40144, 1.0)
    set_float32(mock_ecu.for_unit(2), 40146, 0.0)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert (
        hass.states.get("binary_sensor.inverter_704000000001_online").state == STATE_ON
    )
    assert (
        hass.states.get("binary_sensor.inverter_703000000002_online").state == STATE_OFF
    )

    registry = er.async_get(hass)
    grid = registry.async_get("binary_sensor.apsystems_ecu_inverters_enabled")
    assert grid is not None
    assert grid.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_grid_connection_unavailable_without_lead_inverter(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    registry.async_update_entity(
        "binary_sensor.apsystems_ecu_inverters_enabled", disabled_by=None
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    entity_id = "binary_sensor.apsystems_ecu_inverters_enabled"
    assert hass.states.get(entity_id).state == STATE_ON

    # A missed cycle keeps the last flag; a quarter of an hour without does not.
    coordinator = mock_config_entry.runtime_data
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(entity_id).state == STATE_ON

    freezer.tick(timedelta(minutes=16))
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
