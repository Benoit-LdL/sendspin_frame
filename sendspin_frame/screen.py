"""The screen runs in its own process, so drawing can never hold up the audio.

Python runs one thread at a time per process (the GIL). With rendering as a thread next to the
sendspin daemon, a track change (cover decode, scene build, crossfade) starved the audio code:
sync errors of several seconds, buffering, and pause/skip taking many seconds to take effect.

Now the audio process only forwards small messages (track metadata, playback state) and the
raw JPEG bytes of covers and photos. All image work - decoding, rendering, crossfades and the
framebuffer writes - happens in this child process, at a lower CPU priority.
"""

from __future__ import annotations

import io
import logging
import multiprocessing as mp
import os
import queue
import signal
import sys
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    from .config import FrameConfig

logger = logging.getLogger(__name__)

# Extra niceness for the screen process, on top of the service's own (Nice=-5 -> +10).
SCREEN_NICE = 15
DEFAULT_ARTWORK_SIDE = 600


def trim_letterbox(img: Image.Image) -> Image.Image:
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


def decode_artwork(data: bytes, side: int) -> Image.Image | None:
    """Decode a cover at (about) the size it is shown at.

    Covers fetched from an artwork URL are often the full-size original (3000x3000 is common).
    JPEG draft mode decodes those directly at 1/2, 1/4 or 1/8 scale, which is many times faster.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (side, side))
        img = trim_letterbox(img.convert("RGB"))
        if max(img.size) > side:
            img.thumbnail((side, side), Image.Resampling.LANCZOS)
        return img
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning("Could not decode artwork: %s", exc)
        return None


# ---------------------------------------------------------------------------- child process


def _screen_main(cfg: FrameConfig, inbox: mp.Queue, ready: Any) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    try:
        os.nice(SCREEN_NICE)
    except OSError:
        pass
    parent = os.getppid()
    # systemd sends SIGTERM to every process of the service: exit through `finally` below,
    # so the console is handed back properly.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    from .display import open_display
    from .engine import Engine
    from .photoframe import decode_photo
    from .state import Photo, SharedState

    d = cfg.display
    try:
        display = open_display(d.device, d.width, d.height, d.rotate, d.hide_console,
                               d.console_tty)
        state = SharedState()
        state.set_music_screen(cfg.now_playing.screen)
        state.set_show_stats(cfg.display.show_stats)
        engine = Engine(cfg, state, display)
    except Exception as exc:  # noqa: BLE001 - report to the parent, which keeps the audio going
        logger.exception("The screen could not be started")
        ready.send(("error", repr(exc)))
        return
    engine.start()
    side = engine.renderer.artwork_request_size()
    ready.send(("ready", side))
    ready.close()

    photo_serial = 0
    try:
        while True:
            try:
                msg = inbox.get(timeout=5)
            except queue.Empty:
                if os.getppid() != parent:  # the audio process is gone
                    break
                continue
            kind = msg[0]
            if kind == "stop":
                break
            if kind == "connection":
                state.set_connection(msg[1], msg[2])
            elif kind == "playback":
                state.set_playback(msg[1])
            elif kind == "track":
                state.set_track(msg[1])
            elif kind == "music_screen":
                state.set_music_screen(msg[1])
                logger.info("While music plays, showing: %s", msg[1])
            elif kind == "pairing":
                state.set_pairing_code(msg[1])
                if msg[1]:
                    logger.info("Showing the pairing code on screen")
            elif kind == "show_stats":
                state.set_show_stats(msg[1])
                logger.info("System stats on screen: %s", "on" if msg[1] else "off")
            elif kind == "artwork":
                state.set_artwork(decode_artwork(msg[1], side) if msg[1] else None)
            elif kind == "photo":
                _, data, taken_at, place = msg
                if data is None:
                    state.set_photo(None)
                    continue
                try:
                    img = decode_photo(data, (display.width, display.height))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Photo Frames: could not decode photo: %s", exc)
                    continue
                photo_serial += 1
                state.set_photo(Photo(img, taken_at=taken_at, place=place, serial=photo_serial))
                logger.info("Photo Frames: showing new photo (%dx%d)", img.width, img.height)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        engine.join(5)
        display.close()


# ---------------------------------------------------------------------------- parent side


class ScreenProcess:
    """Handle on the screen process, used from the audio (sendspin daemon) process."""

    def __init__(self, cfg: FrameConfig) -> None:
        # "spawn": a fresh interpreter, not a fork of the running asyncio loop and audio threads.
        ctx = mp.get_context("spawn")
        self._inbox: mp.Queue = ctx.Queue()
        self._ready_recv, ready_send = ctx.Pipe(duplex=False)
        self._ready_send = ready_send
        self._proc = ctx.Process(target=_screen_main, args=(cfg, self._inbox, ready_send),
                                 name="sendspin-frame-screen", daemon=True)
        self.artwork_side = DEFAULT_ARTWORK_SIDE
        self.running = False

    def start(self, timeout: float = 60) -> None:
        """Start the screen and wait until it's up. Blocking: call it from a thread."""
        self._proc.start()
        self._ready_send.close()
        try:
            if not self._ready_recv.poll(timeout):
                raise RuntimeError(f"no answer within {timeout:.0f}s")
            kind, value = self._ready_recv.recv()
        except (EOFError, OSError, RuntimeError) as exc:
            logger.error("Screen process did not start (%s); continuing with audio only", exc)
            return
        if kind != "ready":
            logger.error("Screen process failed (%s); continuing with audio only", value)
            return
        self.artwork_side = value
        self.running = True

    def send(self, *msg: Any) -> None:
        """Non-blocking: the queue's own feeder thread writes to the pipe."""
        if self.running and self._proc.is_alive():
            self._inbox.put_nowait(msg)

    def stop(self, timeout: float = 5) -> None:
        """Blocking: call it from a thread."""
        if self._proc.is_alive():
            self._inbox.put_nowait(("stop",))
            self._proc.join(timeout)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(2)
        self.running = False
