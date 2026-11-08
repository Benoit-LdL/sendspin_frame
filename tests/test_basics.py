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
from sendspin_frame.state import Photo, SharedState, Track


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


def test_pixel_shift_stays_within_bounds_and_moves():
    from sendspin_frame.engine import pixel_shift_offset

    assert pixel_shift_offset(0, 180, 1e9) == (0, 0)
    offsets = {pixel_shift_offset(6, 180, i * 180.0) for i in range(16)}
    assert len(offsets) >= 12
    assert all(abs(dx) <= 6 and abs(dy) <= 6 for dx, dy in offsets)


def test_pixel_shift_moves_picture_and_partial_updates():
    from sendspin_frame.config import FrameConfig
    from sendspin_frame.display import Display
    from sendspin_frame.engine import Engine
    from sendspin_frame.state import SharedState

    class Capture(Display):
        width, height = 40, 20

        def __init__(self):
            self.last = None

        def show(self, img, box=None, fast=False):
            self.last = (img.copy(), box)

    cfg = FrameConfig()
    cfg.display.transition_ms = 0
    disp = Capture()
    engine = Engine(cfg, SharedState(), disp)
    engine._offset = (3, -2)
    frame = Image.new("RGB", (40, 20), (255, 255, 255))
    engine._show_full(frame)
    engine._frame = frame
    shown, _ = disp.last
    assert shown.getpixel((0, 10)) == (0, 0, 0)  # uncovered edge is black
    assert shown.getpixel((20, 10)) == (255, 255, 255)
    assert shown.getpixel((20, 19)) == (0, 0, 0)  # moved up by 2


def test_mqtt_discovery_payload():
    import json

    from sendspin_frame.config import MqttConfig
    from sendspin_frame.mqtt import MqttBridge, make_entities, node_id

    assert node_id("Living Room TV!") == "living_room_tv"
    got = {}
    ents = make_entities(lambda: "cover", lambda v: got.update(screen=v),
                         lambda: False, lambda v: got.update(stats=v))
    b = MqttBridge(MqttConfig(host="x"), "TV", ents)
    sel, sw = ents
    p = json.loads(b.discovery_payload(sel))
    assert p["command_topic"] == "sendspin_frame/tv/music_screen/set"
    assert p["options"] == ["Cover art", "Photo slideshow"]
    assert b.discovery_topic(sel) == "homeassistant/select/tv/music_screen/config"
    p = json.loads(b.discovery_payload(sw))
    assert (p["payload_on"], p["payload_off"]) == ("ON", "OFF")
    assert b.discovery_topic(sw) == "homeassistant/switch/tv/show_stats/config"
    assert sel.parse("Photo slideshow") == "photos" and sw.parse("ON") is True


def test_sysstats_reads_proc(tmp_path):
    from sendspin_frame.sysstats import StatsReader

    (tmp_path / "proc").mkdir()
    stat = tmp_path / "proc" / "stat"
    stat.write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (tmp_path / "proc" / "meminfo").write_text(
        "MemTotal:  1000 kB\nMemFree: 100 kB\nMemAvailable:  750 kB\n")
    therm = tmp_path / "sys" / "class/thermal/thermal_zone0"
    therm.mkdir(parents=True)
    (therm / "temp").write_text("52300\n")
    r = StatsReader(str(tmp_path / "proc"), str(tmp_path / "sys"))
    stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")  # 100 busy of 200 ticks
    s = r.read()
    assert (round(s.cpu_pct), round(s.ram_pct), round(s.temp_c)) == (50, 25, 52)
    assert s.text() == "CPU 50%   RAM 25%   52°C"


def test_stats_overlay_toggles_on_and_off():
    from sendspin_frame.config import FrameConfig
    from sendspin_frame.display import Display
    from sendspin_frame.engine import Engine

    class Capture(Display):
        width, height = 640, 360

        def __init__(self):
            self.boxes = []

        def show(self, img, box=None, fast=False):
            self.boxes.append(box)

    cfg = FrameConfig()
    cfg.display.transition_ms = 0
    disp = Capture()
    state = SharedState()
    engine = Engine(cfg, state, disp)
    engine._tick()  # idle clock screen
    before = engine._frame.crop(engine._stats_box())
    state.set_show_stats(True)
    engine._tick()
    with_stats = engine._frame.crop(engine._stats_box())
    assert disp.boxes[-1] == engine._stats_box()  # only the corner was redrawn
    assert before.tobytes() != with_stats.tobytes()
    state.set_show_stats(False)
    engine._tick()
    assert engine._frame.crop(engine._stats_box()).tobytes() == before.tobytes()


def test_photo_status_badge_follows_connection():
    from sendspin_frame.config import FrameConfig
    from sendspin_frame.renderer import STATUS_ERROR, STATUS_OK, Renderer, status_badge

    r = Renderer(FrameConfig(), (640, 360))
    photo = Photo(Image.new("RGB", (640, 360), (128, 128, 128)), serial=1)
    on, off = r.photo(photo, True), r.photo(photo, False)
    assert on.key != off.key  # the screen is redrawn when the connection changes
    colors = {c for _, c in status_badge(40, True).getcolors(4096)}
    assert any(c[:3] == STATUS_OK for c in colors)
    colors = {c for _, c in status_badge(40, False).getcolors(4096)}
    assert any(c[:3] == STATUS_ERROR for c in colors)


def test_pairing_code_takes_over_the_screen():
    from sendspin_frame.config import FrameConfig
    from sendspin_frame.display import PngDisplay
    from sendspin_frame.engine import Engine

    state = SharedState()
    engine = Engine(FrameConfig(), state, PngDisplay("/dev/null", 320, 180))
    now = time.monotonic()
    assert engine._mode(state.snapshot(), now) == "clock"
    state.set_pairing_code("123 456")
    assert engine._mode(state.snapshot(), time.monotonic()) == "pairing"
    assert engine._build("pairing", state.snapshot()).key[1] == "123 456"
    state.set_pairing_code(None)
    assert engine._mode(state.snapshot(), time.monotonic()) == "clock"


def test_now_playing_popup_on_photos_scrolls_long_text():
    from sendspin_frame.config import FrameConfig
    from sendspin_frame.display import Display
    from sendspin_frame.engine import Engine

    class Capture(Display):
        width, height = 640, 360

        def __init__(self):
            self.boxes = []

        def show(self, img, box=None, fast=False):
            self.boxes.append(box)

    cfg = FrameConfig()
    cfg.display.transition_ms = 0
    disp = Capture()
    state = SharedState()
    engine = Engine(cfg, state, disp)
    state.set_photo(Photo(Image.new("RGB", (640, 360), (128, 128, 128)), serial=1))
    state.set_connection(True, "MA")
    engine._tick()  # idle photos: no popup
    assert engine._popup is None
    pbox = engine.renderer.popup_box()
    empty = engine._frame.crop(pbox).tobytes()

    state.set_music_screen("photos")
    state.set_track(Track(title="Short", artist="A"))
    state.set_playback("playing")
    engine._tick()
    popup = engine._popup
    assert popup is not None and not popup.scrolls
    assert engine._frame.crop(pbox).tobytes() != empty
    assert disp.boxes[-1] == pbox  # only the popup was redrawn

    state.set_track(Track(title="A Really Quite Long Song Title That Will Never Fit (Live)",
                          artist="Some Artist featuring Another Artist"))
    engine._tick()
    popup = engine._popup
    assert popup.scrolls
    t0 = popup.start
    assert popup.offset(t0) == 0 and popup.next_change(t0) == pytest.approx(popup.REST_S)
    later = t0 + popup.REST_S + 1
    assert popup.offset(later) > 0
    assert popup.draw(t0).tobytes() != popup.draw(later).tobytes()

    state.set_playback("paused")
    engine._tick()
    assert engine._popup.offset(later) == 0  # paused: no scrolling

    state.set_playback("stopped")
    state.set_track(None)
    engine._tick()
    assert engine._popup is None
    assert engine._frame.crop(pbox).tobytes() == empty


def test_popup_text_and_media_kind():
    from sendspin_frame.engine import popup_text
    from sendspin_frame.state import MediaInfo

    song = Track(title="Song", artist="Band", album="Album", duration_ms=200_000)
    assert popup_text(song, None) == ("Song  ·  Band  ·  Album", "music")
    stream = Track(title="Song", artist="Band", album="Studio Brussel")
    assert popup_text(stream, None)[1] == "radio"  # no length: guessed to be radio
    assert popup_text(stream, MediaInfo("radio", "Studio Brussel")) == (
        "Studio Brussel  ·  Song  ·  Band", "radio")
    ep = Track(title="Episode 1", artist="The Show")
    assert popup_text(ep, MediaInfo("podcast", "The Show")) == ("The Show  ·  Episode 1", "podcast")
    book = Track(title="Dune", artist="Frank Herbert", duration_ms=1)
    assert popup_text(book, MediaInfo("audiobook", "Dune")) == ("Dune", "audiobook")


def test_music_assistant_api_parsing():
    from sendspin_frame.ma_api import api_url, find_player, media_from_player, media_from_queue
    from sendspin_frame.state import MediaInfo

    assert api_url("", "ws://10.0.0.5:8927/sendspin") == "ws://10.0.0.5:8095/ws"
    assert api_url("http://ma.local:8095", "") == "ws://ma.local:8095/ws"
    assert api_url("https://ma.example.com", "") == "wss://ma.example.com:8095/ws"
    assert api_url("", "") is None

    players = [
        {"player_id": "abc", "type": "protocol"},
        {"player_id": "up1234abcd", "type": "player",
         "output_protocols": [{"output_protocol_id": "abc"}]},
    ]
    assert find_player(players, "abc")["player_id"] == "up1234abcd"
    assert find_player(players[:1], "abc")["player_id"] == "abc"  # not wrapped yet
    assert find_player(players, "zzz") is None

    def queue(media_item):
        return {"queue_id": "q", "current_item": {"name": "Item", "media_item": media_item}}

    assert media_from_queue(queue({"media_type": "radio", "name": "Studio Brussel"})) == \
        MediaInfo("radio", "Studio Brussel")
    assert media_from_queue(queue({"media_type": "podcast_episode", "name": "Ep 1",
                                   "podcast": {"name": "The Show"}})) == \
        MediaInfo("podcast", "The Show")
    assert media_from_queue(queue({"media_type": "audiobook", "name": "Dune"})) == \
        MediaInfo("audiobook", "Dune")
    assert media_from_queue(queue({"media_type": "track", "name": "Song",
                                   "album": {"name": "Kid A"}})) == MediaInfo("music", "Kid A")
    assert media_from_queue(queue({"media_type": "track", "album": None})) == MediaInfo("music")
    assert media_from_queue({"current_item": None}) is None
    assert media_from_player({"current_media": {"media_type": "radio"}}) == MediaInfo("radio")
    assert media_from_player({"current_media": None}) is None
