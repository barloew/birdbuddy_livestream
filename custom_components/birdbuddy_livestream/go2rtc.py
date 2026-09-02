"""Integration with go2rtc.

The Bird Buddy HLS URL changes with every session, so go2rtc cannot pick it up
from a static configuration. Instead the source is registered through the API
on every start, and Home Assistant is pointed at go2rtc.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

LOGGER = logging.getLogger(__name__)

# go2rtc rejects sources containing spaces, so individual ffmpeg flags cannot
# go into the URL. They are not needed either: go2rtc restarts the ffmpeg
# process itself once it exits. Anyone who does want custom flags can define a
# template in go2rtc.yaml and reference it by name; see the README.


def _normalise(base_url: str) -> str:
    """Accept an address with or without a scheme and port.

    "192.168.1.10" is what people type; aiohttp rejects it outright, which
    surfaces as an unhelpful "go2rtc is unreachable".
    """
    url = base_url.strip().rstrip("/")
    if not url:
        return url
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if parsed.port is None:
        url = f"{url}:1984"
    return url


class Go2RtcError(Exception):
    """go2rtc did not accept the stream."""


class Go2RtcClient:
    """Talks to the go2rtc API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        rtsp_port: int = 8554,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._base_url = _normalise(base_url)
        self._rtsp_port = rtsp_port

    @property
    def _host(self) -> str:
        """Return the hostname from the API URL, for building the RTSP address."""
        parsed = urlparse(self._base_url)
        return parsed.hostname or self._base_url

    def rtsp_url(self, name: str) -> str:
        """Return the address where Home Assistant can pick up the stream."""
        return f"rtsp://{self._host}:{self._rtsp_port}/{name}"

    def build_source(
        self, hls_url: str, transcode: bool, input_template: str = ""
    ) -> str:
        """Build the go2rtc source string.

        Without transcoding the HEVC stream is copied through, which saves CPU
        but only plays in Safari. With transcoding it becomes H.264 and every
        browser can display it.

        The string must not contain spaces; go2rtc rejects those.
        """
        source = f"ffmpeg:{hls_url}"
        if input_template:
            source += f"#input={input_template}"
        source += "#video=h264" if transcode else "#video=copy"

        if " " in source:
            raise Go2RtcError("source contains spaces, which go2rtc rejects")
        return source

    async def async_publish(
        self,
        name: str,
        hls_url: str,
        transcode: bool = True,
        input_template: str = "",
    ) -> str:
        """Register or replace the stream and return its RTSP address."""
        source = self.build_source(hls_url, transcode, input_template)
        await self.async_publish_raw(name, source)

        if not await self.async_exists(name):
            raise Go2RtcError(
                f"go2rtc accepted stream {name} but does not report it back"
            )

        LOGGER.debug("Published stream %s to go2rtc (transcode=%s)", name, transcode)
        return self.rtsp_url(name)

    async def async_publish_raw(self, name: str, source: str) -> str:
        """Give the stream exactly one source, replacing whatever it had."""
        if " " in source:
            raise Go2RtcError("source contains spaces, which go2rtc rejects")

        await self._async_set_source(name, source)
        return self.rtsp_url(name)

    async def _async_set_source(self, name: str, source: str) -> None:
        """Replace the stream's source, creating the stream when it is new.

        The verb matters. In go2rtc, PUT creates a stream and *appends* another
        source to one that already exists, while PATCH replaces the source
        outright. Publishing with PUT throughout leaves the stream holding
        every source it was ever given: the placeholder, plus each expired
        Kinesis URL. go2rtc then keeps retrying all of them, which fills its
        log with 403s, and keeps feeding consumers from the first one that
        still works, which is why a placeholder went on playing after the live
        stream had been handed over.

        PATCH also avoids the gap that deleting and recreating leaves behind,
        during which anything connecting gets a 404.
        """
        query = f"?name={quote(name, safe='')}&src={quote(source, safe='')}"
        url = f"{self._base_url}/api/streams{query}"

        try:
            async with self._session.patch(url) as resp:
                if resp.status < 400:
                    LOGGER.debug("Replaced the source of stream %s", name)
                    return
                patch_status = resp.status
        except aiohttp.ClientError as err:
            raise Go2RtcError(f"go2rtc is unreachable: {err}") from err

        # PATCH fails when the stream does not exist yet, which is expected on
        # the first publish after go2rtc started.
        LOGGER.debug(
            "PATCH on stream %s returned %s, creating it instead", name, patch_status
        )

        try:
            async with self._session.put(url) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise Go2RtcError(f"go2rtc returned {resp.status}: {body[:200]}")
        except aiohttp.ClientError as err:
            raise Go2RtcError(f"go2rtc is unreachable: {err}") from err

        LOGGER.debug("Created stream %s", name)

    async def async_exists(self, name: str) -> bool:
        """Return whether go2rtc knows about the stream.

        The Home Assistant stream worker reconnects after a hiccup; if the
        stream is gone by then it receives a 404 instead of video.
        """
        url = f"{self._base_url}/api/streams?src={quote(name, safe='')}"
        try:
            async with self._session.get(url) as resp:
                return resp.status < 400
        except aiohttp.ClientError:
            return False

    async def async_can_produce(self, name: str, timeout: float = 8) -> bool:
        """Check whether the stream actually yields video.

        Asks go2rtc for a single frame, which forces it to start the source.
        Registering a stream through the API always succeeds, even when the
        source is nonsense, so this is the only way to find out before handing
        the address to Home Assistant, whose stream worker does not retry.
        """
        url = f"{self._base_url}/api/frame.jpeg?src={quote(name, safe='')}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status >= 400:
                    LOGGER.debug(
                        "Frame probe for %s returned %s", name, resp.status
                    )
                    return False
                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            LOGGER.debug("Frame probe for %s failed: %s", name, err)
            return False

        if not data:
            LOGGER.debug("Frame probe for %s returned an empty body", name)
            return False

        LOGGER.debug("Frame probe for %s returned %d bytes", name, len(data))
        return True

    async def async_activity(self, name: str) -> tuple[int | None, int, Any]:
        """Return bytes received, viewer count and the producer's identity.

        A byte count of None means no producer is running. Zero consumers means
        nobody is watching. The identity matters because go2rtc restarts the
        producer whenever the source changes, and its byte counter starts over
        at zero: without noticing the restart, that reads as a stall.
        """
        url = f"{self._base_url}/api/streams?src={quote(name, safe='')}"
        try:
            async with self._session.get(url) as resp:
                if resp.status >= 400:
                    return None, 0
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as err:
            LOGGER.debug("Could not read status of stream %s: %s", name, err)
            return None, 0

        payload = data or {}
        consumers = len(payload.get("consumers") or [])

        producers = payload.get("producers") or []
        if len(producers) > 1:
            LOGGER.warning(
                "Stream %s has %d sources; go2rtc will feed viewers from the "
                "first one that works, which may not be the live stream",
                name,
                len(producers),
            )
        if not producers:
            return None, consumers

        return int(producers[0].get("bytes_recv") or 0), consumers

    async def async_park(self, name: str, source: str) -> None:
        """Point the stream at a harmless source after a session ends.

        The registration is kept so the Home Assistant stream worker does not
        hit a 404 while reconnecting, but the expired Kinesis URL cannot stay:
        anything that polls the stream would make go2rtc launch ffmpeg against
        a dead URL, filling its log with 403s. go2rtc rejects `null:`, so the
        placeholder source is used instead — it costs nothing until something
        actually connects.
        """
        try:
            await self.async_publish_raw(name, source)
        except Go2RtcError as err:
            LOGGER.debug("Parking stream %s failed: %s", name, err)
        else:
            LOGGER.debug("Parked stream %s", name)

    async def async_delete(self, name: str) -> None:
        """Remove the stream.

        Not used when a session stops; see async_park for that. Deleting the
        stream would give the Home Assistant stream worker a 404 while it is
        reconnecting.
        """
        url = f"{self._base_url}/api/streams?src={quote(name, safe='')}"
        try:
            async with self._session.delete(url) as resp:
                if resp.status >= 400:
                    LOGGER.debug("Deleting stream %s returned %s", name, resp.status)
        except aiohttp.ClientError as err:
            LOGGER.debug("Deleting stream %s failed: %s", name, err)

    async def async_check(self) -> bool:
        """Return whether go2rtc responds."""
        try:
            async with self._session.get(f"{self._base_url}/api") as resp:
                return resp.status < 400
        except aiohttp.ClientError:
            return False
