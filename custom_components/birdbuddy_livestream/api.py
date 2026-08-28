"""Manages the account-wide Bird Buddy watching session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp
from birdbuddy.client import BirdBuddy

from .const import (
    ACTIVE_WITHOUT_URL_LIMIT,
    KEEPALIVE_MISSES_BEFORE_LOST,
    DEFAULT_START_TIMEOUT,
    STREAMABLE_FEEDER_STATES,
    KEEPALIVE_INTERVAL,
    POLL_INTERVAL,
    WARMUP_MIN_SEGMENTS,
    WARMUP_POLL,
    WARMUP_TIMEOUT,
    WATCHING_COOLDOWN,
    WATCHING_KEEP,
    WATCHING_START,
    WATCHING_START_CHECK,
    WATCHING_STOP,
)

LOGGER = logging.getLogger(__name__)


class WatchingError(Exception):
    """The feeder could not set up the livestream."""


@dataclass(slots=True)
class ActiveStream:
    """A running watching session."""

    feeder_id: str
    watching_id: str
    stream_url: str


class BirdBuddyWatcher:
    """Wraps pybirdbuddy and adds the watching mutations.

    Bird Buddy allows only one active watching session per account, and the
    session mutations take no feederId. This class therefore serialises all
    access, tracks which feeder currently holds the session, and runs the
    keepalive.
    """

    def __init__(
        self, client: BirdBuddy, session: aiohttp.ClientSession | None = None
    ) -> None:
        """Initialise the watcher."""
        self._client = client
        self._session = session
        self._lock = asyncio.Lock()
        self._active: ActiveStream | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_misses = 0
        self._lost_listeners: dict[str, Callable[[], None]] = {}

    @property
    def client(self) -> BirdBuddy:
        """Return the underlying pybirdbuddy client."""
        return self._client

    @property
    def active(self) -> ActiveStream | None:
        """Return the running session, if any."""
        return self._active

    def is_active(self, feeder_id: str) -> bool:
        """Return whether this specific feeder is currently streaming."""
        return self._active is not None and self._active.feeder_id == feeder_id

    # -- listeners ---------------------------------------------------------

    def register_lost_listener(
        self, feeder_id: str, callback: Callable[[], None]
    ) -> None:
        """Register a callback for when the session is dropped."""
        self._lost_listeners[feeder_id] = callback

    def unregister_listeners(self, feeder_id: str) -> None:
        """Remove all callbacks for a feeder."""
        self._lost_listeners.pop(feeder_id, None)

    # -- starting ----------------------------------------------------------

    async def async_start(
        self,
        feeder_id: str,
        timeout: int = DEFAULT_START_TIMEOUT,
        clear_stale: bool = False,
    ) -> str:
        """Start the stream and return the HLS master playlist URL."""
        async with self._lock:
            if self._active is not None:
                if self._active.feeder_id == feeder_id:
                    return self._active.stream_url
                LOGGER.debug(
                    "Session held by feeder %s, stopping it first",
                    self._active.feeder_id,
                )
                await self._async_stop_locked()

            # A session may still be running on the server that we do not know
            # about: after a Home Assistant restart, or when the mobile app
            # left one behind. Starting on top of it answers ACTIVE without a
            # streamUrl, and no amount of polling produces one.
            #
            # Only done when the caller has reason to believe that is the case,
            # because clearing costs a stop the feeder has to recover from.
            if clear_stale:
                LOGGER.info("Clearing a stale session before starting")
                await self._async_reset_session()

            variables = {"startWatchingInput": {"feederId": feeder_id}}
            result = await self._request(WATCHING_START, variables, "watchingStartV2")
            active_without_url = 0

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            while True:
                typename = (result or {}).get("__typename")
                watching = (result or {}).get("watching") or {}

                if typename is None:
                    # Bird Buddy occasionally answers without a recognisable
                    # result type. Polling again clears it, so this is noted
                    # rather than treated as a failure.
                    LOGGER.debug("Unrecognised watching response: %s", result)
                else:
                    LOGGER.debug(
                        "watching %s / state=%s", typename, watching.get("state")
                    )

                if typename == "WatchingActiveResult" and not watching.get("streamUrl"):
                    # ACTIVE but no URL means the server considers a session to
                    # be running while withholding its address. Polling will
                    # not fix that; give up early with something readable.
                    active_without_url += 1
                    if active_without_url >= ACTIVE_WITHOUT_URL_LIMIT:
                        await self._async_cooldown_locked()
                        raise WatchingError(
                            "Bird Buddy reports an active session but hands out "
                            "no stream address. Close the Bird Buddy app and try "
                            "again."
                        )

                if typename == "WatchingActiveResult" and watching.get("streamUrl"):
                    self._active = ActiveStream(
                        feeder_id=feeder_id,
                        watching_id=watching["id"],
                        stream_url=watching["streamUrl"],
                    )
                    # Start sending keepalives right away. The warm-up below
                    # easily outlasts the server-side timeout, and without
                    # keepalives the session dies halfway through it.
                    self._start_keepalive()

                    # ACTIVE only means the session was set up. The feeder
                    # delivers a single fragment and then goes quiet for about
                    # ten seconds.
                    await self._async_wait_for_segments(self._active.stream_url)
                    return self._active.stream_url

                if typename == "WatchingFailedResult":
                    reason = result.get("failedReason")
                    await self._async_cooldown_locked()
                    raise WatchingError(
                        f"Bird Buddy refused the stream: {reason}. "
                        "This often fails when the Wi-Fi signal is weak."
                    )

                if loop.time() > deadline:
                    await self._async_cooldown_locked()
                    raise WatchingError(
                        f"No active stream within {timeout}s (last: {typename})"
                    )

                await asyncio.sleep(POLL_INTERVAL)
                result = await self._request(
                    WATCHING_START_CHECK, variables, "watchingStartCheck"
                )

    async def _async_wait_for_segments(self, master_url: str) -> None:
        """Wait until the HLS playlist actually keeps producing segments.

        Gives up after the timeout: a stuttering stream beats no stream.
        """
        if self._session is None:
            return

        child_url: str | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WARMUP_TIMEOUT

        while loop.time() < deadline:
            try:
                if child_url is None:
                    async with self._session.get(master_url) as resp:
                        master = await resp.text()
                    variant = next(
                        (
                            line.strip()
                            for line in master.splitlines()
                            if line.strip() and not line.startswith("#")
                        ),
                        None,
                    )
                    if variant is None:
                        return
                    child_url = urljoin(master_url, variant)

                async with self._session.get(child_url) as resp:
                    playlist = await resp.text()
            except (aiohttp.ClientError, TimeoutError) as err:
                LOGGER.debug("Could not fetch playlist: %s", err)
                await asyncio.sleep(WARMUP_POLL)
                continue

            segments = [
                line
                for line in playlist.splitlines()
                if line.strip() and not line.startswith("#")
            ]
            LOGGER.debug("Warm-up: %d segments in the playlist", len(segments))

            if len(segments) >= WARMUP_MIN_SEGMENTS:
                LOGGER.info("Playlist is running, releasing the stream URL")
                return

            await asyncio.sleep(WARMUP_POLL)

        LOGGER.warning(
            "Playlist stayed thin during warm-up; the stream may stutter"
        )

    # -- keepalive ---------------------------------------------------------

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _cancel_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """Send watchingActiveKeep until the session ends.

        Deliberately runs without the lock: async_start holds it during the
        warm-up, which is exactly when keepalives must keep flowing.
        """
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)

            if self._active is None:
                return

            try:
                result = await self._request(WATCHING_KEEP, None, "watchingActiveKeep")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                LOGGER.warning("Keepalive failed: %s", err)
                self._notify_lost()
                return

            state = (result or {}).get("state")
            LOGGER.debug("Keepalive: state=%s", state)

            if state is None:
                # An answer without a state is not the same as a session that
                # ended. Dropping the session on one unreadable reply used to
                # tear everything down and set the supervisor restarting.
                self._keepalive_misses += 1
                if self._keepalive_misses >= KEEPALIVE_MISSES_BEFORE_LOST:
                    LOGGER.info("Keepalive returned no state repeatedly, giving up")
                    self._notify_lost()
                    return
                continue

            self._keepalive_misses = 0

            if state != "ACTIVE":
                LOGGER.info("Session is now %s, keepalive stops", state)
                self._notify_lost()
                return

    async def async_refresh_stream_url(self) -> str | None:
        """Fetch a fresh stream URL for the running session.

        The Kinesis URL is a temporary session address that dies for good once
        it expires, and go2rtc only starts pulling it when a viewer connects.
        A URL published minutes earlier is therefore likely to be stale by the
        time anyone watches, which is why this is called just before handing
        the address out and again when the source stalls, rather than on a
        timer. watchingStartCheck is a mutation; firing it every keepalive put
        the feeder under constant needless load.
        """
        if self._active is None:
            return None

        feeder_id = self._active.feeder_id
        variables = {"startWatchingInput": {"feederId": feeder_id}}

        try:
            result = await self._request(
                WATCHING_START_CHECK, variables, "watchingStartCheck"
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Could not fetch a fresh URL: %s", err)
            return None

        if result.get("__typename") != "WatchingActiveResult":
            LOGGER.debug("No fresh URL: %s", result.get("__typename"))
            return None

        watching = result.get("watching") or {}
        fresh = watching.get("streamUrl")
        if not fresh or self._active is None:
            return None

        LOGGER.debug("Fresh stream URL received for %s", feeder_id)
        self._active.stream_url = fresh
        return fresh

    def _notify_lost(self) -> None:
        """The server dropped the session."""
        if self._active is None:
            return
        feeder_id = self._active.feeder_id
        self._active = None
        if (callback := self._lost_listeners.get(feeder_id)) is not None:
            callback()

    # -- feeder state ------------------------------------------------------

    async def async_feeder_state(self, feeder_id: str) -> str | None:
        """Return the feeder's current state, refreshing it from the cloud.

        Used to tell "asleep" apart from "broken": the feeder puts itself into
        DEEP_SLEEP at night, and no livestream can be started until it wakes.
        """
        try:
            await self._client.refresh()
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Could not refresh the feeder state: %s", err)
            return None

        feeder = self._client.feeders.get(feeder_id)
        if feeder is None:
            return None

        state = feeder.get("state")
        return str(state) if state is not None else None

    @staticmethod
    def is_streamable(state: str | None) -> bool:
        """Whether a livestream can be started in this feeder state.

        An unknown state counts as streamable: better to try and fail than to
        refuse because Bird Buddy introduced a state we have not seen.
        """
        if state is None:
            return True
        return state in STREAMABLE_FEEDER_STATES

    # -- stopping ----------------------------------------------------------

    async def async_stop(self) -> list[str]:
        """End the session. Returns photos taken during the stream."""
        # Cancel the keepalive before touching the lock. If anything below
        # stalls, the feeder still stops being kept awake, which is the part
        # that costs battery.
        self._cancel_keepalive()

        async with self._lock:
            return await self._async_stop_locked()

    async def _async_stop_locked(self) -> list[str]:
        self._cancel_keepalive()

        if self._active is None:
            return []

        self._active = None
        try:
            result = await self._request(WATCHING_STOP, None, "watchingActiveStop")
        except Exception:  # noqa: BLE001
            LOGGER.debug("watchingActiveStop failed, falling back to cooldown")
            await self._async_cooldown_locked()
            return []

        await self._async_cooldown_locked()
        return (result or {}).get("imageUrls") or []

    async def _async_reset_session(self) -> None:
        """Release a session the server still holds for this account.

        Only watchingActiveStop is sent. watchingCooldown belongs at the end of
        a session, and sending it just before a start puts the feeder into a
        cooldown it then refuses to stream out of, answering UNSPECIFIED.
        """
        try:
            await self._request(WATCHING_STOP, None, "watchingActiveStop")
        except Exception:  # noqa: BLE001
            LOGGER.debug("watchingActiveStop during reset failed", exc_info=True)

    async def _async_cooldown_locked(self) -> None:
        try:
            await self._request(WATCHING_COOLDOWN, None, "watchingCooldown")
        except Exception:  # noqa: BLE001
            LOGGER.debug("watchingCooldown failed", exc_info=True)

    # -- transport ---------------------------------------------------------

    async def _request(
        self, query: str, variables: dict | None, subscript: str
    ) -> dict:
        kwargs: dict = {"query": query, "subscript": subscript}
        if variables is not None:
            kwargs["variables"] = variables
        return await self._client._make_request(**kwargs)  # noqa: SLF001
