"""Tests for the sensors created from the mock ECU."""

from __future__ import annotations

from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apsystems_modbus.const import CONF_INVERTERS, DOMAIN

from .conftest import INVERTERS, set_float32


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_ecu_totals(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    power = hass.states.get("sensor.apsystems_ecu_power")
    assert power is not None
    assert float(power.state) == pytest.approx(710.0)
    assert power.attributes["unit_of_measurement"] == "W"

    energy = hass.states.get("sensor.apsystems_ecu_energy_today")
    assert energy is not None
    assert float(energy.state) == pytest.approx(1.096, abs=0.001)
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert "state_class" not in energy.attributes

    online = hass.states.get("sensor.apsystems_ecu_inverters_online")
    assert online is not None
    assert online.state == "2"

    updated = hass.states.get("sensor.apsystems_ecu_last_data_update")
    assert updated is not None
    assert updated.attributes["device_class"] == "timestamp"


async def test_inverter_sensors(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    prefix = "sensor.inverter_704000000001_"

    assert float(hass.states.get(f"{prefix}power").state) == 204.0
    assert float(hass.states.get(f"{prefix}energy_today").state) == pytest.approx(
        0.554, abs=0.001
    )
    assert float(hass.states.get(f"{prefix}ac_voltage").state) == 228.0
    assert float(hass.states.get(f"{prefix}frequency").state) == pytest.approx(49.98)
    assert float(hass.states.get(f"{prefix}temperature").state) == 24.0
    assert hass.states.get(f"{prefix}status").state == "mppt"
    assert hass.states.get(f"{prefix}event_code").state == "ok"
    # States carry no float32 noise: at most three decimals.
    for key in (
        "ac_current",
        "dc_voltage_1",
        "dc_current_2",
        "dc_power_1",
        "energy_today",
    ):
        decimals = hass.states.get(f"{prefix}{key}").state.partition(".")[2]
        assert len(decimals) <= 3, key
    assert float(hass.states.get(f"{prefix}dc_voltage_1").state) == pytest.approx(
        34.0, abs=0.1
    )
    assert float(hass.states.get(f"{prefix}dc_power_2").state) == pytest.approx(
        115.0, abs=1
    )


async def test_channels_beyond_model_and_extras_are_disabled(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)

    enabled = registry.async_get("sensor.inverter_704000000001_dc_voltage_2")
    assert enabled is not None
    assert enabled.disabled_by is None

    for key in ("dc_voltage_3", "dc_power_4", "apparent_power", "power_factor"):
        entry = registry.async_get_entity_id("sensor", DOMAIN, f"704000000001-{key}")
        assert entry is not None
        assert (
            registry.async_get(entry).disabled_by
            is er.RegistryEntryDisabler.INTEGRATION
        )


async def test_devices(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    registry = dr.async_get(hass)

    ecu = registry.async_get_device_by_identifier(
        (DOMAIN, "ecu-704000000001"), mock_config_entry.entry_id
    )
    assert ecu is not None
    assert ecu.name == "APsystems ECU"
    assert ecu.manufacturer == "APsystems"

    inverter = registry.async_get_device_by_identifier(
        (DOMAIN, "703000000002"), mock_config_entry.entry_id
    )
    assert inverter is not None
    assert inverter.name == "Inverter 703000000002"
    assert inverter.model == "DS3"
    assert inverter.sw_version is None
    assert inverter.via_device_id == ecu.id


async def test_missing_inverter_goes_unavailable_totals_stay(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.inverter_703000000002_power").state == STATE_UNAVAILABLE
    )
    assert hass.states.get("sensor.inverter_704000000001_power").state == "204.0"
    assert float(hass.states.get("sensor.apsystems_ecu_power").state) == 204.0
    # The daily total keeps the last complete sum.
    assert float(
        hass.states.get("sensor.apsystems_ecu_energy_today").state
    ) == pytest.approx(1.096, abs=0.001)
    assert hass.states.get("sensor.apsystems_ecu_inverters_online").state == "1"


async def test_removed_inverter_device_is_cleaned_up(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    registry = dr.async_get(hass)
    assert registry.async_get_device_by_identifier(
        (DOMAIN, "703000000002"), mock_config_entry.entry_id
    )

    # Reconfigured down to unit 1 only.
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_INVERTERS: INVERTERS[:1]},
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        registry.async_get_device_by_identifier(
            (DOMAIN, "703000000002"), mock_config_entry.entry_id
        )
        is None
    )
    assert registry.async_get_device_by_identifier(
        (DOMAIN, "704000000001"), mock_config_entry.entry_id
    )


async def test_lifetime_and_peak_sensors(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    lifetime = hass.states.get("sensor.apsystems_ecu_lifetime_energy")
    assert lifetime is not None
    assert float(lifetime.state) == pytest.approx(1.096, abs=0.001)
    assert lifetime.attributes["state_class"] == "total_increasing"
    assert lifetime.attributes["unit_of_measurement"] == "kWh"
    assert (
        float(hass.states.get("sensor.apsystems_ecu_lifetime_peak_power").state)
        == 710.0
    )


async def test_lifetime_stays_available_while_ecu_is_unreachable(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.apsystems_ecu_power").state == STATE_UNAVAILABLE
    lifetime = hass.states.get("sensor.apsystems_ecu_lifetime_energy")
    assert float(lifetime.state) == pytest.approx(1.096, abs=0.001)


async def test_lifetime_survives_removing_and_adding_again(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    mock_config_entry.runtime_data.ledger.calibrate(21_971_040)
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    again = MockConfigEntry(
        domain=DOMAIN, unique_id="704000000001", data=dict(mock_config_entry.data)
    )
    await setup_entry(hass, again)
    lifetime = hass.states.get("sensor.apsystems_ecu_lifetime_energy")
    assert float(lifetime.state) == pytest.approx(21_971.04, abs=0.001)


async def test_energy_today_follows_the_ecu_and_lifetime_stays_guarded(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    lifetime = float(hass.states.get("sensor.apsystems_ecu_lifetime_energy").state)

    # The ECU resets its daily counters during the night.
    for unit_id in (1, 2):
        set_float32(mock_ecu.for_unit(unit_id), 40154, 0.0)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert float(hass.states.get("sensor.apsystems_ecu_energy_today").state) == 0.0
    assert (
        float(hass.states.get("sensor.inverter_704000000001_energy_today").state) == 0.0
    )
    assert (
        float(hass.states.get("sensor.apsystems_ecu_lifetime_energy").state) == lifetime
    )

    # After an ECU restart the old value comes back for a while: today shows it,
    # the lifetime does not count it again.
    set_float32(mock_ecu.for_unit(1), 40154, 554.08)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert float(
        hass.states.get("sensor.inverter_704000000001_energy_today").state
    ) == pytest.approx(0.554, abs=0.001)
    assert (
        float(hass.states.get("sensor.apsystems_ecu_lifetime_energy").state) == lifetime
    )


async def test_energy_today_has_no_statistics(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    for entity_id in (
        "sensor.apsystems_ecu_energy_today",
        "sensor.inverter_704000000001_energy_today",
    ):
        assert "state_class" not in hass.states.get(entity_id).attributes, entity_id


async def test_impossible_daily_counter_is_left_out(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    set_float32(mock_ecu.for_unit(1), 40154, 3.0e9)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert float(
        hass.states.get("sensor.inverter_704000000001_energy_today").state
    ) == pytest.approx(0.554, abs=0.001)
