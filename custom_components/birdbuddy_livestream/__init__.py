"""The Bird Buddy Livestream integration."""

from __future__ import annotations

import logging

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import AuthenticationFailedError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BirdBuddyWatcher
from .const import DOMAIN

LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CAMERA]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    client = BirdBuddy(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])

    try:
        if not await client.refresh():
            raise ConfigEntryAuthFailed("Could not sign in to Bird Buddy")
    except ConfigEntryAuthFailed:
        raise
    except (AuthenticationFailedError, KeyError) as err:
        # A rejected sign-in surfaces as a KeyError, because pybirdbuddy reads
        # the token from a response that is a Problem rather than an Auth.
        raise ConfigEntryAuthFailed(
            f"Bird Buddy rejected the sign-in: {err}"
        ) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Bird Buddy is unreachable: {err}") from err

    if not client.feeders:
        raise ConfigEntryNotReady("No feeders found on this account")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = BirdBuddyWatcher(
        client, async_get_clientsession(hass)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear the integration down."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unloaded:
        watcher: BirdBuddyWatcher = hass.data[DOMAIN].pop(entry.entry_id)
        # Do not leave the feeder streaming when Home Assistant unloads us.
        try:
            await watcher.async_stop()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Cleaning up the watching session failed", exc_info=True)

    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
