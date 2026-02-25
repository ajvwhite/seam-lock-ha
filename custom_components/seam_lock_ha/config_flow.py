"""Config flow for Seam Lock integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_API_KEY,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_EVENT_LIMIT,
    CONF_POLL_INTERVAL,
    CONF_WEBHOOK_SECRET,
    DEFAULT_EVENT_LIMIT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class SeamLockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Seam Lock."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._api_key: str | None = None
        self._devices: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 -- enter Seam API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY]
            try:
                from .coordinator import _create_seam_with_timeout

                seam = _create_seam_with_timeout(api_key)
                try:
                    devices = await self.hass.async_add_executor_job(seam.locks.list)
                finally:
                    # Close the validation client's HTTP session immediately
                    # to avoid leaking TCP sockets from the connection pool.
                    try:
                        seam.client.close()
                    except Exception:  # noqa: BLE001
                        pass

                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._api_key = api_key
                    self._devices = [
                        {
                            "device_id": d.device_id,
                            "name": getattr(d, "display_name", None)
                            or d.device_id,
                            "type": getattr(d, "device_type", "unknown"),
                        }
                        for d in devices
                    ]
                    return await self.async_step_select_device()
            except Exception as err:
                _LOGGER.error("Failed to connect to Seam API: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2 -- pick a lock from the discovered devices.

        The device name from the Seam API is stored as CONF_DEVICE_NAME
        and used as the initial HA device registry name. The user can
        rename it afterward in the HA UI (Settings > Devices).
        """
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            device_name = next(
                (
                    d["name"]
                    for d in self._devices
                    if d["device_id"] == device_id
                ),
                "Seam Lock",
            )

            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_API_KEY: self._api_key,
                    CONF_DEVICE_ID: device_id,
                    CONF_DEVICE_NAME: device_name,
                },
                options={
                    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                    CONF_EVENT_LIMIT: DEFAULT_EVENT_LIMIT,
                    CONF_WEBHOOK_SECRET: "",
                },
            )

        device_options = {
            d["device_id"]: f"{d['name']} ({d['type']})"
            for d in self._devices
        }
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_ID): vol.In(device_options)}
            ),
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SeamLockOptionsFlow:
        """Return the options flow handler."""
        return SeamLockOptionsFlow(config_entry)


class SeamLockOptionsFlow(config_entries.OptionsFlow):
    """Handle options -- polling interval, event limit, webhook secret."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_POLL_INTERVAL,
                        default=current.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=10, max=600)),
                    vol.Optional(
                        CONF_EVENT_LIMIT,
                        default=current.get(
                            CONF_EVENT_LIMIT, DEFAULT_EVENT_LIMIT
                        ),
                    ): vol.All(int, vol.Range(min=5, max=100)),
                    vol.Optional(
                        CONF_WEBHOOK_SECRET,
                        default=current.get(CONF_WEBHOOK_SECRET, ""),
                    ): str,
                }
            ),
        )
