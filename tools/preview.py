"""Render sample screens to PNG files, to tweak the look without a Pi or a server.

    python tools/preview.py --out previews --size 1024x600 --art cover.jpg --photo photo.jpg
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sendspin_frame.config import FrameConfig  # noqa: E402
from sendspin_frame.renderer import Renderer  # noqa: E402
from sendspin_frame.state import Photo, Track  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="previews")
    ap.add_argument("--size", action="append", default=[])
    ap.add_argument("--art", help="cover art image")
    ap.add_argument("--photo", help="photo image")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sizes = args.size or ["1024x600", "800x480", "1920x1080", "720x1280"]
    art = Image.open(args.art) if args.art else None
    photo_img = Image.open(args.photo) if args.photo else None
    now = time.monotonic()
    track = Track(title="Everything In Its Right Place", artist="Radiohead",
                  album="Kid A", year=2000, progress_ms=83_000, duration_ms=251_000,
                  anchor_mono=now)
    long_track = Track(title="A Really Quite Long Song Title That Needs To Wrap Onto Two Lines (Live)",
                       artist="Some Artist featuring Another Artist and Friends",
                       album="Live at the Venue", progress_ms=5_000, duration_ms=3_725_000,
                       anchor_mono=now)
    for s in sizes:
        w, h = (int(v) for v in s.split("x"))
        r = Renderer(FrameConfig(), (w, h))
        t0 = time.perf_counter()
        scene = r.now_playing(track, art, 1, lambda: False)
        scene.frame(now).save(out / f"np_{s}.png")
        t1 = time.perf_counter()
        r.now_playing(long_track, None, 0, lambda: True).frame(now).save(out / f"np_noart_{s}.png")
        if photo_img is not None:
            r.photo(Photo(photo_img, taken_at=1690000000, place="Ghent, Belgium")).frame(now).save(
                out / f"photo_{s}.png")
        r.clock(True, "Music Assistant").frame(now).save(out / f"clock_{s}.png")
        print(f"{s}: now-playing scene rendered in {(t1 - t0) * 1000:.0f} ms")


if __name__ == "__main__":
    main()
