"""DataUpdateCoordinator for the Seam Lock integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
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

# Timeout for individual Seam API calls — (connect, read) tuple.
# Connect covers DNS + TCP + TLS handshake; read covers waiting for
# the response body.  Keeping connect short ensures a DNS hang or
# unreachable host fails fast instead of blocking an executor thread.
_API_TIMEOUT: tuple[int, int] = (5, 10)

# Async-level ceiling for the entire poll cycle (all API calls combined).
# If exceeded, the _abort event is set so the executor thread stops
# between API calls rather than continuing the full sequence.
_POLL_TIMEOUT_SECONDS = 30

# How often to re-fetch access codes (seconds).  Access codes rarely
# change, so polling them every cycle wastes an API call + thread time.
_ACCESS_CODE_REFRESH_SECONDS = 300  # 5 min

# How often to re-fetch event history when webhooks deliver events in
# real-time (seconds).  Set to 0 to disable throttling.
_EVENT_REFRESH_SECONDS = 300  # 5 min


def _create_seam_with_timeout(api_key: str) -> Seam:
    """Create a Seam client with enforced request-level timeouts.

    The Seam SDK uses ``requests.Session`` internally but does not set
    a default timeout.  Without one, any HTTP call can block an executor
    thread indefinitely if the Seam API is slow or unreachable.

    We monkey-patch ``Session.request`` so that **every** call made
    through this client has a ceiling of ``_API_TIMEOUT``.
    """
    seam = Seam(api_key=api_key)
    _orig_request = seam.client.request

    def _timeout_request(method: str, url: str, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _API_TIMEOUT
        return _orig_request(method, url, **kwargs)

    seam.client.request = _timeout_request  # type: ignore[assignment]
    return seam


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
    """Coordinate Seam API polling and webhook-delivered updates."""

    def __init__(
        self,
        hass: HomeAssistant,
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
        self._api_key = api_key
        self._device_id = device_id
        self._event_limit = min(event_limit, _MAX_STORED_EVENTS)
        self._seam: Seam | None = None
        self._reconcile_unsub: CALLBACK_TYPE | None = None
        self._poll_lock = threading.Lock()
        # Set by the async side on timeout so the executor thread stops
        # between API calls instead of running the full sequence.
        self._abort = threading.Event()
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
    def seam(self) -> Seam:
        """Lazy Seam client — created once, reused for all API calls."""
        if self._seam is None:
            self._seam = _create_seam_with_timeout(self._api_key)
        return self._seam

    def shutdown(self) -> None:
        """Release all resources.  Called from ``async_unload_entry``."""
        # Signal any in-flight executor job to stop early
        self._abort.set()

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
        change on lock events).  This reduces the reconciliation from
        3 API calls to 1, cutting executor thread occupation by ~2/3.
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
        """Full API poll.

        All synchronous Seam API calls run inside a **single** executor
        job (``_poll_all``) so only one thread is occupied per cycle.

        ``_poll_all`` acquires ``_poll_lock`` (a ``threading.Lock``) so
        that at most one executor thread performs API calls at any time.
        If a previous thread is still running (e.g. surviving an async
        timeout), the new thread returns ``None`` immediately instead
        of piling up — this prevents the thread-pool starvation that
        causes UI freezes on constrained hardware like the HA Yellow.

        On timeout, ``_abort`` is set so the executor thread stops
        between API calls rather than continuing the full sequence.
        This limits post-timeout thread occupation to at most one
        in-flight HTTP call (~10 s) instead of all remaining calls.
        """
        self._abort.clear()
        try:
            result = await asyncio.wait_for(
                self.hass.async_add_executor_job(self._poll_all),
                timeout=_POLL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._abort.set()
            _LOGGER.warning(
                "Seam API poll timed out after %ds; signalled executor "
                "thread to stop after its current HTTP call",
                _POLL_TIMEOUT_SECONDS,
            )
            raise UpdateFailed(
                f"Poll timed out after {_POLL_TIMEOUT_SECONDS}s"
            ) from None
        finally:
            self._is_reconciliation = False

        if result is None:
            _LOGGER.debug(
                "Poll skipped — previous executor thread still active"
            )
            return self.data

        prev = self.data

        # -- Unpack device (required) --------------------------------------
        device = result.get("device")
        device_error = result.get("device_error")
        if device_error is not None:
            raise UpdateFailed(
                f"Device poll failed: {device_error}"
            ) from device_error

        prev.device_name = (
            getattr(device, "display_name", None) or prev.device_name
        )
        props = device.properties
        prev.locked = getattr(props, "locked", prev.locked)
        prev.online = getattr(props, "online", prev.online)

        model_obj = getattr(props, "model", None)
        if model_obj:
            mdname = getattr(model_obj, "display_name", None)
            if mdname:
                prev.model_display_name = mdname

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

        # -- Unpack access codes (non-fatal) -------------------------------
        codes = result.get("codes")
        if codes is not None:
            prev.access_codes = {
                c.access_code_id: c.name
                or f"Unnamed Code ({c.access_code_id[:8]})"
                for c in codes
            }

        # -- Unpack events (non-fatal) -------------------------------------
        api_events = result.get("events")
        if api_events is not None:
            prev.events = self._merge_events(prev.events, api_events)
            self._dispatch_new_events(api_events)

        self._derive_summary(prev)
        return prev

    # -- API call wrappers -----------------------------------------------------

    def _poll_all(self) -> dict[str, Any] | None:
        """Run all Seam API calls in a single executor job.

        Acquires ``_poll_lock`` to ensure only one executor thread
        performs API calls at a time.  Returns ``None`` (without
        blocking) if another thread already holds the lock — the async
        caller treats this as "previous poll still running, skip".

        Checks ``_abort`` between calls so the thread stops quickly
        when the async side has timed out.  Reconciliation polls
        (after a webhook) only fetch device state.  Access codes
        and event history are independently throttled by time.
        """
        if not self._poll_lock.acquire(blocking=False):
            return None
        try:
            result: dict[str, Any] = {}
            now = time.monotonic()
            reconciling = self._is_reconciliation

            # Device state (required — failure aborts the poll)
            try:
                result["device"] = self.seam.devices.get(
                    device_id=self._device_id
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Device poll failed: %s", err)
                result["device_error"] = err
                return result

            # Reconciliation only needs device state — the webhook
            # already delivered the event and access codes don't change.
            if reconciling:
                return result

            # Abort check: async side timed out, stop making calls
            if self._abort.is_set():
                return result

            # Access codes — throttled (rarely change)
            needs_codes = (
                self._last_access_code_fetch == 0.0
                or now - self._last_access_code_fetch
                >= _ACCESS_CODE_REFRESH_SECONDS
            )
            if needs_codes:
                try:
                    result["codes"] = self.seam.access_codes.list(
                        device_id=self._device_id
                    )
                    self._last_access_code_fetch = now
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Access codes fetch failed: %s", err)

            # Abort check
            if self._abort.is_set():
                return result

            # Events — throttled when webhooks deliver them in real-time
            skip_events = (
                self._webhook_active
                and self._last_event_fetch != 0.0
                and now - self._last_event_fetch < _EVENT_REFRESH_SECONDS
            )
            if not skip_events:
                try:
                    result["events"] = self._fetch_events()
                    self._last_event_fetch = now
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Events fetch failed: %s", err)

            return result
        finally:
            self._poll_lock.release()

    def _fetch_events(self) -> list[dict[str, Any]]:
        """Fetch events via a single batch API call.

        Uses the ``event_types`` (plural) parameter supported by
        seam >= 0.24.0.  No fallback to individual per-type calls —
        those would multiply executor thread occupation and are the
        main cause of thread-pool starvation on constrained hardware.
        If this call fails, the caller's blanket except skips events
        for this cycle; they'll be picked up on the next poll.
        """
        since_dt = datetime.now(timezone.utc) - timedelta(
            days=_EVENT_LOOKBACK_DAYS
        )
        since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        all_event_types = [
            "lock.unlocked", "lock.locked", "lock.access_denied",
            *DEVICE_EVENT_TYPES,
        ]

        raw = self.seam.events.list(
            device_id=self._device_id,
            event_types=all_event_types,
            since=since_str,
            limit=self._event_limit,
        )

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
