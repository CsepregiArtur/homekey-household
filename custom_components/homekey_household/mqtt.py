"""MQTT layer for HomeKey Household.

Responsibilities:

* a thin transport abstraction so unit tests run without a broker,
* topic parsing for the documented **household** namespace (nothing else),
* construction and publication of HMAC-authenticated lock/unlock commands.

The integration reuses Home Assistant's core MQTT integration as its transport;
it never opens a second broker connection.

Only the topics in the firmware 0.10.0 contract are recognised. Reserved /
not-implemented topics (``events``, ``backup/request``, ``backup/data``,
``restore/*``) are explicitly rejected by the parser. Legacy ``<CLIENT_ID>/*``
topics are handled solely for the **shared broker LWT availability topic**; no
legacy command topic is ever published.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from .command import NonceTracker, build_command, current_epoch_seconds
from .const import (
    COMMAND_ACTIONS,
    JSON_SUBTOPICS,
    LEGACY_AUTH_SUFFIX,
    LEGACY_STATE_SUFFIX,
    LEGACY_STATUS_SUFFIX,
    PLAIN_SUBTOPICS,
    RESERVED_TOPICS,
    TOPIC_CMD_LOCK,
    TOPIC_CMD_UNLOCK,
    TOPIC_HOUSEHOLD,
    TOPIC_NODES,
    TOPIC_ROOT,
    TOPIC_SUBSCRIBE_ALL,
    legacy_status_subscribe,
    node_base,
    node_topic,
)
from .models import AuthenticatedCommand, ValidationError

_LOGGER = logging.getLogger(__name__)

Unsubscribe = Callable[[], None]
MessageCallback = Callable[["HomeKeyMessage"], Awaitable[None] | None]

# Kind used for the legacy shared-LWT availability message.
KIND_LEGACY_STATUS: Final = "legacy_status"

# Subtopics the integration acts upon (JSON ∪ plain).
_KNOWN_SUBTOPICS: Final[frozenset[str]] = JSON_SUBTOPICS | PLAIN_SUBTOPICS

# Legacy subtopic suffixes that are parsed for the LWT availability signal.
_LEGACY_SUFFIX_KINDS: Final[dict[str, str]] = {
    LEGACY_STATUS_SUFFIX: KIND_LEGACY_STATUS,
    # Parsed only to be *rejected* as a source of HA V2 state (kept for logging
    # clarity and explicit legacy rejection tests).
    LEGACY_STATE_SUFFIX: "legacy_state",
    LEGACY_AUTH_SUFFIX: "legacy_auth",
}


@dataclass(frozen=True)
class HomeKeyMessage:
    """A parsed MQTT message scoped to a household node."""

    household_id: str
    subtopic: str
    payload: str
    node_id: str | None = None
    qos: int = 0
    retain: bool = False
    legacy: bool = False


@dataclass(frozen=True)
class ParsedTopic:
    """Result of parsing a topic string."""

    household_id: str | None
    node_id: str | None
    subtopic: str | None
    legacy: bool = False
    reserved: bool = False


def _is_valid_id(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    return all(ch in allowed for ch in value)


def parse_topic(topic: str, legacy_prefix: str | None = None) -> ParsedTopic | None:
    """Parse an MQTT topic into a structured value, or ``None`` if unrelated.

    Recognises only:

    * ``homekey/household/<hid>/nodes/<nid>/<subtopic>`` (documented contract)
    * ``<legacy_prefix><client_id>/status`` (shared broker LWT only)

    Reserved subtopics are returned with ``reserved=True`` so callers can log and
    reject them explicitly rather than silently ignoring malformed input.
    """
    parts = topic.split("/")
    if parts and parts[0] == TOPIC_ROOT:
        if len(parts) == 2 and parts[1] == TOPIC_HOUSEHOLD:
            # ``homekey/household`` on its own is not a usable topic.
            return None
        if len(parts) >= 3 and parts[1] == TOPIC_HOUSEHOLD:
            household_id = parts[2]
            if not _is_valid_id(household_id):
                return None
            if len(parts) >= 6 and parts[3] == TOPIC_NODES:
                node_id = parts[4]
                if not _is_valid_id(node_id):
                    return None
                subtopic = "/".join(parts[5:])
                return ParsedTopic(
                    household_id=household_id,
                    node_id=node_id,
                    subtopic=subtopic,
                    reserved=subtopic in RESERVED_TOPICS,
                )
            return None
        return None

    if legacy_prefix:
        return _parse_legacy_topic(topic, legacy_prefix)
    return None


def _parse_legacy_topic(topic: str, prefix: str) -> ParsedTopic | None:
    """Parse legacy ``<prefix><client_id>/<suffix>`` topics."""
    if not topic.startswith(prefix):
        return None
    rest = topic[len(prefix) :]
    if "/" not in rest:
        return None
    client_id, suffix = rest.split("/", 1)
    if not client_id:
        return None
    kind = _LEGACY_SUFFIX_KINDS.get(suffix)
    if kind is None:
        return None
    return ParsedTopic(
        household_id=None,
        node_id=client_id,
        subtopic=kind,
        legacy=True,
    )


class MqttTransport(Protocol):
    """Minimal MQTT transport used by :class:`HomeKeyMqttClient`."""

    def subscribe(
        self,
        topic: str,
        callback: Callable[[str, str, int, bool], Awaitable[None] | None],
        qos: int = 0,
    ) -> Awaitable[Unsubscribe]:
        """Subscribe to ``topic``; resolve to an unsubscribe callable."""
        ...

    def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> Awaitable[None]:
        """Publish ``payload`` to ``topic``."""
        ...


class HAMqttTransport:
    """Transport backed by the Home Assistant core MQTT integration."""

    def __init__(self, hass: Any) -> None:
        self._hass = hass

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[str, str, int, bool], Awaitable[None] | None],
        qos: int = 0,
    ) -> Unsubscribe:
        from homeassistant.components import mqtt
        from homeassistant.components.mqtt.models import ReceiveMessage

        async def _forward(message: ReceiveMessage) -> None:
            payload = message.payload
            if not isinstance(payload, str):
                payload = payload.decode("utf-8", errors="replace")
            result = callback(message.topic, payload, message.qos, message.retain)
            if result is not None:
                await result

        return await mqtt.async_subscribe(self._hass, topic, _forward, qos=qos)

    def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> Awaitable[None]:
        from homeassistant.components import mqtt

        return mqtt.async_publish(self._hass, topic, payload, qos=qos, retain=retain)


def dumps_command(command: AuthenticatedCommand) -> str:
    """Serialise an authenticated command payload deterministically."""
    return json.dumps(command.to_dict(), separators=(",", ":"))


class HomeKeyMqttClient:
    """Subscribes to the household namespace and publishes authenticated commands."""

    def __init__(
        self,
        hass: Any,
        transport: MqttTransport,
        message_callback: MessageCallback,
        *,
        household_id: str,
        legacy_prefix: str | None = None,
    ) -> None:
        self._hass = hass
        self._transport = transport
        self._callback = message_callback
        self._household_id = household_id
        self._legacy_prefix = legacy_prefix
        self._unsubscribes: list[Unsubscribe] = []
        self._nonces = NonceTracker()

    @property
    def household_id(self) -> str:
        return self._household_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Wait for the broker and subscribe to the household namespace."""
        await self._ensure_mqtt_ready()
        subscriptions: list[tuple[str, int]] = [(TOPIC_SUBSCRIBE_ALL, 1)]
        if self._legacy_prefix:
            # Only the shared broker LWT availability topic is needed from the
            # legacy namespace (MQTT allows one will per connection).
            subscriptions.append(
                (legacy_status_subscribe(self._legacy_prefix), 1)
            )
        for topic, qos in subscriptions:
            unsubscribe = await self._transport.subscribe(
                topic, self._on_message, qos=qos
            )
            self._unsubscribes.append(unsubscribe)
            _LOGGER.debug("Subscribed to %s", topic)

    async def async_stop(self) -> None:
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes.clear()

    async def _ensure_mqtt_ready(self) -> None:
        """Wait for the HA MQTT client to connect (no-op for fake transports)."""
        if not isinstance(self._transport, HAMqttTransport):
            return
        from homeassistant.components import mqtt

        await mqtt.async_wait_for_mqtt_client(self._hass)

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------
    async def _on_message(
        self, topic: str, payload: str, qos: int, retain: bool
    ) -> None:
        parsed = parse_topic(topic, self._legacy_prefix)
        if parsed is None:
            _LOGGER.debug("Ignoring unrelated MQTT topic: %s", topic)
            return
        if parsed.reserved:
            _LOGGER.debug(
                "Ignoring reserved/not-implemented household topic: %s", topic
            )
            return
        if parsed.legacy:
            # Legacy topics are only used for the shared LWT availability signal.
            if parsed.subtopic != KIND_LEGACY_STATUS:
                _LOGGER.debug("Ignoring legacy topic (not authoritative): %s", topic)
                return
            message = HomeKeyMessage(
                household_id=self._household_id,
                node_id=parsed.node_id,
                subtopic=KIND_LEGACY_STATUS,
                payload=payload,
                qos=qos,
                retain=retain,
                legacy=True,
            )
        else:
            if parsed.subtopic not in _KNOWN_SUBTOPICS:
                _LOGGER.debug("Ignoring unknown household subtopic: %s", topic)
                return
            assert parsed.subtopic is not None  # noqa: S101 - narrowed above
            message = HomeKeyMessage(
                household_id=parsed.household_id or self._household_id,
                node_id=parsed.node_id,
                subtopic=parsed.subtopic,
                payload=payload,
                qos=qos,
                retain=retain,
            )

        result = self._callback(message)
        if result is not None:
            await result

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    async def async_publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> None:
        await self._transport.publish(topic, payload, qos=qos, retain=retain)

    async def async_publish_authenticated_command(
        self,
        household_id: str,
        node_id: str,
        key: bytes,
        action: str,
        *,
        ts: int | None = None,
    ) -> AuthenticatedCommand:
        """Publish an HMAC-authenticated lock/unlock command.

        Fails closed: publishing requires a non-empty key. The action is derived
        from the topic, never from the payload. The command is published at QoS 1
        and **not** retained (per the contract).

        Returns the command that was published so callers can correlate the
        ``req_id`` locally; the MAC is not logged.
        """
        if not key:
            raise ValidationError("command credential unavailable; refusing to publish")
        if action not in COMMAND_ACTIONS.values():
            raise ValidationError(f"unsupported action: {action!r}")

        subtopic = (
            TOPIC_CMD_LOCK if action == "lock" else TOPIC_CMD_UNLOCK
        )
        command = build_command(
            key,
            action,
            ts=ts if ts is not None else current_epoch_seconds(),
            nonce_tracker=self._nonces,
        )
        topic = node_topic(household_id, node_id, subtopic)
        await self.async_publish(topic, dumps_command(command), qos=1, retain=False)
        _LOGGER.info(
            "Published authenticated %s command for %s/%s (req_id=%s)",
            action,
            household_id,
            node_id,
            command.req_id,
        )
        return command

    async def async_lock(
        self, household_id: str, node_id: str, key: bytes, *, ts: int | None = None
    ) -> AuthenticatedCommand:
        return await self.async_publish_authenticated_command(
            household_id, node_id, key, "lock", ts=ts
        )

    async def async_unlock(
        self, household_id: str, node_id: str, key: bytes, *, ts: int | None = None
    ) -> AuthenticatedCommand:
        return await self.async_publish_authenticated_command(
            household_id, node_id, key, "unlock", ts=ts
        )


def build_node_base(household_id: str, node_id: str) -> str:
    """Re-export the node base topic builder for convenience."""
    return node_base(household_id, node_id)


__all__ = [
    "HAMqttTransport",
    "HomeKeyMessage",
    "HomeKeyMqttClient",
    "KIND_LEGACY_STATUS",
    "MqttTransport",
    "ParsedTopic",
    "build_node_base",
    "dumps_command",
    "parse_topic",
]
