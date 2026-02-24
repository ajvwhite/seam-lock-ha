"""Seam Smart Lock integration for Home Assistant.

A integration to use Seam to access Smart Locks to expose more comprehensive
information, this was initially created to fix limitations in direct
manufacturer implementations.

Author: ajvwhite

Supports both polling (fallback) and Seam webhooks (instant updates).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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
)
from .coordinator import SeamLockCoordinator

_LOGGER = logging.getLogger(__name__)

_PN_ID_PREFIX = "seam_lock_ha_webhook_"


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
    return f"seam_lock_ha_{entry.entry_id}"


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

        # -- Signature verification --------------------------------------------
        # Use the Seam SDK's built-in SeamWebhook verifier when a secret
        # is configured.  It handles Svix HMAC-SHA256 + timestamp replay
        # protection internally and returns a SeamEvent object.
        if secret:
            try:
                from seam import SeamWebhook

                verifier = SeamWebhook(secret)
                headers = {k: v for k, v in request.headers.items()}
                verified_event = verifier.verify(
                    raw_body.decode("utf-8"), headers
                )
                # Build a plain dict from the verified SeamEvent for the
                # coordinator (which operates on dicts, not SeamEvent objects)
                payload = {
                    "event_id": getattr(verified_event, "event_id", None),
                    "event_type": getattr(verified_event, "event_type", None),
                    "occurred_at": getattr(verified_event, "occurred_at", None),
                    "created_at": getattr(verified_event, "created_at", None),
                    "device_id": getattr(verified_event, "device_id", None),
                    "method": getattr(verified_event, "method", None),
                    "access_code_id": getattr(
                        verified_event, "access_code_id", None
                    ),
                }
                _LOGGER.debug("Seam webhook: SDK signature verified OK")
            except ImportError:
                _LOGGER.warning(
                    "seam SDK not available for verification, "
                    "falling back to unverified"
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Seam webhook: signature verification FAILED: %s", err
                )
                return Response(status=401, text="Invalid signature")
        else:
            _LOGGER.debug(
                "Seam webhook: no secret configured, skipping verification"
            )

        # -- Extract event data ------------------------------------------------
        # The Seam/Svix webhook body IS the flat event object — confirmed by
        # SDK source (SeamWebhook.verify passes the body to
        # SeamEvent.from_dict directly).
        #
        # Structure from Seam API docs:
        #   {
        #     "event_id": "...",
        #     "event_type": "lock.unlocked",
        #     "occurred_at": "2026-02-24T20:00:00.123Z",
        #     "created_at": "...",
        #     "device_id": "...",
        #     "method": "keycode",         // for lock.locked/unlocked
        #     "access_code_id": "...",      // when code used
        #     ...
        #   }

        event_type = payload.get("event_type", "")
        if not event_type:
            _LOGGER.debug("Seam webhook: no event_type found, ignoring")
            _LOGGER.debug("Seam webhook payload keys: %s", list(payload.keys()))
            return Response(status=200, text="OK")

        _LOGGER.info(
            "Seam webhook received: type=%s, event_id=%s, device_id=%s, "
            "occurred_at=%s, method=%s, access_code_id=%s",
            event_type,
            payload.get("event_id", "MISSING"),
            payload.get("device_id", "MISSING"),
            payload.get("occurred_at", "MISSING"),
            payload.get("method", "NONE"),
            payload.get("access_code_id", "NONE"),
        )

        # Delegate to coordinator (patches data + schedules reconcile)
        coordinator.handle_webhook_event(payload)

        return Response(status=200, text="OK")

    return _handle_webhook
