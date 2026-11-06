"""A fake 'Music Assistant' for testing sendspin-frame without the real thing.

It connects to a sendspin-frame client (server-initiated, like Music Assistant does), plays
silence and walks through a scenario: track 1 -> track 2 -> pause -> stop. It also serves a
fake Nextcloud Photo Frames endpoint at http://127.0.0.1:8099/apps/photo_frames/test/image

    python tools/test_server.py --client ws://127.0.0.1:8928/sendspin --art a.jpg --art b.jpg \
        --photo p1.jpg --photo p2.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import logging
import time
from pathlib import Path

from aiohttp import web
from aiosendspin.models.types import PlaybackStateType
from aiosendspin.noise import Identity
from aiosendspin.server import AudioFormat, ClientAddedEvent, SendspinServer
from aiosendspin.server.roles.metadata.state import Metadata
from PIL import Image
from sendspin.serve.server import SendspinPlayerServer

FMT = AudioFormat(sample_rate=48000, bit_depth=16, channels=2)
CHUNK = b"\x00" * (48000 // 50 * 4)  # 20 ms of stereo 16-bit silence
log = logging.getLogger("test_server")


async def play(group, stop: asyncio.Event) -> None:
    stream = group.start_stream()
    try:
        while not stop.is_set():
            stream.prepare_audio(CHUNK, FMT)
            await stream.commit_audio()
            await stream.sleep_to_limit_buffer(max_buffer_us=2_000_000)
    finally:
        stream.stop()


async def scenario(group, arts: list[Image.Image], step: float) -> None:
    md = group.group_role("metadata")
    aw = group.group_role("artwork")
    stop = asyncio.Event()
    player = asyncio.create_task(play(group, stop))
    tracks = [
        Metadata(title="Everything In Its Right Place", artist="Radiohead", album="Kid A",
                 year=2000, artwork_url="http://example.invalid/a.jpg",
                 track_progress=12_000, track_duration=251_000, playback_speed=1000),
        Metadata(title="Teardrop", artist="Massive Attack", album="Mezzanine", year=1998,
                 artwork_url="http://example.invalid/b.jpg",
                 track_progress=0, track_duration=330_000, playback_speed=1000),
    ]
    for i, meta in enumerate(tracks):
        log.info("Now playing: %s", meta.title)
        md.set_metadata(meta)
        await asyncio.sleep(0.3)  # artwork arrives a bit after the metadata, like in real life
        await aw.set_album_artwork(arts[i % len(arts)] if arts else None)
        await asyncio.sleep(step)
    log.info("Pause")
    last = tracks[-1]
    md.set_metadata(Metadata(**{**last.__dict__, "track_progress": int(step * 1000),
                                "playback_speed": 0}))
    stop.set()
    await player
    group._set_playback_state(PlaybackStateType.PAUSED)  # private API; fine for a test
    await asyncio.sleep(step)
    log.info("Stop")
    await group.stop()
    log.info("Scenario done; client should now show the idle screen")


def photo_app(photos: list[Path], rotate_s: int) -> web.Application:
    async def image(request: web.Request) -> web.StreamResponse:
        if not photos:
            return web.Response(status=404)
        slot = int(time.time() // rotate_s)
        path = photos[slot % len(photos)]
        expires = email.utils.formatdate((slot + 1) * rotate_s, usegmt=True)
        return web.Response(body=path.read_bytes(), content_type="image/jpeg", headers={
            "Expires": expires, "X-Photo-Timestamp": "1690000000",
            "X-Photo-Place": "Ghent%2C%20Belgium", "X-Frame-Rotation-Unit": "minute",
        })

    app = web.Application()
    app.router.add_get("/apps/photo_frames/{token}/image", image)
    return app


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="ws://127.0.0.1:8928/sendspin")
    ap.add_argument("--art", action="append", default=[])
    ap.add_argument("--photo", action="append", default=[])
    ap.add_argument("--step", type=float, default=10)
    ap.add_argument("--photo-rotate", type=int, default=20)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    arts = [Image.open(p).convert("RGB") for p in args.art]

    runner = web.AppRunner(photo_app([Path(p) for p in args.photo], args.photo_rotate))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8099).start()

    loop = asyncio.get_running_loop()
    server = SendspinPlayerServer(loop=loop, identity=Identity.generate(), server_name="Fake MA")
    done = asyncio.Event()

    def on_event(srv: SendspinServer, event) -> None:
        if isinstance(event, ClientAddedEvent):
            client = srv.get_client(event.client_id)
            log.info("Client added: %s roles=%s", client.name if client else "?",
                     getattr(client, "roles", "?"))
            task = asyncio.create_task(scenario(client.group, arts, args.step))
            task.add_done_callback(lambda t: (t.exception() and log.error("scenario: %r", t.exception()), done.set()))

    server.add_event_listener(on_event)
    await server.start_server(port=8927, discover_clients=False)
    server.connect_to_client(args.client)
    await done.wait()
    await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
