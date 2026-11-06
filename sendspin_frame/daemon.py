"""The official `sendspin daemon`, extended with the metadata + artwork roles and a screen.

Audio playback, clock sync, volume, reconnects and settings are 100% the upstream sendspin
daemon. We only (1) ask the server for metadata and album artwork as well, and (2) forward
what arrives to the render thread.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from aiosendspin.client import SendspinClient
from aiosendspin.models.artwork import ArtworkChannel
from aiosendspin.models.core import GroupUpdateServerPayload, ServerStatePayload
from aiosendspin.models.metadata import SessionUpdateMetadata
from aiosendspin.models.types import ArtworkSource, PictureFormat, Roles
from PIL import Image, UnidentifiedImageError
from sendspin.daemon import daemon as daemon_mod

from .config import FrameConfig
from .display import Display, open_display
from .engine import Engine
from .photoframe import PhotoFrameFetcher
from .state import SharedState, Track

logger = logging.getLogger(__name__)

ARTWORK_URL_FALLBACK_S = 3.0


def _decode(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return _trim_letterbox(img.convert("RGB"))
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning("Could not decode artwork: %s", exc)
        return None


def _trim_letterbox(img: Image.Image) -> Image.Image:
    """The server pads non-square artwork with black bars; cut them off again."""
    bbox = img.convert("L").point(lambda v: 255 if v > 6 else 0).getbbox()
    if not bbox:
        return img
    x0, y0, x1, y1 = bbox
    w, h = img.size
    # Only trim symmetric bars on one axis (that's what letterboxing looks like).
    horiz = x0 > w * 0.03 and abs(x0 - (w - x1)) <= 3 and y0 <= 2 and y1 >= h - 2
    vert = y0 > h * 0.03 and abs(y0 - (h - y1)) <= 3 and x0 <= 2 and x1 >= w - 2
    return img.crop(bbox) if horiz or vert else img


class FrameRuntime:
    """Owns the screen, the render thread and the photo fetcher; listens to the client."""

    def __init__(self, cfg: FrameConfig) -> None:
        self.cfg = cfg
        self.state = SharedState()
        self.display: Display | None = None
        self.engine: Engine | None = None
        self._tasks: list[asyncio.Task] = []
        self._decoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artwork")
        self._client: SendspinClient | None = None
        self._group_playback: str | None = None
        self._art_received_at = 0.0
        self._art_url: str | None = None
        self._art_url_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        d = self.cfg.display
        self.display = open_display(d.device, d.width, d.height, d.rotate, d.hide_console,
                                    d.console_tty)
        self.engine = Engine(self.cfg, self.state, self.display)
        self.engine.start()
        if self.cfg.photos.url and self.cfg.idle.mode == "photos":
            fetcher = PhotoFrameFetcher(self.cfg.photos, self.state,
                                        (self.display.width, self.display.height))
            self._tasks.append(asyncio.create_task(fetcher.run(), name="photoframe"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.engine is not None:
            self.engine.stop()
            await asyncio.to_thread(self.engine.join, 5)
        if self.display is not None:
            self.display.close()
        self._decoder.shutdown(wait=False, cancel_futures=True)

    @property
    def artwork_size(self) -> int:
        assert self.engine is not None, "start() must run before the client is created"
        return max(64, min(1200, self.engine.renderer.artwork_request_size()))

    # ------------------------------------------------------------------ client wiring

    def attach(self, client: SendspinClient) -> None:
        self._client = client
        client.add_metadata_listener(self._on_metadata)
        client.add_group_update_listener(self._on_group_update)
        client.add_artwork_listener(self._on_artwork)
        client.add_disconnect_listener(self._on_disconnect)

    def _mark_connected(self) -> None:
        info = self._client.server_info if self._client else None
        snap = self.state.snapshot()
        name = info.name if info else None
        if not snap.connected or snap.server_name != name:
            self.state.set_connection(True, name)

    def _on_disconnect(self) -> None:
        self._group_playback = None
        self.state.set_connection(False)

    def _on_group_update(self, payload: GroupUpdateServerPayload) -> None:
        self._mark_connected()
        if payload.playback_state is not None:
            self._group_playback = payload.playback_state.value
            self.state.set_playback(self._group_playback)

    def _on_metadata(self, payload: ServerStatePayload | None) -> None:
        self._mark_connected()
        md = getattr(payload, "metadata", None) if payload is not None else None
        if not isinstance(md, SessionUpdateMetadata) or not (md.title or md.artist):
            self.state.set_track(None)
            return
        progress_ms = duration_ms = None
        speed = 1000
        if md.progress is not None:
            speed = md.progress.playback_speed
            duration_ms = md.progress.track_duration or None
            pos = self._client.current_track_position() if self._client else None
            progress_ms = pos if pos is not None else md.progress.track_progress
        logger.debug("Metadata: %r", md)
        track = Track(
            title=md.title, artist=md.artist, album=md.album, album_artist=md.album_artist,
            artwork_url=md.artwork_url, year=md.year, progress_ms=progress_ms,
            duration_ms=duration_ms, speed=speed, anchor_mono=time.monotonic(),
        )
        self.state.set_track(track)
        if md.progress is not None:
            # Playback speed 0 means paused, whatever the group state says (yet).
            if speed == 0 and self.state.snapshot().playback == "playing":
                self.state.set_playback("paused")
            elif speed > 0 and self._group_playback in (None, "playing"):
                self.state.set_playback("playing")
        if md.artwork_url and md.artwork_url != self._art_url:
            self._art_url = md.artwork_url
            if self._art_url_task:
                self._art_url_task.cancel()
            self._art_url_task = asyncio.create_task(self._artwork_url_fallback(md.artwork_url))

    def _on_artwork(self, channel: int, data: bytes) -> None:
        if channel != 0:
            return
        self._art_received_at = time.monotonic()
        if not data:
            self.state.set_artwork(None)
            return
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._decoder, _decode, data)
        fut.add_done_callback(
            lambda f: None if f.cancelled() or f.exception() else self.state.set_artwork(f.result())
        )

    async def _artwork_url_fallback(self, url: str) -> None:
        """If the server announces an artwork URL but never sends the image, fetch it."""
        started = time.monotonic()
        await asyncio.sleep(ARTWORK_URL_FALLBACK_S)
        if self._art_received_at >= started or not url.startswith(("http://", "https://")):
            return
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as r:
                r.raise_for_status()
                data = await r.read()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Artwork URL fallback failed: %s", exc)
            return
        if self._art_received_at < started and url == self._art_url:
            img = await asyncio.get_running_loop().run_in_executor(self._decoder, _decode, data)
            if img is not None:
                logger.info("Using artwork from URL (server sent no artwork image)")
                self.state.set_artwork(img)


class FrameDaemon(daemon_mod.SendspinDaemon):
    """Upstream daemon + metadata/artwork roles + the screen."""

    runtime: FrameRuntime  # set by the CLI before the daemon is constructed

    async def run(self) -> int:
        await self.runtime.start()
        try:
            return await super().run()
        finally:
            await self.runtime.stop()

    def _create_client(self) -> SendspinClient:
        original = daemon_mod.SendspinClient
        size = self.runtime.artwork_size

        def factory(*args, **kwargs) -> SendspinClient:
            roles = list(kwargs.get("roles", []))
            for role in (Roles.METADATA, Roles.ARTWORK):
                if role not in roles:
                    roles.append(role)
            kwargs["roles"] = roles
            kwargs.setdefault("artwork_channels", [
                ArtworkChannel(source=ArtworkSource.ALBUM, format=PictureFormat.JPEG,
                               width=size, height=size),
            ])
            return original(*args, **kwargs)

        daemon_mod.SendspinClient = factory  # type: ignore[misc]
        try:
            return super()._create_client()
        finally:
            daemon_mod.SendspinClient = original  # type: ignore[misc]

    def _attach_client(self, client: SendspinClient) -> None:
        super()._attach_client(client)
        self.runtime.attach(client)
