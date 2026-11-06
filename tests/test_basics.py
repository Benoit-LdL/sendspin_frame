"""Fast tests that need no screen, audio device or server."""

from __future__ import annotations

import time

import numpy as np
import pytest
from PIL import Image

from sendspin_frame.config import load_config
from sendspin_frame.screen import decode_artwork, trim_letterbox
from sendspin_frame.display import FramebufferDisplay
from sendspin_frame.photoframe import image_url
from sendspin_frame.renderer import Renderer, ellipsize, fmt_time, wrap
from sendspin_frame.state import Photo, Track


def test_config_roundtrip(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[sendspin]\nname = "Kitchen"\nextra_args = ["--static-delay-ms", "50"]\n'
                 '[display]\nrotate = 90\n[photos]\nurl = "https://x/apps/photo_frames/abc"\n')
    cfg = load_config(str(p))
    assert cfg.sendspin.name == "Kitchen"
    assert cfg.sendspin.extra_args == ["--static-delay-ms", "50"]
    assert cfg.display.rotate == 90
    assert cfg.photos.url.endswith("abc")


def test_config_rejects_bad_values(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[display]\nrotate = 45\n")
    with pytest.raises(ValueError):
        load_config(str(p))


def test_example_config_loads():
    load_config("config.example.toml")


def test_track_position_extrapolates_and_pauses():
    t = Track(progress_ms=10_000, duration_ms=60_000, speed=1000, anchor_mono=100.0)
    assert t.position_ms(102.5) == 12_500
    assert Track(progress_ms=10_000, speed=0, anchor_mono=100.0).position_ms(150) == 10_000
    assert t.position_ms(1000) == 60_000  # clamped to duration


def test_text_helpers():
    r = Renderer(load_config(None), (800, 480))
    font = r.fonts.get("bold", 30)
    assert ellipsize("x" * 500, font, 200).endswith("…")
    lines = wrap("word " * 100, font, 300, 2)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert fmt_time(3_725_000) == "1:02:05" and fmt_time(65_000) == "1:05"


@pytest.mark.parametrize("size", [(800, 480), (1024, 600), (720, 1280)])
def test_scenes_render(size):
    r = Renderer(load_config(None), size)
    now = time.monotonic()
    t = Track(title="Song", artist="Artist", album="Album", progress_ms=1000,
              duration_ms=200_000, anchor_mono=now)
    art = Image.new("RGB", (300, 300), (200, 40, 40))
    for scene in (r.now_playing(t, art, 1, lambda: False), r.now_playing(t, None, 0, lambda: True),
                  r.photo(Photo(Image.new("RGB", (640, 480), (10, 120, 200)), place="Here")),
                  r.clock(False, None)):
        assert scene.frame(now).size == size
        if scene.dyn_box:
            region, box = scene.dynamic_region(now)
            assert region.size == (box[2] - box[0], box[3] - box[1])


def test_rgb565_conversion():
    d = FramebufferDisplay.__new__(FramebufferDisplay)
    d.bpp, d.phys_w, d.phys_h, d._dither_img = 16, 8, 8, None
    px = np.frombuffer(d._convert(Image.new("RGB", (8, 8), (255, 128, 7)), 0, fast=True), "<u2")
    assert (px == ((255 >> 3) << 11 | (128 >> 2) << 5 | (7 >> 3))).all()
    white = np.frombuffer(d._convert(Image.new("RGB", (8, 8), (255, 255, 255))), "<u2")
    assert (white == 0xFFFF).all()
    d.bpp = 32
    assert d._convert(Image.new("RGB", (1, 1), (1, 2, 3))) == b"\x03\x02\x01\x00"


def test_trim_letterbox():
    img = Image.new("RGB", (300, 300), (0, 0, 0))
    img.paste((180, 90, 60), (50, 0, 250, 300))
    assert trim_letterbox(img).size == (200, 300)
    full = Image.new("RGB", (300, 300), (180, 90, 60))
    assert trim_letterbox(full).size == (300, 300)


def test_decode_artwork_downscales_big_covers():
    import io
    buf = io.BytesIO()
    Image.new("RGB", (3000, 3000), (200, 120, 40)).save(buf, "JPEG")
    img = decode_artwork(buf.getvalue(), 600)
    assert img is not None and max(img.size) <= 600
    assert decode_artwork(b"not an image", 600) is None


def test_photoframe_url():
    assert image_url("https://c/apps/photo_frames/abc/") == "https://c/apps/photo_frames/abc/image"
    assert image_url("https://c/apps/photo_frames/abc/image") == "https://c/apps/photo_frames/abc/image"


def test_music_screen_setting_picks_photos_or_cover():
    from sendspin_frame.config import FrameConfig
    from sendspin_frame.display import PngDisplay
    from sendspin_frame.engine import Engine
    from sendspin_frame.state import SharedState

    state = SharedState()
    engine = Engine(FrameConfig(), state, PngDisplay("/dev/null", 320, 180))
    state.set_connection(True, "MA")
    state.set_track(Track(title="T", artist="A"))
    state.set_playback("playing")
    now = time.monotonic()
    assert engine._mode(state.snapshot(), now) == "np"
    state.set_music_screen("photos")
    assert engine._mode(state.snapshot(), now) == "np"  # no photo yet: keep the cover
    state.set_photo(Photo(Image.new("RGB", (32, 18)), serial=1))
    assert engine._mode(state.snapshot(), now) == "photo"
    state.set_music_screen("cover")
    assert engine._mode(state.snapshot(), now) == "np"
