"""Diagnostics: the last poll per inverter, without addresses or serial numbers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_MAC
from homeassistant.core import HomeAssistant

from .const import CONF_ECU_ID, CONF_SERIAL_NUMBER
from .coordinator import ApsystemsConfigEntry
from .ecu import Inverter

# CONF_ECU_ID: entries made before the field was removed may still hold it.
TO_REDACT = {CONF_HOST, CONF_MAC, CONF_ECU_ID, CONF_SERIAL_NUMBER}


def _component_values(inverter: Inverter) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in ("identity", "measurements", "status"):
        component = getattr(inverter, name)
        values[name] = {
            field: getattr(component, field) for field in component.declared_fields
        }
    return values


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ApsystemsConfigEntry
) -> dict[str, Any]:
    """Return what the integration knows, redacted."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {
            # The title can hold the ECU id, so it is left out.
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "available": sorted(data.available) if data else None,
            "recent": sorted(data.recent) if data else None,
            "value_age_seconds": coordinator.value_ages(),
            "power": data.power if data else None,
            "energy_today": data.energy_today if data else None,
            "online": data.online if data else None,
            "last_change": data.last_change.isoformat()
            if data and data.last_change
            else None,
        },
        "polling": {"settle_s": coordinator.settle},
        "power_limit": {
            "control_unit": coordinator.power_limit.control.unit_id,
            "desired": coordinator.power_limit.desired,
            "pending": coordinator.power_limit.pending,
            "shown": coordinator.power_limit.shown,
            "deviations": coordinator.power_limit.deviations,
            "last_rewrite": coordinator.power_limit.last_rewrite.isoformat()
            if coordinator.power_limit.last_rewrite
            else None,
            "refreshing": coordinator.power_limit.refreshing,
            "next_refresh": coordinator.power_limit.next_refresh.isoformat()
            if coordinator.power_limit.next_refresh
            else None,
            "rewrites": coordinator.power_limit.rewrites,
            "installation_power_kw": coordinator.power_limit.installation_power,
            "overshoot_refreshes": coordinator.power_limit.overshoot_refreshes,
        },
        "lifetime": {
            "lifetime_energy_wh": coordinator.ledger.lifetime,
            "peak_power_w": coordinator.ledger.peak_power,
        },
        "inverters": {
            str(unit_id): async_redact_data(_component_values(inverter), TO_REDACT)
            for unit_id, inverter in sorted(coordinator.inverters.items())
        },
    }
