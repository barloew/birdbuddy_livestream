"""Supplies the still image shown on the camera card.

The feeder is asleep most of the time, and waking it for a thumbnail would
flatten the battery. This module produces a placeholder instead, drawn from
whichever source the user picked, optionally with the current status written
across it.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_PREVIEW_ENTITY,
    CONF_PREVIEW_FILE,
    CONF_PREVIEW_SOURCE,
    CONF_STATUS_OVERLAY,
    DEFAULT_PREVIEW_SOURCE,
    DEFAULT_STATUS_OVERLAY,
    PREVIEW_ENTITY,
    PREVIEW_FILE,
    PREVIEW_LAST_FRAME,
    PREVIEW_NONE,
    SIGNED_URL_TTL,
    STATUS_ERROR,
    STATUS_STREAMING,
    STATUS_WAKING,
    STATUS_WARMING_UP,
)

LOGGER = logging.getLogger(__name__)

# Text drawn over the preview per status. STATUS_IDLE gets nothing, because a
# calm picture of the feeder needs no explanation.
OVERLAY_TEXT: dict[str, str] = {
    STATUS_WAKING: "Waking up the feeder...",
    STATUS_WARMING_UP: "Almost there...",
    STATUS_STREAMING: "Live",
    STATUS_ERROR: "Livestream unavailable",
}


class PreviewProvider:
    """Resolves and caches the preview image for one feeder."""

    def __init__(self, hass: HomeAssistant, options: dict[str, Any]) -> None:
        """Initialise the provider."""
        self._hass = hass
        self._options = options
        self._last_frame: bytes | None = None
        self._cache: bytes | None = None
        self._cache_key: tuple | None = None

    def update_options(self, options: dict[str, Any]) -> None:
        """Apply new options and drop the cache."""
        self._options = options
        self._cache = None
        self._cache_key = None

    def set_last_frame(self, image: bytes) -> None:
        """Store a still grabbed from a running stream."""
        self._last_frame = image
        self._cache = None
        self._cache_key = None

    @property
    def has_last_frame(self) -> bool:
        """Return whether a frame from a previous stream is available."""
        return self._last_frame is not None

    @property
    def _source(self) -> str:
        return self._options.get(CONF_PREVIEW_SOURCE, DEFAULT_PREVIEW_SOURCE)

    @property
    def _overlay_enabled(self) -> bool:
        return bool(self._options.get(CONF_STATUS_OVERLAY, DEFAULT_STATUS_OVERLAY))

    async def async_image(self, status: str) -> bytes | None:
        """Return the preview image for the given status.

        Returns None when no preview is configured or the source could not be
        read; Home Assistant then shows its own placeholder.
        """
        if self._source == PREVIEW_NONE:
            return None

        # Only the overlay text varies per status, so cache on that.
        key = (self._source, status if self._overlay_enabled else None)
        if self._cache is not None and self._cache_key == key:
            return self._cache

        base = await self._async_base_image()
        if base is None:
            return None

        image = base
        if self._overlay_enabled and (text := OVERLAY_TEXT.get(status)):
            image = await self._hass.async_add_executor_job(
                _draw_overlay, base, text
            )

        self._cache = image
        self._cache_key = key
        return image

    async def _async_base_image(self) -> bytes | None:
        """Fetch the unannotated image from the configured source."""
        source = self._source

        if source == PREVIEW_LAST_FRAME:
            return self._last_frame

        if source == PREVIEW_FILE:
            path = str(self._options.get(CONF_PREVIEW_FILE) or "").strip()
            if not path:
                return None
            if not self._hass.config.is_allowed_path(path):
                LOGGER.warning(
                    "Preview image %s is outside the allowed directories; add "
                    "its folder to allowlist_external_dirs",
                    path,
                )
                return None
            try:
                return await self._hass.async_add_executor_job(_read_file, path)
            except OSError as err:
                LOGGER.warning("Could not read preview image %s: %s", path, err)
                return None

        if source == PREVIEW_ENTITY:
            return await self._async_entity_picture()

        return None

    async def _async_entity_picture(self) -> bytes | None:
        """Fetch the picture of another entity, such as a recent-visitor sensor."""
        entity_id = str(self._options.get(CONF_PREVIEW_ENTITY) or "").strip()
        if not entity_id:
            return None

        state = self._hass.states.get(entity_id)
        if state is None:
            LOGGER.debug("Preview entity %s does not exist", entity_id)
            return None

        picture = state.attributes.get("entity_picture")
        if not picture:
            LOGGER.debug("Preview entity %s has no entity_picture", entity_id)
            return None

        url = await self._async_resolve_picture(picture)
        if url is None:
            return None

        session = async_get_clientsession(self._hass)
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    LOGGER.debug("Preview picture returned %s", resp.status)
                    return None
                return await resp.read()
        except aiohttp.ClientError as err:
            LOGGER.debug("Could not fetch preview picture: %s", err)
            return None

    async def _async_resolve_picture(self, picture: str) -> str | None:
        """Turn an entity_picture value into a URL we can actually fetch.

        Pictures hosted elsewhere are absolute and can be fetched as-is. Local
        ones point at /api/... and need authentication, so they get a signed
        path instead.
        """
        if picture.startswith(("http://", "https://")):
            return picture

        try:
            from homeassistant.components.http.auth import async_sign_path
        except ImportError:  # pragma: no cover
            LOGGER.debug("Signed paths are unavailable on this Home Assistant")
            return None

        try:
            base = get_url(self._hass, prefer_external=False)
        except NoURLAvailableError:
            LOGGER.debug("No internal URL configured, cannot fetch the picture")
            return None

        from datetime import timedelta

        signed = async_sign_path(
            self._hass, picture, timedelta(seconds=SIGNED_URL_TTL)
        )
        return f"{base}{signed}"


def _read_file(path: str) -> bytes:
    """Read a file from disk. Runs in the executor."""
    with open(path, "rb") as handle:
        return handle.read()


def _draw_overlay(image: bytes, text: str) -> bytes:
    """Write the status across the bottom of the image.

    Runs in the executor because Pillow is blocking. Returns the image
    unchanged if Pillow is unavailable or the image cannot be decoded, since a
    picture without a caption beats no picture at all.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover
        LOGGER.debug("Pillow is unavailable, skipping the status overlay")
        return image

    try:
        with Image.open(io.BytesIO(image)) as src:
            canvas = src.convert("RGB")
    except Exception:  # noqa: BLE001
        LOGGER.debug("Could not decode the preview image", exc_info=True)
        return image

    width, height = canvas.size
    draw = ImageDraw.Draw(canvas, "RGBA")

    # Scale the banner with the image so it stays readable on any resolution.
    band = max(28, height // 12)
    font_size = max(14, band // 2)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    draw.rectangle((0, height - band, width, height), fill=(0, 0, 0, 150))

    box = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((width - text_w) / 2, height - band + (band - text_h) / 2 - box[1]),
        text,
        font=font,
        fill=(255, 255, 255, 235),
    )

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=85)
    return out.getvalue()
