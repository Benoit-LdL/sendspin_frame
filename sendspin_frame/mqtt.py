"""Home Assistant integration over MQTT (with MQTT discovery: no YAML needed in Home Assistant).

Creates a device with:
  - a "Screen during music" select (Cover art / Photo slideshow)
  - a "System stats on screen" switch (CPU / RAM / temperature overlay)
  - diagnostic sensors, refreshed every `mqtt.stats_interval` seconds: CPU temperature, CPU
    frequency, CPU use, RAM use, free memory, uptime, IP address, MAC address, WiFi signal
  - with [updates]: "System package updates" and "sendspin-frame updates" counters, an
    "Update status" sensor, and "Check for updates" / "Update system packages" /
    "Update sendspin-frame" buttons (see updates.py)
The select and switch states are published as retained messages, so the choices survive
restarts of sendspin-frame and of Home Assistant.

Topics (<id> is the player name, lower case, e.g. "tv"):
    sendspin_frame/<id>/status              "online" / "offline" (availability, last will)
    sendspin_frame/<id>/<entity>            current value (retained)
    sendspin_frame/<id>/<entity>/attributes extra attributes of a sensor (JSON, retained)
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
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import aiomqtt

from .config import MqttConfig

logger = logging.getLogger(__name__)

RESTORE_WAIT_S = 1.5
PRESS = "PRESS"


@dataclass
class Entity:
    """One Home Assistant entity, with the mapping between our values and MQTT payloads.

    select / switch: `to_payload`, `get` and `set`. Their state is restored from the broker.
    sensor: `get` (None = unknown, nothing published) and optionally `attributes`.
    button: `press`.
    `extra` holds more discovery options (unit_of_measurement, entity_category, ...).
    """

    key: str  # topic part and unique-id suffix
    component: str  # "select" | "switch" | "sensor" | "button"
    name: str
    icon: str
    to_payload: dict[Any, str] = field(default_factory=dict)  # our value -> MQTT payload
    get: Callable[[], Any] | None = None
    set: Callable[[Any], None] | None = None
    press: Callable[[], None] | None = None
    attributes: Callable[[], dict[str, Any]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def restorable(self) -> bool:
        return self.set is not None and bool(self.to_payload)

    @property
    def has_command(self) -> bool:
        return self.set is not None or self.press is not None

    def parse(self, payload: str) -> Any:
        for value, text in self.to_payload.items():
            if payload == text or (isinstance(value, str) and payload.lower() == value):
                return value
        raise ValueError(payload)

    def state_payload(self) -> str | None:
        if self.get is None:
            return None
        value = self.get()
        if value is None:
            return None
        return self.to_payload.get(value, str(value))


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


def make_stats_entities(get: Callable[[str], Any]) -> list[Entity]:
    """Diagnostic sensors; `get(key)` gives the latest value (see StatsReader.details)."""

    def sensor(key: str, name: str, icon: str, unit: str | None = None,
               device_class: str | None = None, state_class: str = "measurement") -> Entity:
        extra: dict[str, Any] = {"entity_category": "diagnostic"}
        if unit:
            extra.update(unit_of_measurement=unit, state_class=state_class)
        if device_class:
            extra["device_class"] = device_class
        return Entity(key, "sensor", name, icon, get=lambda: get(key), extra=extra)

    return [
        sensor("cpu_temperature", "CPU temperature", "mdi:thermometer", "°C", "temperature"),
        sensor("cpu_frequency", "CPU frequency", "mdi:speedometer", "MHz", "frequency"),
        sensor("cpu_use", "CPU use", "mdi:cpu-64-bit", "%"),
        sensor("ram_use", "RAM use", "mdi:memory", "%"),
        sensor("free_memory", "Free memory", "mdi:memory", "MB", "data_size"),
        sensor("uptime", "Uptime", "mdi:clock-outline", "s", "duration", "total_increasing"),
        sensor("ip_address", "IP address", "mdi:ip-network"),
        sensor("mac_address", "MAC address", "mdi:network-outline"),
        sensor("wifi_signal", "WiFi signal", "mdi:wifi", "dBm", "signal_strength"),
    ]


def make_update_entities(updates: Any) -> list[Entity]:
    """Entities for an updates.UpdateManager."""
    count = {"unit_of_measurement": "packages", "state_class": "measurement"}
    return [
        Entity("system_updates", "sensor", "System package updates", "mdi:package-up",
               get=updates.system_count, attributes=updates.system_attributes, extra=count),
        Entity("app_updates", "sensor", "sendspin-frame updates", "mdi:update",
               get=updates.app_count, attributes=updates.app_attributes, extra=count),
        Entity("update_status", "sensor", "Update status", "mdi:progress-download",
               get=updates.status_text, attributes=updates.status_attributes,
               extra={"entity_category": "diagnostic"}),
        Entity("check_updates", "button", "Check for updates", "mdi:refresh",
               press=updates.request_check),
        Entity("update_system", "button", "Update system packages", "mdi:package-down",
               press=lambda: updates.request_update("system")),
        Entity("update_app", "button", "Update sendspin-frame", "mdi:download",
               press=lambda: updates.request_update("app")),
    ]


class MqttBridge:
    def __init__(self, cfg: MqttConfig, player_name: str, entities: list[Entity]) -> None:
        self.cfg = cfg
        self.name = player_name or socket.gethostname()
        self.id = node_id(self.name)
        self.base = f"sendspin_frame/{self.id}"
        self.entities = entities
        self._restored = False
        self._client: aiomqtt.Client | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def status_topic(self) -> str:
        return f"{self.base}/status"

    @property
    def ha_status_topic(self) -> str:
        return f"{self.cfg.discovery_prefix}/status"

    def state_topic(self, e: Entity) -> str:
        return f"{self.base}/{e.key}"

    def attributes_topic(self, e: Entity) -> str:
        return f"{self.base}/{e.key}/attributes"

    def command_topic(self, e: Entity) -> str:
        return f"{self.base}/{e.key}/set"

    def discovery_topic(self, e: Entity) -> str:
        return f"{self.cfg.discovery_prefix}/{e.component}/{self.id}/{e.key}/config"

    def discovery_payload(self, e: Entity) -> str:
        payload: dict[str, Any] = {
            "name": e.name,
            "unique_id": f"sendspin_frame_{self.id}_{e.key}",
            "icon": e.icon,
            "availability_topic": self.status_topic,
            "device": {
                "identifiers": [f"sendspin_frame_{self.id}"],
                "name": self.name,
                "manufacturer": "sendspin-frame",
                "model": "Sendspin player with screen",
                "sw_version": _sw_version(),
            },
        }
        if e.get is not None:
            payload["state_topic"] = self.state_topic(e)
        if e.has_command:
            payload["command_topic"] = self.command_topic(e)
        if e.attributes is not None:
            payload["json_attributes_topic"] = self.attributes_topic(e)
        if e.component == "select":
            payload["options"] = list(e.to_payload.values())
        elif e.component == "switch":
            payload["payload_on"], payload["payload_off"] = e.to_payload[True], e.to_payload[False]
        elif e.component == "button":
            payload["payload_press"] = PRESS
        payload.update(e.extra)
        return json.dumps(payload)

    def notify(self, *keys: str) -> None:
        """Publish the current state of these entities (all with a state when no keys given).

        Call from the asyncio loop when a value changed outside of an MQTT command.
        """
        if self._client is None:
            return
        wanted = [e for e in self.entities if e.get is not None and (not keys or e.key in keys)]
        task = asyncio.ensure_future(self._publish_states(self._client, wanted))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
                if e.has_command:
                    await client.subscribe(self.command_topic(e), qos=1)
            await client.subscribe(self.ha_status_topic)
            await self._announce(client)
            self._client = client
            try:
                async for msg in client.messages:
                    payload = bytes(msg.payload or b"").decode(errors="replace").strip()
                    if msg.topic.matches(self.ha_status_topic):
                        if payload == "online":
                            await self._announce(client)  # Home Assistant restarted
                        continue
                    for e in self.entities:
                        if e.has_command and msg.topic.matches(self.command_topic(e)):
                            await self._handle_command(client, e, payload)
            except asyncio.CancelledError:
                # Stopping cleanly: the broker only sends the last will on a lost connection.
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2):
                        await client.publish(self.status_topic, "offline", qos=1, retain=True)
                raise
            finally:
                self._client = None

    async def _restore(self, client: aiomqtt.Client) -> None:
        """Take over the last choices made in Home Assistant (our retained state messages)."""
        pending = {self.state_topic(e): e for e in self.entities if e.restorable}
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
            if e.restorable:
                await client.unsubscribe(self.state_topic(e))
        self._restored = True

    async def _announce(self, client: aiomqtt.Client) -> None:
        for e in self.entities:
            await client.publish(self.discovery_topic(e), self.discovery_payload(e),
                                 qos=1, retain=True)
        await client.publish(self.status_topic, "online", qos=1, retain=True)
        await self._publish_states(client, self.entities)

    async def _publish_states(self, client: aiomqtt.Client, entities: list[Entity]) -> None:
        for e in entities:
            payload = e.state_payload()
            if payload is None:
                continue  # unknown yet: Home Assistant keeps the retained value, if any
            if e.attributes is not None:
                await client.publish(self.attributes_topic(e), json.dumps(e.attributes()),
                                     qos=1, retain=True)
            await client.publish(self.state_topic(e), payload, qos=1, retain=True)

    async def _handle_command(self, client: aiomqtt.Client, e: Entity, payload: str) -> None:
        if e.press is not None:
            e.press()
            return
        try:
            value = e.parse(payload)
        except ValueError:
            logger.warning("MQTT: unknown value %r for %s", payload, e.key)
            return
        if value != e.get():
            e.set(value)
        await client.publish(self.state_topic(e), e.to_payload[value], qos=1, retain=True)
