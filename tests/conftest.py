"""Shared fixtures: a mock ECU seeded from a captured register dump."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
import json
from pathlib import Path
import struct
from typing import Any
from unittest.mock import patch

from modbus_connection.mock import MockModbusConnection, MockModbusUnit
import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apsystems_modbus.const import (
    CONF_INVERTERS,
    CONF_MODEL,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
    DOMAIN,
)
from custom_components.apsystems_modbus.ecu import GRID_CONNECTION_ADDRESS

pytest_plugins = "pytest_homeassistant_custom_component"

FIXTURES = Path(__file__).parent / "fixtures"


def load_capture(name: str) -> dict[str, dict[int, int]]:
    """Return the holding registers per unit id from a capture file."""
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return {
        unit_id: {int(address): value for address, value in unit["holding"].items()}
        for unit_id, unit in data["units"].items()
    }


def seed_unit(unit: MockModbusUnit, registers: dict[int, int]) -> None:
    """Fill a mock unit's holding registers."""
    for address, value in registers.items():
        unit.holding[address] = value


def set_float32(unit: MockModbusUnit, address: int, value: float) -> None:
    """Overwrite one float32 register pair of a mock unit."""
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    unit.holding[address] = high
    unit.holding[address + 1] = low


@pytest.fixture
def mock_ecu(mock_modbus_connection: MockModbusConnection) -> MockModbusConnection:
    """A mock ECU with inverters on units 1 and 2, as captured by day."""
    for unit_id, registers in load_capture("ecu_r_day").items():
        seed_unit(mock_modbus_connection.for_unit(int(unit_id)), registers)
    return mock_modbus_connection


INVERTERS = [
    {
        CONF_UNIT_ID: 1,
        CONF_SERIAL_NUMBER: "704000000001",
        CONF_MODEL: "DS3",
    },
    {
        CONF_UNIT_ID: 2,
        CONF_SERIAL_NUMBER: "703000000002",
        CONF_MODEL: "DS3",
    },
]


@pytest.fixture(autouse=True)
def enable_custom_integrations_fixture(enable_custom_integrations: None) -> None:
    """Let Home Assistant load integrations from custom_components."""


@pytest.fixture(autouse=True)
def mock_shared_connection(
    mock_modbus_connection: MockModbusConnection,
) -> Generator[None]:
    """Hand the integration mock units instead of a real Modbus connection."""

    def get_unit(hass: Any, entry: Any, params: Any, unit_id: int) -> MockModbusUnit:
        return mock_modbus_connection.for_unit(unit_id)

    @asynccontextmanager
    async def get_temporary_unit(
        hass: Any, params: Any, unit_id: int
    ) -> AsyncIterator[MockModbusUnit]:
        yield mock_modbus_connection.for_unit(unit_id)

    with (
        patch("custom_components.apsystems_modbus.async_get_unit", get_unit),
        patch(
            "custom_components.apsystems_modbus.async_get_temporary_unit",
            get_temporary_unit,
        ),
        patch(
            "custom_components.apsystems_modbus.config_flow.async_get_temporary_unit",
            get_temporary_unit,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def never_write_grid_connection(
    mock_modbus_connection: MockModbusConnection,
) -> Generator[None]:
    """Fail the test if anything even attempts to write the grid connection register."""
    attempts: list[tuple[int, int]] = []

    def guard(unit: MockModbusUnit) -> None:
        original = unit.write_register

        async def write_register(address: int, value: int) -> None:
            if address == GRID_CONNECTION_ADDRESS:
                attempts.append((unit.unit_id, address))
            await original(address, value)

        unit.write_register = write_register  # type: ignore[method-assign]

    for unit_id in range(1, 17):
        guard(mock_modbus_connection.for_unit(unit_id))
    yield
    assert attempts == [], f"register 40188 was written: {attempts}"


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry for the mock ECU with two inverters."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="APsystems ECU",
        unique_id="704000000001",
        data={"host": "192.0.2.10", "port": 502, CONF_INVERTERS: INVERTERS},
    )


def add_inverter(mock_ecu: MockModbusConnection, unit_id: int, serial: str) -> None:
    """Clone unit 2 of the capture onto another unit id with its own serial."""
    registers = load_capture("ecu_r_day")["2"]
    unit = mock_ecu.for_unit(unit_id)
    seed_unit(unit, registers)
    encoded = serial.encode().ljust(32, b"\x00")
    for index in range(16):
        unit.holding[40052 + index] = struct.unpack(
            ">H", encoded[2 * index : 2 * index + 2]
        )[0]
    unit.holding[40068] = unit_id
