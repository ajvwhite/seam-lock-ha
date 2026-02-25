"""Event platform for Seam Lock integration.

Uses HA's EventEntity (introduced 2023.8) to render lock activity as a
proper timeline in the UI.  Each event fires with who, method, and a
human-readable description as extra state data.

Event types rendered:
  locked         - Lock was locked (auto-lock, manual, remote, etc.)
  unlocked       - Lock was unlocked (shows who + method)
  access_denied  - An invalid code or denied entry attempt
  battery_low    - Battery reported low
  battery_changed - Battery status changed
  connected      - Device came back online
  disconnected   - Device went offline
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.components.event import EventEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SeamLockConfigEntry
from .const import (
    ATTRIBUTION,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    DOMAIN,
    EVENT_TYPE_MAP,
    EVENT_TYPE_NAMES,
    MANUFACTURER,
    format_event_description,
)
from .coordinator import SeamLockCoordinator

_LOGGER = logging.getLogger(__name__)


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
    """Event entity that fires on lock activity and device status changes.

    The HA UI renders this as a timeline showing each event with its
    timestamp, translated type name, and description attribute -- giving
    users an at-a-glance activity log.

    Availability is tied to the coordinator's last poll success so the
    entity correctly shows as unavailable if the API is unreachable.
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    _attr_event_types = EVENT_TYPE_NAMES
    _attr_translation_key = "lock_event"
    _attr_icon = "mdi:lock-clock"

    def __init__(
        self,
        coordinator: SeamLockCoordinator,
        device_id: str,
        device_name: str,
    ) -> None:
        self._coordinator = coordinator
        self._device_id = device_id
        self._attr_unique_id = f"seam_lock_ha_{device_id}_event"
        self._attr_name = "Lock Event"
        self._unsub_event: CALLBACK_TYPE | None = None
        self._unsub_coordinator: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        """Tie availability to coordinator health."""
        return self._coordinator.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to lock events and coordinator updates."""
        await super().async_added_to_hass()
        self._unsub_event = self._coordinator.register_event_listener(
            self._handle_event
        )
        # Also listen to coordinator updates so ``available`` refreshes
        # when the coordinator recovers or fails.
        self._unsub_coordinator = self._coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from all listeners."""
        if self._unsub_event is not None:
            self._unsub_event()
            self._unsub_event = None
        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update availability when coordinator state changes."""
        self.async_write_ha_state()

    @callback
    def _handle_event(self, event: dict[str, Any]) -> None:
        """Handle a normalised lock event from the coordinator."""
        event_type = event.get("event_type", "")
        short_type = EVENT_TYPE_MAP.get(event_type)
        if short_type is None:
            return

        description = format_event_description(
            event_type,
            event.get("who", ""),
            event.get("method_display", ""),
        )

        self._trigger_event(
            short_type,
            {
                "description": description,
                "who": event.get("who", ""),
                "method": event.get("method_display", ""),
                "occurred_at": event.get("occurred_at", ""),
            },
        )
        self.async_write_ha_state()
