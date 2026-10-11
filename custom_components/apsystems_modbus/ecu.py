"""Register model of an APsystems ECU and its microinverters.

This module knows nothing about Home Assistant. It only needs a ``ModbusUnit``
from modbus-connection, so it can be tested against the in-memory mock.

The ECU exposes every microinverter as its own Modbus unit (the "address" set
in EMA Manager). Each unit serves SunSpec models 1, 101 and 111, a controls
block in the model 123 layout and a vendor block with per-channel DC values.
Addresses below were captured from an ECU-R (firmware 1.3.23) with DS3
inverters and are fixed; the ECU does not need to be scanned.

Quirks handled here rather than in Home Assistant:

* The controls block announces itself with model id 114 and length 48, but its
  content follows model 123 (export limit at 40189, enable flag at 40193).
* The enable flag reads back as ASCII ``49``/``48`` after it has been written
  and as a plain ``1``/``0`` from the factory.
* Model 111 carries the same quantities as 101 as floats, with more precision
  for the daily energy counter, but leaves the operating state and events
  unimplemented. Measurements therefore come from 111, status from 101.
* The DC block lists power per channel as well, but it equals voltage times
  current exactly, so it is computed instead of read.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag

from modbus_connection import ModbusUnit
from modbus_connection.model import (
    Component,
    Device,
    float32,
    gauge,
    integer,
    raw_register,
    string,
)
from modbus_connection.model.sunspec import bitfield32, enum16

SUNSPEC_MARKER = "SunS"
MANUFACTURER = "APsystems"
COMMON_MODEL = 1
INVERTER_INT_MODEL = 101
INVERTER_FLOAT_MODEL = 111
CONTROLS_MODELS = (114, 123)

REQUEST_TIMEOUT = 15.0
"""Seconds to wait for one reply; the ECU usually answers within 4 s."""
MESSAGE_SPACING = 0.3
"""Quiet time around every request, so a busy ECU is not flooded."""

MAX_CHANNELS = 4
FOUR_CHANNEL_MODELS = ("QT2", "QS1", "DS3D")
DEFAULT_CHANNELS = 2

ENABLED_VALUES = frozenset({1, ord("1")})
DISABLED_VALUES = frozenset({0, ord("0")})
MAX_EXPORT_LIMIT = 100

CONTROLS_HEADER_ADDRESS = 40184
EXPORT_LIMIT_ADDRESS = 40189
EXPORT_LIMIT_ENABLED_ADDRESS = 40193
GRID_CONNECTION_ADDRESS = 40188
"""Read only. Writing 0 here disconnects every inverter from the grid."""


class OperatingState(IntEnum):
    """SunSpec inverter operating state (model 101, St)."""

    OFF = 1
    SLEEPING = 2
    STARTING = 3
    MPPT = 4
    THROTTLED = 5
    SHUTTING_DOWN = 6
    FAULT = 7
    STANDBY = 8


class InverterEvent(IntFlag):
    """SunSpec inverter event flags (model 101, Evt1)."""

    GROUND_FAULT = 1 << 0
    DC_OVER_VOLTAGE = 1 << 1
    AC_DISCONNECT = 1 << 2
    DC_DISCONNECT = 1 << 3
    GRID_DISCONNECT = 1 << 4
    CABINET_OPEN = 1 << 5
    MANUAL_SHUTDOWN = 1 << 6
    OVER_TEMPERATURE = 1 << 7
    OVER_FREQUENCY = 1 << 8
    UNDER_FREQUENCY = 1 << 9
    AC_OVER_VOLTAGE = 1 << 10
    AC_UNDER_VOLTAGE = 1 << 11
    BLOWN_STRING_FUSE = 1 << 12
    UNDER_TEMPERATURE = 1 << 13
    MEMORY_LOSS = 1 << 14
    HW_TEST_FAILURE = 1 << 15


class UnsupportedLayoutError(Exception):
    """The unit does not serve the register layout this integration expects."""


def _validate_export_limit(value: float) -> float:
    """Accept an export limit percentage between 0 and 100."""
    if not 0 <= value <= MAX_EXPORT_LIMIT:
        raise ValueError(f"export limit must be 0-{MAX_EXPORT_LIMIT} %, got {value}")
    return value


def channel_count(model: str | None) -> int:
    """Return how many DC inputs (panels) an inverter model has."""
    if model and model.upper().startswith(FOUR_CHANNEL_MODELS):
        return MAX_CHANNELS
    return DEFAULT_CHANNELS


class Identity(Component):
    """SunSpec model 1 plus the model headers that fix the register layout."""

    # One read: model 1 with the model 101 and 111 headers. The controls
    # header is the same on every unit and checked once by ``check_controls``.
    register_ranges = ((40000, 40123),)

    marker = string(40000, 2)
    common_model = integer(40002, signed=False)
    manufacturer = string(40004, 16)
    model = string(40020, 16)
    serial_number = string(40052, 16)
    inverter_int_model = integer(40070, signed=False)
    inverter_float_model = integer(40122, signed=False)

    def check_layout(self) -> None:
        """Raise ``UnsupportedLayoutError`` unless the layout is the known one."""
        problems = []
        if self.marker != SUNSPEC_MARKER:
            problems.append(f"no SunSpec marker at 40000 ({self.marker!r})")
        if self.manufacturer != MANUFACTURER:
            problems.append(f"manufacturer is {self.manufacturer!r}")
        if self.common_model != COMMON_MODEL:
            problems.append(f"model {self.common_model} at 40002")
        if self.inverter_int_model != INVERTER_INT_MODEL:
            problems.append(f"model {self.inverter_int_model} at 40070")
        if self.inverter_float_model != INVERTER_FLOAT_MODEL:
            problems.append(f"model {self.inverter_float_model} at 40122")
        if problems:
            raise UnsupportedLayoutError("; ".join(problems))


async def async_check_controls(unit: ModbusUnit) -> None:
    """Raise ``UnsupportedLayoutError`` unless the controls block is the known one."""
    (model,) = await unit.read_holding_registers(CONTROLS_HEADER_ADDRESS, 1)
    if model not in CONTROLS_MODELS:
        raise UnsupportedLayoutError(f"model {model} at {CONTROLS_HEADER_ADDRESS}")


class Status(Component):
    """Serial number, operating state and event flags, in one read.

    The serial number rides along every poll at no extra request, so a
    different inverter on a known address is noticed right away.
    """

    register_ranges = ((40052, 40111),)

    serial_number = string(40052, 16)
    operating_state = enum16(40108, OperatingState)
    events = bitfield32(40110, InverterEvent)


class Measurements(Component):
    """Everything polled every cycle, in one 114-register read.

    Covers model 111 (floats), the controls block and DC channels 1-4.
    """

    register_ranges = ((40124, 40237),)

    ac_current = float32(40124, unit="A")
    ac_voltage = float32(40138, unit="V")
    ac_power = float32(40144, unit="W")
    frequency = float32(40146, unit="Hz")
    apparent_power = float32(40148, unit="VA")
    reactive_power = float32(40150, unit="var")
    power_factor = float32(40152)
    energy_today = float32(40154, unit="Wh")
    temperature = float32(40162, unit="°C")

    grid_connection = integer(GRID_CONNECTION_ADDRESS, signed=False, nan=0xFFFF)
    # The ECU ignores "write single register" (function code 6) and only
    # accepts "write multiple registers" (16), even for one register.
    export_limit = gauge(
        EXPORT_LIMIT_ADDRESS,
        0.1,
        signed=False,
        writable=_validate_export_limit,
        force_fc16=True,
    )
    export_limit_enabled_raw = raw_register(
        EXPORT_LIMIT_ENABLED_ADDRESS, writable=True, force_fc16=True
    )

    dc_voltage_1 = float32(40214, unit="V")
    dc_voltage_2 = float32(40216, unit="V")
    dc_voltage_3 = float32(40218, unit="V")
    dc_voltage_4 = float32(40220, unit="V")
    dc_current_1 = float32(40230, unit="A")
    dc_current_2 = float32(40232, unit="A")
    dc_current_3 = float32(40234, unit="A")
    dc_current_4 = float32(40236, unit="A")

    @property
    def is_online(self) -> bool:
        """Whether the inverter is awake and on the grid.

        At dusk an inverter still measures the grid while it delivers 0 W. It
        counts as online until it switches off for the night.

        The ECU occasionally reports a stray 0 for frequency or power while an
        inverter is producing, so either one being positive counts as online.
        """
        return bool(self.frequency) or bool(self.ac_power)

    @property
    def export_limit_enabled(self) -> bool | None:
        """Decode the enable flag; ``None`` for a value the ECU never uses."""
        raw = self.export_limit_enabled_raw
        if raw in ENABLED_VALUES:
            return True
        if raw in DISABLED_VALUES:
            return False
        return None

    @property
    def grid_connected(self) -> bool | None:
        """Whether the ECU reports the inverters as connected to the grid."""
        if self.grid_connection is None:
            return None
        return self.grid_connection == 1

    def dc_voltage(self, channel: int) -> float | None:
        """DC voltage of a channel (1-based)."""
        return self._channel_value("dc_voltage", channel)

    def dc_current(self, channel: int) -> float | None:
        """DC current of a channel (1-based)."""
        return self._channel_value("dc_current", channel)

    def dc_power(self, channel: int) -> float | None:
        """DC power of a channel, as voltage times current."""
        voltage = self.dc_voltage(channel)
        current = self.dc_current(channel)
        if voltage is None or current is None:
            return None
        return voltage * current

    def _channel_value(self, name: str, channel: int) -> float | None:
        if not 1 <= channel <= MAX_CHANNELS:
            raise ValueError(f"channel must be 1-{MAX_CHANNELS}, got {channel}")
        value: float | None = getattr(self, f"{name}_{channel}")
        return value


class Inverter(Device):
    """One microinverter, addressed as one Modbus unit of the ECU."""

    POLLED = ("measurements", "status")

    def __init__(
        self, unit: ModbusUnit, unit_id: int, *, checks_layout: bool = True
    ) -> None:
        """Bind the register components to a unit of the ECU.

        All units of one ECU share the same layout, so only one of them needs
        ``checks_layout``; the others skip the identity read.
        """
        super().__init__(unit)
        self.unit_id = unit_id
        self.checks_layout = checks_layout
        unit.require_timeout(REQUEST_TIMEOUT)
        unit.set_message_spacing(MESSAGE_SPACING)
        self.identity = Identity(unit)
        self.measurements = Measurements(unit)
        self.status = Status(unit)

    async def _async_setup(self) -> None:
        """Read the identity once and make sure the layout is the known one."""
        if not self.checks_layout:
            return
        await self.identity.async_update()
        self.identity.check_layout()
        await async_check_controls(self.modbus_unit)

    async def async_set_export_limit(self, percent: float) -> None:
        """Write the export limit percentage; the ECU applies it to all inverters."""
        await self.measurements.write("export_limit", percent)

    async def async_set_export_limit_enabled(self, enabled: bool) -> None:
        """Switch the export limit on or off for all inverters."""
        await self.measurements.write("export_limit_enabled_raw", int(enabled))
