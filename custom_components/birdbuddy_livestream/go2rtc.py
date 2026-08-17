"""Integration with go2rtc.

The Bird Buddy HLS URL changes with every session, so go2rtc cannot pick it up
from a static configuration. Instead the source is registered through the API
on every start, and Home Assistant is pointed at go2rtc.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlparse

import aiohttp

LOGGER = logging.getLogger(__name__)

# go2rtc rejects sources containing spaces, so individual ffmpeg flags cannot
# go into the URL. They are not needed either: go2rtc restarts the ffmpeg
# process itself once it exits. Anyone who does want custom flags can define a
# template in go2rtc.yaml and reference it by name; see the README.


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
        self._base_url = base_url.rstrip("/")
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
        url = (
            f"{self._base_url}/api/streams"
            f"?name={quote(name, safe='')}&src={quote(source, safe='')}"
        )

        try:
            async with self._session.put(url) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise Go2RtcError(f"go2rtc returned {resp.status}: {body[:200]}")
        except aiohttp.ClientError as err:
            raise Go2RtcError(f"go2rtc is unreachable: {err}") from err

        if not await self.async_exists(name):
            raise Go2RtcError(
                f"go2rtc accepted stream {name} but does not report it back"
            )

        LOGGER.debug("Published stream %s to go2rtc (transcode=%s)", name, transcode)
        return self.rtsp_url(name)

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

    async def async_producer_bytes(self, name: str) -> int | None:
        """Return how many bytes the source has received so far.

        None means no producer is running. A number that stops growing means
        the source has stalled.
        """
        url = f"{self._base_url}/api/streams?src={quote(name, safe='')}"
        try:
            async with self._session.get(url) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as err:
            LOGGER.debug("Could not read status of stream %s: %s", name, err)
            return None

        producers = (data or {}).get("producers") or []
        if not producers:
            return None

        return int(producers[0].get("bytes_recv") or 0)

    async def async_park(self, name: str) -> None:
        """Point the stream at a placeholder after a session ends.

        The registration is kept so the Home Assistant stream worker does not
        hit a 404 while reconnecting. Leaving the expired Kinesis URL in place
        is not an option either: anything that polls the stream makes go2rtc
        launch ffmpeg against a dead URL, filling its log with 403s. A null
        source accepts the connection and produces nothing.
        """
        url = (
            f"{self._base_url}/api/streams"
            f"?name={quote(name, safe='')}&src={quote('null:', safe='')}"
        )
        try:
            async with self._session.put(url) as resp:
                if resp.status >= 400:
                    LOGGER.debug("Parking stream %s returned %s", name, resp.status)
                else:
                    LOGGER.debug("Parked stream %s", name)
        except aiohttp.ClientError as err:
            LOGGER.debug("Parking stream %s failed: %s", name, err)

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
