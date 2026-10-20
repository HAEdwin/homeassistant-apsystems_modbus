"""Inverter output as a switch on the ECU device."""

from __future__ import annotations

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ApsystemsConfigEntry
from .entity import EcuEntity

# InverterOutput serialises the writes itself and lets the latest choice win.
PARALLEL_UPDATES = 0

INVERTER_OUTPUT = SwitchEntityDescription(
    key="inverter_output",
    translation_key="inverter_output",
    device_class=SwitchDeviceClass.SWITCH,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApsystemsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the inverter output switch."""
    async_add_entities([InverterOutputSwitch(entry.runtime_data, INVERTER_OUTPUT)])


class InverterOutputSwitch(EcuEntity, SwitchEntity):
    """Off stops all inverters from producing; on lets them produce again.

    The switch shows the choice. The ECU cannot report whether the inverters
    are off, so the result shows in the power.
    """

    @property
    def available(self) -> bool:
        """Stay available while the ECU is unreachable.

        Home Assistant silently skips actions on unavailable entities, and
        turning the output on must always be possible.
        """
        return True

    @property
    def is_on(self) -> bool:
        """Return the chosen state."""
        return self.coordinator.inverter_output.enabled

    async def async_turn_on(self, **kwargs: object) -> None:
        """Let the inverters produce again."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Stop all inverters from producing."""
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        """Apply the choice and show it, also when the write fails."""
        try:
            await self.coordinator.inverter_output.async_set(enabled)
        finally:
            self.async_write_ha_state()
