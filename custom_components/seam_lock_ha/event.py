"""Event platform for Seam Lock integration.

Uses HA's EventEntity (introduced 2023.8) to render lock/unlock/access_denied
events as a proper timeline in the UI.  Each event fires with who, method,
and timestamp as extra state data.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SeamLockConfigEntry
from .const import ATTRIBUTION, CONF_DEVICE_ID, CONF_DEVICE_NAME, DOMAIN, MANUFACTURER
from .coordinator import SeamLockCoordinator

_LOGGER = logging.getLogger(__name__)

# Map Seam event_type → short event name for EventEntity
_EVENT_TYPE_MAP: dict[str, str] = {
    "lock.locked": "locked",
    "lock.unlocked": "unlocked",
    "lock.access_denied": "access_denied",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeamLockConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up event entities."""
    coordinator = entry.runtime_data.coordinator
    did = entry.data[CONF_DEVICE_ID]
    dname = entry.data.get(CONF_DEVICE_NAME, "Seam Lock")

    async_add_entities([SeamLockEventEntity(coordinator, did, dname)])


class SeamLockEventEntity(EventEntity):
    """Event entity that fires on lock/unlock/access_denied.

    The HA UI renders this as a timeline showing each event with its
    timestamp and type, which is far more useful than a sensor that
    just says '14 events'.
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    _attr_device_class = EventDeviceClass.DOORBELL  # closest match — lock icon
    _attr_event_types = ["locked", "unlocked", "access_denied"]
    _attr_translation_key = "lock_event"

    def __init__(
        self,
        coordinator: SeamLockCoordinator,
        device_id: str,
        device_name: str,
    ) -> None:
        """Initialise the event entity."""
        self._coordinator = coordinator
        self._device_id = device_id
        self._attr_unique_id = f"seam_lock_ha_{device_id}_event"
        self._attr_name = "Lock Event"
        self._unsub: callback | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the parent lock device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to lock events from the coordinator."""
        await super().async_added_to_hass()
        self._unsub = self._coordinator.register_event_listener(
            self._handle_event
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from lock events."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_event(self, event: dict[str, Any]) -> None:
        """Handle a normalised lock event from the coordinator."""
        seam_type = event.get("event_type", "")
        short_type = _EVENT_TYPE_MAP.get(seam_type)
        if short_type is None:
            return

        self._trigger_event(
            short_type,
            {
                "who": event.get("who", "Unknown"),
                "method": event.get("method_display", "Unknown"),
                "occurred_at": event.get("occurred_at", ""),
            },
        )
        self.async_write_ha_state()
        _LOGGER.debug(
            "Lock event entity fired: %s (who=%s, method=%s)",
            short_type,
            event.get("who"),
            event.get("method_display"),
        )
