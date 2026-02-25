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
    DEVICE_EVENT_TYPES,
    DOMAIN,
    EVENT_TYPE_MAP,
    HA_EVENT_SEAM_LOCK,
    UNLOCK_METHODS,
    WATCHED_EVENT_TYPES,
    format_event_description,
)

_LOGGER = logging.getLogger(__name__)

_RECONCILE_DELAY_SECONDS = 8
_EVENT_LOOKBACK_DAYS = 7

# Hard ceiling on stored events to bound memory.
_MAX_STORED_EVENTS = 100

# Timeout for individual Seam API calls (seconds).
# Enforced via monkey-patch on the SDK's underlying requests.Session.
_API_TIMEOUT_SECONDS = 15


def _create_seam_with_timeout(api_key: str) -> Seam:
    """Create a Seam client with enforced request-level timeouts.

    The Seam SDK uses ``requests.Session`` internally but does not set
    a default timeout.  Without one, any HTTP call can block an executor
    thread indefinitely if the Seam API is slow or unreachable.

    We monkey-patch ``Session.request`` so that **every** call made
    through this client has a ceiling of ``_API_TIMEOUT_SECONDS``.
    """
    seam = Seam(api_key=api_key)
    _orig_request = seam.client.request

    def _timeout_request(method: str, url: str, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _API_TIMEOUT_SECONDS
        return _orig_request(method, url, **kwargs)

    seam.client.request = _timeout_request  # type: ignore[assignment]
    return seam


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
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )
        self._api_key = api_key
        self._device_id = device_id
        self._event_limit = min(event_limit, _MAX_STORED_EVENTS)
        self._seam: Seam | None = None
        self._reconcile_unsub: CALLBACK_TYPE | None = None
        self._poll_in_progress = False

        self.data = SeamLockData()
        self._event_listeners: list[callback] = []
        # Track which event IDs have been dispatched to listeners so
        # the polling path can detect genuinely new events.
        self._dispatched_event_ids: set[str] = set()
        # Whether the initial seed of known event IDs has been done.
        # Prevents flooding the timeline with historical events on startup.
        self._dispatch_seeded: bool = False

    def register_event_listener(self, listener: callback) -> CALLBACK_TYPE:
        """Register a listener for lock events.  Returns an unsubscribe cb."""
        self._event_listeners.append(listener)

        @callback
        def _unsub() -> None:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

        return _unsub

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def seam(self) -> Seam:
        """Lazy Seam client — created once, reused for all API calls."""
        if self._seam is None:
            self._seam = _create_seam_with_timeout(self._api_key)
        return self._seam

    def shutdown(self) -> None:
        """Release all resources.  Called from ``async_unload_entry``."""
        # Cancel any pending reconciliation timer
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None

        # Close the underlying requests.Session to release TCP sockets
        # from the urllib3 connection pool.  Without this, each reload
        # leaks a session with open connections.
        if self._seam is not None:
            try:
                self._seam.client.close()
            except Exception:  # noqa: BLE001
                pass
            self._seam = None

        # Drop listener references
        self._event_listeners.clear()
        self._dispatched_event_ids.clear()
        self._dispatch_seeded = False

    # -- Webhook instant-update path -------------------------------------------

    @callback
    def handle_webhook_event(self, payload: dict[str, Any]) -> None:
        """Process a Seam event delivered via webhook."""
        event_type = payload.get("event_type", "")
        if event_type not in WATCHED_EVENT_TYPES:
            return

        device_id = payload.get("device_id")
        if device_id and device_id != self._device_id:
            return

        entry = self._normalise_event(payload)

        # -- Fast-patch current data -------------------------------------------
        if event_type == "lock.unlocked":
            self.data.locked = False
            self.data.last_unlock_time = entry["occurred_dt"]
            self.data.last_unlock_method = entry["method_display"]
            self.data.last_unlock_by = entry["who"]
            self.data.total_unlocks_today += 1
        elif event_type == "lock.locked":
            self.data.locked = True
            self.data.last_lock_time = entry["occurred_dt"]
        elif event_type == "device.connected":
            self.data.online = True
        elif event_type == "device.disconnected":
            self.data.online = False

        # Dedup and append (O(n) with n capped at _MAX_STORED_EVENTS)
        existing_ids = {
            e.get("event_id") for e in self.data.events if e.get("event_id")
        }
        if entry["event_id"] not in existing_ids:
            self.data.events.insert(0, entry)
            if len(self.data.events) > _MAX_STORED_EVENTS:
                del self.data.events[_MAX_STORED_EVENTS:]

        # Mark as dispatched so the polling path doesn't re-fire it
        self._dispatched_event_ids.add(entry["event_id"])
        # Cap the tracking set to avoid unbounded growth
        if len(self._dispatched_event_ids) > _MAX_STORED_EVENTS * 2:
            self._dispatched_event_ids = {
                e.get("event_id")
                for e in self.data.events[:_MAX_STORED_EVENTS]
                if e.get("event_id")
            }

        # Fire HA bus event for automations
        self.hass.bus.async_fire(
            HA_EVENT_SEAM_LOCK,
            {
                "device_id": self._device_id,
                "event_type": event_type,
                "occurred_at": entry["occurred_at"],
                "method": entry["method_display"],
                "who": entry["who"],
            },
        )

        # Notify EventEntity listeners
        for listener in self._event_listeners:
            try:
                listener(entry)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Event listener error", exc_info=True)

        # Push to entities
        self.async_set_updated_data(self.data)

        # Schedule ONE reconciliation (debounced — cancels previous)
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        """Schedule a single delayed API poll.  Self-debouncing."""
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
        """Full API poll.

        The ``DataUpdateCoordinator`` base class already debounces
        concurrent refresh requests.  The ``_poll_in_progress`` flag is
        an extra safety net to ensure we never queue multiple executor
        jobs for the same poll cycle.
        """
        if self._poll_in_progress:
            _LOGGER.debug("Poll already in progress, skipping")
            return self.data

        self._poll_in_progress = True
        try:
            prev = self.data

            # -- Device state (single API call) --------------------------------
            try:
                device = await self.hass.async_add_executor_job(
                    self._fetch_device
                )
                prev.device = device
                prev.device_name = (
                    getattr(device, "display_name", None)
                    or prev.device_name
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
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Device poll failed: %s", err)
                raise UpdateFailed(f"Device poll failed: {err}") from err

            # -- Access codes (non-fatal) --------------------------------------
            try:
                codes = await self.hass.async_add_executor_job(
                    self._fetch_access_codes
                )
                prev.access_codes = {
                    c.access_code_id: c.name
                    or f"Unnamed Code ({c.access_code_id[:8]})"
                    for c in codes
                }
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Access codes fetch failed: %s", err)

            # -- Events (non-fatal) --------------------------------------------
            try:
                api_events = await self.hass.async_add_executor_job(
                    self._fetch_events
                )
                prev.events = self._merge_events(prev.events, api_events)

                # Dispatch genuinely new events to listeners (e.g. EventEntity)
                # so the timeline updates even without webhooks.
                self._dispatch_new_events(api_events)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Events fetch failed: %s", err)

            self._derive_summary(prev)
            return prev

        finally:
            self._poll_in_progress = False

    # -- API call wrappers (each runs in executor, timeout via requests) --------

    def _fetch_device(self) -> Any:
        return self.seam.devices.get(device_id=self._device_id)

    def _fetch_access_codes(self) -> list[Any]:
        return self.seam.access_codes.list(device_id=self._device_id)

    def _fetch_events(self) -> list[dict[str, Any]]:
        """Fetch events — tries single ``event_types`` call first."""
        since_dt = datetime.now(timezone.utc) - timedelta(
            days=_EVENT_LOOKBACK_DAYS
        )
        since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # All event types we want in the timeline
        all_event_types = [
            "lock.unlocked", "lock.locked", "lock.access_denied",
            *DEVICE_EVENT_TYPES,
        ]

        raw: list[Any] = []
        try:
            evts = self.seam.events.list(
                device_id=self._device_id,
                event_types=all_event_types,
                since=since_str,
                limit=self._event_limit,
            )
            raw.extend(evts)
        except Exception:  # noqa: BLE001
            # Fallback for older SDK versions without event_types (plural)
            for etype in all_event_types:
                try:
                    evts = self.seam.events.list(
                        device_id=self._device_id,
                        event_type=etype,
                        since=since_str,
                        limit=self._event_limit,
                    )
                    raw.extend(evts)
                except Exception as err2:  # noqa: BLE001
                    _LOGGER.debug("events.list(%s) failed: %s", etype, err2)

        normalised: list[dict[str, Any]] = []
        for ev in raw:
            try:
                normalised.append(
                    self._normalise_event(
                        {
                            "event_id": getattr(ev, "event_id", None),
                            "event_type": getattr(
                                ev, "event_type", "unknown"
                            ),
                            "occurred_at": getattr(ev, "occurred_at", None),
                            "created_at": getattr(ev, "created_at", None),
                            "method": getattr(ev, "method", None),
                            "access_code_id": getattr(
                                ev, "access_code_id", None
                            ),
                        }
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        return normalised

    # -- Pure helpers (no I/O) ------------------------------------------------

    @callback
    def _dispatch_new_events(
        self, api_events: list[dict[str, Any]]
    ) -> None:
        """Dispatch events not yet seen to EventEntity listeners.

        Only fires events whose event_id hasn't been dispatched before
        (webhook or previous poll).  On the first call with active
        listeners, seeds the tracking set from ALL known events (both
        previously stored and just-fetched) without firing — avoids
        flooding the timeline with historical events at startup.
        """
        if not self._event_listeners:
            return

        # First dispatch with listeners: seed from everything we know
        # about (self.data.events already merged with api_events by
        # the caller).  This covers the case where a webhook arrived
        # between first poll and entity registration.
        if not self._dispatch_seeded:
            self._dispatched_event_ids = {
                e.get("event_id")
                for e in self.data.events
                if e.get("event_id")
            }
            self._dispatch_seeded = True
            return

        new_events: list[dict[str, Any]] = []
        for ev in api_events:
            eid = ev.get("event_id")
            if eid and eid not in self._dispatched_event_ids:
                new_events.append(ev)
                self._dispatched_event_ids.add(eid)

        # Cap the tracking set
        if len(self._dispatched_event_ids) > _MAX_STORED_EVENTS * 2:
            self._dispatched_event_ids = {
                e.get("event_id")
                for e in self.data.events[:_MAX_STORED_EVENTS]
                if e.get("event_id")
            }

        # Dispatch newest first (api_events are already sorted desc)
        for ev in new_events:
            for listener in self._event_listeners:
                try:
                    listener(ev)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Event listener error", exc_info=True)

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                return None
        return None

    def _normalise_event(self, raw: dict[str, Any]) -> dict[str, Any]:
        method_raw = raw.get("method")
        method_display = UNLOCK_METHODS.get(
            method_raw, method_raw or "Unknown"
        )
        access_code_id = raw.get("access_code_id")
        who = self._resolve_who(method_raw, access_code_id)

        occurred_at_raw = raw.get("occurred_at") or raw.get("created_at")
        occurred_dt = self._parse_timestamp(occurred_at_raw)
        if occurred_dt is None:
            occurred_dt = datetime.now(timezone.utc)

        event_id = raw.get("event_id")
        if not event_id:
            event_id = f"wh_{uuid.uuid4().hex[:12]}"

        return {
            "event_id": event_id,
            "event_type": raw.get("event_type", "unknown"),
            "occurred_at": occurred_dt.isoformat(),
            "occurred_dt": occurred_dt,
            "method": method_raw,
            "method_display": method_display,
            "access_code_id": access_code_id,
            "who": who,
        }

    def _resolve_who(
        self, method_raw: str | None, access_code_id: str | None
    ) -> str:
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
        seen_ids: set[str] = set()
        seen_sigs: set[tuple[str, str]] = set()
        merged: list[dict[str, Any]] = []

        for event in [*existing, *api_events]:
            eid = event.get("event_id", "")
            if eid and eid in seen_ids:
                continue

            etype = event.get("event_type", "")
            occ = event.get("occurred_at", "")
            sig = (etype, occ)
            if etype and occ and sig in seen_sigs:
                continue

            if eid:
                seen_ids.add(eid)
            if etype and occ:
                seen_sigs.add(sig)
            merged.append(event)

        merged.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)
        return merged[:_MAX_STORED_EVENTS]

    def _derive_summary(self, data: SeamLockData) -> None:
        """Recompute summary fields using HA's configured timezone."""
        local_now = dt_util.now()
        today_local = local_now.date()
        unlocks_today = 0
        found_unlock = False
        found_lock = False

        for event in data.events:
            etype = event.get("event_type", "")
            occurred_dt: datetime | None = event.get("occurred_dt")
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
                    local_event = occurred_dt.astimezone(local_now.tzinfo)
                    if local_event.date() == today_local:
                        unlocks_today += 1
                except (ValueError, AttributeError, TypeError):
                    pass

        data.total_unlocks_today = unlocks_today

    def get_formatted_history(
        self, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return event history formatted for attributes / diagnostics."""
        result: list[dict[str, Any]] = []
        for event in (self.data.events or [])[:limit]:
            result.append(
                {
                    "time": event.get("occurred_at"),
                    "action": format_event_description(
                        event.get("event_type", ""),
                        event.get("who", ""),
                        event.get("method_display", ""),
                    ),
                    "method": event.get("method_display", ""),
                    "who": event.get("who", ""),
                }
            )
        return result
