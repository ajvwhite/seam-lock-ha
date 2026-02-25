"""Seam Smart Lock integration for Home Assistant.

Author: ajvwhite

Supports both polling (fallback) and Seam webhooks (instant updates).
Hardened against webhook flooding and resource exhaustion.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp.web import Request, Response

from homeassistant.components.persistent_notification import (
    async_create as pn_create,
    async_dismiss as pn_dismiss,
)
from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_API_KEY,
    CONF_DEVICE_ID,
    CONF_EVENT_LIMIT,
    CONF_POLL_INTERVAL,
    CONF_WEBHOOK_SECRET,
    DEFAULT_EVENT_LIMIT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_WEBHOOK,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import SeamLockCoordinator

_LOGGER = logging.getLogger(__name__)

_PN_ID_PREFIX = "seam_lock_ha_webhook_"

# -- Webhook hardening constants -----------------------------------------------
_MAX_BODY_BYTES = 65_536          # 64 KB (a Seam event is ~1 KB)
_RATE_LIMIT_MAX = 30              # max events accepted per window
_RATE_LIMIT_WINDOW = 60           # seconds

# Sentinel: verifier was attempted but import/init failed — don't retry.
_VERIFIER_FAILED: object = object()


# -- Runtime data (stored on entry.runtime_data) -------------------------------


@dataclass
class SeamLockRuntimeData:
    """Runtime data for a single config entry."""

    coordinator: SeamLockCoordinator
    webhook_secret: str = ""
    webhook_url: str = ""

    # Cached verifier — created once, reused for all requests.
    # None = not yet attempted, _VERIFIER_FAILED = import/init failed.
    _verifier: Any = field(default=None, repr=False)

    # Rate limiter state
    _rate_timestamps: list[float] = field(default_factory=list, repr=False)
    _rate_limit_warned: bool = field(default=False, repr=False)

    def get_verifier(self) -> Any | None:
        """Return a cached SeamWebhook verifier (created once).

        Returns ``None`` and caches the failure if the SDK cannot be
        imported or the verifier cannot be constructed — avoids retrying
        a doomed import on every webhook.
        """
        if self._verifier is _VERIFIER_FAILED:
            return None
        if self._verifier is not None:
            return self._verifier
        if not self.webhook_secret:
            return None
        try:
            from seam import SeamWebhook

            self._verifier = SeamWebhook(self.webhook_secret)
            return self._verifier
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not create SeamWebhook verifier")
            self._verifier = _VERIFIER_FAILED
            return None

    def check_rate_limit(self) -> bool:
        """Return True if within rate limit, False if exceeded.

        Logs a warning on the *first* rejection only; suppresses
        subsequent warnings until the window resets.
        """
        now = time.monotonic()
        cutoff = now - _RATE_LIMIT_WINDOW

        # Prune old timestamps
        self._rate_timestamps = [
            t for t in self._rate_timestamps if t > cutoff
        ]

        if len(self._rate_timestamps) >= _RATE_LIMIT_MAX:
            if not self._rate_limit_warned:
                _LOGGER.warning(
                    "Seam webhook rate limit exceeded (%d/%ds) — "
                    "rejecting further requests until window resets",
                    _RATE_LIMIT_MAX,
                    _RATE_LIMIT_WINDOW,
                )
                self._rate_limit_warned = True
            return False

        # Under limit — reset warning flag so next burst logs once
        self._rate_limit_warned = False
        self._rate_timestamps.append(now)
        return True


type SeamLockConfigEntry = ConfigEntry[SeamLockRuntimeData]


# -- Helpers -------------------------------------------------------------------


def _webhook_id(entry: ConfigEntry) -> str:
    return f"seam_lock_ha_{entry.entry_id}"


def _pn_id(entry: ConfigEntry) -> str:
    return f"{_PN_ID_PREFIX}{entry.entry_id}"


def _get_webhook_url(hass: HomeAssistant, wh_id: str) -> str:
    try:
        from homeassistant.helpers.network import get_url

        base = get_url(hass, allow_internal=False, prefer_external=True)
    except Exception:  # noqa: BLE001
        base = (
            hass.config.external_url
            or hass.config.internal_url
            or "http://homeassistant.local:8123"
        )
    return f"{base}/api/webhook/{wh_id}"


# -- Setup / Teardown ---------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant, entry: SeamLockConfigEntry
) -> bool:
    """Set up Seam Lock from a config entry."""
    api_key: str = entry.data[CONF_API_KEY]
    device_id: str = entry.data[CONF_DEVICE_ID]
    webhook_secret: str = entry.options.get(CONF_WEBHOOK_SECRET, "")
    event_limit: int = entry.options.get(CONF_EVENT_LIMIT, DEFAULT_EVENT_LIMIT)

    default_interval = (
        DEFAULT_POLL_INTERVAL_WEBHOOK
        if webhook_secret
        else DEFAULT_POLL_INTERVAL
    )
    poll_interval: int = entry.options.get(CONF_POLL_INTERVAL, default_interval)

    coordinator = SeamLockCoordinator(
        hass,
        api_key=api_key,
        device_id=device_id,
        poll_interval=poll_interval,
        event_limit=event_limit,
    )
    await coordinator.async_config_entry_first_refresh()

    # Register webhook
    wh_id = _webhook_id(entry)
    webhook_register(
        hass,
        DOMAIN,
        "Seam Lock",
        wh_id,
        _build_webhook_handler(entry.entry_id),
    )
    webhook_url = _get_webhook_url(hass, wh_id)
    _LOGGER.info("Seam Lock webhook endpoint registered")

    # Store runtime data
    entry.runtime_data = SeamLockRuntimeData(
        coordinator=coordinator,
        webhook_secret=webhook_secret,
        webhook_url=webhook_url,
    )
    # Pre-warm the verifier (caches success or failure)
    entry.runtime_data.get_verifier()

    if webhook_secret:
        pn_dismiss(hass, _pn_id(entry))
    else:
        pn_create(
            hass,
            (
                f"**Add this URL in the "
                f"[Seam Console Webhooks](https://console.seam.co):**\n\n"
                f"```\n{webhook_url}\n```\n\n"
                f"**Select event types:** `lock.locked`, `lock.unlocked`, "
                f"`lock.access_denied`, `device.connected`, "
                f"`device.disconnected`, `device.low_battery`, "
                f"`device.battery_status_changed`\n\n"
                f"After creating the webhook, **copy the secret** "
                f"(starts with `whsec_`) and paste it into this "
                f"integration's options:\n\n"
                f"Settings > Devices & Services > Seam Smart Lock > Configure"
            ),
            title="Seam Lock \u2014 Webhook Setup",
            notification_id=_pn_id(entry),
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SeamLockConfigEntry
) -> bool:
    """Unload — clean up webhook, timers, and HTTP session."""
    webhook_unregister(hass, _webhook_id(entry))
    pn_dismiss(hass, _pn_id(entry))

    if hasattr(entry, "runtime_data") and entry.runtime_data:
        entry.runtime_data.coordinator.shutdown()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: SeamLockConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


# -- Webhook handler -----------------------------------------------------------


def _build_webhook_handler(entry_id: str):
    """Return an async webhook handler closure bound to a config entry."""

    async def _handle_webhook(
        hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or not hasattr(entry, "runtime_data"):
            return Response(status=404, text="Not loaded")

        runtime: SeamLockRuntimeData = entry.runtime_data
        coordinator = runtime.coordinator

        # -- Rate limiting (protect event loop) --------------------------------
        if not runtime.check_rate_limit():
            return Response(status=429, text="Rate limited")

        # -- Read body with size limit -----------------------------------------
        try:
            raw_body = await request.content.read(_MAX_BODY_BYTES + 1)
            if len(raw_body) > _MAX_BODY_BYTES:
                _LOGGER.warning(
                    "Seam webhook body too large (%d bytes)", len(raw_body)
                )
                return Response(status=413, text="Payload too large")
            payload: dict[str, Any] = json.loads(raw_body)
        except json.JSONDecodeError:
            return Response(status=400, text="Invalid JSON")
        except Exception:  # noqa: BLE001
            return Response(status=400, text="Bad request")

        # -- Signature verification (cached verifier) --------------------------
        verifier = runtime.get_verifier()
        if verifier is not None:
            try:
                headers = dict(request.headers)
                verified_event = verifier.verify(
                    raw_body.decode("utf-8"), headers
                )
                payload = {
                    "event_id": getattr(verified_event, "event_id", None),
                    "event_type": getattr(
                        verified_event, "event_type", None
                    ),
                    "occurred_at": getattr(
                        verified_event, "occurred_at", None
                    ),
                    "created_at": getattr(
                        verified_event, "created_at", None
                    ),
                    "device_id": getattr(
                        verified_event, "device_id", None
                    ),
                    "method": getattr(verified_event, "method", None),
                    "access_code_id": getattr(
                        verified_event, "access_code_id", None
                    ),
                }
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Webhook signature failed: %s", err)
                return Response(status=401, text="Invalid signature")
        elif runtime.webhook_secret:
            # Secret configured but verifier init failed permanently
            return Response(status=500, text="Verifier error")

        # -- Validate and dispatch ---------------------------------------------
        event_type = payload.get("event_type", "")
        if not event_type:
            return Response(status=200, text="OK")

        _LOGGER.debug(
            "Webhook: %s event_id=%s",
            event_type,
            payload.get("event_id", "?"),
        )

        coordinator.handle_webhook_event(payload)
        return Response(status=200, text="OK")

    return _handle_webhook
