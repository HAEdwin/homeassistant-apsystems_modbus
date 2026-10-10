"""Constants for the APsystems Modbus integration."""

from datetime import timedelta

DOMAIN = "apsystems_modbus"
MANUFACTURER = "APsystems"

CONF_UNIT_IDS = "unit_ids"
CONF_INVERTERS = "inverters"
CONF_ECU_ID = "ecu_id"

CONF_UNIT_ID = "unit_id"
CONF_SERIAL_NUMBER = "serial_number"
CONF_MODEL = "model"

DEFAULT_PORT = 502
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 60
MAX_SCAN_INTERVAL = 900

MAX_TIMEOUTS_PER_CYCLE = 2
"""After this many unanswered inverters in one cycle the rest is skipped."""
MAX_BACKOFF = timedelta(minutes=15)
"""Longest poll interval while the ECU is not responding."""
