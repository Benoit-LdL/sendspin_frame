"""The official `sendspin daemon`, extended with the metadata + artwork roles and a screen.

Audio playback, clock sync, volume, reconnects and settings are 100% the upstream sendspin
daemon. We only (1) ask the server for metadata and album artwork as well, and (2) forward
what arrives to the screen process (see screen.py), so drawing never competes with the audio.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiosendspin.client import SendspinClient
from aiosendspin.models.artwork import ArtworkChannel
from aiosendspin.models.core import GroupUpdateServerPayload, ServerStatePayload
from aiosendspin.models.metadata import SessionUpdateMetadata
from aiosendspin.models.types import ArtworkSource, PictureFormat, Roles
from sendspin.daemon import daemon as daemon_mod

from .config import FrameConfig
from .photoframe import PhotoFrameFetcher
from .screen import ScreenProcess
from .state import Track

logger = logging.getLogger(__name__)

ARTWORK_URL_FALLBACK_S = 3.0


class FrameRuntime:
    """Listens to the Sendspin client and forwards everything to the screen process."""

    def __init__(self, cfg: FrameConfig) -> None:
        self.cfg = cfg
        self.screen = ScreenProcess(cfg)
        self._tasks: list[asyncio.Task] = []
        self._client: SendspinClient | None = None
        self._group_playback: str | None = None
        # Mirror of what the screen knows, to avoid sending redundant updates.
        self._connected = False
        self._server_name: str | None = None
        self._playback = "stopped"
        self._music_screen = cfg.now_playing.screen
        self._show_stats = cfg.display.show_stats
        self._update_notice: str | None = None
        self._art_received_at = 0.0
        self._art_url: str | None = None
        self._art_url_task: asyncio.Task | None = None
        self.sendspin_id: str | None = None  # our player id on the Sendspin side

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await asyncio.to_thread(self.screen.start)
        # Also when idle.mode isn't "photos": the slideshow can be switched on during music.
        if self.screen.running and self.cfg.photos.url:
            fetcher = PhotoFrameFetcher(self.cfg.photos, self._on_photo)
            self._tasks.append(asyncio.create_task(fetcher.run(), name="photoframe"))
        ma = self.cfg.music_assistant
        if self.screen.running and ma.token:
            from .ma_api import MusicAssistantApi, api_url

            url = api_url(ma.url, self.cfg.sendspin.server_url)
            if url:
                api = MusicAssistantApi(url, ma.token, lambda: self.sendspin_id,
                                        lambda media: self.screen.send("media", media))
                self._tasks.append(asyncio.create_task(api.run(), name="music-assistant-api"))
            else:
                logger.warning("music_assistant.token is set but there is no address: "
                               "set music_assistant.url (or sendspin.server_url)")
        updates = None
        notice_on_screen = self.cfg.updates.show_on_screen and self.screen.running
        if self.cfg.updates.enabled and (self.cfg.mqtt.host or notice_on_screen):
            from .updates import UpdateManager

            # The helper needs root: install.sh adds the sudoers rule for it. Low priority,
            # so apt never competes with the audio.
            updates = UpdateManager(self.cfg.updates,
                                    ["nice", "-n", "10", "sudo", "-n", self.cfg.updates.helper])
            if notice_on_screen:
                updates.on_change = lambda: self._set_update_notice(updates.screen_notice())
            self._tasks.append(asyncio.create_task(updates.run(), name="updates"))
        if self.cfg.mqtt.host:
            from .mqtt import (
                MqttBridge,
                make_entities,
                make_stats_entities,
                make_update_entities,
            )

            entities = make_entities(lambda: self._music_screen, self.set_music_screen,
                                     lambda: self._show_stats, self.set_show_stats)
            stats: dict[str, object] = {}
            stats_keys: list[str] = []
            if self.cfg.mqtt.stats_interval > 0:
                stats_entities = make_stats_entities(stats.get)
                stats_keys = [e.key for e in stats_entities]
                entities += stats_entities
            if updates is not None:
                entities += make_update_entities(updates)
            bridge = MqttBridge(self.cfg.mqtt, self.cfg.sendspin.name, entities)
            if updates is not None:
                to_screen = updates.on_change

                def on_change() -> None:
                    bridge.notify()
                    to_screen()

                updates.on_change = on_change
            if stats_keys:
                self._tasks.append(asyncio.create_task(
                    self._publish_stats(bridge, stats, stats_keys), name="mqtt-stats"))
            self._tasks.append(asyncio.create_task(bridge.run(), name="mqtt"))

    async def _publish_stats(self, bridge, stats: dict[str, object], keys: list[str]) -> None:
        """Refresh the diagnostic sensors in Home Assistant (a few small /proc and /sys reads)."""
        from .sysstats import StatsReader

        reader = StatsReader()
        while True:
            stats.update(reader.details())
            bridge.notify(*keys)
            await asyncio.sleep(self.cfg.mqtt.stats_interval)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.to_thread(self.screen.stop)

    @property
    def artwork_size(self) -> int:
        return max(64, min(1200, self.screen.artwork_side))

    # ------------------------------------------------------------------ state forwarding

    def _set_connection(self, connected: bool, server_name: str | None = None) -> None:
        self._connected = connected
        self._server_name = server_name if connected else None
        if not connected:
            self._playback = "stopped"
        self.screen.send("connection", connected, self._server_name)

    def _set_playback(self, playback: str) -> None:
        if playback != self._playback:
            self._playback = playback
            self.screen.send("playback", playback)

    def set_music_screen(self, screen: str) -> None:
        """What to show while music plays: "cover" or "photos" (can change at any time)."""
        self._music_screen = screen
        self.screen.send("music_screen", screen)

    def set_show_stats(self, show: bool) -> None:
        """CPU / RAM / temperature overlay on the screen (can change at any time)."""
        self._show_stats = show
        self.screen.send("show_stats", show)

    def _set_update_notice(self, text: str | None) -> None:
        """The "update available" popup on the screen (None = no popup)."""
        if text != self._update_notice:
            self._update_notice = text
            self.screen.send("update_notice", text)

    def _on_photo(self, data: bytes | None, taken_at: float | None, place: str | None) -> None:
        self.screen.send("photo", data, taken_at, place)

    # ------------------------------------------------------------------ client wiring

    def attach(self, client: SendspinClient) -> None:
        self._client = client
        client.add_metadata_listener(self._on_metadata)
        client.add_group_update_listener(self._on_group_update)
        client.add_artwork_listener(self._on_artwork)
        client.add_disconnect_listener(self._on_disconnect)

    def _mark_connected(self) -> None:
        info = self._client.server_info if self._client else None
        name = info.name if info else None
        if not self._connected or self._server_name != name:
            self._set_connection(True, name)

    def _on_disconnect(self) -> None:
        self._group_playback = None
        self._set_connection(False)

    def _on_group_update(self, payload: GroupUpdateServerPayload) -> None:
        self._mark_connected()
        if payload.playback_state is not None:
            self._group_playback = payload.playback_state.value
            self._set_playback(self._group_playback)

    def _on_metadata(self, payload: ServerStatePayload | None) -> None:
        self._mark_connected()
        md = getattr(payload, "metadata", None) if payload is not None else None
        if not isinstance(md, SessionUpdateMetadata) or not (md.title or md.artist):
            self.screen.send("track", None)
            return
        progress_ms = duration_ms = None
        speed = 1000
        if md.progress is not None:
            speed = md.progress.playback_speed
            duration_ms = md.progress.track_duration or None
            pos = self._client.current_track_position() if self._client else None
            progress_ms = pos if pos is not None else md.progress.track_progress
        logger.debug("Metadata: %r", md)
        # anchor_mono is CLOCK_MONOTONIC, which is the same clock in the screen process.
        track = Track(
            title=md.title, artist=md.artist, album=md.album, album_artist=md.album_artist,
            artwork_url=md.artwork_url, year=md.year, progress_ms=progress_ms,
            duration_ms=duration_ms, speed=speed, anchor_mono=time.monotonic(),
        )
        self.screen.send("track", track)
        if md.progress is not None:
            # Playback speed 0 means paused, whatever the group state says (yet).
            if speed == 0 and self._playback == "playing":
                self._set_playback("paused")
            elif speed > 0 and self._group_playback in (None, "playing"):
                self._set_playback("playing")
        if md.artwork_url and md.artwork_url != self._art_url:
            self._art_url = md.artwork_url
            if self._art_url_task:
                self._art_url_task.cancel()
            self._art_url_task = asyncio.create_task(self._artwork_url_fallback(md.artwork_url))

    def _on_artwork(self, channel: int, data: bytes) -> None:
        if channel != 0:
            return
        self._art_received_at = time.monotonic()
        self.screen.send("artwork", data or None)

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
        if self._art_received_at < started and url == self._art_url and data:
            logger.info("Using artwork from URL (server sent no artwork image)")
            self.screen.send("artwork", data)


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

    async def _show_pairing_code(self, pairing_code: str | None, *, grouped: str | None) -> None:
        """Music Assistant asks for the code "shown on the device's screen": show it there."""
        await super()._show_pairing_code(pairing_code, grouped=grouped)
        self.runtime.screen.send("pairing", grouped)

    def _attach_client(self, client: SendspinClient) -> None:
        super()._attach_client(client)
        self.runtime.sendspin_id = self._args.identity.peer_id
        self.runtime.attach(client)
