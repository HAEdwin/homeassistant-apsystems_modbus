"""Lifetime energy and peak power, which the ECU does not keep itself.

Over Modbus the ECU only reports a daily energy counter per inverter, reset
to zero once a night. This module turns those counters into a lifetime total:
per inverter it keeps a base of all finished days plus the last counter it
accepted, and stores both so the total survives restarts, reconfiguring and
removing and re-adding the integration.

Readings that do not fit a counter going up wait for the next poll:

* A lower reading is a new day only once the counter runs up again from the
  low value. If the next reading is back at the old level, it was a stray
  reading. A real reset is booked when production starts, without losing
  anything, also when Home Assistant was off during the nightly reset.
* A jump up of more than ``MAX_STEP_WH`` counts only when the next reading
  confirms it, so one bad high reading never sticks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
SAVE_DELAY = 30
"""Seconds to collect changes before writing; also written on unload and stop."""
MAX_DAILY_WH = 50_000
"""No microinverter produces 50 kWh in a day; a higher counter is a bad reading."""
MAX_STEP_WH = 2000
"""Largest rise between two polls taken without confirmation (2 kWh)."""
APPLY_START_AFTER_POLLS = 12
"""Apply a start value once every inverter was read, or after this many polls."""


class _StoredLedger(TypedDict, total=False):
    counters: dict[str, dict[str, float | None]]
    offset: float
    peak_power: float
    reported: float
    calibrate_to: float | None


@dataclass
class _Counter:
    """Energy of one inverter, in Wh."""

    base: float = 0.0
    """Energy of all finished days."""
    last: float | None = None
    """Last accepted daily counter."""
    pending: float | None = None
    """A reading that waits for the next poll: a drop or a large jump."""

    @property
    def total(self) -> float:
        return self.base + (self.last or 0.0)


def _store(hass: HomeAssistant, ecu_key: str) -> Store[_StoredLedger]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{ecu_key}")


async def async_stored_lifetime(hass: HomeAssistant, ecu_key: str) -> float | None:
    """Return the stored lifetime energy of an ECU in Wh, if any."""
    ledger = EnergyLedger(hass, ecu_key)
    await ledger.async_load()
    return ledger.lifetime if ledger.has_data else None


async def async_request_start(
    hass: HomeAssistant, ecu_key: str, lifetime_wh: float | None
) -> None:
    """Ask the next setup to start the lifetime total at a given value.

    ``None`` clears what was stored, so counting starts over. The value is
    applied after the first poll, so today's production is not counted twice.
    """
    ledger = EnergyLedger(hass, ecu_key)
    await ledger.async_load()
    ledger.request_calibration(lifetime_wh)
    await ledger.async_save()


class EnergyLedger:
    """Lifetime total and peak power of one ECU, persisted in ``.storage``."""

    def __init__(self, hass: HomeAssistant, ecu_key: str) -> None:
        """Prepare the ledger for the ECU identified by ``ecu_key``."""
        self._store = _store(hass, ecu_key)
        self._counters: dict[str, _Counter] = {}
        self._offset = 0.0
        self._reported = 0.0
        self._calibrate_to: float | None = None
        self._polls_waiting = 0
        self.peak_power = 0.0

    @property
    def has_data(self) -> bool:
        """Whether anything was counted or set yet."""
        return bool(self._counters) or self._reported > 0 or self.peak_power > 0

    @property
    def lifetime(self) -> float:
        """Lifetime energy in Wh; never lower than reported before."""
        return self._reported

    def today_of(self, serial: str) -> float | None:
        """Return the accepted daily counter of one inverter in Wh."""
        counter = self._counters.get(serial)
        return None if counter is None else counter.last

    def lifetime_of(self, serial: str) -> float | None:
        """Lifetime energy of one inverter in Wh, without the start offset."""
        counter = self._counters.get(serial)
        return None if counter is None or counter.last is None else counter.total

    async def async_load(self) -> None:
        """Read the stored state."""
        data = await self._store.async_load() or {}
        self._counters = {
            serial: _Counter(
                base=values.get("base") or 0.0,
                last=values.get("last"),
                pending=values.get("pending"),
            )
            for serial, values in data.get("counters", {}).items()
        }
        self._offset = data.get("offset", 0.0)
        self._reported = data.get("reported", 0.0)
        self.peak_power = data.get("peak_power", 0.0)
        self._calibrate_to = data.get("calibrate_to")

    async def async_save(self) -> None:
        """Write the state now."""
        await self._store.async_save(self._as_data())

    def schedule_save(self) -> None:
        """Write the state soon, bundling the changes of several polls."""
        self._store.async_delay_save(self._as_data, SAVE_DELAY)

    def record_daily(self, serial: str, watt_hours: float) -> None:
        """Take a daily counter read from one inverter."""
        if not math.isfinite(watt_hours) or not 0 <= watt_hours <= MAX_DAILY_WH:
            return
        counter = self._counters.setdefault(serial, _Counter())
        last, pending = counter.last, counter.pending
        counter.pending = None
        if last is None:
            counter.last = watt_hours
        elif pending is None:
            if last <= watt_hours <= last + MAX_STEP_WH:
                counter.last = watt_hours
            else:
                counter.pending = watt_hours
        elif pending > last:
            # A large jump: real if the counter kept going from there.
            if watt_hours >= pending:
                counter.last = watt_hours
            else:
                self.record_daily(serial, watt_hours)
        elif watt_hours >= last:
            # Back at the old level: the drop was a stray reading.
            self.record_daily(serial, watt_hours)
        elif watt_hours > pending:
            # Running up again from the low value: a new day has started.
            counter.base += last
            counter.last = watt_hours
        else:
            counter.pending = watt_hours

    def record_power(self, watts: float) -> None:
        """Keep the highest total power seen."""
        if math.isfinite(watts) and watts > self.peak_power:
            self.peak_power = watts

    def finish_poll(self, *, complete: bool) -> None:
        """Update the reported total after a poll and apply a requested start.

        The start value includes today's production of every inverter, so it
        waits for a poll in which all of them were read.
        """
        counted = sum(counter.total for counter in self._counters.values())
        if self._calibrate_to is not None and self._counters:
            self._polls_waiting += 1
            if complete or self._polls_waiting >= APPLY_START_AFTER_POLLS:
                self._offset = self._calibrate_to - counted
                self._reported = self._calibrate_to
                self._calibrate_to = None
                return
        self._reported = max(self._reported, self._offset + counted)

    def calibrate(self, lifetime_wh: float) -> None:
        """Continue counting from ``lifetime_wh`` from now on."""
        counted = sum(counter.total for counter in self._counters.values())
        self._offset = lifetime_wh - counted
        self._reported = lifetime_wh

    def set_peak_power(self, watts: float) -> None:
        """Set the peak power, for example to reset it."""
        self.peak_power = max(watts, 0.0)

    def request_calibration(self, lifetime_wh: float | None) -> None:
        """Start at ``lifetime_wh`` after the next poll; ``None`` starts over.

        Starting over clears the lifetime total; the peak power is kept.
        """
        if lifetime_wh is None:
            self._counters = {}
            self._offset = 0.0
            self._reported = 0.0
            self._calibrate_to = None
            return
        self._calibrate_to = lifetime_wh

    def _as_data(self) -> _StoredLedger:
        return {
            "counters": {
                serial: asdict(counter) for serial, counter in self._counters.items()
            },
            "offset": self._offset,
            "reported": self._reported,
            "peak_power": self.peak_power,
            "calibrate_to": self._calibrate_to,
        }
