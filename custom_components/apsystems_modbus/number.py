"""The power limit as numbers on the ECU device: in percent, and in kW."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import ApsystemsConfigEntry, EcuCoordinator
from .entity import EcuEntity
from .power_limit import NO_LIMIT

# Home Assistant does not serialise set_value calls here; PowerLimit does that
# itself and lets the latest value win, so a burst of changes does not turn
# into a burst of writes.
PARALLEL_UPDATES = 0

POWER_LIMIT = NumberEntityDescription(
    key="power_limit",
    translation_key="power_limit",
    native_min_value=0,
    native_max_value=NO_LIMIT,
    native_step=1,
    native_unit_of_measurement=PERCENTAGE,
    mode=NumberMode.SLIDER,
)
POWER_LIMIT_KW_KEY = "power_limit_kw"
KW_STEP = 0.1
ROUNDING_SLACK = 1e-9
"""Keeps 0.6 kW of 6.0 kW at 10 % instead of 11 % from floating point noise."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApsystemsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the power limit, and its kW twin when the installation power is known."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [PowerLimitNumber(coordinator, POWER_LIMIT)]
    total = coordinator.power_limit.installation_power
    if total is None:
        # The installation power was cleared: the kW number goes with it.
        registry = er.async_get(hass)
        unique_id = f"ecu-{entry.unique_id}-{POWER_LIMIT_KW_KEY}"
        if entity_id := registry.async_get_entity_id("number", DOMAIN, unique_id):
            registry.async_remove(entity_id)
    else:
        entities.append(
            PowerLimitKwNumber(
                coordinator,
                NumberEntityDescription(
                    key=POWER_LIMIT_KW_KEY,
                    translation_key=POWER_LIMIT_KW_KEY,
                    device_class=NumberDeviceClass.POWER,
                    native_min_value=0,
                    native_max_value=total,
                    native_step=KW_STEP,
                    native_unit_of_measurement=UnitOfPower.KILO_WATT,
                    mode=NumberMode.BOX,
                ),
            )
        )
    async_add_entities(entities)


class PowerLimitNumber(EcuEntity, NumberEntity):
    """Limit the output of all inverters, 100 % meaning no limit.

    The number shows what the ECU reports. Setting it writes to the ECU right
    away; the integration then keeps the ECU on that value.
    """

    @property
    def available(self) -> bool:
        """Stay available while the ECU is unreachable.

        Home Assistant silently skips actions on unavailable entities. A
        release must be attempted even when the last poll failed, so this
        entity only follows the config entry, not the coordinator.
        """
        return True

    @property
    def native_value(self) -> float | None:
        """Return the limit the ECU reports, None before the first read."""
        return self.coordinator.power_limit.shown

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the chosen limit and whether it still has to reach the ECU."""
        limit = self.coordinator.power_limit
        return {"desired": limit.desired, "pending": limit.pending}

    async def async_set_native_value(self, value: float) -> None:
        """Write the limit to the ECU and show it at once."""
        await self._async_set_percent(value)

    async def _async_set_percent(self, percent: float) -> None:
        """Set the limit and update both numbers, also when the write fails."""
        try:
            await self.coordinator.power_limit.async_set(percent)
        finally:
            # The other number shows the same setting; refresh both now.
            self.coordinator.async_update_listeners()


class PowerLimitKwNumber(PowerLimitNumber):
    """The same limit in kW, from the installation power the user entered.

    The ECU only knows whole percent of each inverter's own maximum. A value
    in kW is turned into the next whole percent up, so the limit is never
    stricter than asked, and shown as that percent of the installation power.
    """

    def __init__(
        self, coordinator: EcuCoordinator, description: NumberEntityDescription
    ) -> None:
        """Remember the installation power the numbers are based on."""
        super().__init__(coordinator, description)
        self._total = float(description.native_max_value or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Leave the attributes to the number in percent."""
        return None

    @property
    def native_value(self) -> float | None:
        """Return the reported limit as kW of the installation power."""
        percent = self.coordinator.power_limit.shown
        if percent is None:
            return None
        return round(percent / 100 * self._total, 1)

    async def async_set_native_value(self, value: float) -> None:
        """Turn kW into the next whole percent and set that.

        The value shown is rounded, so setting it again changes nothing.
        """
        if not math.isfinite(value):
            await self._async_set_percent(value)  # Refused with a clear message.
            return
        shown = self.coordinator.power_limit.shown
        if shown is not None and self.native_value == round(value, 1):
            await self._async_set_percent(shown)
            return
        percent = math.ceil(value / self._total * 100 - ROUNDING_SLACK)
        await self._async_set_percent(min(max(percent, 0), NO_LIMIT))
