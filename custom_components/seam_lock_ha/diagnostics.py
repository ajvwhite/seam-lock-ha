"""Diagnostics for Seam Lock integration.

Provides a sanitised snapshot for debugging. API keys, webhook secrets,
and access code PINs are redacted.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import SeamLockConfigEntry
from .const import CONF_API_KEY, CONF_WEBHOOK_SECRET

REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SeamLockConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # Redact sensitive fields from config data
    data_sanitised = dict(entry.data)
    if CONF_API_KEY in data_sanitised:
        key = data_sanitised[CONF_API_KEY]
        data_sanitised[CONF_API_KEY] = f"{key[:8]}...{REDACTED}"

    options_sanitised = dict(entry.options)
    if options_sanitised.get(CONF_WEBHOOK_SECRET):
        options_sanitised[CONF_WEBHOOK_SECRET] = REDACTED

    result: dict[str, Any] = {
        "config_entry": {
            "data": data_sanitised,
            "options": options_sanitised,
            "version": entry.version,
        },
    }

    if hasattr(entry, "runtime_data") and entry.runtime_data:
        runtime = entry.runtime_data
        d = runtime.coordinator.data

        def _fmt_dt(val: Any) -> str | None:
            """Format datetime to ISO string for JSON serialisation."""
            if val is None:
                return None
            if hasattr(val, "isoformat"):
                return val.isoformat()
            return str(val)

        result["coordinator"] = {
            "device_name": d.device_name,
            "locked": d.locked,
            "online": d.online,
            "battery_level": d.battery_level,
            "battery_status": d.battery_status,
            "door_open": d.door_open,
            "last_unlock_by": d.last_unlock_by,
            "last_unlock_time": _fmt_dt(d.last_unlock_time),
            "last_unlock_method": d.last_unlock_method,
            "last_lock_time": _fmt_dt(d.last_lock_time),
            "total_unlocks_today": d.total_unlocks_today,
            "access_code_count": len(d.access_codes),
            "event_count": len(d.events),
            "recent_events": runtime.coordinator.get_formatted_history(
                limit=10
            ),
        }

    return result
