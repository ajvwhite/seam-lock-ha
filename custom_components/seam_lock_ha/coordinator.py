"""DataUpdateCoordinator for the Seam Lock integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

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

# Seam API base URL.
_SEAM_API_BASE = "https://connect.getseam.com"

# Timeout for individual Seam API calls.
# total covers the full request lifecycle; connect covers DNS + TCP + TLS.
_API_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3)

# Timeout for lock/unlock commands (action may wait for confirmation).
_COMMAND_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=3)

# Async-level ceiling for the entire poll cycle (all API calls combined).
_POLL_TIMEOUT_SECONDS = 30

# How often to re-fetch access codes (seconds).  Access codes rarely
# change, so polling them every cycle wastes an API call.
_ACCESS_CODE_REFRESH_SECONDS = 300  # 5 min

# How often to re-fetch event history when webhooks deliver events in
# real-time (seconds).  Set to 0 to disable throttling.
_EVENT_REFRESH_SECONDS = 300  # 5 min


class AsyncSeamClient:
    """Lightweight async Seam API client using aiohttp.

    Replaces the synchronous ``seam`` SDK to eliminate executor thread
    usage.  All HTTP calls are non-blocking and handled natively by
    the event loop — zero threads consumed per API call.
    """

    def __init__(
        self, session: aiohttp.ClientSession, api_key: str
    ) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout = _API_TIMEOUT,
    ) -> dict[str, Any]:
        """POST to the Seam API and return the parsed JSON response."""
        async with self._session.post(
            f"{_SEAM_API_BASE}{path}",
            json=body or {},
            headers=self._headers,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_device(self, device_id: str) -> dict[str, Any]:
        """Fetch a single device by ID."""
        data = await self._post("/devices/get", {"device_id": device_id})
        return data["device"]

    async def list_access_codes(
        self, device_id: str
    ) -> list[dict[str, Any]]:
        """List access codes for a device."""
        data = await self._post(
            "/access_codes/list", {"device_id": device_id}
        )
        return data["access_codes"]

    async def list_events(
        self,
        device_id: str,
        event_types: list[str],
        since: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """List events matching the given filters."""
        data = await self._post(
            "/events/list",
            {
                "device_id": device_id,
                "event_types": event_types,
                "since": since,
                "limit": limit,
            },
        )
        return data["events"]

    async def lock_door(self, device_id: str) -> None:
        """Send a lock command."""
        await self._post(
            "/locks/lock_door",
            {"device_id": device_id},
            timeout=_COMMAND_TIMEOUT,
        )

    async def unlock_door(self, device_id: str) -> None:
        """Send an unlock command."""
        await self._post(
            "/locks/unlock_door",
            {"device_id": device_id},
            timeout=_COMMAND_TIMEOUT,
        )

    async def list_locks(self) -> list[dict[str, Any]]:
        """List all locks visible to the API key (used by config flow)."""
        data = await self._post("/locks/list")
        return data.get("devices", [])


class SeamLockData:
    """Container for all data about the lock."""

    __slots__ = (
        "access_codes",
        "battery_level",
        "battery_status",
        "device_name",
        "door_open",
        "events",
        "last_lock_time",
        "last_unlock_by",
        "last_unlock_method",
        "last_unlock_time",
        "locked",
        "model_display_name",
        "online",
        "total_unlocks_today",
    )

    def __init__(self) -> None:
        self.locked: bool | None = None
        self.online: bool = False
        self.battery_level: float | None = None
        self.battery_status: str | None = None
        self.door_open: bool | None = None
        self.device_name: str = "Seam Lock"
        self.model_display_name: str | None = None
        self.events: list[dict[str, Any]] = []
        self.last_unlock_by: str | None = None
        self.last_unlock_time: datetime | None = None
        self.last_unlock_method: str | None = None
        self.last_lock_time: datetime | None = None
        self.total_unlocks_today: int = 0
        self.access_codes: dict[str, str] = {}


class SeamLockCoordinator(DataUpdateCoordinator[SeamLockData]):
    """Coordinate Seam API polling and webhook-delivered updates.

    All API calls use aiohttp and run natively on the event loop —
    zero executor threads are consumed.  Independent calls (device
    state, access codes, event history) execute in parallel via
    asyncio tasks, reducing total poll time compared to the previous
    sequential executor approach.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        api_key: str,
        device_id: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        event_limit: int = DEFAULT_EVENT_LIMIT,
        webhook_active: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )
        self._device_id = device_id
        self._event_limit = min(event_limit, _MAX_STORED_EVENTS)
        self._client = AsyncSeamClient(session, api_key)
        self._reconcile_unsub: CALLBACK_TYPE | None = None
        self._webhook_active = webhook_active

        # Throttle secondary API calls that rarely yield new data.
        # 0.0 is a sentinel meaning "never fetched" — first poll always runs.
        self._last_access_code_fetch: float = 0.0
        self._last_event_fetch: float = 0.0
        # True when the current poll is a post-webhook reconciliation
        # (only device state needed — webhook already delivered the event).
        self._is_reconciliation = False

        self.data = SeamLockData()
        self._event_listeners: list[callback] = []
        # Persistent set of event IDs for O(1) webhook dedup.
        self._event_id_cache: set[str] = set()
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
    def client(self) -> AsyncSeamClient:
        """Public access to the async API client for lock/unlock commands."""
        return self._client

    def shutdown(self) -> None:
        """Release all resources.  Called from ``async_unload_entry``."""
        # Cancel any pending reconciliation timer
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None

        # Drop listener references and caches
        self._event_listeners.clear()
        self._event_id_cache.clear()
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

        # Dedup via persistent cache — O(1) lookup instead of rebuilding
        # a set from self.data.events on every webhook.
        eid = entry["event_id"]
        if eid not in self._event_id_cache:
            self.data.events.insert(0, entry)
            self._event_id_cache.add(eid)
            if len(self.data.events) > _MAX_STORED_EVENTS:
                del self.data.events[_MAX_STORED_EVENTS:]
            # Prune cache when it grows too large
            if len(self._event_id_cache) > _MAX_STORED_EVENTS * 2:
                self._event_id_cache = {
                    e.get("event_id")
                    for e in self.data.events
                    if e.get("event_id")
                }

        # Mark as dispatched so the polling path doesn't re-fire it
        self._dispatched_event_ids.add(eid)
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
        """Schedule a single delayed API poll.  Self-debouncing.

        Sets ``_is_reconciliation`` so the poll only fetches device state
        (the webhook already delivered the event and access codes don't
        change on lock events).
        """
        if self._reconcile_unsub is not None:
            self._reconcile_unsub()
            self._reconcile_unsub = None

        @callback
        def _do_reconcile(_now: datetime) -> None:
            self._reconcile_unsub = None
            self._is_reconciliation = True
            self.hass.async_create_task(self.async_request_refresh())

        self._reconcile_unsub = async_call_later(
            self.hass, _RECONCILE_DELAY_SECONDS, _do_reconcile
        )

    # -- Full polling path -----------------------------------------------------

    async def _async_update_data(self) -> SeamLockData:
        """Full API poll — fully async, zero executor threads.

        All Seam API calls use aiohttp and run natively on the event
        loop.  Independent calls (device, codes, events) execute in
        parallel via asyncio tasks, reducing total poll time compared
        to the previous sequential executor approach.
        """
        try:
            async with asyncio.timeout(_POLL_TIMEOUT_SECONDS):
                return await self._async_poll()
        except TimeoutError:
            _LOGGER.warning(
                "Seam API poll timed out after %ds",
                _POLL_TIMEOUT_SECONDS,
            )
            raise UpdateFailed(
                f"Poll timed out after {_POLL_TIMEOUT_SECONDS}s"
            ) from None
        finally:
            self._is_reconciliation = False

    async def _async_poll(self) -> SeamLockData:
        """Execute all API fetches and unpack results.

        A ``finally`` block ensures every task created here is cancelled
        on *any* exit — normal return, device failure, or parent timeout.
        This prevents orphaned tasks that would leak HTTP connections and
        produce ``Task was destroyed but it is pending`` warnings.
        """
        prev = self.data
        now = time.monotonic()
        reconciling = self._is_reconciliation

        # -- Start all API calls concurrently --------------------------------
        device_task = asyncio.create_task(
            self._client.get_device(self._device_id),
            name="seam_poll_device",
        )

        codes_task: asyncio.Task[list[dict[str, Any]]] | None = None
        events_task: asyncio.Task[list[dict[str, Any]]] | None = None

        if not reconciling:
            needs_codes = (
                self._last_access_code_fetch == 0.0
                or now - self._last_access_code_fetch
                >= _ACCESS_CODE_REFRESH_SECONDS
            )
            skip_events = (
                self._webhook_active
                and self._last_event_fetch != 0.0
                and now - self._last_event_fetch < _EVENT_REFRESH_SECONDS
            )

            if needs_codes:
                codes_task = asyncio.create_task(
                    self._client.list_access_codes(self._device_id),
                    name="seam_poll_codes",
                )
            if not skip_events:
                events_task = asyncio.create_task(
                    self._fetch_events_async(),
                    name="seam_poll_events",
                )

        try:
            # -- Device state (required — failure aborts) --------------------
            try:
                device = await device_task
            except Exception as err:
                raise UpdateFailed(
                    f"Device poll failed: {err}"
                ) from err

            # Unpack device state
            prev.device_name = (
                device.get("display_name") or prev.device_name
            )
            props = device.get("properties") or {}
            prev.locked = props.get("locked", prev.locked)
            prev.online = props.get("online", prev.online)

            model_obj = props.get("model")
            if isinstance(model_obj, dict):
                mdname = model_obj.get("display_name")
                if mdname:
                    prev.model_display_name = mdname

            battery = props.get("battery")
            if isinstance(battery, dict):
                level = battery.get("level")
                if level is not None:
                    prev.battery_level = round(level * 100, 1)
                bstatus = battery.get("status")
                if bstatus is not None:
                    prev.battery_status = bstatus
            else:
                raw = props.get("battery_level")
                if raw is not None:
                    prev.battery_level = round(raw * 100, 1)

            prev.door_open = props.get("door_open", prev.door_open)

            # Reconciliation only needs device state — the webhook
            # already delivered the event and access codes don't change.
            if reconciling:
                self._derive_summary(prev)
                return prev

            # -- Access codes (non-fatal) ------------------------------------
            if codes_task:
                try:
                    codes = await codes_task
                    prev.access_codes = {
                        c["access_code_id"]: c.get("name")
                        or f"Unnamed Code ({c['access_code_id'][:8]})"
                        for c in codes
                    }
                    self._last_access_code_fetch = now
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Access codes fetch failed: %s", err)

            # -- Events (non-fatal) ------------------------------------------
            if events_task:
                try:
                    api_events = await events_task
                    prev.events = self._merge_events(
                        prev.events, api_events
                    )
                    self._dispatch_new_events(api_events)
                    self._last_event_fetch = now
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Events fetch failed: %s", err)

            self._derive_summary(prev)
            return prev

        finally:
            # Cancel any in-flight tasks on *any* exit (error, timeout,
            # or normal return after reconciliation skipped optional tasks).
            for task in (device_task, codes_task, events_task):
                if task is not None and not task.done():
                    task.cancel()

    # -- Async API call wrappers -----------------------------------------------

    async def _fetch_events_async(self) -> list[dict[str, Any]]:
        """Fetch and normalise events via the async API client."""
        since_dt = datetime.now(timezone.utc) - timedelta(
            days=_EVENT_LOOKBACK_DAYS
        )
        since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        all_event_types = [
            "lock.unlocked", "lock.locked", "lock.access_denied",
            *DEVICE_EVENT_TYPES,
        ]

        raw = await self._client.list_events(
            device_id=self._device_id,
            event_types=all_event_types,
            since=since_str,
            limit=self._event_limit,
        )

        normalised: list[dict[str, Any]] = []
        for ev in raw:
            try:
                normalised.append(self._normalise_event(ev))
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
            # Deterministic fallback: the same physical event always
            # produces the same synthetic ID regardless of how many
            # times it is fetched from the API.  Without this, every
            # poll generates a new random ID for the same event,
            # causing _dispatch_new_events to re-fire it as "new".
            sig = (
                f"{raw.get('event_type', '')}:"
                f"{occurred_dt.isoformat()}:"
                f"{method_raw or ''}:"
                f"{access_code_id or ''}"
            )
            event_id = f"syn_{hashlib.sha256(sig.encode()).hexdigest()[:16]}"

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
        """Recompute summary fields using HA's configured timezone.

        Computes the UTC boundary for "today" once, then uses simple
        datetime comparison per event instead of per-event astimezone()
        calls — avoids repeated timezone conversion overhead.
        """
        local_now = dt_util.now()
        # Start-of-today in local tz, converted to UTC for direct comparison
        today_start_local = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_start_utc = today_start_local.astimezone(timezone.utc)

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
                if occurred_dt >= today_start_utc:
                    unlocks_today += 1
                elif found_unlock and found_lock:
                    # Events are sorted newest-first; once we pass today
                    # and have both summaries, no useful work remains.
                    break

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
