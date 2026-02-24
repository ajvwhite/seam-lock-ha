"""Sensor platform for Seam Lock integration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
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
    """Set up sensor entities."""
    coordinator = entry.runtime_data.coordinator
    did = entry.data[CONF_DEVICE_ID]
    dname = entry.data.get(CONF_DEVICE_NAME, "Seam Lock")

    async_add_entities(
        [
            SeamBatterySensor(coordinator, did, dname),
            SeamLastUnlockBySensor(coordinator, did, dname),
            SeamLastUnlockTimeSensor(coordinator, did, dname),
            SeamLastUnlockMethodSensor(coordinator, did, dname),
            SeamUnlocksTodaySensor(coordinator, did, dname),
            SeamAccessCodeCountSensor(coordinator, did, dname),
            SeamEventLogSensor(coordinator, did, dname),
        ]
    )


# -- Base class ----------------------------------------------------------------


class _Base(
    CoordinatorEntity[SeamLockCoordinator], RestoreEntity, SensorEntity
):
    """Base sensor with coordinator binding, state restoration, and device link."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: SeamLockCoordinator,
        device_id: str,
        device_name: str,
        key: str,
        name: str,
    ) -> None:
        """Initialise the base sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"seam_lock_ha_{device_id}_{key}"
        self._attr_name = name

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the parent lock device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )

    @property
    def available(self) -> bool:
        """Available whenever we have data."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )


# -- Battery -------------------------------------------------------------------


class SeamBatterySensor(_Base):
    """Battery level as a percentage."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SeamLockCoordinator, did: str, dname: str
    ) -> None:
        super().__init__(coordinator, did, dname, "battery", "Battery")

    async def async_added_to_hass(self) -> None:
        """Restore battery level."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if self.coordinator.data.battery_level is None and last.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                try:
                    self.coordinator.data.battery_level = float(last.state)
                except (ValueError, TypeError):
                    pass

    @property
    def native_value(self) -> float | None:
        return (
            self.coordinator.data.battery_level
            if self.coordinator.data
            else None
        )

    @property
    def icon(self) -> str:
        level = self.native_value
        if level is None:
            return "mdi:battery-unknown"
        if level <= 10:
            return "mdi:battery-alert"
        if level <= 30:
            return "mdi:battery-low"
        if level <= 60:
            return "mdi:battery-medium"
        return "mdi:battery-high"


# -- Last Unlock By ------------------------------------------------------------


class SeamLastUnlockBySensor(_Base):
    """Who last unlocked the door (access code name, or method)."""

    _attr_icon = "mdi:account-key"

    def __init__(
        self, coordinator: SeamLockCoordinator, did: str, dname: str
    ) -> None:
        super().__init__(
            coordinator, did, dname, "last_unlock_by", "Last Unlocked By"
        )

    async def async_added_to_hass(self) -> None:
        """Restore last unlocked by."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if self.coordinator.data.last_unlock_by is None and last.state not in (
                None,
                "unknown",
                "unavailable",
                "N/A",
            ):
                self.coordinator.data.last_unlock_by = last.state

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.last_unlock_by or "N/A"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if d is None:
            return {}
        # Format the datetime as ISO string for the attribute
        unlock_time = d.last_unlock_time
        if isinstance(unlock_time, datetime):
            unlock_time = unlock_time.isoformat()
        return {"time": unlock_time, "method": d.last_unlock_method}


# -- Last Unlock Time ----------------------------------------------------------


class SeamLastUnlockTimeSensor(_Base):
    """Timestamp of the most recent unlock.

    IMPORTANT: SensorDeviceClass.TIMESTAMP requires native_value to be a
    datetime object (with timezone info), NOT a string.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self, coordinator: SeamLockCoordinator, did: str, dname: str
    ) -> None:
        super().__init__(
            coordinator, did, dname, "last_unlock_time", "Last Unlock Time"
        )

    async def async_added_to_hass(self) -> None:
        """Restore last unlock time from saved state."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if self.coordinator.data.last_unlock_time is None and last.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                # Restore: parse the saved ISO string back to datetime
                restored = SeamLockCoordinator._parse_timestamp(last.state)
                if restored is not None:
                    self.coordinator.data.last_unlock_time = restored

    @property
    def native_value(self) -> datetime | None:
        """Return a timezone-aware datetime (required by TIMESTAMP class)."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.last_unlock_time


# -- Last Unlock Method --------------------------------------------------------


class SeamLastUnlockMethodSensor(_Base):
    """How the last unlock was performed."""

    _attr_icon = "mdi:key-variant"

    def __init__(
        self, coordinator: SeamLockCoordinator, did: str, dname: str
    ) -> None:
        super().__init__(
            coordinator, did, dname, "last_unlock_method", "Last Unlock Method"
        )

    async def async_added_to_hass(self) -> None:
        """Restore last unlock method."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if (
                self.coordinator.data.last_unlock_method is None
                and last.state not in (None, "unknown", "unavailable", "N/A")
            ):
                self.coordinator.data.last_unlock_method = last.state

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.last_unlock_method or "N/A"


# -- Unlocks Today -------------------------------------------------------------


class SeamUnlocksTodaySensor(_Base):
    """Number of unlock events today."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:door-open"

    def __init__(
        self, coordinator: SeamLockCoordinator, did: str, dname: str
    ) -> None:
        super().__init__(
            coordinator, did, dname, "unlocks_today", "Unlocks Today"
        )

    @property
    def native_value(self) -> int:
        return (
            self.coordinator.data.total_unlocks_today
            if self.coordinator.data
            else 0
        )


# -- Access Code Count ---------------------------------------------------------


class SeamAccessCodeCountSensor(_Base):
    """Number of access codes registered via Seam."""

    _attr_icon = "mdi:dialpad"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SeamLockCoordinator, did: str, dname: str
    ) -> None:
        super().__init__(
            coordinator,
            did,
            dname,
            "access_code_count",
            "Registered Access Codes",
        )

    @property
    def native_value(self) -> int:
        return (
            len(self.coordinator.data.access_codes)
            if self.coordinator.data
            else 0
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if d and d.access_codes:
            return {"code_names": list(d.access_codes.values())}
        return {}


# -- Full Event Log ------------------------------------------------------------


class SeamEventLogSensor(_Base):
    """Complete event history exposed via attributes."""

    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SeamLockCoordinator, did: str, dname: str
    ) -> None:
        super().__init__(
            coordinator, did, dname, "event_log", "Lock Event Log"
        )

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return f"{len(self.coordinator.data.events)} events"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        history = self.coordinator.get_formatted_history(limit=25)
        attrs: dict[str, Any] = {
            "events": history,
            "event_count": len(history),
        }
        for i, ev in enumerate(history[:5], 1):
            attrs[f"event_{i}_time"] = ev.get("time")
            attrs[f"event_{i}_action"] = ev.get("action")
            attrs[f"event_{i}_who"] = ev.get("who")
            attrs[f"event_{i}_method"] = ev.get("method")
        return attrs
