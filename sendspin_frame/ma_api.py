"""Music Assistant's own API: what kind of thing this player is playing, and from where.

Sendspin only sends title / artist / album. Music Assistant knows more: whether it's a song, a
radio station, a podcast episode or an audiobook, and the station / podcast / book name. This
connects to its websocket API (ws://<host>:8095/ws) with a long-lived token and follows our
player.

Our Sendspin connection is a "protocol player" in Music Assistant. The player you see in its UI
(which owns the queue) is a different one that lists our Sendspin id in `output_protocols`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .state import MediaInfo

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8095
FIND_PLAYER_RETRY_S = 30.0
REFRESH_DEBOUNCE_S = 0.3


class AuthError(Exception):
    pass


def api_url(url: str, sendspin_url: str) -> str | None:
    """The websocket URL from `music_assistant.url`, or else the host of `sendspin.server_url`."""
    if url:
        u = urlsplit(url if "://" in url else f"http://{url}")
        scheme = {"https": "wss", "wss": "wss"}.get(u.scheme, "ws")
        port = f":{u.port}" if u.port else f":{DEFAULT_PORT}"
        path = u.path.rstrip("/") or ""
        return f"{scheme}://{u.hostname}{port}{path if path.endswith('/ws') else path + '/ws'}"
    if sendspin_url:
        host = urlsplit(sendspin_url).hostname
        if host:
            return f"ws://{host}:{DEFAULT_PORT}/ws"
    return None


def _name(obj: Any) -> str | None:
    return obj.get("name") or None if isinstance(obj, dict) else None


def media_from_queue(queue: dict[str, Any] | None) -> MediaInfo | None:
    """Kind and source name from a PlayerQueue (its `current_item.media_item`)."""
    item = (queue or {}).get("current_item")
    if not isinstance(item, dict):
        return None
    mi = item.get("media_item") if isinstance(item.get("media_item"), dict) else {}
    sd = item.get("streamdetails") if isinstance(item.get("streamdetails"), dict) else {}
    mt = mi.get("media_type") or sd.get("media_type")
    if mt == "radio":
        return MediaInfo("radio", _name(mi) or item.get("name"))
    if mt == "podcast_episode":
        return MediaInfo("podcast", _name(mi.get("podcast")))
    if mt == "audiobook":
        return MediaInfo("audiobook", _name(mi) or item.get("name"))
    if mt == "track":
        return MediaInfo("music", _name(mi.get("album")))
    return None


def media_from_player(player: dict[str, Any]) -> MediaInfo | None:
    """Fallback without a queue (e.g. another source): only the kind is reliable."""
    media = player.get("current_media")
    if not isinstance(media, dict):
        return None
    kind = {"radio": "radio", "podcast_episode": "podcast", "audiobook": "audiobook",
            "track": "music"}.get(media.get("media_type") or "")
    return MediaInfo(kind) if kind else None


def find_player(players: list[dict[str, Any]], sendspin_id: str) -> dict[str, Any] | None:
    """The Music Assistant player that plays through our Sendspin connection."""
    ours = None
    for p in players:
        protocols = p.get("output_protocols") or []
        if p.get("type") != "protocol" and any(
            isinstance(o, dict) and o.get("output_protocol_id") == sendspin_id for o in protocols
        ):
            return p
        if p.get("player_id") == sendspin_id:
            ours = p  # not wrapped (yet): use the protocol player itself
    return ours


class MusicAssistantApi:
    """Keeps a connection to Music Assistant and reports `MediaInfo` for our player."""

    def __init__(self, url: str, token: str, sendspin_id: Callable[[], str | None],
                 on_media: Callable[[MediaInfo | None], None]) -> None:
        self.url = url
        self.token = token
        self._sendspin_id = sendspin_id
        self._on_media = on_media
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._partial: dict[str, list] = {}  # results that come in several messages
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._player_id: str | None = None
        self._queue_id: str | None = None
        self._media: MediaInfo | None = None
        self._refresh_task: asyncio.Task | None = None

    async def run(self) -> None:
        backoff = 5.0
        while True:
            try:
                await self._session()
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                logger.error("Music Assistant API: %s. Check music_assistant.token.", exc)
                backoff = 600.0
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ConnectionError) as exc:
                logger.warning("Music Assistant API: %s; reconnecting in %.0fs", exc or
                               type(exc).__name__, backoff)
            except Exception:  # noqa: BLE001 - keep trying whatever happens
                logger.exception("Music Assistant API: unexpected error")
            self._set_media(None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    # ------------------------------------------------------------------ connection

    async def _session(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.ws_connect(
            self.url, heartbeat=55, max_msg_size=0
        ) as ws:
            info = await ws.receive_json(timeout=15)
            if "server_id" not in info:
                raise ConnectionError(f"unexpected first message from {self.url}")
            self._ws = ws
            reader = asyncio.create_task(self._read(ws))
            try:
                result = await self._command("auth", token=self.token)
                if not (isinstance(result, dict) and result.get("authenticated")):
                    raise AuthError("not authenticated")
                logger.info("Music Assistant API: connected to %s (%s)",
                            info.get("name") or self.url, info.get("server_version"))
                self._player_id = self._queue_id = None
                await self._find_player()
                finder = asyncio.create_task(self._keep_finding())
                try:
                    await reader
                finally:
                    finder.cancel()
            finally:
                reader.cancel()
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("connection closed"))
                self._pending.clear()
                self._partial.clear()
                if self._refresh_task:
                    self._refresh_task.cancel()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = msg.json()
            except ValueError:
                continue
            if "event" in data:
                self._on_event(data)
                continue
            message_id = str(data.get("message_id"))
            fut = self._pending.get(message_id)
            if fut is None or fut.done():
                continue
            if "error_code" in data:
                code, details = data.get("error_code"), data.get("details")
                exc_type = AuthError if code in (20, 23) else RuntimeError
                fut.set_exception(exc_type(f"{details} (error {code})"))
            elif data.get("partial"):
                self._partial.setdefault(message_id, []).extend(data.get("result") or [])
            else:
                result = data.get("result")
                if message_id in self._partial:
                    result = [*self._partial.pop(message_id), *(result or [])]
                fut.set_result(result)
        raise ConnectionError("connection closed by Music Assistant")

    async def _command(self, command: str, **args: Any) -> Any:
        if self._ws is None:
            raise ConnectionError("not connected")
        message_id = str(next(self._ids))
        fut = asyncio.get_running_loop().create_future()
        self._pending[message_id] = fut
        try:
            await self._ws.send_json({"message_id": message_id, "command": command, "args": args})
            return await asyncio.wait_for(fut, 20)
        finally:
            self._pending.pop(message_id, None)

    # ------------------------------------------------------------------ our player

    async def _keep_finding(self) -> None:
        """Music Assistant only lists us a little while after the Sendspin connection."""
        while True:
            await asyncio.sleep(FIND_PLAYER_RETRY_S)
            if self._player_id is None:
                with contextlib.suppress(RuntimeError, asyncio.TimeoutError):
                    await self._find_player()

    async def _find_player(self) -> None:
        sendspin_id = self._sendspin_id()
        if not sendspin_id:
            return
        players = await self._command("players/all", return_protocol_players=True)
        player = find_player(players or [], sendspin_id)
        if player is None:
            logger.debug("Music Assistant API: player %s not found (yet)", sendspin_id)
            return
        if player["player_id"] != self._player_id:
            logger.info("Music Assistant API: following player %s (%s)",
                        player.get("display_name") or player.get("name"), player["player_id"])
        self._player_id = player["player_id"]
        await self._refresh(player)

    async def _refresh(self, player: dict[str, Any] | None = None) -> None:
        """Ask for our player's active queue and report what's playing."""
        if self._player_id is None:
            return
        queue = None
        with contextlib.suppress(RuntimeError):
            queue = await self._command("player_queues/get_active_queue",
                                        player_id=self._player_id)
        self._queue_id = queue.get("queue_id") if isinstance(queue, dict) else None
        media = media_from_queue(queue)
        if media is None and player is None:
            with contextlib.suppress(RuntimeError):
                player = await self._command("players/get", player_id=self._player_id)
        if media is None and isinstance(player, dict):
            media = media_from_player(player)
        self._set_media(media)

    def _schedule_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            return

        async def later() -> None:
            await asyncio.sleep(REFRESH_DEBOUNCE_S)  # bursts of events: ask once
            try:
                await self._refresh()
            except (ConnectionError, asyncio.TimeoutError, AuthError) as exc:
                logger.debug("Music Assistant API: refresh failed: %s", exc)

        self._refresh_task = asyncio.create_task(later())

    def _on_event(self, msg: dict[str, Any]) -> None:
        event, object_id, data = msg.get("event"), msg.get("object_id"), msg.get("data")
        if event == "queue_updated" and object_id and object_id == self._queue_id:
            self._set_media(media_from_queue(data) or self._media)
        elif event == "player_updated" and object_id == self._player_id:
            self._schedule_refresh()  # e.g. another queue / source became active
        elif event in ("player_added", "player_removed") and self._player_id is None:
            self._schedule_find()

    def _schedule_find(self) -> None:
        async def find() -> None:
            with contextlib.suppress(ConnectionError, asyncio.TimeoutError, RuntimeError):
                await self._find_player()

        asyncio.create_task(find())

    def _set_media(self, media: MediaInfo | None) -> None:
        if media != self._media:
            self._media = media
            logger.debug("Music Assistant API: now playing %s", media)
            self._on_media(media)
