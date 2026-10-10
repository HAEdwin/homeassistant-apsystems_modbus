"""Constants for the APsystems Modbus integration."""

from datetime import timedelta
from typing import TypedDict

DOMAIN = "apsystems_modbus"

CONF_UNIT_IDS = "unit_ids"
CONF_INVERTERS = "inverters"
CONF_ECU_ID = "ecu_id"
CONF_LIFETIME_ENERGY = "lifetime_energy"
CONF_PEAK_POWER = "peak_power"

CONF_UNIT_ID = "unit_id"
CONF_SERIAL_NUMBER = "serial_number"
CONF_MODEL = "model"


class InverterConfig(TypedDict):
    """One inverter as stored in the config entry."""

    unit_id: int
    serial_number: str
    model: str


DEFAULT_PORT = 502
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 60
MAX_SCAN_INTERVAL = 900

MAX_TIMEOUTS_PER_CYCLE = 2
"""After this many unanswered inverters in one cycle the rest is skipped."""
MAX_INVERTER_POWER = 5000
"""Watts no microinverter reaches; a higher reading is not counted in totals."""
MAX_BACKOFF = timedelta(minutes=15)
"""Longest poll interval while the ECU is not responding."""
