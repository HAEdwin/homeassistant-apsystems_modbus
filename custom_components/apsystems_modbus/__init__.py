"""The APsystems Modbus integration."""

from __future__ import annotations

from modbus_connection import ModbusError, ModbusTcpParams

from homeassistant.components.modbus import async_get_unit
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr

from .const import CONF_INVERTERS, CONF_SERIAL_NUMBER, CONF_UNIT_ID, DOMAIN
from .coordinator import ApsystemsConfigEntry, EcuCoordinator
from .ecu import Inverter, UnsupportedLayoutError
from .entity import ecu_device_info, ecu_identifier

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> bool:
    """Connect to the ECU and start polling in the background.

    Home Assistant only waits for one read here, so a slow or silent ECU does
    not delay startup. The first full poll runs as a background task.
    """
    params = ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    inverters: dict[int, Inverter] = {}
    lead_unit_id = min(item[CONF_UNIT_ID] for item in entry.data[CONF_INVERTERS])
    try:
        for item in entry.data[CONF_INVERTERS]:
            unit_id: int = item[CONF_UNIT_ID]
            unit = async_get_unit(hass, entry, params, unit_id)
            inverters[unit_id] = Inverter(
                unit, unit_id, checks_layout=unit_id == lead_unit_id
            )
    except HomeAssistantError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="link_in_use",
            translation_placeholders={"host": entry.data[CONF_HOST]},
        ) from err

    lead = inverters[lead_unit_id]
    try:
        await lead.async_ensure_setup()
    except UnsupportedLayoutError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unsupported_layout",
            translation_placeholders={"details": str(err)},
        ) from err
    except ModbusError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": entry.data[CONF_HOST]},
        ) from err

    coordinator = EcuCoordinator(hass, entry, inverters)
    entry.runtime_data = coordinator
    # The inverter devices hang below the ECU device, so register that first.
    registry = dr.async_get(hass)
    ecu_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, **ecu_device_info(entry)
    )
    coordinator.ecu_device_id = ecu_device.id
    _remove_stale_devices(registry, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first poll"
    )
    return True


def _remove_stale_devices(
    registry: dr.DeviceRegistry, entry: ApsystemsConfigEntry
) -> None:
    """Drop inverters that are no longer configured, e.g. after a replacement."""
    current = {ecu_identifier(entry)} | {
        (DOMAIN, item[CONF_SERIAL_NUMBER]) for item in entry.data[CONF_INVERTERS]
    }
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & current:
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_unload_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> bool:
    """Stop polling; the shared Modbus connection is released by the modbus integration."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
