"""Lock platform for Seam Lock integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SeamLockConfigEntry
from .const import ATTRIBUTION, CONF_DEVICE_ID, CONF_DEVICE_NAME, DOMAIN, MANUFACTURER
from .coordinator import SeamLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeamLockConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the lock entity."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            SeamLock(
                coordinator=runtime.coordinator,
                device_id=entry.data[CONF_DEVICE_ID],
                device_name=entry.data.get(CONF_DEVICE_NAME, "Seam Lock"),
            )
        ]
    )


class SeamLock(
    CoordinatorEntity[SeamLockCoordinator], RestoreEntity, LockEntity
):
    """Representation of a Seam-managed smart lock."""

    _attr_has_entity_name = True
    _attr_name = None  # Uses device name
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: SeamLockCoordinator,
        device_id: str,
        device_name: str,
    ) -> None:
        """Initialise the lock entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._device_name = device_name
        self._attr_unique_id = f"seam_lock_{device_id}"

    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if self.coordinator.data.locked is None:
                self.coordinator.data.locked = last.state == "locked"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the device registry."""
        info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model="FE789WB Encode WiFi Lever",
        )
        if self.coordinator.data and self.coordinator.data.device:
            model_obj = getattr(
                self.coordinator.data.device.properties, "model", None
            )
            if model_obj:
                display = getattr(model_obj, "display_name", None)
                if display:
                    info["model"] = display
        return info

    @property
    def available(self) -> bool:
        """Lock is available when coordinator succeeded and device is online."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.online
        )

    @property
    def is_locked(self) -> bool | None:
        """Return the lock state."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.locked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose useful attributes (no secrets or URLs)."""
        data = self.coordinator.data
        if data is None:
            return {}

        attrs: dict[str, Any] = {
            "online": data.online,
            "battery_level": data.battery_level,
            "last_unlock_by": data.last_unlock_by or "N/A",
            "last_unlock_time": data.last_unlock_time or "N/A",
            "last_unlock_method": data.last_unlock_method or "N/A",
            "last_lock_time": data.last_lock_time or "N/A",
            "unlocks_today": data.total_unlocks_today,
        }

        if data.door_open is not None:
            attrs["door_open"] = data.door_open

        if data.access_codes:
            attrs["registered_access_codes"] = len(data.access_codes)

        return attrs

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the door."""
        await self.hass.async_add_executor_job(
            lambda: self.coordinator.seam.locks.lock_door(
                device_id=self._device_id
            )
        )
        self.coordinator.data.locked = True
        self.coordinator.async_set_updated_data(self.coordinator.data)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the door."""
        await self.hass.async_add_executor_job(
            lambda: self.coordinator.seam.locks.unlock_door(
                device_id=self._device_id
            )
        )
        self.coordinator.data.locked = False
        self.coordinator.async_set_updated_data(self.coordinator.data)
