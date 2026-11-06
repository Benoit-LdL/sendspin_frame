"""Thread-safe shared state between the asyncio side (Sendspin, Nextcloud) and the render thread."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace

from PIL import Image


@dataclass(frozen=True)
class Track:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    artwork_url: str | None = None
    year: int | None = None
    # Progress as reported by the server, anchored at `anchor_mono` (time.monotonic()).
    progress_ms: int | None = None
    duration_ms: int | None = None
    speed: int = 1000  # playback speed * 1000 (0 = paused)
    anchor_mono: float = 0.0

    def position_ms(self, now_mono: float) -> int | None:
        if self.progress_ms is None:
            return None
        pos = self.progress_ms + (now_mono - self.anchor_mono) * self.speed
        if self.duration_ms:
            pos = min(pos, self.duration_ms)
        return max(0, int(pos))

    def content_key(self) -> tuple:
        return (self.title, self.artist, self.album, self.album_artist)


@dataclass(frozen=True)
class Photo:
    image: Image.Image
    taken_at: float | None = None  # unix timestamp
    place: str | None = None
    serial: int = 0


@dataclass(frozen=True)
class Snapshot:
    connected: bool
    server_name: str | None
    playback: str  # "playing" | "paused" | "stopped"
    playback_since: float  # monotonic time of the last playback state change
    track: Track | None
    artwork: Image.Image | None
    artwork_serial: int
    photo: Photo | None
    music_screen: str  # "cover" | "photos": what to show while music plays
    show_stats: bool  # CPU / RAM / temperature overlay
    pairing_code: str | None  # shown full screen while Music Assistant pairs with us
    pairing_since: float
    version: int


class SharedState:
    """Holds the latest known state; the render thread waits on `changed`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.changed = threading.Event()
        self._snap = Snapshot(
            connected=False, server_name=None, playback="stopped",
            playback_since=time.monotonic(), track=None, artwork=None,
            artwork_serial=0, photo=None, music_screen="cover", show_stats=False,
            pairing_code=None, pairing_since=0.0, version=0,
        )

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snap

    def _update(self, **changes) -> None:
        with self._lock:
            if "playback" in changes and changes["playback"] != self._snap.playback:
                changes["playback_since"] = time.monotonic()
            self._snap = replace(self._snap, version=self._snap.version + 1, **changes)
        self.changed.set()

    def set_connection(self, connected: bool, server_name: str | None = None) -> None:
        if connected:
            self._update(connected=True, server_name=server_name)
        else:
            self._update(connected=False, server_name=None, playback="stopped")

    def set_playback(self, playback: str) -> None:
        self._update(playback=playback)

    def set_track(self, track: Track | None) -> None:
        self._update(track=track)

    def set_artwork(self, image: Image.Image | None) -> None:
        with self._lock:
            serial = self._snap.artwork_serial + 1
        self._update(artwork=image, artwork_serial=serial)

    def set_photo(self, photo: Photo | None) -> None:
        self._update(photo=photo)

    def set_music_screen(self, screen: str) -> None:
        self._update(music_screen=screen)

    def set_show_stats(self, show: bool) -> None:
        self._update(show_stats=show)

    def set_pairing_code(self, code: str | None) -> None:
        self._update(pairing_code=code, pairing_since=time.monotonic())
