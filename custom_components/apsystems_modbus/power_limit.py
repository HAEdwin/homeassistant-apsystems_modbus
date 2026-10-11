"""The power limit of the ECU: one setting that applies to every inverter.

The ECU keeps a single limit for the whole installation. It is served through
each inverter's unit, but a write through any one unit applies to all. The
limit is a percentage with an on/off flag; a percentage without the flag does
nothing, and 100 % releases the inverters. This module presents both as one
number: 100 means no limit, anything lower means limited.

What makes this harder than it sounds is that the ECU is not a reliable
messenger. It does not always read back correctly: at dusk it occasionally
reports 30 % for one poll, and after a restart it may report its factory
values. It does not always reach every inverter either: an inverter can miss
a change, the ECU never sends it again, and a restart of the ECU clears the
limit on every inverter while it keeps reporting the old value. None of that
shows over Modbus. So:

* The value shown to the user is what the ECU reads back, but a change is
  only adopted once two polls in a row agree, so a one-poll glitch does not
  move the control.
* The value the user chose is remembered, also across a restart of Home
  Assistant (the number entity restores it).
* While a choice exists, the ECU is made to follow it: once two polls in a
  row read something else, the choice is written again, at most once per
  ten minutes. A failed user write is retried at the next usable poll.
* The choice is sent again ("refreshed") 15 and 75 minutes after it was set,
  once after Home Assistant starts, and, below 100, after the ECU was
  unreachable, which is how a restart of the ECU shows. The ECU only passes a
  changed percentage on to the inverters, so a refresh writes one percent
  less (or more, at 0), waits at least one ECU round and then writes the
  chosen value again. The in-between value is not shown.

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
"""Least time between two corrective writes after a deviating readback."""
REFRESH_DELAY = timedelta(minutes=15)
"""Time from a change to its first refresh."""
REFRESH_REPEAT = timedelta(hours=1)
"""Time between the refreshes that follow one change."""
REFRESHES_PER_CHANGE = 2
"""Refreshes after each change: an inverter may miss more than one round."""
STEP_HOLD = timedelta(minutes=6)
"""How long the in-between value stays, so the ECU passes it on at least once.

The ECU talks to the inverters in rounds of about five minutes.
"""


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


def _step_for(percent: int) -> int:
    """Return the in-between value for a refresh of ``percent``."""
    return percent - 1 if percent > 0 else 1


class PowerLimit:
    """Writes the limit, shows what the ECU reports and keeps the two aligned."""

    def __init__(self, coordinator: EcuCoordinator, control: Inverter) -> None:
        """Bind to the inverter whose unit carries the writes and the readback."""
        self._coordinator = coordinator
        self.control = control
        self.desired: int | None = None
        """The limit the user chose; None until a choice is made or restored."""
        self.pending = False
        """Whether a user write failed and still has to reach the ECU."""
        self.shown: int | None = None
        """The limit presented to the user."""
        self.deviations = 0
        """Polls in a row that read something other than ``shown``."""
        self.last_rewrite: datetime | None = None
        self.next_refresh: datetime | None = None
        """When the chosen limit is sent again; None when nothing is planned."""
        self.refreshes_left = 0
        self.refresh_count = 0
        """Refreshes started since the integration was loaded."""
        self.last_refresh: datetime | None = None
        """When the last refresh wrote its in-between value."""
        self._step_written: datetime | None = None
        self._ecu_was_silent = False
        self._lock = asyncio.Lock()

    @property
    def refreshing(self) -> bool:
        """Whether a refresh has written its in-between value."""
        return self._step_written is not None

    def restore(self, percent: int) -> None:
        """Take over the choice remembered from before Home Assistant started.

        The ECU may have restarted in the meantime, so the choice is sent
        again at the first usable poll.
        """
        if self.desired is not None:
            return
        self.desired = percent
        self._plan_refreshes(dt_util.utcnow(), delay=timedelta(0), count=1)

    def note_silent_ecu(self) -> None:
        """Record that a poll found the ECU unreachable.

        A restart of the ECU clears the limit on the inverters and breaks
        the open connection, so the first poll after it fails. The limit is
        then sent again once the ECU answers.
        """
        self._ecu_was_silent = True

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
                # A refresh under way would otherwise hold up the retry.
                self._step_written = None
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
            self._plan_refreshes(dt_util.utcnow())

    async def _async_write(self, percent: int, *, show: bool = True) -> None:
        """Write the percentage, then the flag; a half write never limits more.

        Below 100 the percentage goes first and the flag switches it on; at
        100 the percentage is reset and the flag switched off. Either way a
        failed second write leaves the inverters at full power. The second
        write gets one immediate retry. ``show`` makes the written value the
        shown one; the in-between value of a refresh is not shown.
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
        if show:
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
        now = dt_util.utcnow()

        if self._step_written is not None:
            # A refresh is under way and the ECU reports its in-between value.
            # Finishing only needs the ECU to answer, so it is not held up by
            # silent inverters.
            if now - self._step_written >= STEP_HOLD:
                await self._async_finish_refresh(now)
            return

        self._follow(reported)
        # Writing waits for a poll that was not cut short; one inverter that
        # stays silent (a broken one, say) does not hold it up.
        if not data.cut_short:
            await self._async_keep_aligned(data, reported, now)

    def _follow(self, reported: int) -> None:
        """Update the shown value from the readback of one poll."""
        if reported == self.desired:
            self._adopt(reported)
            self.pending = False
        elif reported == self.shown or self.shown is None:
            # Follow the ECU, but only once two polls in a row agree.
            self._adopt(reported)
        else:
            self.deviations += 1
            if self.deviations >= CONFIRM_POLLS:
                self.shown = reported

    async def _async_keep_aligned(
        self, data: EcuData, reported: int, now: datetime
    ) -> None:
        """Write whatever is needed to keep the ECU on the chosen limit."""
        if self._ecu_was_silent:
            self._ecu_was_silent = False
            # A restart only ever clears limits, so at 100 there is nothing
            # to put back. At most one such refresh an hour, in case a
            # network drops the connection between every poll.
            if (
                self.desired is not None
                and self.desired < NO_LIMIT
                and not self._refreshed_within(now, REFRESH_REPEAT)
            ):
                self._plan_refreshes(
                    now, delay=timedelta(0), count=max(self.refreshes_left, 1)
                )
        if self.desired is None:
            return

        # Retry a failed write, or write again once the ECU has stably
        # reported another value.
        if (
            reported != self.desired
            and (self.pending or self.shown == reported)
            and not self._rewrote_recently()
        ):
            await self._async_rewrite(reported)
            return

        if (
            self.next_refresh is not None
            and now >= self.next_refresh
            # In the morning the weakest inverters wake up last.
            and data.online == len(data.available)
        ):
            await self._async_start_refresh(now)

    def _adopt(self, percent: int) -> None:
        """Show ``percent`` and start counting deviations from it afresh."""
        self.shown = percent
        self.deviations = 0

    def _plan_refreshes(
        self,
        now: datetime,
        *,
        delay: timedelta = REFRESH_DELAY,
        count: int = REFRESHES_PER_CHANGE,
    ) -> None:
        """Plan ``count`` refreshes, the first one after ``delay``.

        Any refresh under way is dropped: the value just written takes over.
        """
        self._step_written = None
        self.next_refresh = now + delay
        self.refreshes_left = max(count, 1)

    def _refreshed_within(self, now: datetime, period: timedelta) -> bool:
        """Whether a refresh started within ``period`` before ``now``."""
        return self.last_refresh is not None and now - self.last_refresh < period

    def _rewrote_recently(self) -> bool:
        """Whether a corrective write happened within the last interval."""
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
            self._plan_refreshes(dt_util.utcnow())
        _LOGGER.info(
            "The ECU reported a power limit of %s %%; set it back to %s %%",
            reported,
            desired,
        )

    async def _async_start_refresh(self, now: datetime) -> None:
        """Write the in-between value, so the next write is a change again."""
        async with self._lock:
            # A change made while this poll waited for the lock planned its
            # own refreshes; check again.
            if (
                (desired := self.desired) is None
                or self._step_written is not None
                or self.next_refresh is None
                or now < self.next_refresh
            ):
                return
            step = _step_for(desired)
            try:
                await self._async_write(step, show=False)
            except ModbusError as err:
                # Nothing changed; the next usable poll tries again.
                _LOGGER.debug("Refreshing power limit %s %% failed: %s", desired, err)
                return
            self._step_written = dt_util.utcnow()
            self.refresh_count += 1
            self.last_refresh = now
        _LOGGER.debug("Refreshing power limit %s %% via %s %%", desired, step)

    async def _async_finish_refresh(self, now: datetime) -> None:
        """Write the chosen value back after the in-between value."""
        async with self._lock:
            if self._step_written is None or (desired := self.desired) is None:
                return
            try:
                await self._async_write(desired)
            except ModbusError as err:
                # The ECU stays on the in-between value until the next poll.
                _LOGGER.debug("Finishing power limit refresh failed: %s", err)
                return
            self._step_written = None
            if self.pending:
                # This write also delivered a choice that had failed earlier.
                self.pending = False
                self._plan_refreshes(now)
                return
            self.refreshes_left -= 1
            if self.refreshes_left > 0 and self.last_refresh is not None:
                self.next_refresh = self.last_refresh + REFRESH_REPEAT
            else:
                self.next_refresh = None
