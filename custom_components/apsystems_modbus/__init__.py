"""The APsystems Modbus integration."""

from __future__ import annotations

from modbus_connection import ModbusError, ModbusTcpParams, ModbusTimeoutError

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
from .lifetime import EnergyLedger

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
LAYOUT_CHECK_ATTEMPTS = 2


async def async_setup_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> bool:
    """Connect to the ECU and start polling in the background.

    Home Assistant only waits for the layout check here, so a slow or silent
    ECU does not delay startup. The first full poll runs as a background task.
    """
    params = ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    inverters: dict[int, Inverter] = {}
    try:
        for item in entry.data[CONF_INVERTERS]:
            unit_id = item[CONF_UNIT_ID]
            unit = async_get_unit(hass, entry, params, unit_id)
            inverters[unit_id] = Inverter(unit, unit_id, checks_layout=False)
    except HomeAssistantError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="link_in_use",
            translation_placeholders={"host": entry.data[CONF_HOST]},
        ) from err
    lead = await _async_check_layout(entry, inverters)

    # The stored totals belong to the ECU, not to this entry, so they survive
    # removing and re-adding the integration.
    ledger = EnergyLedger(hass, entry.unique_id or entry.entry_id)
    await ledger.async_load()
    coordinator = EcuCoordinator(hass, entry, inverters, lead, ledger)
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


async def _async_check_layout(
    entry: ApsystemsConfigEntry, inverters: dict[int, Inverter]
) -> Inverter:
    """Check the register layout on the first inverter that answers.

    All units of one ECU share the layout. A second inverter is tried when the
    first one is silent, so one broken inverter does not block the setup.
    """
    for inverter in sorted(inverters.values(), key=lambda inv: inv.unit_id)[
        :LAYOUT_CHECK_ATTEMPTS
    ]:
        inverter.checks_layout = True
        try:
            await inverter.async_ensure_setup()
        except ModbusTimeoutError:
            inverter.checks_layout = False
            continue
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
        return inverter
    raise ConfigEntryNotReady(
        translation_domain=DOMAIN,
        translation_key="cannot_connect",
        translation_placeholders={"host": entry.data[CONF_HOST]},
    )


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
    """Stop polling and store the totals; the modbus integration closes the link."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.ledger.async_save()
    return unloaded
