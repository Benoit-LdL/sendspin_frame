"""Home Assistant integration over MQTT (with MQTT discovery: no YAML needed in Home Assistant).

Creates a device with a "Screen during music" select (Cover art / Photo slideshow). The choice
is published as a retained message, so it survives restarts of sendspin-frame and of Home
Assistant.

Topics (<id> is the player name, lower case, e.g. "tv"):
    sendspin_frame/<id>/status             "online" / "offline" (availability, last will)
    sendspin_frame/<id>/music_screen       current choice (retained)
    sendspin_frame/<id>/music_screen/set   commands from Home Assistant
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version

import aiomqtt

from .config import MqttConfig

logger = logging.getLogger(__name__)

# Config value <-> label shown in Home Assistant.
OPTIONS = {"cover": "Cover art", "photos": "Photo slideshow"}
LABELS = {label: value for value, label in OPTIONS.items()}
RESTORE_WAIT_S = 1.5


def _sw_version() -> str:
    try:
        return version("sendspin-frame")
    except PackageNotFoundError:
        return "dev"


def node_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "sendspin_frame"


class MqttBridge:
    def __init__(self, cfg: MqttConfig, player_name: str,
                 get_music_screen: Callable[[], str],
                 set_music_screen: Callable[[str], None]) -> None:
        self.cfg = cfg
        self.name = player_name or socket.gethostname()
        self.id = node_id(self.name)
        self.base = f"sendspin_frame/{self.id}"
        self._get = get_music_screen
        self._set = set_music_screen
        self._restored = False

    @property
    def _topics(self) -> dict[str, str]:
        return {
            "status": f"{self.base}/status",
            "state": f"{self.base}/music_screen",
            "command": f"{self.base}/music_screen/set",
            "discovery": f"{self.cfg.discovery_prefix}/select/{self.id}/music_screen/config",
            "ha_status": f"{self.cfg.discovery_prefix}/status",
        }

    def _discovery_payload(self) -> str:
        t = self._topics
        return json.dumps({
            "name": "Screen during music",
            "unique_id": f"sendspin_frame_{self.id}_music_screen",
            "icon": "mdi:television-play",
            "options": list(OPTIONS.values()),
            "state_topic": t["state"],
            "command_topic": t["command"],
            "availability_topic": t["status"],
            "device": {
                "identifiers": [f"sendspin_frame_{self.id}"],
                "name": self.name,
                "manufacturer": "sendspin-frame",
                "model": "Sendspin player with screen",
                "sw_version": _sw_version(),
            },
        })

    async def run(self) -> None:
        backoff = 5.0
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as exc:
                logger.warning("MQTT: %s; reconnecting in %.0fs", exc, backoff)
            except Exception:  # noqa: BLE001 - keep trying whatever happens
                logger.exception("MQTT: unexpected error; reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def _session(self) -> None:
        t = self._topics
        will = aiomqtt.Will(t["status"], "offline", qos=1, retain=True)
        async with aiomqtt.Client(
            self.cfg.host, self.cfg.port,
            username=self.cfg.username or None, password=self.cfg.password or None,
            identifier=f"sendspin_frame_{self.id}", will=will,
        ) as client:
            logger.info("MQTT: connected to %s:%d as %s", self.cfg.host, self.cfg.port, self.base)
            if not self._restored:
                await self._restore_choice(client)
            await client.subscribe(t["command"], qos=1)
            await client.subscribe(t["ha_status"])
            await self._announce(client)
            try:
                async for msg in client.messages:
                    payload = msg.payload.decode(errors="replace") if isinstance(
                        msg.payload, (bytes, bytearray)) else str(msg.payload)
                    if msg.topic.matches(t["command"]):
                        await self._handle_command(client, payload.strip())
                    elif msg.topic.matches(t["ha_status"]) and payload == "online":
                        await self._announce(client)  # Home Assistant restarted
            except asyncio.CancelledError:
                # Stopping cleanly: the broker only sends the last will on a lost connection.
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2):
                        await client.publish(t["status"], "offline", qos=1, retain=True)
                raise

    async def _restore_choice(self, client: aiomqtt.Client) -> None:
        """Take over the last choice made in Home Assistant (our retained state message)."""
        topic = self._topics["state"]
        await client.subscribe(topic, qos=1)
        try:
            async with asyncio.timeout(RESTORE_WAIT_S):
                async for msg in client.messages:
                    if msg.topic.matches(topic):
                        value = LABELS.get(bytes(msg.payload).decode(errors="replace"))
                        if value and value != self._get():
                            logger.info("MQTT: restoring screen during music: %s", value)
                            self._set(value)
                        break
        except TimeoutError:
            pass
        await client.unsubscribe(topic)
        self._restored = True

    async def _announce(self, client: aiomqtt.Client) -> None:
        t = self._topics
        await client.publish(t["discovery"], self._discovery_payload(), qos=1, retain=True)
        await client.publish(t["status"], "online", qos=1, retain=True)
        await client.publish(t["state"], OPTIONS[self._get()], qos=1, retain=True)

    async def _handle_command(self, client: aiomqtt.Client, payload: str) -> None:
        value = LABELS.get(payload) or (payload.lower() if payload.lower() in OPTIONS else None)
        if value is None:
            logger.warning("MQTT: unknown screen choice %r", payload)
            return
        if value != self._get():
            self._set(value)
        await client.publish(self._topics["state"], OPTIONS[value], qos=1, retain=True)
