# sendspin-frame: project history & handoff

Notes on what was built so far and why, so work can resume in a new session (human or AI).
Last updated: 2026-11-06.

## Goal

A Raspberry Pi Zero 2 W on **Raspberry Pi OS Lite** (no desktop) that:
1. Acts as a **Sendspin** player for Music Assistant (synced with the other Sendspin speakers).
2. Shows **cover art, title, artist and progress** on a screen, simple and good-looking.
3. When idle, shows the **Nextcloud Photo Frames** slideshow (Photo Frames app on the same
   Nextcloud server).

## Key design decisions

| Decision | Why |
|---|---|
| One Python process: the **upstream `sendspin daemon`** with the metadata and artwork roles added | Reuses the official, tested audio and sync code. A second "display-only" client probably wouldn't be tied to the same player/group in MA. |
| Hooks into upstream by **subclassing `SendspinDaemon`** and swapping the class in `sendspin.daemon.daemon` before calling `sendspin.cli.main()` | Every `sendspin daemon` CLI option keeps working. `sendspin` is **pinned to 9.1.0** because this depends on its internals (`_create_client`, `_attach_client`). |
| Draws **straight to `/dev/fb0` with Pillow + numpy** (no X, Wayland, SDL or browser) | Snappy on Pi OS Lite and light on the Pi Zero's 512 MB RAM. |
| Photo Frames read **natively** via `GET <frame-url>/image` | Public endpoint. It returns the current photo plus `Expires`, `X-Photo-Timestamp` and `X-Photo-Place` headers, and the server decides the rotation. That's what the app's own JS page does, minus the browser. |
| Cover art from Sendspin's **artwork role** (JPEG at screen size), with `artwork_url` HTTP fallback after 3 s | No extra requests or scaling. The fallback covers servers that don't push images. |
| Static scene + small **dynamic region** (progress bar, clock) | A progress tick rewrites only a few framebuffer rows. |

## What's in the repo

```
sendspin_frame/__main__.py   entry point (sendspin-frame); config -> args for `sendspin daemon`
sendspin_frame/daemon.py     FrameDaemon (subclass) + FrameRuntime (listeners -> shared state)
sendspin_frame/screen.py     screen process (spawned): receives state, decodes images, runs the engine
sendspin_frame/engine.py     render thread: mode choice, debounce, artwork grace, crossfades
sendspin_frame/renderer.py   Pillow scenes: now playing, photo, clock; fonts; accent colour
sendspin_frame/display.py    framebuffer (16/24/32 bpp, RGB565 dither, rotation, FBIOBLANK,
                             KD_GRAPHICS console hiding) + PNG backend for testing
sendspin_frame/photoframe.py Nextcloud Photo Frames poller
sendspin_frame/config.py     TOML config (dataclasses, validation)
sendspin_frame/state.py      thread-safe shared state (asyncio <-> render thread)
tools/preview.py             render sample screens to PNG
tools/test_server.py         fake Music Assistant + fake Photo Frames, for end-to-end tests
tests/test_basics.py         11 unit tests (config, rendering, RGB565, letterbox trim, ...)
install.sh                   Pi installer (uv, venv in /opt/sendspin-frame, Inter font, user
                             `sendspin`, systemd, disables getty@tty1, cmdline.txt tweaks)
systemd/sendspin-frame.service
config.example.toml          all options, documented
docs/screenshots/            README screenshots
```

## API facts confirmed from source (aiosendspin 10.2.2 / sendspin 9.1.0)

- `SendspinClient(..., roles=[...], artwork_channels=[ArtworkChannel(source=ALBUM, format=JPEG, width, height)])`.
- Listeners:
  - `add_metadata_listener(cb(ServerStatePayload|None))`. Each metadata object **replaces** the
    previous one wholesale.
  - `add_group_update_listener` (has `playback_state`).
  - `add_artwork_listener(cb(channel, bytes))`. Empty bytes = clear.
  - `add_disconnect_listener`.
- `client.current_track_position()` gives the synced position in ms (None before time sync).
- The server **letterboxes** artwork with black bars to the requested size. We trim them.
- The Sendspin daemon listens on port 8928 and advertises `_sendspin._tcp` over mDNS. MA connects
  to it on its own.
- Python ≥ 3.12 is required. Every dependency has aarch64 wheels except `mpris-api` and `tunit`,
  which are tiny pure-Python sdists. 64-bit Pi OS only.

## First test on a real Pi (2026-11-06)

- The Pi ("pi-tv", on a 4K TV) previously ran `cage -- foot ~/.local/bin/sendspin` (pipx) from
  `~/.profile` with autologin. That was removed before installing.
- Audio works on the default ALSA device. The framebuffer came up at 1920x1080, 16 bpp.
- **Problem:** with rendering as a thread in the audio process, track changes caused
  `Sync error ... ms too large` (up to 5.8 s), buffering, and ~20 s pause latency. The plain
  `sendspin daemon` was snappy, so the GIL was the cause. MA only sends `artwork_url` (no artwork
  images), and the full-size originals were decoded at full size.
- **Fix:** all image work moved to a separate "spawn" process (`screen.py`), niced. The audio
  process forwards small messages plus raw JPEG bytes. Covers are decoded with JPEG draft mode at
  display size. **Not yet confirmed on the Pi.**
- Also added: `now_playing.screen = "cover" | "photos"` (slideshow during music; can change at run
  time via `FrameRuntime.set_music_screen`), tty1 opened write-only (the service user can't read it).
- A `display.resolution` setting was tried and removed again at the user's request: lowering the
  resolution (`video=` in cmdline.txt) is now a README tip instead.
- Added: `display.pixel_shift` (OLED burn-in: whole picture moves, black edges), and an MQTT
  bridge (`mqtt.py`) so Home Assistant gets a "Screen during music" select and a "System stats
  on screen" switch via MQTT discovery (states retained and restored on start).
- Also: `sendspin.delay_ms`, `sendspin.server_url` (MA missed a restarted player over mDNS),
  Music Assistant logo badge (green/red) on the photo screen, CPU/RAM/temp overlay
  (`sysstats.py`, drawn by the engine in the top-left corner on every screen).
- "Now playing" popup next to the badge on the photo screen (`photos.show_now_playing`), with
  an icon (music / radio / podcast / audiobook) and the album / station / show name. Sendspin
  doesn't send the media type, so `ma_api.py` asks Music Assistant's websocket API
  (ws://host:8095/ws, long-lived token in `music_assistant.token`). Our Sendspin id is a
  "protocol player" in MA; the player owning the queue is the one listing our id in
  `output_protocols` (found via `players/all` with `return_protocol_players`). **Not yet
  tested against a real Music Assistant.**

## Bugs found and fixed during testing

1. The progress bar didn't follow pause or seek on the same track: the draw closure captured an
   old `Track`. It now reads the live track.
2. Non-square covers showed black bars (server letterboxing). They are now trimmed.
3. RGB565 conversion was too slow for crossfades. It's now uint8 math, with dithering only on
   final frames.
4. The thread attribute `_stop` clashed with `threading.Thread` internals. Renamed.

## Testing status

- ✅ End-to-end on a desktop: the fake server played track 1, track 2, paused, then stopped. The
  idle screen then showed the photo slideshow. Screens were rendered to PNG and checked by eye.
- ✅ Unit tests pass. A clean `pip install .` works. The installer passes `bash -n`.
- ❌ **Not yet tested on a real Pi**: framebuffer output, colour depth, `display.rotate`
  direction, console hiding (KDSETMODE plus CAP_SYS_TTY_CONFIG), HDMI or DAC audio, and
  performance on the Zero 2 W.
- ❌ **Not yet tested with real Music Assistant playback**, in particular whether MA pushes
  artwork images and group playback state the way the fake server does.

**Caution when testing on a desktop:** the real Music Assistant on the LAN auto-discovers test
players over mDNS. Always run local tests with `--interface 127.0.0.1`. An earlier test player
named **"Pi Frame Test"** may still be listed in MA and can be deleted there.

## Git / GitHub status

- Target repo: https://github.com/Benoit-LdL/sendspin_frame.git (empty when last checked).
- Done: `git init -b main` in this folder, and placeholder links replaced with
  `Benoit-LdL/sendspin_frame`.
- **Not done yet:** git user.name and user.email (not configured on this machine), the first
  commit, `git remote add origin ...`, and `git push -u origin main`. Pushing needs GitHub
  credentials (a token or an SSH key). `gh` is not installed.
- This `HANDOFF.md` is in the project folder. Decide whether to commit it or delete it.

## Next steps

1. Commit and push (see above).
2. On the Pi: clone the repo, run `sudo ./install.sh`, set `photos.url` (and `sendspin.name`,
   `sendspin.audio_device`) in `/etc/sendspin-frame/config.toml`, then reboot.
3. Check `cat /sys/class/graphics/fb0/bits_per_pixel`, `journalctl -u sendspin-frame -e`, and
   CPU use during crossfades (lower `transition_fps` or set `transition_ms = 0` if needed).
4. Ideas not built yet: touchscreen controls (controller role: play/pause/next), and CJK font
   fallback. (Done: a "now playing" popup next to the badge on the photo screen, `photos.show_now_playing`.)

## Resuming development on a desktop

```bash
uv venv && uv pip install -e . pytest
python -m pytest -q tests
# Fonts: put Inter-{Light,Regular,Medium,SemiBold,Bold}.ttf in ./fonts (gitignored),
# from https://github.com/rsms/inter/releases (v4.1, extras/ttf/).
python tools/preview.py --out previews --art cover.jpg --photo photo.jpg
```
For the full end-to-end test, see "Development" in README.md.
