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
PLATFORMS: list[str] = ["lock", "sensor", "binary_sensor"]

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
