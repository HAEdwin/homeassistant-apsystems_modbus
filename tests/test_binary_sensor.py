"""Tests for the online and grid connection binary sensors."""

from __future__ import annotations

from modbus_connection.mock import MockModbusConnection

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import set_float32


async def test_online_and_grid_connection(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    set_float32(mock_ecu.for_unit(2), 40144, 0.0)
    set_float32(mock_ecu.for_unit(2), 40146, 0.0)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        hass.states.get("binary_sensor.inverter_704000000001_online").state == STATE_ON
    )
    assert (
        hass.states.get("binary_sensor.inverter_703000000002_online").state == STATE_OFF
    )

    registry = er.async_get(hass)
    grid = registry.async_get("binary_sensor.apsystems_ecu_grid_connection")
    assert grid is not None
    assert grid.disabled_by is er.RegistryEntryDisabler.INTEGRATION
