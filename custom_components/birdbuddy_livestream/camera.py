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
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from datetime import timedelta

from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .api import BirdBuddyWatcher, WatchingError
from .const import (
    CONF_AUTO_OFF,
    CONF_CONTINUOUS,
    CONF_GO2RTC_INPUT,
    CONF_GO2RTC_RTSP_PORT,
    CONF_GO2RTC_URL,
    CONF_INSTANT_START,
    CONF_PLACEHOLDER_SOURCE,
    CONF_PREVIEW_SOURCE,
    CONF_RETRY_INTERVAL,
    CONF_START_TIMEOUT,
    CONF_STOP_WHEN_UNWATCHED,
    CONF_TRANSCODE,
    DEFAULT_AUTO_OFF,
    DEFAULT_CONTINUOUS,
    DEFAULT_GO2RTC_RTSP_PORT,
    DEFAULT_INSTANT_START,
    DEFAULT_PLACEHOLDER_SOURCE,
    DEFAULT_PREVIEW_SOURCE,
    DEFAULT_RETRY_INTERVAL,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_WHEN_UNWATCHED,
    DEFAULT_TRANSCODE,
    DOMAIN,
    IDLE_CHECKS_BEFORE_STOP,
    LAST_FRAME_INTERVAL,
    PLACEHOLDER_PROBE_TIMEOUT,
    PREVIEW_LAST_FRAME,
    SIGNAL_STATUS,
    STALE_CHECKS_BEFORE_REPUBLISH,
    STATE_POLL_INTERVAL,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_SLEEPING,
    STATUS_STREAMING,
    STATUS_WAKING,
    STATUS_WARMING_UP,
    STREAM_SOURCE_GRACE,
    SUPERVISE_INTERVAL,
    TEARDOWN_STEP_TIMEOUT,
)
from .go2rtc import Go2RtcClient, Go2RtcError
from .preview import PreviewProvider

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
        entities.append(BirdBuddyCamera(hass, entry, watcher, feeder, go2rtc))

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
        hass: HomeAssistant,
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
        self._seen_viewer = False
        self._idle_count = 0
        self._cancel_supervisor: Any = None
        self._cancel_state_poll: Any = None
        self._retry_after = 0.0
        # Home Assistant calls stream_source() while the entity is being added,
        # to work out which WebRTC provider fits. Without this flag every
        # restart would wake the feeder.
        self._may_wake_feeder = False
        self._enabled = True
        self._preview = PreviewProvider(hass, dict(entry.options))
        self._status = STATUS_IDLE
        self._status_detail: str | None = None
        self._cancel_frame_grab: Any = None

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
    def _instant_start(self) -> bool:
        """Whether to hand out a placeholder while the feeder wakes.

        Only possible with go2rtc: it needs a source that produces frames
        immediately, so Home Assistant's stream worker has something to attach
        to before the feeder is ready.
        """
        if self._go2rtc is None:
            return False
        return bool(
            self._entry.options.get(CONF_INSTANT_START, DEFAULT_INSTANT_START)
        )

    @property
    def _placeholder_source(self) -> str:
        return (
            str(self._entry.options.get(CONF_PLACEHOLDER_SOURCE, "")).strip()
            or DEFAULT_PLACEHOLDER_SOURCE
        )

    @property
    def _continuous(self) -> bool:
        """Whether to keep the stream up permanently.

        Only sensible on mains power. The feeder does not record Bird Buddy
        postcards while it streams, so this trades those for a feed something
        like Frigate can watch around the clock.
        """
        return bool(self._entry.options.get(CONF_CONTINUOUS, DEFAULT_CONTINUOUS))

    @property
    def _retry_interval(self) -> int:
        return int(
            self._entry.options.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL)
        )

    @property
    def _stop_when_unwatched(self) -> bool:
        # Pointless in continuous mode, where nobody watching is the norm.
        if self._continuous:
            return False
        return bool(
            self._entry.options.get(
                CONF_STOP_WHEN_UNWATCHED, DEFAULT_STOP_WHEN_UNWATCHED
            )
        )

    @property
    def _preview_source(self) -> str:
        return self._entry.options.get(CONF_PREVIEW_SOURCE, DEFAULT_PREVIEW_SOURCE)

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

    @callback
    def _set_status(self, status: str, detail: str | None = None) -> None:
        """Publish a status change to the sensor and refresh the preview."""
        if status == self._status and detail == self._status_detail:
            return
        self._status = status
        self._status_detail = detail
        async_dispatcher_send(
            self.hass, SIGNAL_STATUS.format(self._feeder_id), status, detail
        )
        self._async_refresh_preview()

    @callback
    def _async_refresh_preview(self) -> None:
        """Make the frontend fetch the preview image again.

        The camera card requests /api/camera_proxy/<entity>?token=..., and the
        browser only refetches when that URL changes. The token rotates on Home
        Assistant's own schedule, so without forcing it the card keeps showing
        a stale picture until a restart.
        """
        self.async_update_token()
        self.async_write_ha_state()

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
            self._set_status(STATUS_WARMING_UP)

            if self._instant_start:
                # Give the stream worker a source that already produces frames.
                # Once the feeder is ready, _async_start_session swaps the go2rtc
                # source and calls Stream.update_source, which restarts the
                # worker on the same RTSP address.
                placeholder = await self._async_publish_placeholder()
                if placeholder is not None:
                    return placeholder

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
        """Return the preview image while the feeder is asleep.

        A thumbnail is not worth waking the feeder for; that costs a
        disproportionate amount of battery. Instead the card shows whichever
        image the user configured, captioned with the current status.
        """
        return await self._preview.async_image(self._status, self._status_detail)

    async def async_turn_on(self) -> None:
        """Turn the camera on and start the session in the background."""
        LOGGER.debug("turn_on called for %s", self._feeder_id)
        self._enabled = True
        self._may_wake_feeder = True
        self._retry_after = 0.0
        self._async_ensure_start_task()
        self._async_start_state_poll()
        self.hass.async_create_task(
            self._async_poll_state(None),
            name=f"{DOMAIN} state {self._feeder_id}",
        )

        if self._continuous:
            self._async_start_supervisor()
        self._async_refresh_preview()

    async def async_turn_off(self) -> None:
        """Turn the camera off and end any running session.

        In continuous mode this also holds the supervisor off, so turning the
        camera off really means off until it is turned back on.
        """
        self._enabled = False
        self._retry_after = 0.0
        await self._async_teardown()

    async def async_added_to_hass(self) -> None:
        """From now on, stream_source() calls count as real viewing requests."""
        await super().async_added_to_hass()
        self._may_wake_feeder = True
        self._watcher.register_lost_listener(
            self._feeder_id, self._handle_session_lost
        )
        self._watcher.register_url_listener(self._feeder_id, self._handle_fresh_url)
        self._preview.update_options(dict(self._entry.options))
        self._async_refresh_preview()

        if self._continuous:
            self._async_start_supervisor()
            # Do not wait out the first interval on startup.
            self.hass.async_create_task(
                self._async_supervise(None),
                name=f"{DOMAIN} supervise {self._feeder_id}",
            )

    async def async_will_remove_from_hass(self) -> None:
        """Do not leave the feeder streaming when the entity disappears."""
        self._may_wake_feeder = False
        self._async_stop_supervisor()
        self._async_stop_state_poll()
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
        # Check first, so a sleeping feeder is reported as such instead of as a
        # ninety-second timeout.
        state = await self._watcher.async_feeder_state(self._feeder_id)
        if not self._watcher.is_streamable(state):
            LOGGER.info(
                "Feeder %s reports %s, no livestream possible",
                self._feeder_id,
                state,
            )
            self._stream_url = None
            self._last_error = f"The feeder is not available ({state})"
            self._set_status(STATUS_SLEEPING, state)
            self._async_refresh_preview()
            return

        self._set_status(STATUS_WAKING)
        try:
            self._stream_url = await self._watcher.async_start(
                self._feeder_id, timeout=self._start_timeout
            )
        except WatchingError as err:
            self._stream_url = None
            self._last_error = str(err)
            LOGGER.error("Could not start the livestream: %s", err)
            self._set_status(STATUS_ERROR, str(err))
        except Exception as err:  # noqa: BLE001
            self._stream_url = None
            self._last_error = str(err)
            LOGGER.exception("Unexpected error while starting the livestream")
            self._set_status(STATUS_ERROR, str(err))
        else:
            self._last_error = None
            LOGGER.info("Livestream active for feeder %s", self._feeder_id)
            await self._async_publish_to_go2rtc()
            self._async_restart_ha_stream()
            self._schedule_auto_off()
            self._start_frame_grabs()
            self._set_status(STATUS_STREAMING)
        finally:
            self._async_refresh_preview()

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

    async def _async_publish_placeholder(self) -> str | None:
        """Park the go2rtc stream on a source that yields frames right away."""
        if self._go2rtc is None:
            return None

        try:
            url = await self._go2rtc.async_publish_raw(
                self._go2rtc_name, self._placeholder_source
            )
        except Go2RtcError as err:
            LOGGER.warning(
                "Could not publish the placeholder, falling back to asking the "
                "user to retry: %s",
                err,
            )
            return None

        # Registering always succeeds, even for a source go2rtc cannot start.
        # Handing Home Assistant an address that then 404s is worse than asking
        # the user to try again, because its stream worker does not retry.
        if not await self._go2rtc.async_can_produce(
            self._go2rtc_name, PLACEHOLDER_PROBE_TIMEOUT
        ):
            LOGGER.warning(
                "The placeholder source %r produces no video; check the go2rtc "
                "log and adjust the placeholder setting",
                self._placeholder_source,
            )
            return None

        LOGGER.info("Placeholder running while feeder %s wakes up", self._feeder_id)
        return url

    @callback
    def _async_restart_ha_stream(self) -> None:
        """Restart Home Assistant's stream worker on the same address.

        Stream.update_source sets _fast_restart_once, which makes the worker
        loop restart instead of breaking out, even without keepalive. That is
        what turns the placeholder into the real picture without a second click.
        """
        stream = getattr(self, "stream", None)
        if stream is None or not self._stream_url:
            return

        update_source = getattr(stream, "update_source", None)
        if update_source is None:
            LOGGER.debug(
                "This Home Assistant has no Stream.update_source; the viewer "
                "has to reopen the stream"
            )
            return

        LOGGER.info("Swapping the placeholder for the live stream")
        update_source(self._stream_url)

        # Replacing the go2rtc source kills the placeholder producer, so the
        # stream worker may already have hit EOF and exited before we get here.
        # update_source only flags a live thread for restart; a dead one needs
        # start(), which is a no-op while the thread is still running.
        start = getattr(stream, "start", None)
        if start is None:
            return

        # Stream.start is a coroutine in current Home Assistant and a plain
        # method in older ones, so handle both.
        self.hass.async_create_task(
            self._async_restart_worker(start),
            name=f"{DOMAIN} worker restart {self._feeder_id}",
        )

    async def _async_restart_worker(self, start: Any) -> None:
        """Recreate the stream worker thread when it has exited."""
        try:
            result = start()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            LOGGER.debug("Restarting the stream worker failed", exc_info=True)
        else:
            LOGGER.debug("Stream worker restarted")

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
        """Watch the go2rtc stream: republish when stalled, stop when unwatched."""
        if self._go2rtc is None or self._pending_hls is None:
            return

        current, consumers = await self._go2rtc.async_activity(self._go2rtc_name)

        if await self._async_check_viewers(consumers):
            return

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

    async def _async_check_viewers(self, consumers: int) -> bool:
        """Stop the session once nobody is watching. Returns True if stopped.

        Only applies after a viewer has actually connected: during the wake-up
        there are no consumers yet, and stopping then would kill the session
        we are busy setting up.
        """
        if not self._stop_when_unwatched:
            return False

        if consumers > 0:
            self._seen_viewer = True
            self._idle_count = 0
            return False

        if not self._seen_viewer:
            return False

        self._idle_count += 1
        LOGGER.debug("No viewers on the go2rtc stream (%dx)", self._idle_count)

        if self._idle_count < IDLE_CHECKS_BEFORE_STOP:
            return False

        LOGGER.info("Nobody is watching any more, stopping the livestream")
        await self._async_teardown()
        return True

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
            return

        LOGGER.debug("go2rtc now uses the fresh Kinesis URL")

        # Replacing the source ends the running ffmpeg process, so the stream
        # worker hits EOF and exits exactly as it does on the initial swap.
        # Without reviving it here the picture never comes back after a stall.
        self._async_restart_ha_stream()

    # -- feeder state ------------------------------------------------------

    @callback
    def _async_start_state_poll(self) -> None:
        """Keep the reported status honest while no session is running.

        Without this the card and the sensor only ever say "idle", and a start
        attempt against a sleeping feeder surfaces as a timeout rather than as
        "the feeder is asleep".
        """
        if self._cancel_state_poll is not None:
            return

        self._cancel_state_poll = async_track_time_interval(
            self.hass,
            self._async_poll_state,
            timedelta(seconds=STATE_POLL_INTERVAL),
        )

    @callback
    def _async_stop_state_poll(self) -> None:
        if self._cancel_state_poll is not None:
            self._cancel_state_poll()
            self._cancel_state_poll = None

    async def _async_poll_state(self, _now: Any) -> None:
        """Read the feeder state and reflect it in the status."""
        if self._session_active:
            return
        if self._start_task is not None and not self._start_task.done():
            return
        # An error worth reading should not be overwritten by a routine poll.
        if self._status == STATUS_ERROR:
            return

        state = await self._watcher.async_feeder_state(self._feeder_id)
        if state is None:
            return

        if self._watcher.is_streamable(state):
            self._set_status(STATUS_IDLE)
        else:
            LOGGER.debug("Feeder %s reports %s", self._feeder_id, state)
            self._set_status(STATUS_SLEEPING, state)

    # -- continuous mode ---------------------------------------------------

    @callback
    def _async_start_supervisor(self) -> None:
        """Keep the session up while continuous mode is enabled."""
        if self._cancel_supervisor is not None:
            return

        self._cancel_supervisor = async_track_time_interval(
            self.hass,
            self._async_supervise,
            timedelta(seconds=SUPERVISE_INTERVAL),
        )
        LOGGER.debug("Continuous mode supervisor started for %s", self._feeder_id)

    @callback
    def _async_stop_supervisor(self) -> None:
        if self._cancel_supervisor is not None:
            self._cancel_supervisor()
            self._cancel_supervisor = None

    async def _async_supervise(self, _now: Any) -> None:
        """Restart the session whenever it is not running.

        The feeder puts itself into DEEP_SLEEP at night, and no livestream can
        be started then. Rather than hammering the API until sunrise, a failed
        or refused start backs off for the retry interval.
        """
        if not self._continuous or not self._enabled:
            return

        if self._session_active:
            return

        if self._start_task is not None and not self._start_task.done():
            return

        loop = self.hass.loop
        if loop.time() < self._retry_after:
            return

        state = await self._watcher.async_feeder_state(self._feeder_id)

        if not self._watcher.is_streamable(state):
            LOGGER.debug(
                "Feeder %s is %s, not starting a stream", self._feeder_id, state
            )
            self._set_status(STATUS_SLEEPING, state)
            self._retry_after = loop.time() + self._retry_interval
            return

        LOGGER.info(
            "Continuous mode: feeder %s is %s, starting the stream",
            self._feeder_id,
            state,
        )
        self._may_wake_feeder = True
        self._async_ensure_start_task()
        # Back off regardless, so a failing start cannot spin.
        self._retry_after = loop.time() + self._retry_interval

    # -- last frame --------------------------------------------------------

    def _start_frame_grabs(self) -> None:
        """Periodically keep a still from the running stream.

        Only worth doing when the user actually wants the last frame as their
        preview; otherwise it is pointless work.
        """
        if self._preview_source != PREVIEW_LAST_FRAME:
            return
        if self._cancel_frame_grab is not None:
            return

        self._cancel_frame_grab = async_track_time_interval(
            self.hass,
            self._async_grab_frame,
            timedelta(seconds=LAST_FRAME_INTERVAL),
        )
        # Grab one right away rather than waiting out the first interval.
        self.hass.async_create_task(
            self._async_grab_frame(None), name=f"{DOMAIN} frame {self._feeder_id}"
        )

    def _stop_frame_grabs(self) -> None:
        if self._cancel_frame_grab is not None:
            self._cancel_frame_grab()
            self._cancel_frame_grab = None

    async def _async_grab_frame(self, _now: Any) -> None:
        """Store a keyframe from the running stream as the preview image."""
        if not self._session_active:
            return

        try:
            stream = await self.async_create_stream()
            if stream is None:
                return
            image = await stream.async_get_image()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Could not grab a frame from the stream", exc_info=True)
            return

        if image:
            self._preview.set_last_frame(image)
            LOGGER.debug("Stored a fresh preview frame")
            self._async_refresh_preview()

    # -- teardown ----------------------------------------------------------

    @callback
    def _handle_session_lost(self) -> None:
        """The watcher reports that the server dropped the session."""
        LOGGER.info("Session for %s was dropped", self._feeder_id)
        self._stop_frame_grabs()
        self._stream_url = None
        if self._cancel_auto_off is not None:
            self._cancel_auto_off()
            self._cancel_auto_off = None
        self._set_status(STATUS_IDLE)
        self._async_refresh_preview()

    @callback
    def _schedule_auto_off(self) -> None:
        """(Re)start the timer that puts the feeder back to sleep."""
        if self._cancel_auto_off is not None:
            self._cancel_auto_off()
            self._cancel_auto_off = None

        # In continuous mode the stream is meant to stay up.
        if self._continuous or not self._session_active:
            return

        self._cancel_auto_off = async_call_later(
            self.hass, self._auto_off, self._async_auto_off
        )

    async def _async_auto_off(self, _now: Any) -> None:
        LOGGER.debug("Auto-off after %ss, stopping the stream", self._auto_off)
        self._cancel_auto_off = None
        await self._async_teardown()

    async def _async_teardown(self) -> None:
        """Stop timers and end the session.

        Every remote step gets its own timeout. A stalled call here used to
        leave the keepalive running, which kept the feeder streaming until its
        battery gave out.
        """
        LOGGER.debug("Teardown starting for %s", self._feeder_id)

        if self._cancel_auto_off is not None:
            self._cancel_auto_off()
            self._cancel_auto_off = None

        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        self._start_task = None

        self._stop_frame_grabs()
        self._stream_url = None
        self._pending_hls = None
        self._last_bytes = None
        self._stale_count = 0
        self._seen_viewer = False
        self._idle_count = 0

        # Release the feeder first: that is the step that matters for battery.
        await self._async_step(
            "stopping the Bird Buddy session", self._watcher.async_stop()
        )

        # Without this the Home Assistant stream worker keeps reconnecting to a
        # go2rtc stream with a dead URL behind it, producing an endless series
        # of demux timeouts.
        await self._async_step(
            "stopping the Home Assistant stream", self._async_stop_ha_stream()
        )

        # Replace the expired Kinesis URL with a placeholder, so anything that
        # still polls the stream does not make go2rtc retry a dead source.
        if self._go2rtc is not None:
            await self._async_step(
                "parking the go2rtc stream",
                self._go2rtc.async_park(
                    self._go2rtc_name, self._placeholder_source
                ),
            )

        LOGGER.debug("Teardown finished for %s", self._feeder_id)
        self._set_status(STATUS_IDLE)
        self._async_refresh_preview()

    async def _async_step(self, description: str, coro: Any) -> None:
        """Run one teardown step, bounded and never fatal."""
        try:
            async with asyncio.timeout(TEARDOWN_STEP_TIMEOUT):
                await coro
        except TimeoutError:
            LOGGER.warning(
                "Timed out while %s after %ss; continuing",
                description,
                TEARDOWN_STEP_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            LOGGER.debug("Failed while %s", description, exc_info=True)
        else:
            LOGGER.debug("Done %s", description)

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
