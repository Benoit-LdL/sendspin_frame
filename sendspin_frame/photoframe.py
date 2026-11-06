"""Native client for the Nextcloud "Photo Frames" app (no browser needed).

The app exposes the frame's current photo at  <frame url>/image  (public, no login). The server
decides which photo is current and announces when it changes through the `Expires` header, so
we fetch the image, sleep until it expires, and fetch again - exactly what the app's own web
page does, minus the browser.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import io
import logging
import time
import urllib.parse

import aiohttp
from PIL import Image, ImageOps

from .config import PhotosConfig
from .state import Photo, SharedState

logger = logging.getLogger(__name__)

MAX_SLEEP = 3600


def image_url(frame_url: str) -> str:
    url = frame_url.strip().rstrip("/")
    return url if url.endswith("/image") else url + "/image"


def decode_photo(data: bytes, max_size: tuple[int, int]) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    # JPEG "draft" mode decodes directly at 1/2, 1/4 or 1/8 scale: a big speed-up on a Pi Zero.
    side = max(max_size)
    img.draft("RGB", (side, side))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((side, side), Image.Resampling.LANCZOS)
    return img


class PhotoFrameFetcher:
    def __init__(self, cfg: PhotosConfig, state: SharedState, screen: tuple[int, int]) -> None:
        self.cfg = cfg
        self.state = state
        self.screen = screen
        self.url = image_url(cfg.url)
        self._serial = 0
        self._last_hash: str | None = None

    async def run(self) -> None:
        logger.info("Photo Frames: polling %s", self.url)
        timeout = aiohttp.ClientTimeout(total=60, connect=15)
        ssl = None if self.cfg.verify_tls else False
        backoff = 10.0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    delay = await self._fetch(session, ssl)
                    backoff = 10.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep polling whatever happens
                    logger.warning("Photo Frames: fetch failed (%s), retrying in %.0fs", exc, backoff)
                    delay = backoff
                    backoff = min(backoff * 2, 600)
                await asyncio.sleep(delay)

    async def _fetch(self, session: aiohttp.ClientSession, ssl) -> float:
        async with session.get(self.url, ssl=ssl, headers={"Cache-Control": "no-cache"}) as resp:
            if resp.status == 404:
                logger.warning("Photo Frames: no photo available (empty album or wrong URL)")
                self.state.set_photo(None)
                self._last_hash = None
                return 300
            resp.raise_for_status()
            data = await resp.read()
            headers = resp.headers

        delay = float(self.cfg.min_refresh)
        if expires := headers.get("Expires"):
            try:
                exp = email.utils.parsedate_to_datetime(expires).timestamp()
                delay = max(delay, min(MAX_SLEEP, exp - time.time() + 2))
            except (TypeError, ValueError):
                logger.debug("Unparseable Expires header: %s", expires)

        digest = hashlib.sha1(data).hexdigest()
        if digest == self._last_hash:
            return delay
        self._last_hash = digest

        img = await asyncio.to_thread(decode_photo, data, self.screen)
        taken_at = None
        try:
            ts = float(headers.get("X-Photo-Timestamp", "") or 0)
            taken_at = ts if ts > 0 else None
        except ValueError:
            pass
        place = urllib.parse.unquote(headers.get("X-Photo-Place", "") or "") or None
        self._serial += 1
        self.state.set_photo(Photo(img, taken_at=taken_at, place=place, serial=self._serial))
        logger.info("Photo Frames: new photo (%dx%d), next check in %.0fs", img.width, img.height, delay)
        return delay
