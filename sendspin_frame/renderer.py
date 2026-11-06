"""Pillow-based scene rendering: now playing, photo slideshow and idle clock.

Every scene is split in a static `base` image (rendered once) and an optional small dynamic
region (progress bar, clock) that is re-rendered on top of a clean copy of the base. That way a
progress update only touches a few rows of the framebuffer, which keeps the Pi Zero idle.
"""

from __future__ import annotations

import colorsys
import functools
import glob
import hashlib
import logging
import os
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .config import FrameConfig, StyleConfig
from .state import Photo, Track

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]
WHITE = (255, 255, 255)

# --------------------------------------------------------------------------------------- fonts

_FONT_FILES = {
    "light": ["Inter-Light", "DejaVuSans-ExtraLight", "DejaVuSans"],
    "regular": ["Inter-Regular", "DejaVuSans"],
    "medium": ["Inter-Medium", "Inter-Regular", "DejaVuSans"],
    "semibold": ["Inter-SemiBold", "Inter-Bold", "DejaVuSans-Bold"],
    "bold": ["Inter-Bold", "DejaVuSans-Bold"],
}


def _font_dirs() -> list[str]:
    dirs = []
    if env := os.environ.get("SENDSPIN_FRAME_FONTS"):
        dirs.append(env)
    dirs.append(str(Path(__file__).resolve().parent.parent / "fonts"))
    dirs += ["/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".local/share/fonts")]
    return dirs


@functools.cache
def _find_font(stem: str) -> str | None:
    for d in _font_dirs():
        for ext in ("ttf", "otf"):
            hits = glob.glob(f"{d}/**/{stem}.{ext}", recursive=True)
            if hits:
                return sorted(hits)[0]
    return None


class Fonts:
    def __init__(self, style: StyleConfig) -> None:
        self._paths: dict[str, str | None] = {}
        for weight, stems in _FONT_FILES.items():
            override = style.font_bold if weight in ("bold", "semibold") else style.font_regular
            path = override if override and Path(override).exists() else None
            for stem in stems:
                if path:
                    break
                path = _find_font(stem)
            self._paths[weight] = path
        logger.info("Fonts: %s", {k: Path(v).name if v else "builtin" for k, v in self._paths.items()})

    @functools.lru_cache(maxsize=64)  # noqa: B019 - one Fonts instance per process
    def get(self, weight: str, size: int) -> ImageFont.FreeTypeFont:
        size = max(6, int(size))
        path = self._paths.get(weight)
        if path:
            return ImageFont.truetype(path, size)
        return ImageFont.load_default(size)


# ------------------------------------------------------------------------------------- helpers


def blurred_fill(img: Image.Image, size: tuple[int, int], brightness: float,
                 saturation: float = 1.25) -> Image.Image:
    """Cheap, smooth 'frosted' background: blur a tiny version, then scale up."""
    w, h = size
    small = ImageOps.fit(img.convert("RGB"), (max(8, w // 20), max(8, h // 20)),
                         Image.Resampling.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(radius=max(1.5, small.width / 18)))
    small = ImageEnhance.Color(small).enhance(saturation)
    small = ImageEnhance.Brightness(small).enhance(brightness)
    bg = small.resize((w, h), Image.Resampling.BICUBIC)
    return _vignette(bg)


def _vignette(img: Image.Image, strength: float = 0.45) -> Image.Image:
    w, h = img.size
    # A radial mask built small and scaled up; cheap and smooth.
    sw, sh = 64, max(8, int(64 * h / w))
    mask = Image.radial_gradient("L").resize((sw, sh))
    mask = mask.point(lambda v: int(255 - min(255, max(0, v - 70) * 1.7) * strength))
    mask = mask.resize((w, h), Image.Resampling.BICUBIC)
    black = Image.new("RGB", img.size, (0, 0, 0))
    return Image.composite(img, black, mask)


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    ss = 3  # supersampled for smooth corners
    big = Image.new("L", (size[0] * ss, size[1] * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, big.width - 1, big.height - 1),
                                          radius=radius * ss, fill=255)
    return big.resize(size, Image.Resampling.LANCZOS)


def drop_shadow(base: Image.Image, box: Box, radius: int, blur: int, opacity: float,
                offset: tuple[int, int] = (0, 0)) -> None:
    x0, y0, x1, y1 = box
    pad = blur * 3
    sh = Image.new("L", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad, pad + x1 - x0, pad + y1 - y0),
                                         radius=radius, fill=int(255 * opacity))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    base.paste((0, 0, 0), (x0 - pad + offset[0], y0 - pad + offset[1]), sh)


def draw_text(base: Image.Image, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont,
              color: tuple[int, int, int] = WHITE, alpha: float = 1.0, anchor: str = "la",
              shadow: float = 0.0) -> None:
    """Alpha-blended text on an RGB image (ImageDraw alone can't blend on RGB)."""
    if not text:
        return
    left, top, right, bottom = font.getbbox(text, anchor=anchor)
    pad = max(2, int(font.size * 0.25)) if shadow else 1
    w, h = right - left + 2 * pad, bottom - top + 2 * pad
    ox, oy = int(xy[0]) + left - pad, int(xy[1]) + top - pad
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((pad - left, pad - top), text, font=font,
                              fill=int(255 * alpha), anchor=anchor)
    if shadow:
        sm = mask.filter(ImageFilter.GaussianBlur(max(1, font.size // 10)))
        sm = sm.point(lambda v: int(v * shadow))
        base.paste((0, 0, 0), (ox, oy + max(1, font.size // 24)), sm)
    base.paste(color, (ox, oy), mask)


def ellipsize(text: str, font: ImageFont.FreeTypeFont, max_w: float) -> str:
    if font.getlength(text) <= max_w:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.getlength(text[:mid].rstrip() + ell) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell


def wrap(text: str, font: ImageFont.FreeTypeFont, max_w: float, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for i, word in enumerate(words):
        trial = f"{cur} {word}".strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
            continue
        lines.append(cur)
        cur = word
        if len(lines) == max_lines - 1:
            cur = " ".join(words[i:])
            break
    if cur:
        lines.append(cur)
    lines = lines[:max_lines]
    return [ellipsize(line, font, max_w) for line in lines]


def accent_color(img: Image.Image) -> tuple[int, int, int]:
    """A vivid but readable colour from the artwork, for the progress bar."""
    small = img.convert("RGB").resize((40, 40), Image.Resampling.BILINEAR)
    q = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = q.getpalette() or []
    best, best_score = None, 0.0
    for count, idx in q.getcolors() or []:
        r, g, b = (c / 255 for c in palette[idx * 3 : idx * 3 + 3])
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        score = count * (s ** 1.2) * (0.25 + v)
        if s > 0.22 and v > 0.18 and score > best_score:
            best, best_score = (h, s, v), score
    if best is None:
        return (236, 236, 236)
    h, s, v = best
    r, g, b = colorsys.hsv_to_rgb(h, min(s, 0.75), max(v, 0.88))
    return int(r * 255), int(g * 255), int(b * 255)


def placeholder_art(size: int, seed: str) -> Image.Image:
    """A simple 'record' tile, tinted from the track name, when there's no cover art."""
    hue = int(hashlib.md5(seed.encode()).hexdigest()[:4], 16) / 0xFFFF
    c1 = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(hue, 0.55, 0.42))
    c2 = tuple(int(v * 255) for v in colorsys.hsv_to_rgb((hue + 0.08) % 1, 0.65, 0.18))
    grad = Image.linear_gradient("L").resize((size, size))
    tile = Image.composite(Image.new("RGB", (size, size), c2), Image.new("RGB", (size, size), c1), grad)
    d = ImageDraw.Draw(tile, "RGBA")
    cx = cy = size / 2
    for frac, alpha in ((0.36, 70), (0.30, 40), (0.24, 40), (0.11, 120)):
        r = size * frac
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, alpha),
                  width=max(1, size // 160))
    r = size * 0.02
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(235, 235, 235))
    return tile


STATUS_OK = (48, 209, 88)
STATUS_ERROR = (255, 69, 58)


MA_LOGO = Path(__file__).parent / "assets" / "music-assistant.png"


@functools.cache
def _ma_logo_mask() -> Image.Image:
    """The Music Assistant logo as an alpha mask (from the Music Assistant frontend, Apache-2.0)."""
    return Image.open(MA_LOGO).getchannel("A")


def status_badge(size: int, connected: bool) -> Image.Image:
    """The Music Assistant logo on a small dark disc: green = connected, red = not."""
    ss = 4  # supersampled, then scaled down for smooth edges
    s = size * ss
    badge = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(badge).ellipse((0, 0, s - 1, s - 1), fill=(0, 0, 0, 150))
    logo = int(s * 0.58)
    mask = _ma_logo_mask().resize((logo, logo), Image.Resampling.LANCZOS)
    pos = ((s - logo) // 2, (s - logo) // 2 - s // 40)  # optically centred (house shape)
    badge.paste((*(STATUS_OK if connected else STATUS_ERROR), 255), pos, mask)
    return badge.resize((size, size), Image.Resampling.LANCZOS)


def fmt_time(ms: int) -> str:
    s = max(0, int(ms // 1000))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def bottom_gradient(size: tuple[int, int], frac: float, max_alpha: int) -> Image.Image:
    w, h = size
    gh = max(2, int(h * frac))
    ramp = Image.linear_gradient("L").resize((1, gh)).point(lambda v: int((v / 255) ** 1.6 * max_alpha))
    mask = Image.new("L", size, 0)
    mask.paste(ramp.resize((w, gh)), (0, h - gh))
    return mask


# -------------------------------------------------------------------------------------- scenes


@dataclass
class Scene:
    key: Hashable
    base: Image.Image
    dyn_box: Box | None = None
    # (region image to draw into, monotonic now) -> None
    draw_dynamic: Callable[[Image.Image, float], None] | None = None
    # monotonic now -> key; the dynamic region is redrawn when it changes
    dynamic_key: Callable[[float], Hashable] | None = None

    def frame(self, now: float) -> Image.Image:
        img = self.base.copy()
        if self.dyn_box and self.draw_dynamic:
            region = img.crop(self.dyn_box)
            self.draw_dynamic(region, now)
            img.paste(region, self.dyn_box[:2])
        return img

    def dynamic_region(self, now: float) -> tuple[Image.Image, Box] | None:
        if not (self.dyn_box and self.draw_dynamic):
            return None
        region = self.base.crop(self.dyn_box)
        self.draw_dynamic(region, now)
        return region, self.dyn_box


class Renderer:
    def __init__(self, cfg: FrameConfig, size: tuple[int, int]) -> None:
        self.cfg = cfg
        self.size = size
        self.fonts = Fonts(cfg.style)

    @property
    def portrait(self) -> bool:
        return self.size[1] > self.size[0]

    # ---------------------------------------------------------------- now playing

    def artwork_request_size(self) -> int:
        if self.cfg.now_playing.artwork_size:
            return self.cfg.now_playing.artwork_size
        w, h = self.size
        return int(min(w * 0.9, h * 0.55) if self.portrait else min(h * 0.84, w * 0.46))

    def now_playing(self, track: Track, art: Image.Image | None, art_serial: int,
                    paused_fn: Callable[[], bool],
                    live_track: Callable[[], Track | None] | None = None) -> Scene:
        """`live_track` returns the latest metadata of the same song (seek / pause updates)."""
        W, H = self.size
        np_cfg = self.cfg.now_playing
        m = int(min(W, H) * 0.085)

        if self.portrait:
            art_size = int(min(W - 2 * m, H * 0.55))
            art_box = ((W - art_size) // 2, int(m * 1.4))
            text_x, text_w, align = m, W - 2 * m, "center"
        else:
            art_size = int(min(H - 2 * m, W * 0.46))
            art_box = (int(m * 1.1), (H - art_size) // 2)
            text_x = art_box[0] + art_size + int(m * 0.95)
            text_w, align = W - text_x - m, "left"

        seed = f"{track.album or ''}{track.title or ''}"
        cover = (ImageOps.fit(art.convert("RGB"), (art_size, art_size), Image.Resampling.LANCZOS)
                 if art is not None else placeholder_art(art_size, seed))
        accent = accent_color(cover) if art is not None else (236, 236, 236)

        base = blurred_fill(cover, (W, H), self.cfg.style.background_brightness)
        radius = max(4, art_size // 36)
        ax, ay = art_box
        drop_shadow(base, (ax, ay, ax + art_size, ay + art_size), radius,
                    blur=max(4, art_size // 22), opacity=0.6, offset=(0, art_size // 50))
        base.paste(cover, art_box, rounded_mask(cover.size, radius))

        # ---- text block
        k = min(H, W * 0.75) if not self.portrait else min(W * 1.05, H * 0.62)
        f_title = self.fonts.get("bold", k * 0.074)
        f_artist = self.fonts.get("medium", k * 0.05)
        f_album = self.fonts.get("regular", k * 0.038)
        f_time = self.fonts.get("medium", k * 0.03)

        title = track.title or "Unknown title"
        artist = track.artist or track.album_artist or ""
        album = track.album if np_cfg.show_album else ""
        if album and track.year:
            album = f"{album}  ·  {track.year}"

        title_lines = wrap(title, f_title, text_w, 2)
        lines: list[tuple[str, ImageFont.FreeTypeFont, float, float]] = []  # text, font, alpha, gap
        for i, line in enumerate(title_lines):
            lines.append((line, f_title, 1.0, k * 0.012 if i else 0))
        if artist:
            lines.append((ellipsize(artist, f_artist, text_w), f_artist, 0.88, k * 0.03))
        if album:
            lines.append((ellipsize(album, f_album, text_w), f_album, 0.58, k * 0.022))

        show_bar = np_cfg.show_progress and bool(track.duration_ms)
        bar_h = max(3, int(k * 0.009))
        bar_gap = k * 0.075
        time_gap = k * 0.022
        time_h = f_time.getbbox("0:00")[3]

        def line_h(font: ImageFont.FreeTypeFont) -> int:
            asc, desc = font.getmetrics()
            return asc + int(desc * 0.4)

        block_h = sum(line_h(f) + gap for _, f, _, gap in lines)
        dyn_h = bar_gap + bar_h + time_gap + time_h  # also used for the pause badge
        total_h = block_h + dyn_h

        if self.portrait:
            top = ay + art_size + (H - (ay + art_size) - total_h) / 2
        else:
            top = (H - total_h) / 2

        y = top
        for text, font, alpha, gap in lines:
            y += gap
            if align == "center":
                draw_text(base, (W / 2, y), text, font, alpha=alpha, anchor="ma")
            else:
                draw_text(base, (text_x, y), text, font, alpha=alpha)
            y += line_h(font)

        bar_y = int(y + bar_gap)
        dyn_box = (max(0, text_x - 2), int(bar_y - bar_h * 2), min(W, text_x + text_w + 2),
                   int(bar_y + bar_h + time_gap + time_h * 1.6))

        def current() -> Track:
            t = live_track() if live_track else None
            return t if t is not None and t.content_key() == track.content_key() else track

        def position(now: float) -> int:
            return current().position_ms(now) or 0

        def dynamic_key(now: float) -> Hashable:
            return (position(now) // 1000 if show_bar else 0, paused_fn())

        def draw_dynamic(region: Image.Image, now: float) -> None:
            ox, oy = dyn_box[0], dyn_box[1]
            d = ImageDraw.Draw(region, "RGBA")
            by = bar_y - oy
            x0, x1 = text_x - ox, text_x - ox + text_w
            ty = by + bar_h + time_gap
            paused = paused_fn()
            if show_bar:
                pos = position(now)
                dur = current().duration_ms or track.duration_ms or 1
                frac = max(0.0, min(1.0, pos / dur))
                d.rounded_rectangle((x0, by, x1, by + bar_h), radius=bar_h // 2,
                                    fill=(255, 255, 255, 60))
                xf = x0 + int((x1 - x0) * frac)
                if xf > x0 + bar_h:
                    d.rounded_rectangle((x0, by, xf, by + bar_h), radius=bar_h // 2,
                                        fill=(*accent, 255))
                draw_text(region, (x0, ty), fmt_time(pos), f_time, alpha=0.62)
                draw_text(region, (x1, ty), "-" + fmt_time(dur - pos + 999), f_time, alpha=0.62,
                          anchor="ra")
            if paused:
                # Small pause badge, centred under the bar.
                cx = (x0 + x1) // 2
                ih = int(time_h * 1.05)
                bw = max(2, ih // 3)
                top_y = ty + (time_h - ih) // 2 + 1
                label = "Paused"
                lw = f_time.getlength(label)
                start = cx - (ih + lw + ih * 0.5) / 2
                for dx in (0, bw * 1.7):
                    d.rounded_rectangle((start + dx, top_y, start + dx + bw, top_y + ih),
                                        radius=max(1, bw // 3), fill=(255, 255, 255, 200))
                draw_text(region, (start + ih + ih * 0.5, ty), label, f_time, alpha=0.8)

        key = ("np", track.content_key(), art_serial, self.size)
        return Scene(key, base, dyn_box, draw_dynamic, dynamic_key)

    # ---------------------------------------------------------------------- photo

    def photo(self, photo: Photo, connected: bool | None = None) -> Scene:
        """`connected`: draw the Music Assistant status badge (None = don't)."""
        W, H = self.size
        pc = self.cfg.photos
        key = ("photo", photo.serial, connected if pc.show_status else None, self.size)
        img = photo.image.convert("RGB")
        ratio_img, ratio_scr = img.width / img.height, W / H
        if pc.fit == "cover" or abs(ratio_img - ratio_scr) / ratio_scr < 0.1:
            base = ImageOps.fit(img, (W, H), Image.Resampling.LANCZOS)
        else:
            base = blurred_fill(img, (W, H), 0.45, saturation=1.1)
            fitted = ImageOps.contain(img, (W, H), Image.Resampling.LANCZOS)
            pos = ((W - fitted.width) // 2, (H - fitted.height) // 2)
            drop_shadow(base, (*pos, pos[0] + fitted.width, pos[1] + fitted.height), 0,
                        blur=max(4, min(W, H) // 60), opacity=0.5)
            base.paste(fitted, pos)

        has_overlay = pc.show_clock or pc.show_date or pc.show_info
        if has_overlay:
            base.paste((0, 0, 0), (0, 0), bottom_gradient((W, H), 0.32, 150))

        k = min(W, H)
        m = int(k * 0.06)
        if pc.show_info and (photo.place or photo.taken_at):
            f_place = self.fonts.get("medium", k * 0.036)
            f_when = self.fonts.get("regular", k * 0.03)
            y = H - m
            if photo.taken_at:
                when = time.strftime("%-d %B %Y", time.localtime(photo.taken_at))
                draw_text(base, (W - m, y), when, f_when, alpha=0.75, anchor="rd", shadow=0.6)
                y -= int(f_when.size * 1.45)
            if photo.place:
                place = ellipsize(photo.place, f_place, W * 0.45)
                draw_text(base, (W - m, y), place, f_place, alpha=0.95, anchor="rd", shadow=0.6)

        if pc.show_status and connected is not None:
            bs = max(20, int(k * 0.055))
            badge = status_badge(bs, connected)
            base.paste(badge, (W - m // 2 - bs, m // 2), badge)

        if not (pc.show_clock or pc.show_date):
            return Scene(key, base)

        f_clock = self.fonts.get("light" if k >= 600 else "regular", k * 0.17)
        f_date = self.fonts.get("medium", k * 0.04)
        box = (0, int(H * 0.55), int(W * 0.55), H)
        return Scene(key, base, box,
                     self._clock_drawer(box, f_clock, f_date, m, "left"), _minute_key)

    # ---------------------------------------------------------------------- clock

    def clock(self, connected: bool, server_name: str | None) -> Scene:
        W, H = self.size
        base = Image.new("RGB", (W, H), (9, 9, 12))
        base.paste((22, 22, 30), (0, 0), Image.radial_gradient("L").resize((W, H))
                   .point(lambda v: 255 - min(255, v * 1.3)))
        k = min(W, H)
        status = f"Connected to {server_name}" if connected and server_name else (
            "Connected" if connected else "Waiting for Music Assistant…")
        f_status = self.fonts.get("regular", k * 0.03)
        draw_text(base, (W / 2, H - k * 0.07), status, f_status, alpha=0.35, anchor="md")
        f_clock = self.fonts.get("light" if k >= 600 else "regular", k * 0.3)
        f_date = self.fonts.get("medium", k * 0.05)
        box = (0, int(H * 0.2), W, int(H * 0.8))
        return Scene(("clock", connected, server_name, self.size), base, box,
                     self._clock_drawer(box, f_clock, f_date, 0, "center"), _minute_key)

    def blank(self) -> Scene:
        return Scene(("blank", self.size), Image.new("RGB", self.size, (0, 0, 0)))

    def _clock_drawer(self, box: Box, f_clock: ImageFont.FreeTypeFont,
                      f_date: ImageFont.FreeTypeFont, margin: int, align: str):
        style = self.cfg.style
        show_clock = self.cfg.photos.show_clock if align == "left" else True
        show_date = self.cfg.photos.show_date if align == "left" else True

        def draw(region: Image.Image, now: float) -> None:
            lt = time.localtime()
            clock = time.strftime(style.clock_format, lt).lstrip("0") if "%I" in style.clock_format \
                else time.strftime(style.clock_format, lt)
            date = time.strftime(style.date_format, lt)
            rw, rh = region.size
            date_h = int(f_date.size * 1.5) if show_date else 0
            if align == "left":
                x, anchor_c, anchor_d = margin, "ls", "ls"
                y_date = rh - margin
                y_clock = y_date - date_h - int(f_clock.size * 0.04)
            else:
                x, anchor_c, anchor_d = rw / 2, "ms", "ms"
                y_clock = rh / 2 + f_clock.size * 0.32
                y_date = y_clock + date_h + f_date.size * 0.4
            if show_clock:
                draw_text(region, (x, y_clock), clock, f_clock, alpha=0.96, anchor=anchor_c,
                          shadow=0.55 if align == "left" else 0)
            if show_date:
                draw_text(region, (x, y_date if align != "left" or show_clock else rh - margin),
                          date, f_date, alpha=0.8, anchor=anchor_d,
                          shadow=0.55 if align == "left" else 0)

        return draw


def _minute_key(now: float) -> Hashable:
    return time.strftime("%Y%m%d%H%M")
