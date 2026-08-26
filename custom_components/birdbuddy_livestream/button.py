"""Button that wakes the feeder ahead of time."""

from __future__ import annotations

from typing import Any

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import BirdBuddyWatcher
from .const import DOMAIN

LOGGER = logging.getLogger(__name__)

CAMERA_TURN_ON = "turn_on"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one prepare button per feeder."""
    watcher: BirdBuddyWatcher = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        BirdBuddyPrepareButton(feeder)
        for feeder in watcher.client.feeders.values()
        if not feeder.get("supportsWebRTC")
    )


class BirdBuddyPrepareButton(ButtonEntity):
    """Starts the session in advance, so the stream opens on the first click.

    Waking the feeder takes longer than Home Assistant waits during a stream
    request. Pressing this a minute beforehand, or calling it from an
    automation, removes that wait.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "prepare_livestream"
    _attr_icon = "mdi:motion-play-outline"

    def __init__(self, feeder: Any) -> None:
        """Initialise the button."""
        self._feeder_id = feeder.id
        self._attr_unique_id = f"{feeder.id}_prepare_livestream"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, feeder.id)},
            manufacturer="Bird Buddy",
            name=feeder.name,
            model=feeder.get("version"),
            sw_version=feeder.get("firmwareVersion"),
        )

    async def async_press(self) -> None:
        """Turn the matching camera on, which starts the session."""
        registry = er.async_get(self.hass)
        camera_id = registry.async_get_entity_id(
            Platform.CAMERA, DOMAIN, f"{self._feeder_id}_livestream"
        )

        if camera_id is None:
            LOGGER.warning(
                "No camera entity found for feeder %s", self._feeder_id
            )
            return

        await self.hass.services.async_call(
            Platform.CAMERA, CAMERA_TURN_ON, {"entity_id": camera_id}, blocking=False
        )
