"""Tests for the config, discovery, reconfigure and options flows."""

from __future__ import annotations

from modbus_connection import ModbusConnectionError, ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest

from homeassistant.config_entries import SOURCE_DHCP, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apsystems_modbus.config_flow import parse_unit_ids
from custom_components.apsystems_modbus.const import (
    CONF_ECU_ID,
    CONF_INVERTERS,
    CONF_UNIT_IDS,
    DOMAIN,
)

from .conftest import INVERTERS

HOST = "192.0.2.10"
USER_INPUT = {CONF_HOST: HOST, CONF_PORT: 502, CONF_UNIT_IDS: ""}


async def start_user_flow(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def submit_and_wait(hass: HomeAssistant, flow_id: str, user_input: dict) -> dict:
    """Submit a form that starts the search and return the step after it."""
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    return result


async def test_user_flow_with_search(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"
    assert "704000000001" in result["description_placeholders"]["inverters"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ECU_ID: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "APsystems ECU"
    assert result["result"].unique_id == "704000000001"
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_PORT: 502,
        CONF_INVERTERS: INVERTERS,
    }


async def test_user_flow_with_addresses_and_ecu_id(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "2"}
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "1"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ECU_ID: "216000000000"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ECU 216000000000"
    assert result["result"].unique_id == "216000000000"
    assert result["data"][CONF_ECU_ID] == "216000000000"
    assert [item["unit_id"] for item in result["data"][CONF_INVERTERS]] == [2]


async def test_invalid_addresses(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1-300"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_UNIT_IDS: "invalid_unit_ids"}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ModbusConnectionError("Connection refused"), "modbus_disabled"),
        (ModbusConnectionError("No route to host"), "cannot_connect"),
    ],
)
async def test_connection_errors(
    hass: HomeAssistant, mock_ecu: MockModbusConnection, error: Exception, expected: str
) -> None:
    mock_ecu.for_unit(1).fail_requests(error)
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}


async def test_silent_ecu_with_addresses(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1"}
    )
    # The ECU answered the connection but nothing lives on that address.
    assert result["errors"] == {"base": "no_inverters"}


async def test_no_inverters_found(
    hass: HomeAssistant, mock_modbus_connection: MockModbusConnection
) -> None:
    # The ECU is reachable, but no address answers.
    for unit_id in (1, 2):
        mock_modbus_connection.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "no_inverters"}


async def test_ecu_busy_during_search(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(2).fail_read(40004, ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "ecu_busy"}


async def test_unsupported_layout(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).holding[40070] = 103
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "unsupported_layout"}


async def test_not_apsystems(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).holding[40000] = 0x1234
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "not_apsystems"}


async def test_already_configured(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


DISCOVERY = DhcpServiceInfo(ip=HOST, hostname="", macaddress="80971b000001")


async def test_dhcp_discovery(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dhcp_confirm"

    result = await submit_and_wait(hass, result["flow_id"], {})
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_MAC] == "80:97:1b:00:00:01"
    assert result["result"].unique_id == "704000000001"


async def test_dhcp_updates_host_of_known_ecu(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="704000000001",
        data={
            CONF_HOST: "192.0.2.99",
            CONF_PORT: 502,
            CONF_MAC: "80:97:1b:00:00:01",
            CONF_INVERTERS: INVERTERS,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST


@pytest.mark.parametrize(
    ("silent", "reason"), [(True, "cannot_connect"), (False, "not_apsystems")]
)
async def test_dhcp_ignores_devices_that_are_not_an_ecu(
    hass: HomeAssistant,
    mock_modbus_connection: MockModbusConnection,
    silent: bool,
    reason: str,
) -> None:
    if silent:
        mock_modbus_connection.for_unit(1).fail_requests(ModbusTimeoutError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_reconfigure(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await submit_and_wait(
        hass,
        result["flow_id"],
        {CONF_HOST: "192.0.2.11", CONF_PORT: 502, CONF_UNIT_IDS: "1"},
    )
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == "192.0.2.11"
    assert [item["unit_id"] for item in mock_config_entry.data[CONF_INVERTERS]] == [1]


async def test_options_flow(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 600}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_SCAN_INTERVAL: 600}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1-8", list(range(1, 9))),
        ("1, 2, 5", [1, 2, 5]),
        ("3", [3]),
        ("2-3,1", [1, 2, 3]),
    ],
)
def test_parse_unit_ids(text: str, expected: list[int]) -> None:
    assert parse_unit_ids(text) == expected


@pytest.mark.parametrize("text", ["0", "248", "8-1", "a", "", "1-"])
def test_parse_unit_ids_rejects(text: str) -> None:
    with pytest.raises(ValueError, match=r".*"):
        parse_unit_ids(text)


def timeout_once(value: int):
    """Return a register source that times out on the first read only."""
    calls = 0

    def read() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModbusTimeoutError
        return value

    return read


async def test_slow_inverter_during_search_is_retried(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    # The mock evaluates every register source on each read of a unit, so one
    # slow register makes the next read of unit 2 time out once.
    unit = mock_ecu.for_unit(2)
    unit.holding[40000] = timeout_once(unit.holding[40000])
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"


async def test_slow_identity_read_is_retried(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    unit = mock_ecu.for_unit(2)
    unit.holding[40004] = timeout_once(unit.holding[40004])
    result = await start_user_flow(hass)

    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1-2"}
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"


async def test_identity_reads_use_the_polling_timeout(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert mock_ecu.for_unit(1).required_timeout == 15.0


async def test_reopened_form_keeps_connection(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(2).fail_read(40004, ModbusTimeoutError())
    result = await start_user_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "ecu_busy"}

    # Reopening the dialog re-runs the step without input.
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "user"
    defaults = result["data_schema"]({})
    assert defaults[CONF_HOST] == HOST
    assert defaults[CONF_PORT] == 502


async def test_dhcp_failure_returns_to_discovery_screen(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    mock_ecu.for_unit(2).fail_read(40004, ModbusTimeoutError())

    result = await submit_and_wait(hass, result["flow_id"], {})
    assert result["step_id"] == "dhcp_confirm"
    assert result["errors"] == {"base": "ecu_busy"}

    # Reopening the dialog shows the discovery screen again, not the manual form.
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "dhcp_confirm"

    # Confirming again runs the automatic search again.
    mock_ecu.for_unit(2).fail_read(40004, None)
    result = await submit_and_wait(hass, result["flow_id"], {})
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"
