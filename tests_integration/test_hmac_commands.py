"""Real HMAC lock/unlock validation through the actual HA Lock entity (steps 2-6).

Path under test — nothing is bypassed:

    Home Assistant lock service/entity
      -> HomeKeyLock.async_lock()/async_unlock()
      -> coordinator.async_send_lock_command()
      -> mqtt.HomeKeyMqttClient.async_publish_authenticated_command()
      -> real Mosquitto broker
      -> captured MQTT message

The captured message is then verified independently:

* structure: exactly ``ts``/``nonce``/``req_id``/``mac`` and nothing else
* MAC: recomputed from the captured values with an independent implementation
* acceptance: replayed through an independent firmware-0.10.0 verifier
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import pytest
from helpers import HOUSEHOLD, NODE_GATE, publish_telemetry, settle
from pytest_homeassistant_custom_component.common import MockConfigEntry

from firmware_verifier import FirmwareReject, FirmwareVerifier, derive_command_key

DOMAIN = "homekey_household"
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 18830

# Deterministic, synthetic test credential. Never a production value.
TEST_SECRET = "integration-test-recovery-secret"
TEST_SALT = "integration-test-salt"

BASE = f"homekey/household/{HOUSEHOLD}/nodes/{NODE_GATE}"
LOCK_TOPIC = f"{BASE}/command/lock"
UNLOCK_TOPIC = f"{BASE}/command/unlock"


@dataclass
class CapturedCommand:
    topic: str
    payload: str
    qos: int
    retain: bool

    @property
    def body(self) -> dict:
        return json.loads(self.payload)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
async def _setup_mqtt(hass):
    from homeassistant.components import mqtt

    existing = hass.config_entries.async_entries("mqtt")
    if existing:
        entry = existing[0]
    else:
        entry = MockConfigEntry(
            domain="mqtt",
            data={
                mqtt.CONF_BROKER: MQTT_BROKER,
                mqtt.CONF_PORT: MQTT_PORT,
                mqtt.CONF_DISCOVERY: False,
            },
            options={mqtt.CONF_BIRTH_MESSAGE: {}},
            title="MQTT (test broker)",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert await mqtt.async_wait_for_mqtt_client(hass)
    return entry


async def _seed_credential(hass, household_id: str, secret: str = TEST_SECRET, salt: str = TEST_SALT):
    """Store the derived command key, exactly as the config flow would."""
    from custom_components.homekey_household.credential import CommandKeyStore

    store = CommandKeyStore(hass)
    await store.async_load()
    await store.async_set_from_secret(household_id, secret, salt)
    return store


async def _setup_entry(hass, household_id: str = HOUSEHOLD, *, command_control: bool = True):
    await _setup_mqtt(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "household_id": household_id,
            "household_name": household_id,
            "command_control": command_control,
        },
        unique_id=f"{DOMAIN}_{household_id}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _lock_entity_id(hass) -> str:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{HOUSEHOLD}_{NODE_GATE}_lock"
    for entity in registry.entities.values():
        if entity.unique_id == unique_id and entity.platform == DOMAIN:
            return entity.entity_id
    raise AssertionError(f"lock entity not registered ({unique_id})")


async def _wait_for_lock_entity(hass, timeout: float = 10.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            return _lock_entity_id(hass)
        except AssertionError:
            await asyncio.sleep(0.1)
    raise AssertionError("lock entity never registered")


class CommandTap:
    """Subscribes to the command topic and captures published commands."""

    def __init__(self) -> None:
        self.captured: list[CapturedCommand] = []
        self._client = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        import aiomqtt

        self._client = aiomqtt.Client(
            MQTT_BROKER, MQTT_PORT, identifier=f"tap-{int(time.time() * 1000)}"
        )
        await self._client.__aenter__()
        await self._client.subscribe(f"{BASE}/command/+", qos=1)

        async def _reader():
            async for msg in self._client.messages:
                qos = getattr(msg.qos, "value", msg.qos)
                self.captured.append(
                    CapturedCommand(
                        topic=msg.topic.value,
                        payload=bytes(msg.payload).decode(),
                        qos=qos,
                        retain=msg.retain,
                    )
                )

        self._task = asyncio.create_task(_reader())
        # Let the subscription settle on the broker.
        await asyncio.sleep(0.4)
        return self

    async def __aexit__(self, *exc):
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._client is not None:
            await self._client.__aexit__(*exc)

    async def wait_for(self, topic: str, timeout: float = 10.0) -> CapturedCommand:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for cmd in self.captured:
                if cmd.topic == topic:
                    return cmd
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"no command captured on {topic}; saw {[c.topic for c in self.captured]}"
        )


@pytest.fixture
async def hmac_env(hass, mqtt_client):
    """Set up a household with a deterministic command credential + a live node."""
    await _seed_credential(hass, HOUSEHOLD)
    await _setup_entry(hass, HOUSEHOLD)
    await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
    await settle(hass)
    entity_id = await _wait_for_lock_entity(hass)
    return {"entity_id": entity_id, "key": derive_command_key(TEST_SECRET, TEST_SALT)}


# ---------------------------------------------------------------------------
# Step 2: real HMAC lock through the Lock entity
# ---------------------------------------------------------------------------
class TestRealHmacLock:
    async def test_lock_via_ha_lock_entity(self, hass, hmac_env, mqtt_client):
        entity_id = hmac_env["entity_id"]
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "lock", {"entity_id": entity_id}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(LOCK_TOPIC)

        # Topic is the documented authoritative command topic.
        assert captured.topic == LOCK_TOPIC

        # Exactly four fields; none of the forbidden extras.
        body = captured.body
        assert set(body) == {"ts", "nonce", "req_id", "mac"}
        for forbidden in ("action", "unlock", "command", "schema"):
            assert forbidden not in body

        # QoS 1, not retained.
        assert captured.qos == 1
        assert captured.retain is False

        # Field shapes.
        assert isinstance(body["ts"], int) and not isinstance(body["ts"], bool)
        assert len(body["nonce"]) == 32
        assert body["nonce"] == body["nonce"].lower()
        assert all(c in "0123456789abcdef" for c in body["nonce"])
        assert isinstance(body["req_id"], str) and body["req_id"]
        assert len(body["mac"]) == 64
        assert body["mac"] == body["mac"].lower()
        assert all(c in "0123456789abcdef" for c in body["mac"])

    async def test_lock_timestamp_is_current_epoch(self, hass, hmac_env):
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "lock", {"entity_id": hmac_env["entity_id"]}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(LOCK_TOPIC)
        now = int(time.time())
        assert abs(captured.body["ts"] - now) <= 30, "ts is not current epoch seconds"

    async def test_lock_mac_verifies_independently(self, hass, hmac_env):
        key = hmac_env["key"]
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "lock", {"entity_id": hmac_env["entity_id"]}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(LOCK_TOPIC)

        b = captured.body
        canonical = f"{b['ts']}{b['nonce']}{b['req_id']}lock"
        expected = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
        assert b["mac"] == expected

    async def test_lock_accepted_by_independent_firmware_verifier(self, hass, hmac_env):
        key = hmac_env["key"]
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "lock", {"entity_id": hmac_env["entity_id"]}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(LOCK_TOPIC)

        verifier = FirmwareVerifier(key=key, now=int(time.time()))
        action = verifier.verify(BASE, captured.topic, captured.payload)
        assert action == "lock"

    async def test_lock_state_reflects_health_roundtrip(self, hass, hmac_env, mqtt_client):
        """ESP32 -> B/health -> MQTT -> HA lock state (emulated node)."""
        from helpers import health_payload

        # Locked (firmware numeric 1).
        await mqtt_client.publish(f"{BASE}/health", health_payload(lock_current=1), qos=0, retain=False)
        await settle(hass, seconds=0.5)
        state = hass.states.get(hmac_env["entity_id"])
        assert state is not None and state.state == "locked"

        # Unlocked (firmware numeric 0).
        await mqtt_client.publish(f"{BASE}/health", health_payload(lock_current=0), qos=0, retain=False)
        await settle(hass, seconds=0.5)
        state = hass.states.get(hmac_env["entity_id"])
        assert state is not None and state.state == "unlocked"


# ---------------------------------------------------------------------------
# Step 3: real HMAC unlock through the Lock entity
# ---------------------------------------------------------------------------
class TestRealHmacUnlock:
    async def test_unlock_via_ha_lock_entity(self, hass, hmac_env):
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "unlock", {"entity_id": hmac_env["entity_id"]}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(UNLOCK_TOPIC)

        assert captured.topic == UNLOCK_TOPIC
        body = captured.body
        assert set(body) == {"ts", "nonce", "req_id", "mac"}
        for forbidden in ("action", "unlock", "command", "schema"):
            assert forbidden not in body
        assert captured.qos == 1 and captured.retain is False
        assert len(body["mac"]) == 64

    async def test_unlock_mac_verifies_independently(self, hass, hmac_env):
        key = hmac_env["key"]
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "unlock", {"entity_id": hmac_env["entity_id"]}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(UNLOCK_TOPIC)

        b = captured.body
        canonical = f"{b['ts']}{b['nonce']}{b['req_id']}unlock"
        expected = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
        assert b["mac"] == expected

    async def test_unlock_accepted_by_independent_firmware_verifier(self, hass, hmac_env):
        key = hmac_env["key"]
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "unlock", {"entity_id": hmac_env["entity_id"]}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(UNLOCK_TOPIC)
        verifier = FirmwareVerifier(key=key, now=int(time.time()))
        assert verifier.verify(BASE, captured.topic, captured.payload) == "unlock"

    async def test_lock_and_unlock_use_different_inputs(self, hass, hmac_env):
        """Same ts/nonce/req_id must produce a different MAC per action."""
        key = hmac_env["key"]
        ts, nonce, req_id = 1760000000, "0123456789abcdef0123456789abcdef", "abc123"
        lock_mac = hmac.new(
            key, f"{ts}{nonce}{req_id}lock".encode(), hashlib.sha256
        ).hexdigest()
        unlock_mac = hmac.new(
            key, f"{ts}{nonce}{req_id}unlock".encode(), hashlib.sha256
        ).hexdigest()
        assert lock_mac != unlock_mac

        # A lock MAC must NOT be accepted for the unlock topic.
        verifier = FirmwareVerifier(key=key, now=None)
        lock_body = json.dumps(
            {"ts": ts, "nonce": nonce, "req_id": req_id, "mac": lock_mac}
        )
        with pytest.raises(FirmwareReject, match="MAC mismatch"):
            verifier.verify(BASE, UNLOCK_TOPIC, lock_body)


# ---------------------------------------------------------------------------
# Step 6: nonce / request-id validation over 100+ real commands
# ---------------------------------------------------------------------------
class TestNonceAndRequestId:
    async def test_100_real_commands_unique_nonce_and_req_id(self, hass, hmac_env):
        nonces: set[str] = set()
        req_ids: set[str] = set()
        macs: set[str] = set()
        count = 0

        async with CommandTap() as tap:
            for i in range(100):
                # Alternate actions to exercise both topics.
                service = "lock" if i % 2 == 0 else "unlock"
                expected_topic = LOCK_TOPIC if service == "lock" else UNLOCK_TOPIC
                before = len(tap.captured)
                await hass.services.async_call(
                    "lock",
                    service,
                    {"entity_id": hmac_env["entity_id"]},
                    blocking=True,
                )
                # Wait for this call's message specifically.
                deadline = asyncio.get_running_loop().time() + 5
                while (
                    len(tap.captured) <= before
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.02)
                cmd = tap.captured[-1]
                assert cmd.topic == expected_topic

                body = cmd.body
                nonce = body["nonce"]
                assert len(nonce) == 32
                assert all(c in "0123456789abcdef" for c in nonce)
                nonces.add(nonce)
                req_ids.add(body["req_id"])
                macs.add(body["mac"])
                count += 1

        assert count == 100
        assert len(nonces) == 100, "nonce reuse detected"
        assert len(req_ids) == 100, "request id reuse detected"
        assert len(macs) == 100, "MAC reuse detected (MAC should bind nonce)"

    async def test_nonces_are_not_a_predictable_counter(self, hass, hmac_env):
        captured_nonces: list[str] = []
        async with CommandTap() as tap:
            for _ in range(10):
                before = len(tap.captured)
                await hass.services.async_call(
                    "lock", "lock", {"entity_id": hmac_env["entity_id"]}, blocking=True
                )
                deadline = asyncio.get_running_loop().time() + 5
                while (
                    len(tap.captured) <= before
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.02)
                captured_nonces.append(tap.captured[-1].body["nonce"])

        as_ints = [int(n, 16) for n in captured_nonces]
        assert as_ints != sorted(as_ints), "nonces look like a monotonic counter"
        # Consecutive nonces must not differ by a constant step.
        diffs = {b - a for a, b in zip(as_ints, as_ints[1:], strict=False)}
        assert len(diffs) > 1, "nonces look like an arithmetic sequence"
