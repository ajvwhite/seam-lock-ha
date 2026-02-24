"""Seam Smart Lock integration for Home Assistant.

A integration to use Seam to access Smart Locks to expose more comprehensive
information, this was initially created to fix limitations in direct
manufacturer implementations.

Author: ajvwhite

Supports both polling (fallback) and Seam webhooks (instant updates).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
    WATCHED_EVENT_TYPES,
    WEBHOOK_TIMESTAMP_TOLERANCE,
)
from .coordinator import SeamLockCoordinator

_LOGGER = logging.getLogger(__name__)

_PN_ID_PREFIX = "seam_lock_webhook_"


# -- Runtime data (stored on entry.runtime_data) -------------------------------


@dataclass
class SeamLockRuntimeData:
    """Runtime data for a single config entry."""

    coordinator: SeamLockCoordinator
    webhook_secret: str = ""
    webhook_url: str = ""


type SeamLockConfigEntry = ConfigEntry[SeamLockRuntimeData]


# -- Helpers -------------------------------------------------------------------


def _webhook_id(entry: ConfigEntry) -> str:
    """Deterministic webhook id derived from the config entry id."""
    return f"seam_lock_{entry.entry_id}"


def _pn_id(entry: ConfigEntry) -> str:
    """Persistent notification id."""
    return f"{_PN_ID_PREFIX}{entry.entry_id}"


def _get_webhook_url(hass: HomeAssistant, wh_id: str) -> str:
    """Build the full webhook URL from HA's configured external URL."""
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
        DEFAULT_POLL_INTERVAL_WEBHOOK if webhook_secret else DEFAULT_POLL_INTERVAL
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

    # Register webhook endpoint
    wh_id = _webhook_id(entry)
    webhook_register(
        hass,
        DOMAIN,
        "Seam Lock",
        wh_id,
        _build_webhook_handler(entry.entry_id),
    )
    webhook_url = _get_webhook_url(hass, wh_id)
    _LOGGER.info("Seam Lock webhook endpoint: %s", webhook_url)

    # Store runtime data on the config entry (modern HA pattern)
    entry.runtime_data = SeamLockRuntimeData(
        coordinator=coordinator,
        webhook_secret=webhook_secret,
        webhook_url=webhook_url,
    )

    # Persistent notification: show only when secret is NOT configured
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
                f"`device.disconnected`, `device.low_battery`\n\n"
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
    """Unload a config entry."""
    webhook_unregister(hass, _webhook_id(entry))
    pn_dismiss(hass, _pn_id(entry))
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: SeamLockConfigEntry
) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# -- Webhook handler -----------------------------------------------------------


def _build_webhook_handler(entry_id: str):
    """Return an async webhook handler closure bound to a config entry."""

    async def _handle_webhook(
        hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response:
        # Locate runtime data via the config entry
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or not hasattr(entry, "runtime_data"):
            return Response(status=404, text="Integration not loaded")

        runtime: SeamLockRuntimeData = entry.runtime_data
        coordinator = runtime.coordinator
        secret = runtime.webhook_secret

        # -- Parse body --------------------------------------------------------
        try:
            raw_body = await request.read()
            payload: dict[str, Any] = json.loads(raw_body)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Seam webhook: invalid JSON body")
            return Response(status=400, text="Invalid JSON")

        # -- Signature verification (when secret is configured) ----------------
        if secret:
            svix_id = request.headers.get("svix-id", "")
            svix_ts = request.headers.get("svix-timestamp", "")
            svix_sig = request.headers.get("svix-signature", "")

            if not _verify_svix_signature(
                raw_body, secret, svix_id, svix_ts, svix_sig
            ):
                _LOGGER.warning(
                    "Seam webhook: signature verification FAILED"
                )
                return Response(status=401, text="Invalid signature")
            _LOGGER.debug("Seam webhook: signature verified")
        else:
            _LOGGER.debug(
                "Seam webhook: no secret configured, skipping verification"
            )

        # -- Extract event data ------------------------------------------------
        event_data = payload.get("data", payload)
        event_type = (
            event_data.get("event_type")
            or payload.get("event_type")
            or payload.get("type", "")
        )
        if not event_type:
            _LOGGER.debug("Seam webhook: no event_type, ignoring")
            return Response(status=200, text="OK")

        event_data["event_type"] = event_type
        _LOGGER.info("Seam webhook received: %s", event_type)

        # Delegate to coordinator (patches data + schedules reconcile)
        coordinator.handle_webhook_event(event_data)

        return Response(status=200, text="OK")

    return _handle_webhook


# -- Svix signature verification -----------------------------------------------


def _verify_svix_signature(
    raw_body: bytes,
    secret: str,
    msg_id: str,
    timestamp: str,
    signatures: str,
) -> bool:
    """Verify a Svix HMAC-SHA256 webhook signature with replay protection."""
    if not (msg_id and timestamp and signatures):
        _LOGGER.debug("Svix headers incomplete")
        return False

    # Timestamp replay protection
    try:
        ts_int = int(timestamp)
        now = int(time.time())
        if abs(now - ts_int) > WEBHOOK_TIMESTAMP_TOLERANCE:
            _LOGGER.warning(
                "Svix timestamp too old/future (%s vs now %s)", ts_int, now
            )
            return False
    except (ValueError, TypeError):
        _LOGGER.debug("Svix timestamp not a valid integer")
        return False

    # HMAC verification
    try:
        key_b64 = secret.removeprefix("whsec_")
        key = base64.b64decode(key_b64)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Could not decode webhook secret")
        return False

    signed_content = f"{msg_id}.{timestamp}.".encode() + raw_body
    expected = hmac.new(key, signed_content, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected).decode()

    for sig in signatures.split(" "):
        parts = sig.split(",", 1)
        if len(parts) == 2 and parts[0] == "v1":
            if hmac.compare_digest(parts[1], expected_b64):
                return True

    return False
