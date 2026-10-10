"""Sensors: totals on the ECU device, measurements per inverter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import CONF_INVERTERS, CONF_MODEL
from .coordinator import ApsystemsConfigEntry, EcuCoordinator, EcuData
from .ecu import MAX_CHANNELS, Inverter, OperatingState, channel_count
from .entity import EcuEntity, InverterEntity

# Entities only read coordinator data; no per-entity updates to throttle.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EcuSensorDescription(SensorEntityDescription):
    """A total computed over all inverters."""

    value_fn: Callable[[EcuData], StateType | datetime]


@dataclass(frozen=True, kw_only=True)
class InverterSensorDescription(SensorEntityDescription):
    """A value read from one inverter."""

    value_fn: Callable[[Inverter], StateType]
    channel: int = 0
    """DC channel this sensor belongs to, 0 for none."""


ECU_SENSORS: tuple[EcuSensorDescription, ...] = (
    EcuSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda data: data.power,
    ),
    EcuSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        # The inverters reset one by one at night, so the sum steps down in
        # several moves; TOTAL (not TOTAL_INCREASING) keeps that out of the
        # statistics as a series of meter resets.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda data: _kwh(data.energy_today),
    ),
    EcuSensorDescription(
        key="inverters_online",
        translation_key="inverters_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.online,
    ),
    EcuSensorDescription(
        key="last_data_update",
        translation_key="last_data_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.last_change,
    ),
)


def _kwh(watt_hours: float | None) -> float | None:
    """Convert the ECU's Wh counters to kWh, the unit of the Energy dashboard."""
    return None if watt_hours is None else watt_hours / 1000


def _dc_reader(quantity: str, channel: int) -> Callable[[Inverter], float | None]:
    """Return a reader for one DC quantity of one channel."""

    def read(inverter: Inverter) -> float | None:
        value: float | None = getattr(inverter.measurements, quantity)(channel)
        return value

    return read


def _event_state(inverter: Inverter) -> str:
    events = inverter.status.events
    if not events:
        return "ok"
    return ", ".join(
        flag.name.lower() for flag in type(events) if flag in events and flag.name
    )


INVERTER_SENSORS: tuple[InverterSensorDescription, ...] = (
    InverterSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda inverter: inverter.measurements.ac_power,
    ),
    InverterSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda inverter: _kwh(inverter.measurements.energy_today),
    ),
    InverterSensorDescription(
        key="ac_voltage",
        translation_key="ac_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        value_fn=lambda inverter: inverter.measurements.ac_voltage,
    ),
    InverterSensorDescription(
        key="ac_current",
        translation_key="ac_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        value_fn=lambda inverter: inverter.measurements.ac_current,
    ),
    InverterSensorDescription(
        key="frequency",
        translation_key="frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
        value_fn=lambda inverter: inverter.measurements.frequency,
    ),
    InverterSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda inverter: inverter.measurements.temperature,
    ),
    InverterSensorDescription(
        key="apparent_power",
        translation_key="apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda inverter: inverter.measurements.apparent_power,
    ),
    InverterSensorDescription(
        key="reactive_power",
        translation_key="reactive_power",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda inverter: inverter.measurements.reactive_power,
    ),
    InverterSensorDescription(
        key="power_factor",
        translation_key="power_factor",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda inverter: inverter.measurements.power_factor,
    ),
    InverterSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[state.name.lower() for state in OperatingState],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda inverter: (
            state.name.lower()
            if (state := inverter.status.operating_state) is not None
            else None
        ),
    ),
    InverterSensorDescription(
        key="event_code",
        translation_key="event_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_event_state,
    ),
    *(
        InverterSensorDescription(
            key=f"dc_voltage_{channel}",
            translation_key="dc_voltage",
            translation_placeholders={"channel": str(channel)},
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=1,
            channel=channel,
            value_fn=_dc_reader("dc_voltage", channel),
        )
        for channel in range(1, MAX_CHANNELS + 1)
    ),
    *(
        InverterSensorDescription(
            key=f"dc_current_{channel}",
            translation_key="dc_current",
            translation_placeholders={"channel": str(channel)},
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=2,
            channel=channel,
            value_fn=_dc_reader("dc_current", channel),
        )
        for channel in range(1, MAX_CHANNELS + 1)
    ),
    *(
        InverterSensorDescription(
            key=f"dc_power_{channel}",
            translation_key="dc_power",
            translation_placeholders={"channel": str(channel)},
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
            suggested_display_precision=0,
            channel=channel,
            value_fn=_dc_reader("dc_power", channel),
        )
        for channel in range(1, MAX_CHANNELS + 1)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApsystemsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the ECU totals and one set of sensors per inverter."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        EcuSensor(coordinator, description) for description in ECU_SENSORS
    ]
    for item in entry.data[CONF_INVERTERS]:
        channels = channel_count(str(item[CONF_MODEL]))
        entities.extend(
            InverterSensor(coordinator, item, description, channels)
            for description in INVERTER_SENSORS
        )
    async_add_entities(entities)


FLOAT_DIGITS = 3
"""Float32 registers carry about 7 significant digits; more decimals are noise."""


def _rounded[T](value: T) -> T | float:
    """Round floats so the state holds no float32 noise."""
    if isinstance(value, float):
        return round(value, FLOAT_DIGITS)
    return value


class EcuSensor(EcuEntity, SensorEntity):
    """A total over all inverters."""

    entity_description: EcuSensorDescription

    @property
    def native_value(self) -> StateType | datetime:
        """Return the current total."""
        return _rounded(self.entity_description.value_fn(self.coordinator.data))


class InverterSensor(InverterEntity, SensorEntity):
    """A value of one inverter."""

    entity_description: InverterSensorDescription

    def __init__(
        self,
        coordinator: EcuCoordinator,
        item: dict[str, object],
        description: InverterSensorDescription,
        channels: int,
    ) -> None:
        """Enable DC channel sensors only for channels the model has."""
        super().__init__(coordinator, item, description)
        if description.channel > channels:
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> StateType:
        """Return the last read value."""
        return _rounded(self.entity_description.value_fn(self.inverter))
