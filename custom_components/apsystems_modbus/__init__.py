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

from .const import CONF_INVERTERS, CONF_UNIT_ID, DOMAIN
from .coordinator import ApsystemsConfigEntry, EcuCoordinator
from .ecu import Inverter, UnsupportedLayoutError
from .entity import ecu_device_info

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> bool:
    """Connect to the ECU and start polling in the background.

    Home Assistant only waits for one read here, so a slow or silent ECU does
    not delay startup. The first full poll runs as a background task.
    """
    params = ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    inverters: dict[int, Inverter] = {}
    try:
        for item in entry.data[CONF_INVERTERS]:
            unit_id: int = item[CONF_UNIT_ID]
            unit = async_get_unit(hass, entry, params, unit_id)
            inverters[unit_id] = Inverter(unit, unit_id)
    except HomeAssistantError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="link_in_use",
            translation_placeholders={"host": entry.data[CONF_HOST]},
        ) from err

    lead = inverters[min(inverters)]
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
    ecu_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **ecu_device_info(entry)
    )
    coordinator.ecu_device_id = ecu_device.id
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first poll"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> bool:
    """Stop polling; the shared Modbus connection is released by the modbus integration."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
