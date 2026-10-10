"""Binary sensors: inverter online, grid connection reported by the ECU."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_INVERTERS
from .coordinator import ApsystemsConfigEntry
from .entity import EcuEntity, InverterEntity

# Entities only read coordinator data; no per-entity updates to throttle.
PARALLEL_UPDATES = 0

ONLINE = BinarySensorEntityDescription(
    key="online",
    translation_key="online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)

# Shown as "Inverters enabled": whether the ECU has the inverters switched on.
# It does not say whether they produce; at night it still reads on. No device
# class, so the state reads On/Off rather than Connected/Disconnected.
GRID_CONNECTION = BinarySensorEntityDescription(
    key="grid_connection",
    translation_key="grid_connection",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApsystemsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        GridConnectionSensor(coordinator, GRID_CONNECTION)
    ]
    entities.extend(
        InverterOnlineSensor(coordinator, item, ONLINE)
        for item in entry.data[CONF_INVERTERS]
    )
    async_add_entities(entities)


class InverterOnlineSensor(InverterEntity, BinarySensorEntity):
    """Whether the inverter is feeding in (frequency or power above zero)."""

    @property
    def is_on(self) -> bool:
        """Return the online state from the last poll."""
        return self.inverter.measurements.is_online


class GridConnectionSensor(EcuEntity, BinarySensorEntity):
    """The Conn flag of the controls block; read only, never written."""

    @property
    def available(self) -> bool:
        """Available while the inverter the flag is read from answered."""
        return (
            super().available
            and self.coordinator.lead.unit_id in self.coordinator.data.available
        )

    @property
    def is_on(self) -> bool | None:
        """Return the flag as read from the lead inverter."""
        return self.coordinator.lead.measurements.grid_connected
