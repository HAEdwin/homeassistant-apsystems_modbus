"""Poll the ECU's inverters without overloading it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

from modbus_connection import ModbusConnectionError, ModbusError, ModbusTimeoutError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INVERTERS,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_BACKOFF,
    MAX_TIMEOUTS_PER_CYCLE,
)
from .ecu import Inverter, UnsupportedLayoutError

_LOGGER = logging.getLogger(__name__)

type ApsystemsConfigEntry = ConfigEntry[EcuCoordinator]


@dataclass
class EcuData:
    """What the last poll cycle produced."""

    available: set[int] = field(default_factory=set)
    """Unit ids whose measurements were refreshed this cycle."""
    power: float = 0.0
    """Sum of the AC power of the available inverters, in W."""
    energy_today: float | None = None
    """Sum of the daily counters in Wh; a missing inverter counts as last read."""
    online: int = 0
    """Inverters that are feeding in."""
    last_change: datetime | None = None
    """When the ECU last reported different values."""


class EcuCoordinator(DataUpdateCoordinator[EcuData]):
    """One poll cycle reads every inverter in turn, with a budget.

    The ECU is a small device that overloads easily. Each cycle reads the
    inverters one after another; after ``MAX_TIMEOUTS_PER_CYCLE`` unanswered
    inverters the rest of the cycle is skipped, the link is recycled and the
    interval doubles until a cycle completes again. A pending write is let
    through between two inverters via ``write_lock``.
    """

    config_entry: ApsystemsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ApsystemsConfigEntry,
        inverters: dict[int, Inverter],
    ) -> None:
        """Set up polling for the given inverters, keyed by unit id."""
        self._base_interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=self._base_interval,
        )
        self.inverters = inverters
        self.lead = inverters[min(inverters)]
        self.write_lock = asyncio.Lock()
        self.ecu_device_id: str | None = None
        """Device registry id of the ECU, set once the device is registered."""
        self._expected_serials = {
            item[CONF_UNIT_ID]: item[CONF_SERIAL_NUMBER]
            for item in entry.data[CONF_INVERTERS]
        }
        self._degraded = False
        self._reported: set[int] = set()
        self._snapshot: dict[int, tuple[float | None, float | None]] = {}
        self._last_change: datetime | None = None
        self._last_energy: dict[int, float] = {}
        """Last daily counter per inverter, kept while an inverter is missing."""

    async def _async_update_data(self) -> EcuData:
        data = EcuData()
        timeouts = 0
        for unit_id, inverter in sorted(self.inverters.items()):
            if timeouts >= MAX_TIMEOUTS_PER_CYCLE:
                break
            async with self.write_lock:
                try:
                    report = await inverter.async_poll(Inverter.POLLED)
                except ModbusConnectionError as err:
                    raise UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="connection_lost",
                    ) from err
                except ModbusTimeoutError:
                    timeouts += 1
                    continue
                except UnsupportedLayoutError as err:
                    self._report_once(unit_id, "unsupported register layout: %s", err)
                    continue
                except ModbusError as err:
                    _LOGGER.debug("Unit %s: poll failed: %s", unit_id, err)
                    continue
            if "measurements" not in report.updated:
                continue
            if not self._serial_matches(unit_id, inverter):
                continue
            data.available.add(unit_id)

        await self._pace(timeouts)
        if not data.available:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="no_inverter_answered",
            )
        self._aggregate(data)
        return data

    def _serial_matches(self, unit_id: int, inverter: Inverter) -> bool:
        """Guard against a different inverter being moved to a known address."""
        expected = self._expected_serials.get(unit_id)
        actual = inverter.identity.serial_number
        if expected is None or actual == expected:
            return True
        self._report_once(
            unit_id,
            "serial number %s does not match the configured %s; "
            "reconfigure the integration",
            actual,
            expected,
        )
        return False

    def _report_once(self, unit_id: int, message: str, *args: object) -> None:
        """Log a problem with one inverter a single time, not every poll."""
        if unit_id in self._reported:
            return
        self._reported.add(unit_id)
        _LOGGER.warning("Unit %s: %s", unit_id, message % args)

    async def _pace(self, timeouts: int) -> None:
        """Back off while the ECU is struggling, recover when it answers again."""
        if timeouts >= MAX_TIMEOUTS_PER_CYCLE:
            await self.lead.modbus_unit.disconnect()
            current = self.update_interval or self._base_interval
            self.update_interval = min(current * 2, MAX_BACKOFF)
            if not self._degraded:
                self._degraded = True
                _LOGGER.warning(
                    "The ECU stopped answering; polling less often (every %s) "
                    "until it responds again",
                    self.update_interval,
                )
            return
        # A cycle that was not cut short means the ECU answers again, even if
        # one inverter stays silent (for example a broken one).
        if self._degraded:
            self._degraded = False
            self.update_interval = self._base_interval
            _LOGGER.info("The ECU is answering again; normal polling resumed")

    def _aggregate(self, data: EcuData) -> None:
        """Compute the ECU totals from the inverters read this cycle."""
        snapshot: dict[int, tuple[float | None, float | None]] = {}
        for unit_id in data.available:
            measurements = self.inverters[unit_id].measurements
            power = measurements.ac_power or 0.0
            data.power += power
            if measurements.is_online:
                data.online += 1
            if measurements.energy_today is not None:
                self._last_energy[unit_id] = measurements.energy_today
            snapshot[unit_id] = (measurements.energy_today, measurements.ac_power)

        # An inverter that did not answer counts with its last known counter, so
        # the total neither dips nor freezes while one inverter is missing.
        data.energy_today = (
            sum(self._last_energy.values()) if self._last_energy else None
        )

        changed = any(
            self._snapshot.get(unit) != value for unit, value in snapshot.items()
        )
        if changed or self._last_change is None:
            self._last_change = dt_util.utcnow()
        self._snapshot.update(snapshot)
        data.last_change = self._last_change
