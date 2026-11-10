"""Render thread: decides what to show, builds scenes, crossfades and pushes frames."""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
import time

from PIL import Image

from .config import FrameConfig
from .display import Display
from .renderer import Box, NowPlayingPopup, Renderer, Scene, draw_text, rounded_mask
from .state import MediaInfo, SharedState, Snapshot, Track
from .sysstats import StatsReader

logger = logging.getLogger(__name__)

# After the track changes, wait this long for the new cover to arrive before redrawing,
# so a track change is one smooth crossfade instead of "placeholder, then cover".
ARTWORK_GRACE_S = 2.0
# Let bursts of updates (metadata + artwork + state) settle before rebuilding.
SETTLE_S = 0.25
# Stop showing a pairing code that was never cleared (pairing abandoned).
PAIRING_TIMEOUT_S = 300
# How often the CPU / RAM / temperature overlay is refreshed.
STATS_INTERVAL_S = 5.0
# Frame rate of the scrolling "now playing" popup on the photo screen. A rotated display
# rewrites the whole screen for every update, so it scrolls in fewer, bigger steps.
POPUP_FPS = 20
POPUP_FPS_ROTATED = 5


def pixel_shift_offset(max_px: int, period_s: float, now_wall: float) -> tuple[int, int]:
    """Where the picture sits now: a slow walk over points around the centre (OLED burn-in).

    Two rings (full and half radius, offset by half a step) so the edges of static elements
    land on many different pixels over time. Changes once per `period_s`.
    """
    if max_px <= 0:
        return (0, 0)
    step = int(now_wall // max(1.0, period_s)) % 16
    radius = max_px if step % 2 == 0 else max_px / 2
    angle = (step // 2) * (math.tau / 8) + (0 if step % 2 == 0 else math.tau / 16)
    return (round(radius * math.cos(angle)), round(radius * math.sin(angle)))


def popup_text(track: Track, media: MediaInfo | None) -> tuple[str, str]:
    """The "now playing" popup line and its icon kind.

    Without Music Assistant's info, a track without a length is taken to be a radio stream.
    """
    kind = media.kind if media else ("music" if track.duration_ms else "radio")
    source = media.source if media else None
    artist = track.artist or track.album_artist
    if kind == "music":
        parts = [track.title, artist, source or track.album]
    elif kind == "radio":
        parts = [source, track.title, artist]
    else:  # podcast / audiobook: the show or book, then the episode or chapter
        parts = [source, track.title]
    seen: set[str] = set()
    out = []
    for part in parts:
        if part and part.strip().casefold() not in seen:
            seen.add(part.strip().casefold())
            out.append(part.strip())
    return "  ·  ".join(out), kind


def _parse_window(spec: str) -> tuple[dt.time, dt.time] | None:
    if not spec:
        return None
    try:
        a, b = (s.strip() for s in spec.split("-"))
        return dt.time.fromisoformat(a), dt.time.fromisoformat(b)
    except ValueError:
        logger.error('Invalid display.sleep "%s" (expected e.g. "23:00-07:00"), ignoring', spec)
        return None


def _in_window(window: tuple[dt.time, dt.time] | None) -> bool:
    if window is None:
        return False
    now = dt.datetime.now().time()
    start, end = window
    return start <= now < end if start <= end else (now >= start or now < end)


class Engine(threading.Thread):
    def __init__(self, cfg: FrameConfig, state: SharedState, display: Display) -> None:
        super().__init__(name="sendspin-frame-render", daemon=True)
        self.cfg = cfg
        self.state = state
        self.display = display
        self.renderer = Renderer(cfg, (display.width, display.height))
        self._stop_event = threading.Event()
        self._sleep_window = _parse_window(cfg.display.sleep)
        self._scene: Scene | None = None
        self._frame: Image.Image | None = None  # the picture as rendered
        self._shown: Image.Image | None = None  # the same, moved by the pixel shift
        self._offset = (0, 0)
        self._dyn_key = None
        self._powered = True
        self._paused = False
        # Track-change bookkeeping for the artwork grace period.
        self._np_track_key: tuple | None = None
        self._np_art_url: str | None = None
        self._np_art_serial = -1
        self._pending_key = None
        self._pending_since = 0.0
        # Top-left corner overlay: the system stats, and under them the update popup.
        self._notice: str | None = None
        self._stats = StatsReader()
        self._stats_text = ""
        self._stats_on = False
        self._stats_bg: Image.Image | None = None  # the corner without the overlay
        self._stats_next = 0.0
        # "Now playing" popup next to the status badge, on the photo screen during music.
        self._popup_want: tuple[str, bool, str] | None = None  # (text, paused, kind)
        self._popup_start = 0.0  # when the current text first showed (scroll position)
        self._popup: NowPlayingPopup | None = None
        self._popup_bg: Image.Image | None = None  # the area without the popup
        self._popup_key = None

    def stop(self) -> None:
        self._stop_event.set()
        self.state.changed.set()

    # ------------------------------------------------------------------ decisions

    def _music_on(self, snap: Snapshot, now: float) -> bool:
        """Music is playing, or paused / stopped only a short while ago."""
        np_cfg = self.cfg.now_playing
        age = now - snap.playback_since
        return snap.connected and snap.track is not None and (
            snap.playback == "playing"
            or (snap.playback == "paused" and age < np_cfg.paused_timeout)
            or (snap.playback == "stopped" and age < np_cfg.stopped_timeout)
        )

    def _mode(self, snap: Snapshot, now: float) -> str:
        if snap.pairing_code and now - snap.pairing_since < PAIRING_TIMEOUT_S:
            return "pairing"
        if self._music_on(snap, now):
            if snap.music_screen == "photos" and snap.photo is not None:
                return "photo"
            return "np"
        if _in_window(self._sleep_window):
            return "off"
        idle = self.cfg.idle.mode
        if idle == "blank":
            return "off"
        if idle == "photos" and snap.photo is not None:
            return "photo"
        return "clock"

    def _scene_key(self, mode: str, snap: Snapshot) -> tuple:
        size = (self.display.width, self.display.height)
        if mode == "np":
            assert snap.track is not None
            return ("np", snap.track.content_key(), snap.artwork_serial, size)
        if mode == "photo":
            assert snap.photo is not None
            return ("photo", snap.photo.serial, snap.connected, size)
        if mode == "clock":
            return ("clock", snap.connected, snap.server_name, size)
        if mode == "pairing":
            return ("pairing", snap.pairing_code, size)
        return ("blank", size)

    def _build(self, mode: str, snap: Snapshot) -> Scene:
        r = self.renderer
        if mode == "np":
            track = snap.track or Track()
            return r.now_playing(track, snap.artwork, snap.artwork_serial, lambda: self._paused,
                                 lambda: self.state.snapshot().track)
        if mode == "photo":
            assert snap.photo is not None
            return r.photo(snap.photo, snap.connected)
        if mode == "clock":
            return r.clock(snap.connected, snap.server_name)
        if mode == "pairing":
            return r.pairing(snap.pairing_code or "")
        return r.blank()

    def _ready_to_build(self, mode: str, snap: Snapshot, key: tuple, now: float) -> bool:
        """Debounce, and give the artwork a moment to follow a track change."""
        if key != self._pending_key:
            self._pending_key, self._pending_since = key, now
        waited = now - self._pending_since
        if waited < SETTLE_S and self._scene is not None:
            return False
        if mode != "np" or snap.track is None or self._scene is None:
            return True
        track_changed = snap.track.content_key() != self._np_track_key
        art_expected = bool(snap.track.artwork_url) and snap.track.artwork_url != self._np_art_url
        art_arrived = snap.artwork_serial != self._np_art_serial
        if track_changed and art_expected and not art_arrived and waited < ARTWORK_GRACE_S:
            return False
        return True

    # ------------------------------------------------------------------ output

    def _set_power(self, on: bool) -> None:
        if on != self._powered:
            logger.info("Screen %s", "on" if on else "off")
            self._powered = on
            self.display.set_power(on)

    def _shift(self, img: Image.Image) -> Image.Image:
        dx, dy = self._offset
        if not (dx or dy):
            return img
        # Cropping outside the picture fills with black: OLED pixels that are simply off.
        return img.crop((-dx, -dy, img.width - dx, img.height - dy))

    def _show_full(self, frame: Image.Image) -> None:
        self._shown = self._shift(frame)
        self.display.show(self._shown)

    def _update_pixel_shift(self) -> None:
        d = self.cfg.display
        offset = pixel_shift_offset(d.pixel_shift, d.pixel_shift_minutes * 60, time.time())
        if offset != self._offset:
            self._offset = offset
            if self._frame is not None and self._powered:
                self._show_full(self._frame)

    def _paste(self, region: Image.Image, box: Box) -> None:
        """Update one part of the picture (and of the screen)."""
        if self._frame is None or self._shown is None:
            return
        self._frame.paste(region, box[:2])
        dx, dy = self._offset
        if self._shown is not self._frame:
            self._shown.paste(region, (box[0] + dx, box[1] + dy))
        self.display.show(self._shown, (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))

    # ------------------------------------------------------------------ stats overlay

    def _stats_box(self) -> Box:
        """Room for the stats line and, under it, the update popup."""
        W, H = self.display.width, self.display.height
        k = min(W, H)
        return (0, 0, int(W * 0.5), int(k * 0.03) + int(k * 0.026 * 1.6) + int(k * 0.026 * 2.1))

    def _draw_stats(self, region: Image.Image) -> None:
        """The stats line (when on) and the update popup (when there is one)."""
        k = min(self.display.width, self.display.height)
        size = k * 0.026
        font = self.renderer.fonts.get("medium", size)
        m = y = int(k * 0.03)
        if self._stats_on:
            draw_text(region, (m, y), self._stats_text, font, alpha=0.85, anchor="lt", shadow=0.7)
            y += int(size * 1.6)
        if self._notice:
            # A dark pill, like the "now playing" popup.
            h = int(size * 1.9)
            pad = int(h * 0.45)
            w = min(region.width - m, math.ceil(font.getlength(self._notice)) + 2 * pad)
            shape = rounded_mask((w, h), h // 2).point(lambda v: v * 150 // 255)
            region.paste((0, 0, 0), (m, y), shape)
            draw_text(region, (m + pad, y + h / 2), self._notice, font, alpha=0.92, anchor="lm")

    def _update_stats(self, snap: Snapshot, now: float) -> None:
        """Switch the stats and the update popup on / off; refresh the numbers every few seconds."""
        want, notice = snap.show_stats, snap.update_notice
        if (want, notice) == (self._stats_on, self._notice) and (
                not want or now < self._stats_next):
            return
        self._stats_on, self._notice = want, notice
        if self._frame is None or self._stats_bg is None:
            return
        region = self._stats_bg.copy()
        if want:
            self._stats_text = self._stats.read().text()
            self._stats_next = now + STATS_INTERVAL_S
        self._draw_stats(region)
        self._paste(region, self._stats_box())

    # ------------------------------------------------------------------ now playing popup

    def _popup_content(self, mode: str, snap: Snapshot,
                       now: float) -> tuple[str, bool, str] | None:
        """(text, paused, media kind) for the popup, or None for no popup."""
        if mode != "photo" or not self.cfg.photos.show_now_playing or not self._music_on(snap, now):
            return None
        assert snap.track is not None
        text, kind = popup_text(snap.track, snap.media)
        return (text, snap.playback != "playing", kind) if text else None

    def _make_popup(self, now: float) -> None:
        """(Re)build the popup for the current content on the current background."""
        self._popup, self._popup_key = None, None
        if self._popup_want is None or self._popup_bg is None:
            return
        if self._scene is None or self._scene.key[0] != "photo":
            return
        fps = POPUP_FPS_ROTATED if self.cfg.display.rotate else POPUP_FPS
        self._popup = self.renderer.now_playing_popup(self._popup_bg, *self._popup_want,
                                                      self._popup_start, fps)

    def _update_popup(self, now: float) -> None:
        """Show / hide / change the popup, and move the scrolling text along."""
        if self._frame is None or self._popup_bg is None:
            return
        current = self._popup.content if self._popup else None
        if current != self._popup_want:
            self._make_popup(now)
            if self._popup is None:
                if current is not None:
                    self._paste(self._popup_bg, self.renderer.popup_box())
                return
        if self._popup is not None:
            key = self._popup.key(now)
            if key != self._popup_key:
                self._paste(self._popup.draw(now), self.renderer.popup_box())
                self._popup_key = key

    def _crossfade(self, new: Image.Image) -> None:
        old = self._shown
        ms = self.cfg.display.transition_ms
        if old is None or ms <= 0 or old.size != new.size:
            self._show_full(new)
            return
        new_shown = self._shift(new)
        duration = ms / 1000
        frame_dt = 1 / max(1, self.cfg.display.transition_fps)
        start = time.monotonic()
        while True:
            t = (time.monotonic() - start) / duration
            if t >= 1:
                break
            eased = t * t * (3 - 2 * t)  # smoothstep
            self.display.show(Image.blend(old, new_shown, eased), fast=True)
            # Pace the animation; if the Pi is slow we simply show fewer frames.
            sleep_for = frame_dt - ((time.monotonic() - start) % frame_dt)
            time.sleep(max(0.0, sleep_for))
        self._shown = new_shown
        self.display.show(new_shown)

    def _present(self, scene: Scene, now: float, animate: bool) -> None:
        frame = scene.frame(now)
        box = self._stats_box()
        self._stats_bg = frame.crop(box)
        if (self._stats_on or self._notice) and scene.key[0] != "blank":
            if self._stats_on and not self._stats_text:
                self._stats_text = self._stats.read().text()
            region = self._stats_bg.copy()
            self._draw_stats(region)
            frame.paste(region, box[:2])
        self._scene = scene
        pbox = self.renderer.popup_box()
        self._popup_bg = frame.crop(pbox)
        self._make_popup(now)
        if self._popup is not None:
            frame.paste(self._popup.draw(now), pbox[:2])
            self._popup_key = self._popup.key(now)
        if animate:
            self._crossfade(frame)
        else:
            self._show_full(frame)
        self._frame = frame
        self._dyn_key = scene.dynamic_key(now) if scene.dynamic_key else None

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        try:
            # Linux nice values are per thread: keep rendering below audio in priority.
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (OSError, AttributeError):
            pass
        while not self._stop_event.is_set():
            try:
                timeout = self._tick()
            except Exception:  # noqa: BLE001 - never let the display die
                logger.exception("Render error")
                timeout = 2.0
            self.state.changed.wait(timeout)
            self.state.changed.clear()

    def _tick(self) -> float:
        now = time.monotonic()
        snap = self.state.snapshot()
        mode = self._mode(snap, now)
        self._paused = snap.playback != "playing"

        if mode == "off":
            if self._powered:
                self._present(self.renderer.blank(), now, animate=True)
                self._set_power(False)
            return 5.0
        if not self._powered:
            self._set_power(True)
            self._frame = Image.new("RGB", (self.display.width, self.display.height))
            self._shown = self._frame
            self._scene = None
        self._update_pixel_shift()

        want = self._popup_content(mode, snap, now)
        if want is not None and (self._popup_want is None or want[0] != self._popup_want[0]):
            self._popup_start = now  # new text: scroll from the start
        self._popup_want = want

        key = self._scene_key(mode, snap)
        if self._scene is None or self._scene.key != key:
            if not self._ready_to_build(mode, snap, key, now):
                return 0.1
            t0 = time.monotonic()
            scene = self._build(mode, snap)
            logger.debug("Built %s scene in %.0f ms", mode, (time.monotonic() - t0) * 1000)
            if mode == "np" and snap.track is not None:
                self._np_track_key = snap.track.content_key()
                self._np_art_url = snap.track.artwork_url
                self._np_art_serial = snap.artwork_serial
            self._present(scene, time.monotonic(), animate=True)
        elif self._scene.dynamic_key is not None:
            dkey = self._scene.dynamic_key(now)
            if dkey != self._dyn_key:
                result = self._scene.dynamic_region(now)
                if result is not None:
                    self._paste(*result)
                self._dyn_key = dkey
        self._update_stats(snap, now)
        self._update_popup(now)
        wake = 1.0
        if self._stats_on:
            wake = max(0.05, min(wake, self._stats_next - now))
        if self._popup is not None:
            wake = max(0.02, min(wake, self._popup.next_change(now)))

        if mode == "np":
            # Wake shortly after the next whole second of track position.
            track = snap.track
            pos = track.position_ms(now) if track else None
            if pos is not None and not self._paused:
                return min(wake, max(0.05, (1000 - pos % 1000) / 1000 + 0.02))
        return wake
