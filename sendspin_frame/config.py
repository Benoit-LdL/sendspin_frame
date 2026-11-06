"""Configuration for the display side of sendspin-frame (TOML file)."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SendspinConfig:
    # Player name shown in Music Assistant (empty = the hostname).
    name: str = ""
    # Audio output, see `sendspin audio-devices list` (empty = system default).
    audio_device: str = ""
    # Extra `sendspin daemon` command line options, e.g. ["--static-delay-ms", "120"].
    extra_args: list[str] = field(default_factory=list)


@dataclass
class DisplayConfig:
    # "/dev/fb0" for the real screen, "png:/path/file.png" to render into a PNG (testing).
    device: str = "/dev/fb0"
    # Output size override; 0 = use the framebuffer's size (PNG backend defaults to 1280x720).
    width: int = 0
    height: int = 0
    # Software rotation of the whole picture: 0, 90, 180 or 270.
    rotate: int = 0
    # Crossfade length between screens / photos (0 disables animations).
    transition_ms: int = 700
    transition_fps: int = 25
    # Turn the screen off in this window, e.g. "23:00-07:00" (empty = never).
    # The screen always wakes up when music starts playing.
    sleep: str = ""
    # Put the console into graphics mode so the text cursor / login prompt never shows.
    hide_console: bool = True
    console_tty: str = "/dev/tty1"


@dataclass
class NowPlayingConfig:
    # When playback is paused, keep showing the track for this long, then go idle.
    paused_timeout: int = 90
    # When playback stops, go idle after this many seconds.
    stopped_timeout: int = 5
    show_album: bool = True
    show_progress: bool = True
    # Requested artwork size in pixels (0 = derive from the screen size).
    artwork_size: int = 0


@dataclass
class PhotosConfig:
    # Public Photo Frames URL: https://cloud.example.com/apps/photo_frames/<token>
    url: str = ""
    # "contain" shows the whole photo on a blurred fill, "cover" fills the screen (crops).
    fit: str = "contain"
    show_clock: bool = True
    show_date: bool = True
    # Show when / where the photo was taken (from the Photo Frames headers).
    show_info: bool = True
    # Never poll faster than this, even if the frame rotates every minute.
    min_refresh: int = 20
    # Verify TLS certificates (set false for self-signed Nextcloud certs).
    verify_tls: bool = True


@dataclass
class IdleConfig:
    # What to show when nothing is playing: "photos", "clock" or "blank" (screen off).
    # "photos" falls back to "clock" while no photo is available.
    mode: str = "photos"


@dataclass
class StyleConfig:
    # Optional font overrides (paths to .ttf/.otf files).
    font_regular: str = ""
    font_bold: str = ""
    # 24h clock ("%H:%M") or e.g. "%I:%M" for 12h.
    clock_format: str = "%H:%M"
    date_format: str = "%A %-d %B"
    # Background darkening for the blurred artwork, 0 (black) .. 1 (unchanged).
    background_brightness: float = 0.42


@dataclass
class FrameConfig:
    sendspin: SendspinConfig = field(default_factory=SendspinConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    now_playing: NowPlayingConfig = field(default_factory=NowPlayingConfig)
    photos: PhotosConfig = field(default_factory=PhotosConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    style: StyleConfig = field(default_factory=StyleConfig)


def _apply(obj: Any, data: dict[str, Any], prefix: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            logger.warning("Unknown config key %s%s (ignored)", prefix, key)
            continue
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"[{prefix}{key}] must be a table")
            _apply(current, value, f"{prefix}{key}.")
            continue
        if isinstance(current, bool) and not isinstance(value, bool):
            raise ValueError(f"{prefix}{key} must be true or false")
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{prefix}{key} must be a number")
            value = type(current)(value)
        if isinstance(current, list) and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            raise ValueError(f"{prefix}{key} must be a list of strings")
        if isinstance(current, str) and not isinstance(value, str):
            raise ValueError(f"{prefix}{key} must be a string")
        setattr(obj, key, value)


def load_config(path: str | None) -> FrameConfig:
    """Load the TOML config; a missing file just gives defaults."""
    cfg = FrameConfig()
    if not path:
        return cfg
    p = Path(path)
    if not p.exists():
        logger.warning("Config file %s not found, using defaults", p)
        return cfg
    with p.open("rb") as fh:
        _apply(cfg, tomllib.load(fh), "")
    if cfg.display.rotate not in (0, 90, 180, 270):
        raise ValueError("display.rotate must be 0, 90, 180 or 270")
    if cfg.photos.fit not in ("contain", "cover"):
        raise ValueError('photos.fit must be "contain" or "cover"')
    if cfg.idle.mode not in ("photos", "clock", "blank"):
        raise ValueError('idle.mode must be "photos", "clock" or "blank"')
    return cfg
