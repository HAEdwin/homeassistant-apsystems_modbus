"""The power limit as a number on the ECU device."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

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


@dataclass(slots=True)
class StoredLimit(ExtraStoredData):
    """The chosen limit, kept across a restart of Home Assistant."""

    desired: int
    entry_id: str

    def as_dict(self) -> dict[str, Any]:
        """Return the data to store."""
        return {"desired": self.desired, "entry_id": self.entry_id}

    @classmethod
    def from_dict(cls, stored: dict[str, Any]) -> Self | None:
        """Return the stored limit, or None when it is not usable."""
        desired = stored.get("desired")
        entry_id = stored.get("entry_id")
        if (
            type(desired) is not int
            or not 0 <= desired <= NO_LIMIT
            or not isinstance(entry_id, str)
        ):
            return None
        return cls(desired, entry_id)


class PowerLimitNumber(EcuEntity, NumberEntity, RestoreEntity):
    """Limit the output of all inverters, 100 % meaning no limit.

    The number shows what the ECU reports. Setting it writes to the ECU right
    away; the integration then keeps the ECU on that value. The chosen value
    is restored after a restart of Home Assistant, but only for the same
    config entry: removing and adding the integration starts without one.
    """

    async def async_added_to_hass(self) -> None:
        """Take over the limit chosen before Home Assistant restarted."""
        await super().async_added_to_hass()
        if (extra := await self.async_get_last_extra_data()) is None:
            return
        stored = StoredLimit.from_dict(extra.as_dict())
        if stored is None or stored.entry_id != self.coordinator.config_entry.entry_id:
            return
        self.coordinator.power_limit.restore(stored.desired)

    @property
    def extra_restore_state_data(self) -> StoredLimit | None:
        """Remember the chosen limit for the next start."""
        desired = self.coordinator.power_limit.desired
        if desired is None:
            return None
        return StoredLimit(desired, self.coordinator.config_entry.entry_id)

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
