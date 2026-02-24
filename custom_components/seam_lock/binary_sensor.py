"""Binary sensor platform for Seam Lock integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SeamLockConfigEntry
from .const import (
    ATTRIBUTION,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_WEBHOOK_SECRET,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import SeamLockCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeamLockConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities."""
    coordinator = entry.runtime_data.coordinator
    did = entry.data[CONF_DEVICE_ID]
    dname = entry.data.get(CONF_DEVICE_NAME, "Seam Lock")

    entities: list[BinarySensorEntity] = [
        SeamOnlineSensor(coordinator, did, dname),
        SeamWebhookActiveSensor(coordinator, did, dname, entry),
    ]

    if coordinator.data and coordinator.data.door_open is not None:
        entities.append(SeamDoorSensor(coordinator, did, dname))

    async_add_entities(entities)


# -- Connectivity --------------------------------------------------------------


class SeamOnlineSensor(
    CoordinatorEntity[SeamLockCoordinator],
    RestoreEntity,
    BinarySensorEntity,
):
    """Whether the lock is reachable over WiFi."""

    _attr_has_entity_name = True
    _attr_name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: SeamLockCoordinator,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"seam_lock_{device_id}_online"

    async def async_added_to_hass(self) -> None:
        """Restore connectivity state."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if not self.coordinator.data.online:
                self.coordinator.data.online = last.state == "on"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )

    @property
    def is_on(self) -> bool:
        return (
            self.coordinator.data.online if self.coordinator.data else False
        )


# -- Door open/closed ---------------------------------------------------------


class SeamDoorSensor(
    CoordinatorEntity[SeamLockCoordinator],
    RestoreEntity,
    BinarySensorEntity,
):
    """Door contact sensor (if supported by the lock hardware)."""

    _attr_has_entity_name = True
    _attr_name = "Door"
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: SeamLockCoordinator,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"seam_lock_{device_id}_door"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )

    @property
    def is_on(self) -> bool | None:
        return (
            self.coordinator.data.door_open
            if self.coordinator.data
            else None
        )


# -- Webhook status ------------------------------------------------------------


class SeamWebhookActiveSensor(
    CoordinatorEntity[SeamLockCoordinator], BinarySensorEntity
):
    """Indicates whether the webhook secret is configured."""

    _attr_has_entity_name = True
    _attr_name = "Webhook Active"
    _attr_icon = "mdi:webhook"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: SeamLockCoordinator,
        device_id: str,
        device_name: str,
        entry: SeamLockConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry = entry
        self._attr_unique_id = f"seam_lock_{device_id}_webhook"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(CONF_WEBHOOK_SECRET, ""))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the webhook path (not the full external URL) for diagnostics."""
        wh_id = f"seam_lock_{self._entry.entry_id}"
        return {"webhook_path": f"/api/webhook/{wh_id}"}
