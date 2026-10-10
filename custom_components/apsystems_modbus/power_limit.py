"""The power limit of the ECU: one setting that applies to every inverter.

The ECU keeps a single limit for the whole installation. It is served through
each inverter's unit, but a write through any one unit applies to all. The
limit is a percentage with an on/off flag; a percentage without the flag does
nothing, and 100 % releases the inverters. This module presents both as one
number: 100 means no limit, anything lower means limited.

What makes this harder than it sounds is that the ECU does not read back
reliably. At dusk it occasionally reports 30 % for one poll, after a restart
it may report its factory values for a while, and a write right after a
restart can be lost. The inverters themselves keep the last limit they
actually received. So:

* The value shown to the user is what the ECU reads back, but a change is
  only adopted once two polls in a row agree, so a one-poll glitch does not
  move the control.
* The value the user chose is remembered in memory only. After a restart or
  reload nothing is remembered and nothing is written on its own; the ECU
  keeps the setting, and a stale choice can never be reapplied after a long
  outage.
* While a choice exists, the ECU is made to follow it: once two polls in a
  row read something else, the choice is written again, at most once per
  ten minutes. A failed user write is retried at the next clean poll.

Lock order: ``PowerLimit._lock`` first, then the coordinator's ``write_lock``
for each register write. The coordinator holds ``write_lock`` only while it
reads one inverter and releases it in between, and ``async_after_poll`` runs
after that loop without ``write_lock``. Calling it from inside the loop would
deadlock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING

from modbus_connection import ModbusError

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .ecu import MAX_EXPORT_LIMIT, Inverter, Measurements

if TYPE_CHECKING:
    from .coordinator import EcuCoordinator, EcuData

_LOGGER = logging.getLogger(__name__)

NO_LIMIT = MAX_EXPORT_LIMIT
"""The percentage that means "not limited"."""
CONFIRM_POLLS = 2
"""Polls in a row that must agree before a changed readback is adopted."""
REWRITE_INTERVAL = timedelta(minutes=10)
"""Least time between two writes the integration makes on its own."""


def effective_limit(measurements: Measurements) -> int | None:
    """Return the limit the ECU reports as one number, or None if unreadable.

    Enabled gives the percentage, disabled gives 100 whatever the percentage
    register holds, because a percentage without the flag does nothing.
    """
    enabled = measurements.export_limit_enabled
    percent = measurements.export_limit
    if enabled is None or percent is None or not 0 <= percent <= MAX_EXPORT_LIMIT:
        return None
    if not enabled:
        return NO_LIMIT
    return round(percent)


class PowerLimit:
    """Writes the limit, shows what the ECU reports and keeps the two aligned."""

    def __init__(self, coordinator: EcuCoordinator, control: Inverter) -> None:
        """Bind to the inverter whose unit carries the writes and the readback."""
        self._coordinator = coordinator
        self.control = control
        self.desired: int | None = None
        """The limit the user chose; None after a restart or reload."""
        self.pending = False
        """Whether a user write failed and still has to reach the ECU."""
        self.shown: int | None = None
        """The limit presented to the user."""
        self.deviations = 0
        """Polls in a row that read something other than ``shown``."""
        self.last_rewrite: datetime | None = None
        self._lock = asyncio.Lock()

    async def async_set(self, value: float) -> None:
        """Apply a limit chosen by the user.

        Remembers the choice first, then writes it. When several calls arrive
        at once, only the latest value is written. A failure raises a
        translated error and leaves the choice pending for the next poll.
        """
        if not math.isfinite(value):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="power_limit_invalid",
                translation_placeholders={"value": str(value)},
            )
        percent = min(max(round(value), 0), MAX_EXPORT_LIMIT)
        if percent == self.desired and not self.pending:
            return
        self.desired = percent
        async with self._lock:
            if self.desired != percent:
                return  # A later call took over.
            try:
                await self._async_write(percent)
            except ModbusError as err:
                self.pending = True
                _LOGGER.warning(
                    "Power limit %s %% did not reach the ECU (%s); "
                    "it is written again at the next poll",
                    percent,
                    err,
                )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="power_limit_pending",
                    translation_placeholders={"percent": str(percent)},
                ) from err
            self.pending = False

    async def _async_write(self, percent: int) -> None:
        """Write the percentage, then the flag; a half write never limits more.

        Below 100 the percentage goes first and the flag switches it on; at
        100 the percentage is reset and the flag switched off. Either way a
        failed second write leaves the inverters at full power. The second
        write gets one immediate retry.
        """
        async with self._coordinator.write_lock:
            await self.control.async_set_export_limit(percent)
        enabled = percent < NO_LIMIT
        try:
            async with self._coordinator.write_lock:
                await self.control.async_set_export_limit_enabled(enabled)
        except ModbusError:
            async with self._coordinator.write_lock:
                await self.control.async_set_export_limit_enabled(enabled)
        self._adopt(percent)

    async def async_after_poll(self, data: EcuData) -> None:
        """Update the shown value and make the ECU follow the chosen one.

        Called by the coordinator after every successful poll, outside its
        ``write_lock``.
        """
        if self.control.unit_id not in data.available:
            return
        reported = effective_limit(self.control.measurements)
        if reported is None:
            _LOGGER.debug(
                "Unit %s reports an unreadable limit (%s, %s)",
                self.control.unit_id,
                self.control.measurements.export_limit,
                self.control.measurements.export_limit_enabled_raw,
            )
            return

        if reported == self.desired:
            # The ECU reports what the user chose: nothing left to do.
            self._adopt(reported)
            self.pending = False
            return

        # Follow the ECU, but only once two polls in a row agree.
        if reported == self.shown or self.shown is None:
            self._adopt(reported)
        else:
            self.deviations += 1
            if self.deviations >= CONFIRM_POLLS:
                self.shown = reported

        # Make the ECU follow the user: retry a failed write at the first clean
        # poll, or write again once the ECU has stably reported another value.
        if self.desired is None or data.timeouts or self._rewrote_recently():
            return
        if self.pending or self.shown == reported:
            await self._async_rewrite(reported)

    def _adopt(self, percent: int) -> None:
        """Show ``percent`` and start counting deviations from it afresh."""
        self.shown = percent
        self.deviations = 0

    def _rewrote_recently(self) -> bool:
        """Whether the integration wrote on its own within the last interval."""
        return (
            self.last_rewrite is not None
            and dt_util.utcnow() - self.last_rewrite < REWRITE_INTERVAL
        )

    async def _async_rewrite(self, reported: int) -> None:
        """Write the chosen limit again because the ECU reports another one."""
        self.last_rewrite = dt_util.utcnow()
        async with self._lock:
            if (desired := self.desired) is None:
                return
            try:
                await self._async_write(desired)
            except ModbusError as err:
                _LOGGER.debug("Rewriting power limit %s %% failed: %s", desired, err)
                return
        self.pending = False
        _LOGGER.info(
            "The ECU reported a power limit of %s %%; set it back to %s %%",
            reported,
            desired,
        )
