"""Constants for the Seam Lock integration."""

from __future__ import annotations

DOMAIN = "seam_lock_ha"
MANUFACTURER = "Schlage"

# Config / options keys
CONF_API_KEY = "api_key"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_POLL_INTERVAL = "poll_interval"
CONF_EVENT_LIMIT = "event_limit"
CONF_WEBHOOK_SECRET = "webhook_secret"

# Defaults
DEFAULT_POLL_INTERVAL = 30           # seconds — polling-only mode
DEFAULT_POLL_INTERVAL_WEBHOOK = 300  # seconds — reconciliation when webhooks active
DEFAULT_EVENT_LIMIT = 25

# Entity platforms
PLATFORMS: list[str] = ["lock", "sensor", "binary_sensor", "event"]

ATTRIBUTION = "Data provided by Seam API"

# HA event bus name fired for each webhook-delivered lock event
HA_EVENT_SEAM_LOCK = "seam_lock_ha_event"

# Maximum age (seconds) of a Svix webhook timestamp before rejection (replay defence)
WEBHOOK_TIMESTAMP_TOLERANCE = 300

# Seam event types we care about
WATCHED_EVENT_TYPES = frozenset(
    {
        "lock.locked",
        "lock.unlocked",
        "lock.access_denied",
        "device.connected",
        "device.disconnected",
        "device.low_battery",
        "device.battery_status_changed",
    }
)

# Human-readable labels for unlock methods
UNLOCK_METHODS: dict[str, str] = {
    "keycode": "Access Code",
    "manual": "Manual (Thumbturn/Key)",
    "remote": "Remote (App/API)",
    "bluetooth": "Bluetooth",
    "automatic": "Auto-Lock",
    "unknown": "Unknown",
}

# Map Seam API event_type → short EventEntity event name.
# These short names must match the event_types list on the EventEntity
# and the translation keys in strings.json / translations/en.json.
EVENT_TYPE_MAP: dict[str, str] = {
    "lock.locked": "locked",
    "lock.unlocked": "unlocked",
    "lock.access_denied": "access_denied",
    "device.connected": "connected",
    "device.disconnected": "disconnected",
    "device.low_battery": "battery_low",
    "device.battery_status_changed": "battery_changed",
}

# All short event type names (used by EventEntity._attr_event_types)
EVENT_TYPE_NAMES: list[str] = sorted(EVENT_TYPE_MAP.values())

# Seam event types to also fetch during polling (device-level events).
# These supplement the lock events already fetched.
DEVICE_EVENT_TYPES: list[str] = [
    "device.connected",
    "device.disconnected",
    "device.low_battery",
]

# Icon per Seam event type — used by the Activity sensor.
ACTIVITY_ICONS: dict[str, str] = {
    "lock.locked": "mdi:lock",
    "lock.unlocked": "mdi:lock-open",
    "lock.access_denied": "mdi:lock-alert",
    "device.connected": "mdi:wifi",
    "device.disconnected": "mdi:wifi-off",
    "device.low_battery": "mdi:battery-alert",
    "device.battery_status_changed": "mdi:battery-sync",
}


def format_event_description(
    event_type: str,
    who: str,
    method: str,
) -> str:
    """Build a concise human-readable description for a lock event.

    Shared by both EventEntity (event data) and the Activity sensor
    (state text).  Uses the full Seam ``event_type`` (e.g.
    ``lock.unlocked``) rather than the short EventEntity name.
    """
    if event_type == "lock.unlocked":
        parts: list[str] = ["Unlocked"]
        if who and who != "Unknown":
            parts.append(f"by {who}")
        if method and method != "Unknown":
            parts.append(f"via {method}")
        return " ".join(parts)

    if event_type == "lock.locked":
        if method and method != "Unknown":
            return f"Locked via {method}"
        return "Locked"

    if event_type == "lock.access_denied":
        if method and method != "Unknown":
            return f"Access denied ({method})"
        return "Access denied"

    if event_type == "device.low_battery":
        return "Battery low"

    if event_type == "device.battery_status_changed":
        return "Battery status changed"

    if event_type == "device.connected":
        return "Back online"

    if event_type == "device.disconnected":
        return "Became unavailable"

    short = EVENT_TYPE_MAP.get(event_type, event_type)
    return short.replace("_", " ").title() if short else "Unknown event"
