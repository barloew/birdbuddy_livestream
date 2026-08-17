"""Camera platform for the Bird Buddy livestream."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .api import BirdBuddyWatcher, WatchingError
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
    STALE_CHECKS_BEFORE_REPUBLISH,
    STREAM_SOURCE_GRACE,
)
from .go2rtc import Go2RtcClient, Go2RtcError

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one camera entity per feeder."""
    watcher: BirdBuddyWatcher = hass.data[DOMAIN][entry.entry_id]

    go2rtc = _build_go2rtc(hass, entry)
    if go2rtc is not None and not await go2rtc.async_check():
        LOGGER.warning(
            "go2rtc is unreachable; falling back to the direct HLS stream"
        )

    entities: list[BirdBuddyCamera] = []
    for feeder in watcher.client.feeders.values():
        if feeder.get("supportsWebRTC"):
            # These feeders return a WebRTC config instead of a streamUrl,
            # which needs an entirely different transport.
            LOGGER.warning("Feeder %s uses WebRTC and is skipped", feeder.name)
            continue
        entities.append(BirdBuddyCamera(entry, watcher, feeder, go2rtc))

    async_add_entities(entities)


@callback
def _build_go2rtc(hass: HomeAssistant, entry: ConfigEntry) -> Go2RtcClient | None:
    """Build a go2rtc client when an address is configured."""
    base_url = (entry.options.get(CONF_GO2RTC_URL) or "").strip()
    if not base_url:
        return None

    return Go2RtcClient(
        async_get_clientsession(hass),
        base_url,
        int(entry.options.get(CONF_GO2RTC_RTSP_PORT, DEFAULT_GO2RTC_RTSP_PORT)),
    )


class BirdBuddyCamera(Camera):
    """Livestream from a Bird Buddy feeder."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF

    def __init__(
        self,
        entry: ConfigEntry,
        watcher: BirdBuddyWatcher,
        feeder: Any,
        go2rtc: Go2RtcClient | None = None,
    ) -> None:
        """Initialise the camera."""
        super().__init__()
        self._entry = entry
        self._watcher = watcher
        self._go2rtc = go2rtc
        self._feeder_id = feeder.id
        self._stream_url: str | None = None
        self._cancel_auto_off: Any = None
        self._start_task: asyncio.Task | None = None
        self._last_error: str | None = None
        # Latest URL supplied by the watcher, plus the measurements used to
        # decide whether go2rtc is still receiving data.
        self._pending_hls: str | None = None
        self._last_bytes: int | None = None
        self._stale_count = 0
        # Home Assistant calls stream_source() while the entity is being added,
        # to work out which WebRTC provider fits. Without this flag every
        # restart would wake the feeder.
        self._may_wake_feeder = False
        self._enabled = True

        self._attr_unique_id = f"{feeder.id}_livestream"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, feeder.id)},
            manufacturer="Bird Buddy",
            name=feeder.name,
            model=feeder.get("version"),
            sw_version=feeder.get("firmwareVersion"),
        )

    # -- options -----------------------------------------------------------

    @property
    def _auto_off(self) -> int:
        return int(self._entry.options.get(CONF_AUTO_OFF, DEFAULT_AUTO_OFF))

    @property
    def _start_timeout(self) -> int:
        return int(self._entry.options.get(CONF_START_TIMEOUT, DEFAULT_START_TIMEOUT))

    @property
    def _transcode(self) -> bool:
        return bool(self._entry.options.get(CONF_TRANSCODE, DEFAULT_TRANSCODE))

    @property
    def _input_template(self) -> str:
        return str(self._entry.options.get(CONF_GO2RTC_INPUT, "")).strip()

    @property
    def _go2rtc_name(self) -> str:
        return f"birdbuddy_{self._feeder_id.replace('-', '')[:12]}"

    # -- state -------------------------------------------------------------

    @property
    def _session_active(self) -> bool:
        """Return whether a watching session is currently running."""
        return self._watcher.is_active(self._feeder_id)

    @property
    def is_on(self) -> bool:
        """Return whether the camera is allowed to produce video.

        Deliberately not tied to the watching session: Home Assistant refuses
        to open the stream while is_on is False, yet the session only starts
        once the stream is opened.
        """
        return self._enabled

    @property
    def is_streaming(self) -> bool:
        """Return whether video is actually flowing."""
        return self._session_active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose diagnostics that are handy from the UI."""
        return {
            "session_active": self._session_active,
            "starting": self._start_task is not None and not self._start_task.done(),
            "last_error": self._last_error,
        }

    # -- stream ------------------------------------------------------------

    async def stream_source(self) -> str | None:
        """Return the stream URL, waking the feeder in the background if needed.

        Home Assistant aborts this call after 10s, while a sleeping feeder can
        take up to a minute. The session is therefore started as a background
        task and only waited on briefly.
        """
        if not self._may_wake_feeder:
            LOGGER.debug(
                "stream_source() during setup of %s, leaving the feeder asleep",
                self.entity_id or self._feeder_id,
            )
            return self._stream_url

        if self._session_active and self._stream_url:
            await self._async_ensure_go2rtc_stream()
            self._schedule_auto_off()
            return self._stream_url

        LOGGER.debug("stream_source() requested for %s", self._feeder_id)
        task = self._async_ensure_start_task()

        try:
            # shield so our timeout does not cancel the background task.
            async with asyncio.timeout(STREAM_SOURCE_GRACE):
                await asyncio.shield(task)
        except TimeoutError:
            LOGGER.info(
                "Feeder %s is still waking up; the session continues in the "
                "background",
                self._feeder_id,
            )
            raise HomeAssistantError(
                "The Bird Buddy is waking up. Try again in about half a minute."
            ) from None

        if not self._stream_url:
            raise HomeAssistantError(
                self._last_error or "Could not start the livestream."
            )

        self._schedule_auto_off()
        return self._stream_url

    @property
    def use_stream_for_stills(self) -> bool:
        """Let Home Assistant grab a keyframe from the running stream.

        Only meaningful while the stream runs; the feeder has no separate
        snapshot endpoint we can reach without a session.
        """
        return self._session_active

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return no still image while the feeder is asleep.

        A thumbnail is not worth waking the feeder for; that costs a
        disproportionate amount of battery.
        """
        return None

    async def async_turn_on(self) -> None:
        """Turn the camera on and start the session in the background."""
        LOGGER.debug("turn_on called for %s", self._feeder_id)
        self._enabled = True
        self._may_wake_feeder = True
        self._async_ensure_start_task()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the camera off and end any running session."""
        self._enabled = False
        await self._async_teardown()

    async def async_added_to_hass(self) -> None:
        """From now on, stream_source() calls count as real viewing requests."""
        await super().async_added_to_hass()
        self._may_wake_feeder = True
        self._watcher.register_lost_listener(
            self._feeder_id, self._handle_session_lost
        )
        self._watcher.register_url_listener(self._feeder_id, self._handle_fresh_url)

    async def async_will_remove_from_hass(self) -> None:
        """Do not leave the feeder streaming when the entity disappears."""
        self._may_wake_feeder = False
        self._watcher.unregister_listeners(self._feeder_id)
        if self._session_active:
            await self._async_teardown()
        await super().async_will_remove_from_hass()

    # -- starting ----------------------------------------------------------

    @callback
    def _async_ensure_start_task(self) -> asyncio.Task:
        """Start the session in the background, or return the running attempt."""
        if self._start_task is None or self._start_task.done():
            self._start_task = self.hass.async_create_task(
                self._async_start_session(),
                name=f"{DOMAIN} start {self._feeder_id}",
            )
        return self._start_task

    async def _async_start_session(self) -> None:
        """Set up the watching session. Does not raise; stores the error."""
        LOGGER.info(
            "Starting livestream for feeder %s (timeout %ss)",
            self._feeder_id,
            self._start_timeout,
        )
        try:
            self._stream_url = await self._watcher.async_start(
                self._feeder_id, timeout=self._start_timeout
            )
        except WatchingError as err:
            self._stream_url = None
            self._last_error = str(err)
            LOGGER.error("Could not start the livestream: %s", err)
        except Exception as err:  # noqa: BLE001
            self._stream_url = None
            self._last_error = str(err)
            LOGGER.exception("Unexpected error while starting the livestream")
        else:
            self._last_error = None
            LOGGER.info("Livestream active for feeder %s", self._feeder_id)
            await self._async_publish_to_go2rtc()
            self._schedule_auto_off()
        finally:
            self.async_write_ha_state()

    # -- go2rtc ------------------------------------------------------------

    async def _async_publish_to_go2rtc(self) -> None:
        """Hand the Kinesis stream to go2rtc, if configured.

        On failure the direct HLS URL is kept; a stuttering stream beats no
        stream at all.
        """
        if self._go2rtc is None or not self._stream_url:
            return

        try:
            self._stream_url = await self._go2rtc.async_publish(
                self._go2rtc_name,
                self._stream_url,
                self._transcode,
                self._input_template,
            )
        except Go2RtcError as err:
            LOGGER.warning("Skipping go2rtc: %s", err)
        else:
            LOGGER.info("Stream runs through go2rtc: %s", self._stream_url)

    async def _async_ensure_go2rtc_stream(self) -> None:
        """Re-register the stream if go2rtc has forgotten it.

        Streams added through the API do not end up in go2rtc.yaml, so they are
        gone after go2rtc restarts.
        """
        if self._go2rtc is None:
            return
        if not self._stream_url or not self._stream_url.startswith("rtsp://"):
            return

        session = self._watcher.active
        if session is None:
            return

        if await self._go2rtc.async_exists(self._go2rtc_name):
            return

        LOGGER.info("go2rtc no longer knows the stream, publishing it again")
        self._stream_url = session.stream_url
        await self._async_publish_to_go2rtc()

    @callback
    def _handle_fresh_url(self, hls_url: str) -> None:
        """Store a new Kinesis URL from the watcher.

        watchingStartCheck returns a new SessionToken on every call, even when
        the previous one still works. Publishing immediately would make go2rtc
        tear down the running ffmpeg process every 26 seconds, so the URL is
        kept aside and only used once the source actually stalls.
        """
        self._pending_hls = hls_url

        if self._go2rtc is None:
            # Without go2rtc, Home Assistant watches Kinesis directly and
            # stream_source() supplies the fresh URL on the next stream.
            self._stream_url = hls_url
            return

        self.hass.async_create_task(
            self._async_check_producer(),
            name=f"{DOMAIN} health {self._feeder_id}",
        )

    async def _async_check_producer(self) -> None:
        """Republish only when go2rtc stops receiving data."""
        if self._go2rtc is None or self._pending_hls is None:
            return

        current = await self._go2rtc.async_producer_bytes(self._go2rtc_name)

        if current is None:
            # No producer: go2rtc only starts one once someone connects, so
            # this is normal while nobody is watching. Just reset the counter.
            self._last_bytes = None
            self._stale_count = 0
            return

        if self._last_bytes is not None and current <= self._last_bytes:
            self._stale_count += 1
            LOGGER.debug(
                "go2rtc stalled at %d bytes (%dx)", current, self._stale_count
            )
        else:
            self._stale_count = 0

        self._last_bytes = current

        if self._stale_count >= STALE_CHECKS_BEFORE_REPUBLISH:
            LOGGER.info("go2rtc receives nothing, publishing a fresh URL")
            self._stale_count = 0
            self._last_bytes = None
            await self._async_republish(self._pending_hls)

    async def _async_republish(self, hls_url: str) -> None:
        """Update the go2rtc source without changing the RTSP address.

        Home Assistant keeps watching the same RTSP address; only what go2rtc
        fetches behind it changes.
        """
        if self._go2rtc is None:
            return

        try:
            await self._go2rtc.async_publish(
                self._go2rtc_name, hls_url, self._transcode, self._input_template
            )
        except Go2RtcError as err:
            LOGGER.warning("Could not hand the fresh URL to go2rtc: %s", err)
        else:
            LOGGER.debug("go2rtc now uses the fresh Kinesis URL")

    # -- teardown ----------------------------------------------------------

    @callback
    def _handle_session_lost(self) -> None:
        """The watcher reports that the server dropped the session."""
        LOGGER.info("Session for %s was dropped", self._feeder_id)
        self._stream_url = None
        if self._cancel_auto_off is not None:
            self._cancel_auto_off()
            self._cancel_auto_off = None
        self.async_write_ha_state()

    @callback
    def _schedule_auto_off(self) -> None:
        """(Re)start the timer that puts the feeder back to sleep."""
        if self._cancel_auto_off is not None:
            self._cancel_auto_off()
            self._cancel_auto_off = None

        if not self._session_active:
            return

        self._cancel_auto_off = async_call_later(
            self.hass, self._auto_off, self._async_auto_off
        )

    async def _async_auto_off(self, _now: Any) -> None:
        LOGGER.debug("Auto-off after %ss, stopping the stream", self._auto_off)
        self._cancel_auto_off = None
        await self._async_teardown()

    async def _async_teardown(self) -> None:
        """Stop timers and end the session."""
        if self._cancel_auto_off is not None:
            self._cancel_auto_off()
            self._cancel_auto_off = None

        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        self._start_task = None

        self._stream_url = None
        self._pending_hls = None
        self._last_bytes = None
        self._stale_count = 0

        # Without this the Home Assistant stream worker keeps reconnecting to a
        # go2rtc stream with a dead URL behind it, producing an endless series
        # of demux timeouts.
        await self._async_stop_ha_stream()

        # Replace the expired Kinesis URL with a placeholder, so anything that
        # still polls the stream does not make go2rtc retry a dead source.
        if self._go2rtc is not None:
            await self._go2rtc.async_park(self._go2rtc_name)

        try:
            await self._watcher.async_stop()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Stopping the session failed", exc_info=True)

        self.async_write_ha_state()

    async def _async_stop_ha_stream(self) -> None:
        """Stop Home Assistant's internal Stream object, if there is one."""
        stream = getattr(self, "stream", None)
        if stream is None:
            return

        try:
            result = stream.stop()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            LOGGER.debug("Stopping the Home Assistant stream failed", exc_info=True)
        else:
            LOGGER.debug("Home Assistant stream stopped")
