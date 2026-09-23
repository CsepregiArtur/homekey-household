"""Negative command / security tests (step 5).

Two layers are covered:

A. **Integration layer** — the real HA integration must publish **nothing** when a
   command cannot be produced safely:
     * credential missing
     * MQTT transport unavailable
     * authenticated lock control disabled
     * malformed command configuration

B. **Verification layer** — the independent firmware verifier must reject
   tampered / replayed / stale commands, and legacy or boolean-shaped payloads
   must never be treated as authorization.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from helpers import HOUSEHOLD, NODE_GATE, publish_telemetry, settle
from pytest_homeassistant_custom_component.common import MockConfigEntry

from firmware_verifier import (
    MAX_SKEW_SECONDS,
    FirmwareReject,
    FirmwareVerifier,
    derive_command_key,
)

from test_hmac_commands import (
    BASE,
    LOCK_TOPIC,
    MQTT_BROKER,
    MQTT_PORT,
    TEST_SALT,
    TEST_SECRET,
    UNLOCK_TOPIC,
    CommandTap,
    _seed_credential,
    _setup_entry,
    _setup_mqtt,
    _wait_for_lock_entity,
)

DOMAIN = "homekey_household"


class TestIntegrationFailsClosed:
    """A: the integration must not publish when it cannot authenticate."""

    async def test_missing_credential_publishes_nothing(self, hass, mqtt_client):
        """No stored command key -> nothing is published."""
        await _setup_entry(hass, HOUSEHOLD)  # credential intentionally NOT seeded
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        async with CommandTap() as tap:
            # The service call surfaces the failure to the caller.
            with pytest.raises(Exception):  # noqa: B017 - HA wraps in HomeAssistantError
                await hass.services.async_call(
                    "lock", "lock", {"entity_id": entity_id}, blocking=True
                )
            await settle(hass, seconds=0.5)

        assert tap.captured == [], (
            f"command published without a credential: {[c.topic for c in tap.captured]}"
        )

    async def test_unlock_without_credential_publishes_nothing(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        async with CommandTap() as tap:
            with pytest.raises(Exception):  # noqa: B017
                await hass.services.async_call(
                    "lock", "unlock", {"entity_id": entity_id}, blocking=True
                )
            await settle(hass, seconds=0.5)
        assert tap.captured == []

    async def test_command_control_disabled_publishes_nothing(self, hass, mqtt_client):
        """Control disabled in config -> no key provider, so no publication."""
        await _seed_credential(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD, command_control=False)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        async with CommandTap() as tap:
            with pytest.raises(Exception):  # noqa: B017
                await hass.services.async_call(
                    "lock", "lock", {"entity_id": entity_id}, blocking=True
                )
            await settle(hass, seconds=0.5)
        assert tap.captured == []

    async def test_mqtt_transport_unavailable_publishes_nothing(self, hass, mqtt_client):
        """With the coordinator's MQTT client removed, nothing is published."""
        await _seed_credential(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        runtime = hass.data[DOMAIN][
            hass.config_entries.async_entries(DOMAIN)[0].entry_id
        ]
        runtime.coordinator.mqtt = None  # simulate transport loss

        async with CommandTap() as tap:
            with pytest.raises(Exception):  # noqa: B017
                await hass.services.async_call(
                    "lock", "lock", {"entity_id": entity_id}, blocking=True
                )
            await settle(hass, seconds=0.5)
        assert tap.captured == []

    async def test_empty_credential_treated_as_missing(self, hass, mqtt_client):
        """A removed credential must fail closed, not sign with an empty key."""
        from custom_components.homekey_household.credential import CommandKeyStore

        await _seed_credential(hass, HOUSEHOLD)
        store = CommandKeyStore(hass)
        await store.async_load()
        await store.async_remove(HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        async with CommandTap() as tap:
            with pytest.raises(Exception):  # noqa: B017
                await hass.services.async_call(
                    "lock", "lock", {"entity_id": entity_id}, blocking=True
                )
            await settle(hass, seconds=0.5)
        assert tap.captured == []


class TestNeverFallsBackToLegacyTopics:
    """The integration must never publish legacy unauthenticated command topics."""

    async def test_no_legacy_command_topic_ever_published(self, hass, mqtt_client):
        legacy_suffixes = (
            "homekit/set_state",
            "homekit/set_target_state",
            "homekit/set_current_state",
            "homekit/set_battery_lvl",
            "homekit/set_custom_state",
        )
        await _seed_credential(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        # Subscribe to every legacy command topic shape. MQTT requires each
        # wildcard to occupy a whole level, so the legacy topics are matched with
        # a two-level wildcard prefix ("+/<suffix>") rather than an embedded one.
        import aiomqtt

        seen: list[str] = []
        async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as spy:
            for suffix in legacy_suffixes:
                await spy.subscribe(f"+/+/+/{suffix}", qos=1)
                await spy.subscribe(f"+/+/{suffix}", qos=1)
            await asyncio.sleep(0.3)

            reader_done = asyncio.Event()

            async def _read():
                async for msg in spy.messages:
                    seen.append(msg.topic.value)
                reader_done.set()

            task = asyncio.create_task(_read())
            try:
                for service in ("lock", "unlock"):
                    await hass.services.async_call(
                        "lock", service, {"entity_id": entity_id}, blocking=True
                    )
                await settle(hass, seconds=0.5)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        leaked = [t for t in seen if "set_" in t or "homekit" in t]
        assert leaked == [], f"legacy command topic published: {leaked}"


class TestVerificationLayerRejectsTampering:
    """B: the independent verifier must reject every tampered command."""

    @pytest.fixture
    def key(self) -> bytes:
        return derive_command_key(TEST_SECRET, TEST_SALT)

    def _valid(self, key: bytes, action: str = "lock", ts: int | None = None) -> dict:
        ts = ts if ts is not None else int(time.time())
        nonce = "0123456789abcdef0123456789abcdef"
        req_id = "abcdef0123456789"
        mac = hmac.new(
            key, f"{ts}{nonce}{req_id}{action}".encode(), hashlib.sha256
        ).hexdigest()
        return {"ts": ts, "nonce": nonce, "req_id": req_id, "mac": mac}

    def test_valid_command_accepted(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        assert v.verify(BASE, LOCK_TOPIC, json.dumps(body)) == "lock"

    def test_modified_ts_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        body["ts"] += 1
        with pytest.raises(FirmwareReject, match="MAC mismatch"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_modified_nonce_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        body["nonce"] = "f" * 32
        with pytest.raises(FirmwareReject, match="MAC mismatch"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_modified_req_id_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        body["req_id"] = "tampered"
        with pytest.raises(FirmwareReject, match="MAC mismatch"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_modified_mac_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        body["mac"] = "0" * 64
        with pytest.raises(FirmwareReject, match="MAC mismatch"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_wrong_action_rejected(self, key):
        """A lock MAC must not authorize an unlock (topic-derived action)."""
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key, action="lock")
        with pytest.raises(FirmwareReject, match="MAC mismatch"):
            v.verify(BASE, UNLOCK_TOPIC, json.dumps(body))

    def test_wrong_topic_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        with pytest.raises(FirmwareReject, match="unknown command topic"):
            v.verify(BASE, f"{BASE}/command/reboot", json.dumps(body))

    def test_stale_timestamp_rejected(self, key):
        old_ts = int(time.time()) - (MAX_SKEW_SECONDS + 60)
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key, ts=old_ts)
        with pytest.raises(FirmwareReject, match="outside time window"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_future_timestamp_rejected(self, key):
        future_ts = int(time.time()) + (MAX_SKEW_SECONDS + 60)
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key, ts=future_ts)
        with pytest.raises(FirmwareReject, match="outside time window"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_reused_nonce_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        assert v.verify(BASE, LOCK_TOPIC, json.dumps(body)) == "lock"
        with pytest.raises(FirmwareReject, match="replayed nonce"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_replay_window_is_bounded(self, key):
        """Matches the firmware's bounded 32-entry window."""
        v = FirmwareVerifier(key=key, now=None)
        for i in range(40):
            ts = 1760000000 + i
            nonce = f"{i:032x}"
            mac = hmac.new(
                key, f"{ts}{nonce}req{i}lock".encode(), hashlib.sha256
            ).hexdigest()
            v.verify(
                BASE,
                LOCK_TOPIC,
                json.dumps({"ts": ts, "nonce": nonce, "req_id": f"req{i}", "mac": mac}),
            )
        assert len(v.seen_nonces) <= 32

    def test_malformed_json_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        with pytest.raises(FirmwareReject, match="malformed JSON"):
            v.verify(BASE, LOCK_TOPIC, "{not json")

    def test_missing_field_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        del body["mac"]
        with pytest.raises(FirmwareReject, match="mac must be a string"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_wrong_field_type_rejected(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        body["ts"] = str(body["ts"])
        with pytest.raises(FirmwareReject, match="ts must be a number"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    def test_boolean_ts_rejected(self, key):
        """Python bool is an int subclass - must not be accepted as a timestamp."""
        v = FirmwareVerifier(key=key, now=int(time.time()))
        body = self._valid(key)
        body["ts"] = True
        with pytest.raises(FirmwareReject, match="ts must be a number"):
            v.verify(BASE, LOCK_TOPIC, json.dumps(body))

    @pytest.mark.parametrize(
        "payload",
        [
            '{"action":"unlock"}',
            '{"unlock":true}',
            '{"command":"unlock"}',
            "unlock",
            "true",
            '{"action":"unlock","unlock":true,"command":"unlock"}',
        ],
    )
    def test_non_hmac_payloads_cannot_authorize(self, key, payload):
        """Boolean / action-shaped payloads are never authorization."""
        v = FirmwareVerifier(key=key, now=int(time.time()))
        with pytest.raises(FirmwareReject):
            v.verify(BASE, UNLOCK_TOPIC, payload)

    def test_link_upload_style_payload_ignored(self, key):
        v = FirmwareVerifier(key=key, now=int(time.time()))
        # A payload with the right fields plus an extra 'action' is still judged
        # by its MAC over the topic-derived action.
        body = self._valid(key, action="lock")
        body["action"] = "unlock"  # ignored by firmware
        assert v.verify(BASE, LOCK_TOPIC, json.dumps(body)) == "lock"

    def test_empty_key_rejected_if_signing(self, key):
        """A MAC cannot be produced without a key (fail closed upstream)."""
        with pytest.raises(FirmwareReject, match="empty key material"):
            derive_command_key("", "")
