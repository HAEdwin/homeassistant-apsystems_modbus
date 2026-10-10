"""The power limit as a number on the ECU device."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ApsystemsConfigEntry
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApsystemsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the power limit number."""
    async_add_entities([PowerLimitNumber(entry.runtime_data, POWER_LIMIT)])


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
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the chosen limit and whether it still has to reach the ECU."""
        limit = self.coordinator.power_limit
        return {"desired": limit.desired, "pending": limit.pending}

    async def async_set_native_value(self, value: float) -> None:
        """Write the limit to the ECU and show it at once."""
        try:
            await self.coordinator.power_limit.async_set(value)
        finally:
            self.async_write_ha_state()
