"""Config flow for the Bird Buddy Livestream integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import AuthenticationFailedError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTO_OFF,
    CONF_GO2RTC_INPUT,
    CONF_GO2RTC_RTSP_PORT,
    CONF_GO2RTC_URL,
    CONF_START_TIMEOUT,
    CONF_TRANSCODE,
    DEFAULT_AUTO_OFF,
    DEFAULT_GO2RTC_RTSP_PORT,
    DEFAULT_START_TIMEOUT,
    DEFAULT_TRANSCODE,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class BirdBuddyStreamConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for Bird Buddy credentials."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial sign-in step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            client = BirdBuddy(email, user_input[CONF_PASSWORD])
            try:
                ok = await client.refresh()
            except (AuthenticationFailedError, KeyError) as err:
                # authEmailSignIn returns a union: an Auth object on success, a
                # Problem on rejection. pybirdbuddy reaches straight for the
                # token, so a rejected sign-in surfaces as a KeyError.
                LOGGER.error(
                    "Bird Buddy rejected the sign-in for %s: %s. "
                    "Note that accounts created through Google or Facebook "
                    "cannot be used. Enable debug logging on the 'birdbuddy' "
                    "logger to see the reason returned by the server.",
                    email,
                    err,
                )
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Could not reach Bird Buddy")
                errors["base"] = "cannot_connect"
            else:
                if not ok:
                    errors["base"] = "invalid_auth"
                elif not client.feeders:
                    errors["base"] = "no_feeders"
                else:
                    return self.async_create_entry(title=email, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this entry."""
        return BirdBuddyStreamOptionsFlow()


class BirdBuddyStreamOptionsFlow(OptionsFlow):
    """Expose the timing and go2rtc options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_AUTO_OFF,
                    default=options.get(CONF_AUTO_OFF, DEFAULT_AUTO_OFF),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=30,
                        max=1800,
                        step=30,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_START_TIMEOUT,
                    default=options.get(CONF_START_TIMEOUT, DEFAULT_START_TIMEOUT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=30,
                        max=300,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_GO2RTC_URL,
                    default=options.get(CONF_GO2RTC_URL, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(
                    CONF_GO2RTC_RTSP_PORT,
                    default=options.get(
                        CONF_GO2RTC_RTSP_PORT, DEFAULT_GO2RTC_RTSP_PORT
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=65535, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_GO2RTC_INPUT,
                    default=options.get(CONF_GO2RTC_INPUT, ""),
                ): TextSelector(),
                vol.Required(
                    CONF_TRANSCODE,
                    default=options.get(CONF_TRANSCODE, DEFAULT_TRANSCODE),
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
