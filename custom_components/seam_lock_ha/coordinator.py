"""DataUpdateCoordinator for the Seam Lock integration."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from seam import Seam

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_EVENT_LIMIT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    HA_EVENT_SEAM_LOCK,
    UNLOCK_METHODS,
    WATCHED_EVENT_TYPES,
)

_LOGGER = logging.getLogger(__name__)

# Delay before reconciliation poll after a webhook delivery.
_RECONCILE_DELAY_SECONDS = 8

# How far back to fetch events from Seam API.
# The API *requires* `since` or `between` — omitting both causes a 400 error.
_EVENT_LOOKBACK_DAYS = 7


class SeamLockData:
    """Container for all data about the lock."""

    __slots__ = (
        "access_codes",
        "battery_level",
        "battery_status",
        "device",
        "device_name",
        "door_open",
        "events",
        "last_lock_time",
        "last_unlock_by",
        "last_unlock_method",
        "last_unlock_time",
        "locked",
        "online",
        "total_unlocks_today",
    )

    def __init__(self) -> None:
        """Initialise with safe defaults."""
        self.device: Any = None
        self.locked: bool | None = None
        self.online: bool = False
        self.battery_level: float | None = None
        self.battery_status: str | None = None
        self.door_open: bool | None = None
        self.device_name: str = "Seam Lock"

        self.events: list[dict[str, Any]] = []
        self.last_unlock_by: str | None = None
        self.last_unlock_time: datetime | None = None
        self.last_unlock_method: str | None = None
        self.last_lock_time: datetime | None = None
        self.total_unlocks_today: int = 0

        # Access codes cache (id -> display name, never raw PINs)
        self.access_codes: dict[str, str] = {}


class SeamLockCoordinator(DataUpdateCoordinator[SeamLockData]):
    """Coordinate Seam API polling and webhook-delivered updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        device_id: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )
        self._api_key = api_key
        self._device_id = device_id
        self._event_limit = event_limit
        self._seam: Seam | None = None
        self._reconcile_unsub: CALLBACK_TYPE | None = None

        # Live data -- survives across polls and webhooks
        self.data = SeamLockData()

        # Listeners for EventEntity — called with normalised event dicts
        self._event_listeners: list[callback] = []

    def register_event_listener(self, listener: callback) -> callback:
        """Register a listener for lock events. Returns an unsubscribe callback."""
        self._event_listeners.append(listener)

        @callback
        def _unsub() -> None:
            self._event_listeners.remove(listener)

        return _unsub

    @property
    def device_id(self) -> str:
        """Return the Seam device_id this coordinator manages."""
        return self._device_id

    @property
    def seam(self) -> Seam:
        """Lazy-initialised Seam client (created on first use in executor)."""
        if self._seam is None:
            self._seam = Seam(api_key=self._api_key)
        return self._seam

    # -- Webhook instant-update path -------------------------------------------

    @callback
    def handle_webhook_event(self, payload: dict[str, Any]) -> None:
        """Process a Seam event delivered via webhook.

        Patches live data immediately so entities update within seconds,
        then schedules a delayed API reconciliation.
        """
        event_type = payload.get("event_type", "")
        if event_type not in WATCHED_EVENT_TYPES:
            _LOGGER.debug(
                "Webhook event_type %r not in watched set, ignoring",
                event_type,
            )
            return

        device_id = payload.get("device_id")
        if device_id and device_id != self._device_id:
            _LOGGER.debug(
                "Webhook device_id %s != ours %s, ignoring",
                device_id,
                self._device_id,
            )
            return

        entry = self._normalise_event(payload)
        occurred_dt = entry["occurred_dt"]

        _LOGGER.debug(
            "Processing webhook: type=%s, occurred_at=%s, who=%s, "
            "method=%s, event_id=%s",
            event_type,
            entry["occurred_at"],
            entry["who"],
            entry["method_display"],
            entry["event_id"],
        )

        # -- Fast-patch current data -------------------------------------------
        if event_type == "lock.unlocked":
            self.data.locked = False
            self.data.last_unlock_time = occurred_dt
            self.data.last_unlock_method = entry["method_display"]
            self.data.last_unlock_by = entry["who"]
            self.data.total_unlocks_today += 1
            _LOGGER.debug(
                "Patched unlock: time=%s, by=%s, method=%s, "
                "unlocks_today=%d",
                self.data.last_unlock_time,
                self.data.last_unlock_by,
                self.data.last_unlock_method,
                self.data.total_unlocks_today,
            )

        elif event_type == "lock.locked":
            self.data.locked = True
            self.data.last_lock_time = occurred_dt

        elif event_type == "device.connected":
            self.data.online = True

        elif event_type == "device.disconnected":
            self.data.online = False

        # Add to event list (dedup by event_id)
        existing_ids = {
            e.get("event_id") for e in self.data.events if e.get("event_id")
        }
        if entry["event_id"] not in existing_ids:
            self.data.events.insert(0, entry)
            self.data.events = self.data.events[: self._event_limit * 2]
            _LOGGER.debug(
                "Event added to list (total: %d)", len(self.data.events)
            )
        else:
            _LOGGER.debug(
                "Event %s already in list, skipped", entry["event_id"]
            )

        # Fire HA event for automations
        self.hass.bus.async_fire(
            HA_EVENT_SEAM_LOCK,
            {
                "device_id": self._device_id,
                "device_name": self.data.device_name,
                "event_type": event_type,
                "occurred_at": entry["occurred_at"],
                "method": entry["method_display"],
                "who": entry["who"],
            },
        )

        # Notify EventEntity listeners
        for listener in self._event_listeners:
            listener(entry)

        # Push updated data to all entities immediately
        self.async_set_updated_data(self.data)

        # Schedule a delayed reconciliation poll
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        """Schedule a delayed API poll after a webhook delivery."""
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None

        @callback
        def _do_reconcile(_now: datetime) -> None:
            self._reconcile_unsub = None
            self.hass.async_create_task(self.async_request_refresh())

        self._reconcile_unsub = async_call_later(
            self.hass, _RECONCILE_DELAY_SECONDS, _do_reconcile
        )

    # -- Full polling path -----------------------------------------------------

    async def _async_update_data(self) -> SeamLockData:
        """Full API poll -- merges with existing webhook-patched data."""
        try:
            prev = self.data  # Mutate in place to preserve webhook state

            # -- Device state --------------------------------------------------
            device = await self.hass.async_add_executor_job(
                lambda: self.seam.devices.get(device_id=self._device_id)
            )

            prev.device = device
            prev.device_name = (
                getattr(device, "display_name", None) or prev.device_name
            )

            props = device.properties
            prev.locked = getattr(props, "locked", prev.locked)
            prev.online = getattr(props, "online", prev.online)

            battery = getattr(props, "battery", None)
            if battery:
                level = getattr(battery, "level", None)
                if level is not None:
                    prev.battery_level = round(level * 100, 1)
                prev.battery_status = getattr(
                    battery, "status", prev.battery_status
                )
            else:
                raw = getattr(props, "battery_level", None)
                if raw is not None:
                    prev.battery_level = round(raw * 100, 1)

            prev.door_open = getattr(props, "door_open", prev.door_open)

            # -- Access codes (names only -- never raw PINs) -------------------
            try:
                codes = await self.hass.async_add_executor_job(
                    lambda: self.seam.access_codes.list(
                        device_id=self._device_id
                    )
                )
                prev.access_codes = {
                    c.access_code_id: c.name
                    or f"Unnamed Code ({c.access_code_id[:8]})"
                    for c in codes
                }
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not fetch access codes: %s", err)

            # -- Events -- merge API with existing webhook events --------------
            try:
                api_events = await self.hass.async_add_executor_job(
                    self._fetch_events
                )
                before_count = len(prev.events)
                prev.events = self._merge_events(prev.events, api_events)
                _LOGGER.debug(
                    "Event merge: %d existing + %d API -> %d merged",
                    before_count,
                    len(api_events),
                    len(prev.events),
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not fetch/merge events: %s", err)

            # Re-derive summary from the authoritative merged list
            self._derive_summary(prev)

            return prev

        except Exception as err:
            raise UpdateFailed(
                f"Error communicating with Seam API: {err}"
            ) from err

    # -- Helpers ---------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        """Parse a timestamp string or datetime into a tz-aware datetime.

        Seam API returns ISO 8601 strings like '2026-02-24T20:00:00.123Z'.
        The Python SDK passes these through as strings.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            try:
                clean = value.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                _LOGGER.debug("Could not parse timestamp: %r", value)
                return None
        return None

    def _normalise_event(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Create a normalised event dict from a raw API/webhook payload.

        Every returned dict is guaranteed to have:
          - event_id (real or synthetic)
          - event_type
          - occurred_at (ISO string for display/serialisation)
          - occurred_dt (datetime object for comparisons)
          - method, method_display, access_code_id, who
        """
        method_raw = raw.get("method")
        method_display = UNLOCK_METHODS.get(
            method_raw, method_raw or "Unknown"
        )
        access_code_id = raw.get("access_code_id")
        who = self._resolve_who(method_raw, access_code_id)

        # Resolve occurred_at with fallbacks
        occurred_at_raw = raw.get("occurred_at") or raw.get("created_at")
        occurred_dt = self._parse_timestamp(occurred_at_raw)
        if occurred_dt is None:
            occurred_dt = datetime.now(timezone.utc)
            _LOGGER.debug(
                "Event missing occurred_at (raw=%r), using current time",
                occurred_at_raw,
            )

        # ISO string for display / serialisation / dedup signature
        occurred_at_str = occurred_dt.isoformat()

        # Ensure we always have an event_id for deduplication
        event_id = raw.get("event_id")
        if not event_id:
            event_id = f"wh_{uuid.uuid4().hex[:12]}"
            _LOGGER.debug(
                "Event missing event_id, generated synthetic: %s", event_id
            )

        return {
            "event_id": event_id,
            "event_type": raw.get("event_type", "unknown"),
            "occurred_at": occurred_at_str,
            "occurred_dt": occurred_dt,
            "method": method_raw,
            "method_display": method_display,
            "access_code_id": access_code_id,
            "who": who,
        }

    def _resolve_who(
        self, method_raw: str | None, access_code_id: str | None
    ) -> str:
        """Resolve who performed the action -- never exposes raw PIN codes."""
        if access_code_id and access_code_id in self.data.access_codes:
            return self.data.access_codes[access_code_id]
        if access_code_id:
            return f"Code ({access_code_id[:8]})"
        if method_raw == "manual":
            return "Manual (Thumbturn/Key)"
        if method_raw == "remote":
            return "Remote (App/API)"
        if method_raw == "bluetooth":
            return "Bluetooth"
        return UNLOCK_METHODS.get(method_raw, "Unknown")

    def _merge_events(
        self,
        existing: list[dict[str, Any]],
        api_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge existing (webhook) and API events, deduplicated.

        Deduplication uses two strategies:
        1. By event_id (primary — exact match)
        2. By (event_type, occurred_at) (secondary — catches webhook events
           with synthetic IDs that the API later returns with real IDs)
        """
        seen_ids: set[str] = set()
        seen_signatures: set[tuple[str, str]] = set()
        merged: list[dict[str, Any]] = []

        for event in [*existing, *api_events]:
            eid = event.get("event_id", "")

            if eid and eid in seen_ids:
                continue

            etype = event.get("event_type", "")
            occurred = event.get("occurred_at", "")
            sig = (etype, occurred)
            if etype and occurred and sig in seen_signatures:
                continue

            if eid:
                seen_ids.add(eid)
            if etype and occurred:
                seen_signatures.add(sig)
            merged.append(event)

        merged.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)
        return merged[: self._event_limit * 2]

    def _fetch_events(self) -> list[dict[str, Any]]:
        """Fetch lock events from the Seam API (runs in executor).

        CRITICAL: The Seam events.list() API *requires* either `since` or
        `between`.  Omitting both causes a 400/422 error that was silently
        swallowed in previous versions, resulting in zero events returned.
        """
        since_dt = datetime.now(timezone.utc) - timedelta(
            days=_EVENT_LOOKBACK_DAYS
        )
        since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        raw: list[Any] = []

        for etype in ("lock.unlocked", "lock.locked", "lock.access_denied"):
            try:
                evts = self.seam.events.list(
                    device_id=self._device_id,
                    event_type=etype,
                    since=since_str,
                    limit=self._event_limit,
                )
                raw.extend(evts)
                _LOGGER.debug(
                    "Fetched %d %s events from API", len(evts), etype
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "events.list(event_type=%s, since=%s) failed: %s",
                    etype,
                    since_str,
                    err,
                )

        _LOGGER.debug(
            "Total API events fetched: %d (since=%s)", len(raw), since_str
        )

        normalised: list[dict[str, Any]] = []
        for ev in raw:
            try:
                normalised.append(
                    self._normalise_event(
                        {
                            "event_id": getattr(ev, "event_id", None),
                            "event_type": getattr(ev, "event_type", "unknown"),
                            "occurred_at": getattr(ev, "occurred_at", None),
                            "created_at": getattr(ev, "created_at", None),
                            "method": getattr(ev, "method", None),
                            "access_code_id": getattr(
                                ev, "access_code_id", None
                            ),
                        }
                    )
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Skipping unprocessable event: %s", err)

        return normalised

    def _derive_summary(self, data: SeamLockData) -> None:
        """Recompute summary fields from the authoritative event list.

        Uses the HA instance's configured timezone for the 'today' boundary
        so unlocks_today matches the user's local day, not UTC.
        """
        local_now = dt_util.now()  # HA-configured timezone
        today_local = local_now.date()
        unlocks_today = 0
        found_unlock = False
        found_lock = False

        for event in data.events:
            etype = event.get("event_type", "")
            occurred_dt: datetime | None = event.get("occurred_dt")

            # Fallback: parse from string if occurred_dt missing
            if occurred_dt is None:
                occurred_dt = self._parse_timestamp(
                    event.get("occurred_at")
                )

            if etype == "lock.unlocked" and not found_unlock:
                data.last_unlock_time = occurred_dt
                data.last_unlock_method = event.get(
                    "method_display", "Unknown"
                )
                data.last_unlock_by = event.get("who", "Unknown")
                found_unlock = True

            if etype == "lock.locked" and not found_lock:
                data.last_lock_time = occurred_dt
                found_lock = True

            if etype == "lock.unlocked" and occurred_dt is not None:
                try:
                    # Convert event UTC time to local timezone for day match
                    local_event = occurred_dt.astimezone(local_now.tzinfo)
                    if local_event.date() == today_local:
                        unlocks_today += 1
                except (ValueError, AttributeError, TypeError):
                    pass

        data.total_unlocks_today = unlocks_today

        _LOGGER.debug(
            "Derived summary from %d events: last_unlock_by=%s, "
            "last_unlock_time=%s, unlocks_today=%d (local_date=%s)",
            len(data.events),
            data.last_unlock_by,
            data.last_unlock_time,
            data.total_unlocks_today,
            today_local,
        )

    def get_formatted_history(
        self, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return event history formatted for entity attributes."""
        result: list[dict[str, Any]] = []
        for event in (self.data.events or [])[:limit]:
            etype = event.get("event_type", "")
            if etype == "lock.unlocked":
                action = "Unlocked"
            elif etype == "lock.locked":
                action = "Locked"
            else:
                action = "Access Denied"
            result.append(
                {
                    "time": event.get("occurred_at"),
                    "action": action,
                    "method": event.get("method_display", "Unknown"),
                    "who": event.get("who", "Unknown"),
                }
            )
        return result
