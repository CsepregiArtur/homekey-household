"""MQTT client tests: topic parsing dispatch, command publishing, fail-closed.

Covers requirements 1/16/17/18/19/20 at the transport level:

* only documented subtopics are dispatched,
* reserved topics are never dispatched,
* commands are published to the correct topic at QoS 1, not retained,
* a missing credential results in no publication (fail closed),
* no legacy command topic is ever published.
"""

from __future__ import annotations

import json

import pytest

from custom_components.homekey_household.command import make_command_mac
from custom_components.homekey_household.const import (
    ACTION_LOCK,
    ACTION_UNLOCK,
    LEGACY_COMMAND_TOPICS,
    TOPIC_CMD_LOCK,
    TOPIC_CMD_UNLOCK,
    TOPIC_HEALTH,
    TOPIC_LAST_AUTH,
    TOPIC_SECURITY,
    TOPIC_STATE,
    TOPIC_STATUS,
    node_topic,
)
from custom_components.homekey_household.models import ValidationError
from custom_components.homekey_household.mqtt import (
    KIND_LEGACY_STATUS,
    HomeKeyMqttClient,
    parse_topic,
)

HID = "HOUSE-TEST"
NID = "GATE-001"
SECRET = "unit-test-recovery-secret"
SALT = "unit-test-salt"


@pytest.fixture
def received() -> list:
    return []


@pytest.fixture
def client(fake_transport, received):
    return HomeKeyMqttClient(
        None,
        fake_transport,
        received.append,
        household_id=HID,
        legacy_prefix="ESP_",
    )


class TestParseTopic:
    """Only documented topics are recognised."""

    @pytest.mark.parametrize(
        "subtopic",
        [
            "state",
            "status",
            "health",
            "security",
            "backup/status",
            "backup/last",
            "last_auth",
        ],
    )
    def test_documented_subtopics_parse(self, subtopic):
        topic = node_topic(HID, NID, subtopic)
        parsed = parse_topic(topic, "ESP_")
        assert parsed is not None
        assert parsed.household_id == HID
        assert parsed.node_id == NID
        assert parsed.subtopic == subtopic
        assert not parsed.reserved

    @pytest.mark.parametrize(
        "subtopic",
        ["events", "backup/request", "backup/data", "restore/request", "restore/status"],
    )
    def test_reserved_topics_are_flagged(self, subtopic):
        parsed = parse_topic(node_topic(HID, NID, subtopic), "ESP_")
        assert parsed is not None and parsed.reserved

    def test_command_topics_parse(self):
        parsed = parse_topic(node_topic(HID, NID, TOPIC_CMD_LOCK))
        assert parsed is not None and parsed.subtopic == TOPIC_CMD_LOCK

    def test_invalid_household_id_rejected(self):
        assert parse_topic("homekey/household/bad id/nodes/GATE-001/state") is None

    def test_invalid_node_id_rejected(self):
        assert parse_topic("homekey/household/HOME-1/nodes/bad id/state") is None

    def test_unrelated_topic_ignored(self):
        assert parse_topic("somewhere/else", "ESP_") is None

    def test_legacy_status_is_recognised_for_lwt(self):
        parsed = parse_topic("ESP_AABBCCDD/status", "ESP_")
        assert parsed is not None
        assert parsed.subtopic == KIND_LEGACY_STATUS
        assert parsed.legacy

    def test_legacy_wrong_prefix_ignored(self):
        assert parse_topic("OTHER_AABBCCDD/status", "ESP_") is None


class TestDispatch:
    """Documented messages are dispatched; reserved ones are not."""

    @pytest.mark.parametrize(
        "subtopic",
        ["state", "status", "health", "security", "backup/status", "backup/last", "last_auth"],
    )
    async def test_documented_topics_dispatched(self, client, received, subtopic):
        await client._on_message(node_topic(HID, NID, subtopic), "x", 0, False)
        assert len(received) == 1
        assert received[0].subtopic == subtopic

    @pytest.mark.parametrize(
        "subtopic",
        ["events", "backup/request", "backup/data", "restore/request", "restore/status"],
    )
    async def test_reserved_topics_not_dispatched(self, client, received, subtopic):
        await client._on_message(node_topic(HID, NID, subtopic), "{}", 0, False)
        assert received == []

    async def test_legacy_state_not_dispatched_authoritatively(
        self, client, received
    ):
        """Legacy lock state must not be consumed as HA V2 state."""
        await client._on_message("ESP_AABBCCDD/homekit/state", "1", 0, True)
        assert received == []

    async def test_legacy_auth_not_dispatched(self, client, received):
        """Legacy auth must not feed the V2 authentication sensor."""
        await client._on_message(
            "ESP_AABBCCDD/homekey/auth", '{"homekey":true}', 0, False
        )
        assert received == []

    async def test_legacy_status_dispatched_for_availability(self, client, received):
        await client._on_message("ESP_AABBCCDD/status", "online", 1, True)
        assert len(received) == 1
        assert received[0].subtopic == KIND_LEGACY_STATUS
        assert received[0].legacy

    async def test_unknown_subtopic_not_dispatched(self, client, received):
        await client._on_message(node_topic(HID, NID, "made_up"), "x", 0, False)
        assert received == []


class TestSubscriptions:
    """The client subscribes to the household tree and the shared LWT only."""

    async def test_start_subscribes_household_and_lwt(self, client, fake_transport):
        await client.async_start()
        assert "homekey/household/#" in fake_transport.subscriptions
        # VALID MQTT filter: the wildcard is its own topic level. The previous
        # "ESP_+/status" form is rejected by real brokers.
        assert "+/status" in fake_transport.subscriptions
        assert "ESP_+/status" not in fake_transport.subscriptions

    async def test_no_legacy_state_subscription_without_prefix(self, fake_transport):
        client = HomeKeyMqttClient(
            None, fake_transport, lambda m: None, household_id=HID
        )
        await client.async_start()
        assert list(fake_transport.subscriptions) == ["homekey/household/#"]

    async def test_stop_unsubscribes(self, client, fake_transport):
        await client.async_start()
        await client.async_stop()
        assert fake_transport.subscriptions == {}


class TestCommandPublishing:
    """Requirements 16/17/19: authenticated publication and fail-closed behaviour."""

    async def test_lock_publishes_to_lock_topic(
        self, client, fake_transport, command_key
    ):
        command = await client.async_lock(HID, NID, command_key, ts=1760000000)
        topic, payload, qos, retain = fake_transport.published[-1]
        assert topic == node_topic(HID, NID, TOPIC_CMD_LOCK)
        assert qos == 1
        assert retain is False
        assert command.mac

    async def test_unlock_publishes_to_unlock_topic(
        self, client, fake_transport, command_key
    ):
        await client.async_unlock(HID, NID, command_key, ts=1760000000)
        topic = fake_transport.last_topic()
        assert topic == node_topic(HID, NID, TOPIC_CMD_UNLOCK)

    async def test_payload_matches_documented_schema(
        self, client, fake_transport, command_key
    ):
        await client.async_lock(HID, NID, command_key, ts=1760000000)
        payload = json.loads(fake_transport.last_payload())
        assert set(payload) == {"ts", "nonce", "req_id", "mac"}
        assert isinstance(payload["ts"], int)
        assert len(payload["mac"]) == 64
        assert "action" not in payload

    async def test_mac_binds_topic_action(
        self, client, fake_transport, command_key
    ):
        command = await client.async_lock(HID, NID, command_key, ts=1760000000)
        payload = json.loads(fake_transport.last_payload())
        expected = make_command_mac(
            command_key, command.ts, command.nonce, command.req_id, ACTION_LOCK
        )
        assert payload["mac"] == expected

    async def test_missing_credential_fails_closed(self, client, fake_transport):
        with pytest.raises(ValidationError):
            await client.async_lock(HID, NID, b"")
        assert fake_transport.published == []

    async def test_no_unauthenticated_command_ever_published(
        self, client, fake_transport
    ):
        with pytest.raises(ValidationError):
            await client.async_unlock(HID, NID, b"")
        assert fake_transport.published == []

    async def test_never_publishes_legacy_command_topics(
        self, client, fake_transport, command_key
    ):
        await client.async_lock(HID, NID, command_key, ts=1760000000)
        await client.async_unlock(HID, NID, command_key, ts=1760000000)
        for topic, _payload, _qos, _retain in fake_transport.published:
            for legacy in LEGACY_COMMAND_TOPICS:
                assert not topic.endswith(legacy)
            assert topic.endswith("/command/lock") or topic.endswith(
                "/command/unlock"
            )

    async def test_nonces_not_reused_across_commands(
        self, client, fake_transport, command_key
    ):
        nonces = set()
        for _ in range(10):
            command = await client.async_lock(HID, NID, command_key)
            nonces.add(command.nonce)
        assert len(nonces) == 10

    async def test_command_not_retained(self, client, fake_transport, command_key):
        await client.async_lock(HID, NID, command_key, ts=1760000000)
        assert fake_transport.published[-1][3] is False

    async def test_unsupported_action_rejected(self, client, command_key):
        with pytest.raises(ValidationError, match="unsupported action"):
            await client.async_publish_authenticated_command(
                HID, NID, command_key, "reboot"
            )


class TestPayloadNeverContainsSecrets:
    """Requirement 8/22: no secret material leaves the integration."""

    async def test_published_payload_contains_no_recovery_secret(
        self, client, fake_transport, command_key
    ):
        await client.async_lock(HID, NID, command_key, ts=1760000000)
        payload = fake_transport.last_payload()
        assert SECRET not in payload
        assert SALT not in payload
        assert command_key.hex() not in payload

    async def test_published_payload_is_only_four_fields(
        self, client, fake_transport, command_key
    ):
        await client.async_unlock(HID, NID, command_key, ts=1760000000)
        payload = json.loads(fake_transport.last_payload())
        assert sorted(payload) == ["mac", "nonce", "req_id", "ts"]

    async def test_action_names_absent_from_payload(
        self, client, fake_transport, command_key
    ):
        await client.async_unlock(HID, NID, command_key, ts=1760000000)
        payload = json.loads(fake_transport.last_payload())
        assert ACTION_UNLOCK not in payload.values()
        assert ACTION_LOCK not in payload.values()


class TestKnownSubtopicSets:
    """Sanity: the dispatched sets match the documented contract."""

    def test_json_and_plain_subtopics_cover_the_contract(self):
        from custom_components.homekey_household.const import (
            JSON_SUBTOPICS,
            PLAIN_SUBTOPICS,
        )

        assert {
            TOPIC_STATE,
            TOPIC_HEALTH,
            "backup/last",
            TOPIC_LAST_AUTH,
            "lock/last",
        } == JSON_SUBTOPICS
        assert {TOPIC_STATUS, TOPIC_SECURITY, "backup/status"} == PLAIN_SUBTOPICS
