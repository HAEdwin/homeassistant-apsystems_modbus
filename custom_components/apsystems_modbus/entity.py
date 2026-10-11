"""Base entities and device information for the ECU and its inverters."""

from __future__ import annotations

from homeassistant.const import CONF_MAC
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, InverterConfig
from .coordinator import ApsystemsConfigEntry, EcuCoordinator
from .ecu import MANUFACTURER, Inverter


def ecu_identifier(entry: ApsystemsConfigEntry) -> tuple[str, str]:
    """Return the device registry identifier of the ECU."""
    return (DOMAIN, f"ecu-{entry.unique_id}")


def ecu_device_info(entry: ApsystemsConfigEntry) -> DeviceInfo:
    """Describe the ECU; it does not report its own model or serial over Modbus."""
    info = DeviceInfo(
        identifiers={ecu_identifier(entry)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model="ECU",
    )
    if mac := entry.data.get(CONF_MAC):
        info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
    return info


def inverter_device_info(item: InverterConfig, ecu_device_id: str | None) -> DeviceInfo:
    """Describe one inverter from what the config flow read out of model 1."""
    serial = item["serial_number"]
    info = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"Inverter {serial}",
        manufacturer=MANUFACTURER,
        model=item["model"] or None,
        serial_number=serial,
    )
    if ecu_device_id:
        info["via_device_id"] = ecu_device_id
    return info


class EcuEntity(CoordinatorEntity[EcuCoordinator]):
    """An entity on the ECU device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EcuCoordinator, description: EntityDescription
    ) -> None:
        """Attach to the ECU device."""
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        # Prefixed, because without an ECU id the entry is identified by the
        # first inverter's serial number, which that inverter's entities use too.
        self._attr_unique_id = f"ecu-{entry.unique_id}-{description.key}"
        self._attr_device_info = ecu_device_info(entry)

    @property
    def available(self) -> bool:
        """Available once the first poll has produced data."""
        return super().available and self.coordinator.data is not None


class InverterEntity(CoordinatorEntity[EcuCoordinator]):
    """An entity on one inverter device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcuCoordinator,
        item: InverterConfig,
        description: EntityDescription,
    ) -> None:
        """Attach to the inverter configured as ``item``."""
        super().__init__(coordinator)
        self.entity_description = description
        self.unit_id = item["unit_id"]
        self.serial = item["serial_number"]
        self._attr_unique_id = f"{self.serial}-{description.key}"
        self._attr_device_info = inverter_device_info(item, coordinator.ecu_device_id)

    @property
    def inverter(self) -> Inverter:
        """The register model of this inverter."""
        return self.coordinator.inverters[self.unit_id]

    @property
    def available(self) -> bool:
        """Available while this inverter answered in the last cycle."""
        data = self.coordinator.data
        return super().available and data is not None and self.unit_id in data.available
