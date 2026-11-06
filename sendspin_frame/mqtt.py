"""Home Assistant integration over MQTT (with MQTT discovery: no YAML needed in Home Assistant).

Creates a device with:
  - a "Screen during music" select (Cover art / Photo slideshow)
  - a "System stats on screen" switch (CPU / RAM / temperature overlay)
Their states are published as retained messages, so the choices survive restarts of
sendspin-frame and of Home Assistant.

Topics (<id> is the player name, lower case, e.g. "tv"):
    sendspin_frame/<id>/status              "online" / "offline" (availability, last will)
    sendspin_frame/<id>/<entity>            current value (retained)
    sendspin_frame/<id>/<entity>/set        commands from Home Assistant
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import aiomqtt

from .config import MqttConfig

logger = logging.getLogger(__name__)

RESTORE_WAIT_S = 1.5


@dataclass
class Entity:
    """One Home Assistant entity, with the mapping between our values and MQTT payloads."""

    key: str  # topic part and unique-id suffix
    component: str  # "select" | "switch"
    name: str
    icon: str
    to_payload: dict[Any, str]  # our value -> MQTT payload
    get: Callable[[], Any]
    set: Callable[[Any], None]

    def parse(self, payload: str) -> Any:
        for value, text in self.to_payload.items():
            if payload == text or (isinstance(value, str) and payload.lower() == value):
                return value
        raise ValueError(payload)


def _sw_version() -> str:
    try:
        return version("sendspin-frame")
    except PackageNotFoundError:
        return "dev"


def node_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "sendspin_frame"


def make_entities(get_music_screen: Callable[[], str], set_music_screen: Callable[[str], None],
                  get_show_stats: Callable[[], bool],
                  set_show_stats: Callable[[bool], None]) -> list[Entity]:
    return [
        Entity("music_screen", "select", "Screen during music", "mdi:television-play",
               {"cover": "Cover art", "photos": "Photo slideshow"},
               get_music_screen, set_music_screen),
        Entity("show_stats", "switch", "System stats on screen", "mdi:chip",
               {True: "ON", False: "OFF"}, get_show_stats, set_show_stats),
    ]


class MqttBridge:
    def __init__(self, cfg: MqttConfig, player_name: str, entities: list[Entity]) -> None:
        self.cfg = cfg
        self.name = player_name or socket.gethostname()
        self.id = node_id(self.name)
        self.base = f"sendspin_frame/{self.id}"
        self.entities = entities
        self._restored = False

    @property
    def status_topic(self) -> str:
        return f"{self.base}/status"

    @property
    def ha_status_topic(self) -> str:
        return f"{self.cfg.discovery_prefix}/status"

    def state_topic(self, e: Entity) -> str:
        return f"{self.base}/{e.key}"

    def command_topic(self, e: Entity) -> str:
        return f"{self.base}/{e.key}/set"

    def discovery_topic(self, e: Entity) -> str:
        return f"{self.cfg.discovery_prefix}/{e.component}/{self.id}/{e.key}/config"

    def discovery_payload(self, e: Entity) -> str:
        payload: dict[str, Any] = {
            "name": e.name,
            "unique_id": f"sendspin_frame_{self.id}_{e.key}",
            "icon": e.icon,
            "state_topic": self.state_topic(e),
            "command_topic": self.command_topic(e),
            "availability_topic": self.status_topic,
            "device": {
                "identifiers": [f"sendspin_frame_{self.id}"],
                "name": self.name,
                "manufacturer": "sendspin-frame",
                "model": "Sendspin player with screen",
                "sw_version": _sw_version(),
            },
        }
        if e.component == "select":
            payload["options"] = list(e.to_payload.values())
        else:
            payload["payload_on"], payload["payload_off"] = e.to_payload[True], e.to_payload[False]
        return json.dumps(payload)

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
        will = aiomqtt.Will(self.status_topic, "offline", qos=1, retain=True)
        async with aiomqtt.Client(
            self.cfg.host, self.cfg.port,
            username=self.cfg.username or None, password=self.cfg.password or None,
            identifier=f"sendspin_frame_{self.id}", will=will,
        ) as client:
            logger.info("MQTT: connected to %s:%d as %s", self.cfg.host, self.cfg.port, self.base)
            if not self._restored:
                await self._restore(client)
            for e in self.entities:
                await client.subscribe(self.command_topic(e), qos=1)
            await client.subscribe(self.ha_status_topic)
            await self._announce(client)
            try:
                async for msg in client.messages:
                    payload = bytes(msg.payload or b"").decode(errors="replace").strip()
                    if msg.topic.matches(self.ha_status_topic):
                        if payload == "online":
                            await self._announce(client)  # Home Assistant restarted
                        continue
                    for e in self.entities:
                        if msg.topic.matches(self.command_topic(e)):
                            await self._handle_command(client, e, payload)
            except asyncio.CancelledError:
                # Stopping cleanly: the broker only sends the last will on a lost connection.
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2):
                        await client.publish(self.status_topic, "offline", qos=1, retain=True)
                raise

    async def _restore(self, client: aiomqtt.Client) -> None:
        """Take over the last choices made in Home Assistant (our retained state messages)."""
        pending = {self.state_topic(e): e for e in self.entities}
        for topic in pending:
            await client.subscribe(topic, qos=1)
        try:
            async with asyncio.timeout(RESTORE_WAIT_S):
                async for msg in client.messages:
                    e = next((e for t, e in pending.items() if msg.topic.matches(t)), None)
                    if e is None:
                        continue
                    del pending[self.state_topic(e)]
                    with contextlib.suppress(ValueError):
                        value = e.parse(bytes(msg.payload or b"").decode(errors="replace"))
                        if value != e.get():
                            logger.info("MQTT: restoring %s: %s", e.key, value)
                            e.set(value)
                    if not pending:
                        break
        except TimeoutError:
            pass
        for e in self.entities:
            await client.unsubscribe(self.state_topic(e))
        self._restored = True

    async def _announce(self, client: aiomqtt.Client) -> None:
        for e in self.entities:
            await client.publish(self.discovery_topic(e), self.discovery_payload(e),
                                 qos=1, retain=True)
        await client.publish(self.status_topic, "online", qos=1, retain=True)
        for e in self.entities:
            await client.publish(self.state_topic(e), e.to_payload[e.get()], qos=1, retain=True)

    async def _handle_command(self, client: aiomqtt.Client, e: Entity, payload: str) -> None:
        try:
            value = e.parse(payload)
        except ValueError:
            logger.warning("MQTT: unknown value %r for %s", payload, e.key)
            return
        if value != e.get():
            e.set(value)
        await client.publish(self.state_topic(e), e.to_payload[value], qos=1, retain=True)
