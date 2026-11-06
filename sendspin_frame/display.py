"""Output backends: the Linux framebuffer (no X / Wayland needed) and PNG files for testing."""

from __future__ import annotations

import fcntl
import logging
import mmap
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

logger = logging.getLogger(__name__)

FBIOBLANK = 0x4611
FB_BLANK_UNBLANK = 0
FB_BLANK_POWERDOWN = 4
KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01

Box = tuple[int, int, int, int]

# 4x4 Bayer matrix scaled to 0..15.
_BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.uint16)


class Display:
    """Base class. Coordinates are in the logical (already rotated) picture."""

    width: int
    height: int

    def show(self, img: Image.Image, box: Box | None = None, fast: bool = False) -> None:
        """Show `img` (the full picture); `box` limits the update to those rows.

        `fast` marks intermediate animation frames (quality may be traded for speed)."""
        raise NotImplementedError

    def set_power(self, on: bool) -> None:  # noqa: B027 - optional
        pass

    def close(self) -> None:  # noqa: B027 - optional
        pass


class FramebufferDisplay(Display):
    """Writes straight into /dev/fbN. Supports 16 bpp (RGB565) and 24/32 bpp."""

    def __init__(self, device: str, rotate: int, hide_console: bool, console_tty: str) -> None:
        name = Path(device).name
        sysfs = Path("/sys/class/graphics") / name
        phys_w, phys_h = (int(v) for v in (sysfs / "virtual_size").read_text().strip().split(","))
        self.bpp = int((sysfs / "bits_per_pixel").read_text().strip())
        stride_file = sysfs / "stride"
        self.stride = (
            int(stride_file.read_text().strip()) if stride_file.exists() else phys_w * self.bpp // 8
        )
        if self.bpp not in (16, 24, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp} bpp")
        self.phys_w, self.phys_h = phys_w, phys_h
        self.rotate = rotate
        self.width, self.height = (phys_h, phys_w) if rotate in (90, 270) else (phys_w, phys_h)
        self._fd = os.open(device, os.O_RDWR)
        self._mm = mmap.mmap(self._fd, self.stride * phys_h, mmap.MAP_SHARED, mmap.PROT_WRITE)
        self._tty_fd: int | None = None
        self._dither_img: Image.Image | None = None
        if hide_console:
            self._enter_graphics_mode(console_tty)
        logger.info(
            "Framebuffer %s: %dx%d, %d bpp (picture %dx%d, rotate %d)",
            device, phys_w, phys_h, self.bpp, self.width, self.height, rotate,
        )

    def _enter_graphics_mode(self, tty: str) -> None:
        # In KD_GRAPHICS mode the kernel console stops drawing (no cursor, no login prompt).
        try:
            fd = os.open(tty, os.O_RDWR | os.O_NOCTTY)
            fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)
            self._tty_fd = fd
        except OSError as exc:
            logger.warning(
                "Could not put %s in graphics mode (%s); the console cursor may show. "
                "See README (vt.global_cursor_default=0).", tty, exc,
            )

    def _convert(self, img: Image.Image, y0: int = 0, fast: bool = False) -> bytes:
        if self.bpp == 32:
            return img.tobytes("raw", "BGRX")
        if self.bpp == 24:
            return img.tobytes("raw", "BGR")
        if not fast:
            # Ordered dither (saturating add in C), so blurred gradients don't band in 16 bpp.
            img = ImageChops.add(img, self._dither().crop((0, y0, img.width, y0 + img.height)))
        a = np.asarray(img)  # uint8, H x W x 3
        g = a[..., 1]
        out = np.empty((*a.shape[:2], 2), dtype=np.uint8)  # little-endian RGB565
        out[..., 0] = ((g << 3) & 0xE0) | (a[..., 2] >> 3)
        out[..., 1] = (a[..., 0] & 0xF8) | (g >> 5)
        return out.tobytes()

    def _dither(self) -> Image.Image:
        if self._dither_img is None:
            h, w = self.phys_h, self.phys_w
            tile = np.tile(_BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w].astype(np.uint8)
            rgb = np.stack([tile >> 1, tile >> 2, tile >> 1], axis=-1)  # R/B lose 3 bits, G 2
            self._dither_img = Image.fromarray(rgb, "RGB")
        return self._dither_img

    def show(self, img: Image.Image, box: Box | None = None, fast: bool = False) -> None:
        if img.mode != "RGB":
            img = img.convert("RGB")
        if self.rotate:
            # Rotated output: always push the full picture (simple and still fast enough).
            transpose = {
                90: Image.Transpose.ROTATE_270,
                180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_90,
            }[self.rotate]
            img = img.transpose(transpose)
            box = None
        if box is None:
            y0, y1 = 0, self.phys_h
        else:
            y0, y1 = max(0, box[1]), min(self.phys_h, box[3])
            if y1 <= y0:
                return
        # Only whole rows are written, so the update is a single contiguous memcpy per row band.
        band = img.crop((0, y0, self.phys_w, y1)) if (y0, y1) != (0, self.phys_h) else img
        data = self._convert(band, y0, fast)
        row_bytes = self.phys_w * self.bpp // 8
        if row_bytes == self.stride:
            self._mm[y0 * self.stride : y1 * self.stride] = data
        else:
            for i, y in enumerate(range(y0, y1)):
                off = y * self.stride
                self._mm[off : off + row_bytes] = data[i * row_bytes : (i + 1) * row_bytes]

    def set_power(self, on: bool) -> None:
        try:
            fcntl.ioctl(self._fd, FBIOBLANK, FB_BLANK_UNBLANK if on else FB_BLANK_POWERDOWN)
        except OSError as exc:
            logger.debug("FBIOBLANK not supported: %s", exc)

    def close(self) -> None:
        try:
            self._mm[:] = b"\x00" * len(self._mm)
        except (ValueError, OSError):
            pass
        self._mm.close()
        os.close(self._fd)
        if self._tty_fd is not None:
            try:
                fcntl.ioctl(self._tty_fd, KDSETMODE, KD_TEXT)
            except OSError:
                pass
            os.close(self._tty_fd)


class PngDisplay(Display):
    """Renders into a PNG file (atomically replaced) - for development on a desktop machine."""

    def __init__(self, path: str, width: int, height: int) -> None:
        self.path = Path(path)
        self.width, self.height = width or 1280, height or 720
        self.frames_written = 0

    def show(self, img: Image.Image, box: Box | None = None, fast: bool = False) -> None:
        if fast:
            return  # don't bother writing animation frames to disk
        tmp = self.path.with_suffix(".tmp.png")
        img.save(tmp, compress_level=1)
        tmp.replace(self.path)
        self.frames_written += 1


def open_display(device: str, width: int, height: int, rotate: int,
                 hide_console: bool, console_tty: str) -> Display:
    if device.startswith("png:"):
        return PngDisplay(device[4:], width, height)
    return FramebufferDisplay(device, rotate, hide_console, console_tty)
