"""Poll the ECU's inverters without overloading it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import math

from modbus_connection import ModbusConnectionError, ModbusError, ModbusTimeoutError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INVERTERS,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_INVERTER_POWER,
    MAX_TIMEOUTS_PER_CYCLE,
    MIN_POLL_GAP,
    ROUND_PERIOD,
    SCAN_INTERVAL_CHOICES,
    SETTLE_BASE,
    SETTLE_MAX,
    SETTLE_PER_INVERTER,
)
from .ecu import Inverter
from .lifetime import MAX_DAILY_WH, EnergyLedger
from .power_limit import PowerLimit, store_for

_LOGGER = logging.getLogger(__name__)

type ApsystemsConfigEntry = ConfigEntry[EcuCoordinator]


def new_address_issue_id(entry_id: str) -> str:
    """Return the repair issue id telling that an inverter got a new address."""
    return f"addresses_changed_{entry_id}"


def round_interval(seconds: float) -> int:
    """Return the poll interval as a whole number of ECU rounds, in seconds.

    Older versions stored any value from 60 to 900 s. Polling between two
    rounds brings nothing new, so those are rounded to 1, 2 or 3 rounds.
    """
    rounds = round(seconds / ROUND_PERIOD)
    count = len(SCAN_INTERVAL_CHOICES)
    return min(max(rounds, 1), count) * ROUND_PERIOD


def settle_time(inverters: int) -> int:
    """Seconds after the start of a round when every inverter is refreshed."""
    return min(SETTLE_BASE + SETTLE_PER_INVERTER * inverters, SETTLE_MAX)


def until_next_poll(now: datetime, interval: int, settle: int) -> timedelta:
    """Time from ``now`` to the next poll moment, just after an ECU round.

    The ECU starts its rounds on the clock, so the moments are counted from
    the Unix epoch. A moment closer than ``MIN_POLL_GAP`` is skipped.
    """
    wait = interval - (now.timestamp() - settle) % interval
    if wait < MIN_POLL_GAP:
        wait += interval
    return timedelta(seconds=wait)


@dataclass
class EcuData:
    """What the last poll cycle produced."""

    available: set[int] = field(default_factory=set)
    """Unit ids whose measurements were refreshed this cycle."""
    power: float = 0.0
    """Sum of the AC power of the available inverters, in W."""
    energy_today: float | None = None
    """Sum of the daily counters as the ECU reports them, in Wh."""
    online: int = 0
    """Inverters that are awake and on the grid, even at 0 W at dusk."""
    last_change: datetime | None = None
    """When the ECU last reported different values."""
    cut_short: bool = False
    """Whether the cycle stopped early because too many inverters were silent."""


class EcuCoordinator(DataUpdateCoordinator[EcuData]):
    """One poll cycle reads every inverter in turn, with a budget.

    The ECU is a small device that overloads easily. Each cycle reads the
    inverters one after another; after ``MAX_TIMEOUTS_PER_CYCLE`` unanswered
    inverters the rest of the cycle is skipped and the link is recycled. A
    pending write is let through between two inverters via ``write_lock``.

    The ECU fetches new values from the inverters every five minutes, on the
    clock. Every cycle plans the next one just after a coming round, so new
    values reach Home Assistant within about a minute and a half. While the
    ECU does not answer, that is every round, whatever the chosen interval,
    which never asks more of the ECU than normal polling does.
    """

    config_entry: ApsystemsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ApsystemsConfigEntry,
        inverters: dict[int, Inverter],
        lead: Inverter,
        ledger: EnergyLedger,
    ) -> None:
        """Set up polling for the given inverters, keyed by unit id.

        ``lead`` is the inverter whose layout was checked at setup; the
        ECU-wide values such as the grid connection are read from it.
        """
        self.interval = round_interval(
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        """Seconds between polls, a whole number of ECU rounds."""
        self.settle = settle_time(len(inverters))
        """Seconds into a round before polling."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=self.interval),
        )
        self.inverters = inverters
        self.ledger = ledger
        self.lead = lead
        self.write_lock = asyncio.Lock()
        self.ecu_device_id: str | None = None
        """Device registry id of the ECU, set once the device is registered."""
        self.serials: dict[int, str] = {
            item[CONF_UNIT_ID]: item[CONF_SERIAL_NUMBER]
            for item in entry.data[CONF_INVERTERS]
        }
        """Configured serial number per unit id."""
        self.daily: dict[int, float] = {}
        """Last valid daily counter per unit id in Wh, as the ECU reports it."""
        self.power_limit = PowerLimit(self, lead, store_for(hass, entry.entry_id))
        """The ECU-wide power limit, written and read back through ``lead``."""
        self._timeout_budget = min(MAX_TIMEOUTS_PER_CYCLE, len(inverters))
        self._degraded = False
        self._reported: set[int] = set()
        self._snapshot: dict[int, tuple[float | None, float | None]] = {}
        self._last_change: datetime | None = None
        self.daytime_polls = 0
        """Completed polls with at least one inverter online."""
        self.polls_without_news = 0
        """Daytime polls in which no inverter had new values."""
        self.late_inverters: dict[int, int] = {}
        """Per unit id: daytime polls in which this online inverter had old
        values while others had new ones, a sign of polling too early."""

    async def _async_update_data(self) -> EcuData:
        data = EcuData()
        timeouts = 0
        reconnected = False
        for unit_id, inverter in sorted(self.inverters.items()):
            if timeouts >= self._timeout_budget:
                break
            async with self.write_lock:
                try:
                    try:
                        report = await inverter.async_poll(Inverter.POLLED)
                    except ModbusConnectionError as err:
                        # A restart of the ECU leaves the open connection
                        # dead, so the first request after it fails even when
                        # the ECU answers again. One fresh connection usually
                        # sorts that out at once.
                        self.power_limit.note_silent_ecu()
                        if reconnected:
                            raise
                        reconnected = True
                        _LOGGER.debug("Connection lost (%s); reconnecting", err)
                        await self._async_drop_link()
                        report = await inverter.async_poll(Inverter.POLLED)
                except ModbusConnectionError as err:
                    await self._async_note_failure()
                    raise UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="connection_lost",
                    ) from err
                except ModbusTimeoutError:
                    timeouts += 1
                    continue
                except ModbusError as err:
                    _LOGGER.debug("Unit %s: poll failed: %s", unit_id, err)
                    continue
            if "measurements" not in report.updated:
                continue
            if not self._serial_matches(unit_id, inverter):
                continue
            data.available.add(unit_id)

        data.cut_short = timeouts >= self._timeout_budget
        if data.cut_short or not data.available:
            self.power_limit.note_silent_ecu()
            await self._async_note_failure()
        else:
            self._recover()
        if not data.available:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="no_inverter_answered",
            )
        self._aggregate(data)
        # Runs after the loop, so write_lock is free; see power_limit.py.
        await self.power_limit.async_after_poll(data)
        self._plan_next_poll()
        return data

    def _serial_matches(self, unit_id: int, inverter: Inverter) -> bool:
        """Tell whether the values on this address belong to its device.

        An unknown serial number is a replaced inverter: its values simply
        continue under the device of this address. The serial number of
        another configured address means the addresses were changed in EMA
        Manager. Those values are held back until the user reconfigures, so
        two inverters never mix.
        """
        expected = self.serials[unit_id]
        actual = inverter.status.serial_number
        # Unknown when the status read failed this cycle; nothing to compare.
        if actual is None or actual == expected:
            return True
        moved_from = next(
            (other for other, serial in self.serials.items() if serial == actual),
            None,
        )
        if unit_id not in self._reported:
            self._reported.add(unit_id)
            if moved_from is None:
                _LOGGER.warning(
                    "Unit %s: a new inverter answers, its values continue "
                    "under the existing device",
                    unit_id,
                )
            else:
                _LOGGER.warning(
                    "Unit %s: the inverter of unit %s answers here, "
                    "reconfigure the integration",
                    unit_id,
                    moved_from,
                )
                self._report_new_address(actual, moved_from, unit_id)
        return moved_from is None

    def _report_new_address(self, serial: str, old: int, new: int) -> None:
        """Ask the user once to reconfigure after an address change."""
        issue_id = new_address_issue_id(self.config_entry.entry_id)
        if ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id) is not None:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="addresses_changed",
            translation_placeholders={"id": serial, "old": str(old), "new": str(new)},
        )

    async def _async_note_failure(self) -> None:
        """Try again after every round until the ECU answers again."""
        if not self._degraded:
            self._degraded = True
            _LOGGER.warning(
                "The ECU did not answer. Trying again after every round of "
                "the ECU until it responds"
            )
        self._plan_next_poll()
        # A link that stopped answering is opened fresh next time.
        await self._async_drop_link()

    def _plan_next_poll(self) -> None:
        """Poll next just after a coming ECU round.

        The chosen interval while the ECU answers, every round while not.
        """
        interval = ROUND_PERIOD if self._degraded else self.interval
        self.update_interval = until_next_poll(dt_util.utcnow(), interval, self.settle)

    def _recover(self) -> None:
        """Return to the rhythm of the ECU once a cycle completes again.

        A cycle that was not cut short means the ECU answers, even if one
        inverter stays silent (for example a broken one). The completed
        cycle plans the next poll itself.
        """
        if self._degraded:
            self._degraded = False
            _LOGGER.info("The ECU is answering again; normal polling resumed")

    async def _async_drop_link(self) -> None:
        """Close the shared link; the next request opens a new one."""
        try:
            await self.lead.modbus_unit.disconnect()
        except ModbusError as err:
            _LOGGER.debug("Closing the link failed: %s", err)

    def _aggregate(self, data: EcuData) -> None:
        """Compute the ECU totals from the inverters read this cycle."""
        snapshot: dict[int, tuple[float | None, float | None]] = {}
        for unit_id in data.available:
            measurements = self.inverters[unit_id].measurements
            power = measurements.ac_power or 0.0
            # A bad reading would otherwise end up in the stored peak power.
            if 0 <= power <= MAX_INVERTER_POWER:
                data.power += power
            if measurements.is_online:
                data.online += 1
            daily = measurements.energy_today
            if daily is not None:
                # The ledger guards the lifetime total against stray readings.
                self.ledger.record_daily(self.serials[unit_id], daily)
                # Energy today simply follows the ECU, including its nightly
                # reset; only impossible values are left out.
                if math.isfinite(daily) and 0 <= daily <= MAX_DAILY_WH:
                    self.daily[unit_id] = daily
            snapshot[unit_id] = (daily, measurements.ac_power)

        # An inverter that did not answer counts with its last value.
        data.energy_today = sum(self.daily.values()) if self.daily else None

        self.ledger.record_power(data.power)
        self.ledger.finish_poll(complete=len(data.available) == len(self.inverters))
        self.ledger.schedule_save()

        fresh = {
            unit
            for unit, value in snapshot.items()
            if self._snapshot.get(unit) != value
        }
        changed = bool(fresh)
        self._count_news(data, snapshot, fresh)
        if changed or self._last_change is None:
            self._last_change = dt_util.utcnow()
        self._snapshot.update(snapshot)
        data.last_change = self._last_change

    def _count_news(
        self,
        data: EcuData,
        snapshot: dict[int, tuple[float | None, float | None]],
        fresh: set[int],
    ) -> None:
        """Count polls that came before the ECU refreshed, for diagnostics.

        Only daytime polls count, and only inverters seen in an earlier poll,
        so a start, the night and a silent inverter are left out. Daytime
        means an inverter on the grid: at night the ECU often keeps one
        inverter at 1 W, but always with 0 Hz.
        """
        known = {unit for unit in snapshot if unit in self._snapshot}
        on_grid = {unit for unit in known if self._on_grid(unit)}
        if not on_grid:
            return
        self.daytime_polls += 1
        if not fresh & known:
            self.polls_without_news += 1
            return
        for unit in on_grid - fresh:
            self.late_inverters[unit] = self.late_inverters.get(unit, 0) + 1

    def _on_grid(self, unit_id: int) -> bool:
        """Whether an inverter reports a grid frequency this cycle."""
        return bool(self.inverters[unit_id].measurements.frequency)
