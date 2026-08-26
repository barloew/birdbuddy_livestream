"""Status sensor for the Bird Buddy livestream."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import BirdBuddyWatcher
from .const import DOMAIN, SIGNAL_STATUS, STATUS_IDLE, STATUSES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one status sensor per feeder."""
    watcher: BirdBuddyWatcher = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        BirdBuddyStatusSensor(feeder)
        for feeder in watcher.client.feeders.values()
        if not feeder.get("supportsWebRTC")
    )


class BirdBuddyStatusSensor(SensorEntity):
    """Reports where the livestream is in its lifecycle.

    Useful in automations that need to wait for the feeder to be ready before
    opening the stream, since waking it takes longer than Home Assistant is
    willing to wait during a stream request.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "livestream_status"
    # An enum sensor must declare both the device class and its options.
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_icon = "mdi:video-wireless-outline"
    _attr_options = STATUSES
    _attr_should_poll = False

    def __init__(self, feeder: Any) -> None:
        """Initialise the sensor."""
        self._feeder_id = feeder.id
        self._attr_unique_id = f"{feeder.id}_livestream_status"
        self._attr_native_value = STATUS_IDLE
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, feeder.id)},
            manufacturer="Bird Buddy",
            name=feeder.name,
            model=feeder.get("version"),
            sw_version=feeder.get("firmwareVersion"),
        )

    async def async_added_to_hass(self) -> None:
        """Listen for status updates from the camera."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STATUS.format(self._feeder_id),
                self._handle_status,
            )
        )

    @callback
    def _handle_status(self, status: str, detail: str | None) -> None:
        """Store the new status."""
        self._attr_native_value = status
        self._attr_extra_state_attributes = {"detail": detail} if detail else {}
        self.async_write_ha_state()
