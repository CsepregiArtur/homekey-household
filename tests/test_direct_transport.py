"""Direct (broker-less) transport tests.

Two things are verified here, and both matter more than they look:

1. A ``/api/ha/state`` response is restated as the *exact* messages the MQTT
   transport would have delivered. That is what lets the coordinator, the models
   and every entity stay shared, so the two transports cannot drift apart in how
   they read the same firmware.
2. A poll that fails does not immediately flap a lock entity to unavailable, and a
   node that has moved to another household is refused rather than silently
   relabelled.

Nothing here needs a socket: the client is replaced by a fake that returns canned
responses, so what is under test is the translation and the coordinator contract,
not the TLS layer (which is covered against a real node in ``tests_hardware``).
"""

from __future__ import annotations

import json

import pytest

from custom_components.homekey_household.const import (
    DIRECT_OFFLINE_AFTER_FAILURES,
    TOPIC_BACKUP_STATUS,
    TOPIC_HEALTH,
    TOPIC_LAST_AUTH,
    TOPIC_SECURITY,
    TOPIC_STATE,
    TOPIC_STATUS,
    LockState,
)
from custom_components.homekey_household.coordinator import (
    HomeKeyHouseholdCoordinator,
)
from custom_components.homekey_household.direct import (
    DirectNoHouseholdError,
    DirectPoller,
    DirectProtocolError,
    DirectTransportError,
    health_from_state,
    resolve_identity,
    state_to_messages,
)
from custom_components.homekey_household.models import ValidationError
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"
OTHER_HID = "HOME-OTHER"

# The exact health document the firmware publishes on ``B/health``. Included
# verbatim in /api/ha/state by current firmware, which is the whole point: there is
# one implementation of it, so there is nothing to translate.
HEALTH_DOCUMENT = {
    "network": "UNKNOWN",
    "mqtt": "ERROR",
    "mqtt_error": 5,
    "nfc": "OK",
    "lock_current": 1,
    "lock_target": 1,
    "backup": "ok",
    "certificate": "unknown",
    "firmware_version": "0.11.0",
    "uptime": 4321,
    "free_heap": 98765,
    "reset_reason": "1",
    "security": {"all_ok": True, "warnings": ""},
}

NODE_INFO = {
    "protocol": 1,
    "transport": "tls",
    "secure": True,
    "port": 443,
    "fingerprint": "AA:BB:CC",
    "setup_completed": True,
    "device": {
        "name": "HomeKey",
        "model": "HomeKey-ESP32",
        "firmware": "0.11.0",
        "mac": "c8:f0:9e:49:2f:44",
        "node_id": NID,
        "node_name": "Gate",
    },
    "capabilities": {"read_state": True, "write_config": True, "lock_control": True},
}


def node_state(**overrides) -> dict:
    """A firmware-faithful ``/api/ha/state`` response."""
    state = {
        "protocol": 1,
        "firmware": "0.11.0",
        "household_id": HID,
        "household_name": "Test Household",
        "household_state": "ACTIVE",
        "config_version": 1,
        "node_id": NID,
        "node_name": "Gate",
        "node_role": "gate",
        "node_state": "ACTIVE",
        "generation": 3,
        "wifi": {"connected": True, "rssi": -55},
        "health": dict(HEALTH_DOCUMENT),
        "security": "OK",
        "backup_status": "completed",
        "last_auth": {"type": "HomeKey", "result": "SUCCESS", "timestamp": 1700000000},
    }
    state.update(overrides)
    return state


def legacy_node_state(**overrides) -> dict:
    """The shape served by firmware predating the embedded health document."""
    state = {
        "firmware": "0.10.0",
        "uptime_ms": 4321000,
        "free_heap": 98765,
        "wifi": {"connected": True, "rssi": -55},
        "lock": {"available": True, "current": 1, "target": 1},
        "reader": {"connected": False, "type": 2},
        "mqtt": {"configured": True, "connected": False},
    }
    state.update(overrides)
    return state


def subtopics(messages) -> dict[str, str]:
    return {message.subtopic: message.payload for message in messages}


class FakeDirectClient:
    """A DirectClient that answers from memory, and can be made to fail."""

    def __init__(self, state=None, info=None, *, failures: int = 0) -> None:
        self.base_url = "https://192.0.2.10:443"
        self.info = NODE_INFO if info is None else info
        self.state = node_state() if state is None else state
        self.failures_left = failures
        self.calls = 0
        self.lock_actions: list[str] = []

    async def async_get_info(self) -> dict:
        self.calls += 1
        return self.info

    async def async_get_state(self) -> dict:
        if self.failures_left > 0:
            self.failures_left -= 1
            raise DirectTransportError("the node did not answer")
        return self.state

    async def async_lock(self, action: str) -> dict:
        self.lock_actions.append(action)
        return {"action": action, "state": "unlocked", "current": 0, "target": 0}


@pytest.fixture
def coordinator(hass):
    entry = FakeConfigEntry()
    return HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)


class TestStateToMessages:
    """A direct response becomes the messages MQTT would have delivered."""

    def test_emits_only_documented_subtopics(self):
        payloads = subtopics(
            state_to_messages(node_state(), NODE_INFO, household_id=HID, node_id=NID)
        )
        assert set(payloads) == {
            TOPIC_STATUS,
            TOPIC_STATE,
            TOPIC_HEALTH,
            TOPIC_SECURITY,
            TOPIC_BACKUP_STATUS,
            TOPIC_LAST_AUTH,
        }

    def test_status_says_online(self):
        payloads = subtopics(
            state_to_messages(node_state(), NODE_INFO, household_id=HID, node_id=NID)
        )
        # A response that arrived at all is the availability signal for a polled
        # transport: there is no broker to publish a will.
        assert payloads[TOPIC_STATUS] == "online"

    def test_state_message_carries_identity(self):
        payloads = subtopics(
            state_to_messages(node_state(), NODE_INFO, household_id=HID, node_id=NID)
        )
        identity = json.loads(payloads[TOPIC_STATE])
        assert identity["household_id"] == HID
        assert identity["node_id"] == NID
        assert identity["node_name"] == "Gate"
        assert identity["node_role"] == "gate"
        assert identity["node_state"] == "ACTIVE"
        assert identity["generation"] == 3
        assert identity["firmware_version"] == "0.11.0"

    def test_health_document_is_passed_through_unchanged(self):
        payloads = subtopics(
            state_to_messages(node_state(), NODE_INFO, household_id=HID, node_id=NID)
        )
        # Verbatim, down to the values the firmware documents as stubs. Rewriting
        # them here would create a second, silently different interpretation of the
        # very same document.
        assert json.loads(payloads[TOPIC_HEALTH]) == HEALTH_DOCUMENT

    def test_identity_defaults_come_from_info_when_state_omits_them(self):
        state = node_state()
        del state["node_name"]
        del state["node_role"]
        del state["node_state"]
        payloads = subtopics(
            state_to_messages(state, NODE_INFO, household_id=HID, node_id=NID)
        )
        identity = json.loads(payloads[TOPIC_STATE])
        assert identity["node_name"] == "Gate"
        # Absent rather than invented: the model supplies its own documented default.
        assert "node_role" not in identity
        assert "node_state" not in identity

    def test_optional_topics_are_omitted_when_absent(self):
        state = node_state()
        del state["security"]
        del state["backup_status"]
        del state["last_auth"]
        payloads = subtopics(
            state_to_messages(state, NODE_INFO, household_id=HID, node_id=NID)
        )
        assert TOPIC_SECURITY not in payloads
        assert TOPIC_BACKUP_STATUS not in payloads
        assert TOPIC_LAST_AUTH not in payloads

    @pytest.mark.parametrize("value", ["unknown", "", "ok", "completed"])
    def test_backup_status_outside_the_contract_is_not_sent(self, value):
        # ``ok`` is the health manager's internal wording, not the contract's. Sending
        # it would be rejected as a payload fault on every single poll.
        payloads = subtopics(
            state_to_messages(
                node_state(backup_status=value),
                NODE_INFO,
                household_id=HID,
                node_id=NID,
            )
        )
        assert (TOPIC_BACKUP_STATUS in payloads) == (value == "completed")

    def test_unknown_security_value_is_not_sent(self):
        payloads = subtopics(
            state_to_messages(
                node_state(security="FINE"), NODE_INFO, household_id=HID, node_id=NID
            )
        )
        assert TOPIC_SECURITY not in payloads

    def test_unusable_last_auth_is_not_sent(self):
        payloads = subtopics(
            state_to_messages(
                node_state(last_auth={"result": "MAYBE"}),
                NODE_INFO,
                household_id=HID,
                node_id=NID,
            )
        )
        assert TOPIC_LAST_AUTH not in payloads


class TestHealthFromState:
    def test_embedded_document_wins(self):
        assert health_from_state(node_state()) == HEALTH_DOCUMENT

    def test_lock_not_reported_sentinel_is_dropped(self):
        state = node_state()
        state["health"] = {**HEALTH_DOCUMENT, "lock_current": 255, "lock_target": 255}
        health = health_from_state(state)
        # 255 is the health snapshot's "no lock manager" default, not a lock state.
        # Surfacing it would put a magic number in an entity attribute.
        assert "lock_current" not in health
        assert "lock_target" not in health
        # Everything else is untouched.
        assert health["mqtt"] == "ERROR"

    def test_legacy_shape_is_translated(self):
        health = health_from_state(legacy_node_state())
        assert health["network"] == "UNKNOWN"
        assert health["certificate"] == "unknown"
        assert health["lock_current"] == 1
        assert health["lock_target"] == 1
        assert health["mqtt"] == "ERROR"
        assert health["nfc"] == "ERROR"
        assert health["firmware_version"] == "0.10.0"

    def test_legacy_shape_without_lock_manager_invents_no_lock_state(self):
        state = legacy_node_state()
        state["lock"] = {"available": False}
        health = health_from_state(state)
        assert "lock_current" not in health
        assert "lock_target" not in health


class TestResolveIdentity:
    def test_reads_identity_from_the_state(self):
        assert resolve_identity(node_state(), NODE_INFO) == (HID, NID)

    def test_node_id_falls_back_to_the_info_endpoint(self):
        # /api/ha/info carries the node id, so firmware that omits it from the state
        # is still identifiable.
        state = node_state()
        del state["node_id"]
        assert resolve_identity(state, NODE_INFO) == (HID, NID)

    def test_a_household_is_never_guessed_at(self):
        # /api/ha/info deliberately does not publish the household id (it is part of
        # the MQTT topic path and that endpoint is unauthenticated), so an absent
        # household is a refusal rather than something to invent.
        state = node_state()
        del state["household_id"]
        with pytest.raises(DirectNoHouseholdError):
            resolve_identity(state, NODE_INFO)

    def test_refuses_a_node_without_a_household(self):
        with pytest.raises(DirectNoHouseholdError) as excinfo:
            resolve_identity(node_state(household_id=""), NODE_INFO)
        # The message has to say what to do, because nothing is broken.
        assert "Household" in str(excinfo.value)

    def test_refuses_when_no_node_id_can_be_found(self):
        info = {**NODE_INFO, "device": {}}
        with pytest.raises(DirectProtocolError):
            resolve_identity(node_state(node_id=""), info)


class TestPollerIngestion:
    """The poller feeds the coordinator exactly as the MQTT client does."""

    async def test_a_poll_registers_the_node(self, hass, coordinator):
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(), household_id=HID
        )
        await poller.async_poll_once()

        node = coordinator.get_node(NID)
        assert node is not None
        assert node.online is True
        assert node.node_name == "Gate"
        assert node.node_role == "gate"
        assert node.firmware == "0.11.0"
        assert node.generation == 3
        assert poller.node_id == NID

    async def test_health_lands_in_the_shared_model(self, hass, coordinator):
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(), household_id=HID
        )
        await poller.async_poll_once()

        node = coordinator.get_node(NID)
        assert node.health is not None
        assert node.health.mqtt == "ERROR"
        assert node.health.nfc == "OK"
        assert node.health.lock_current == 1
        # Documented stubs, preserved rather than replaced with something plausible.
        assert node.health.network == "UNKNOWN"
        assert node.health.certificate == "unknown"

    async def test_lock_entity_state_is_derived_normally(self, hass, coordinator):
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(), household_id=HID
        )
        await poller.async_poll_once()

        node = coordinator.get_node(NID)
        assert node.lock_state == LockState.LOCKED
        assert node.available is True

    async def test_security_and_backup_and_last_auth_are_ingested(
        self, hass, coordinator
    ):
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(), household_id=HID
        )
        await poller.async_poll_once()

        node = coordinator.get_node(NID)
        assert node.security == "OK"
        assert node.backup_status == "completed"
        assert node.last_auth is not None
        assert node.last_auth.result == "SUCCESS"

    async def test_a_single_failure_does_not_mark_the_node_offline(
        self, hass, coordinator
    ):
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(), household_id=HID
        )
        await poller.async_poll_once()
        client = poller.client
        client.failures_left = 1

        await poller.async_poll_safely()
        assert coordinator.get_node(NID).online is True

    async def test_a_run_of_failures_marks_the_node_offline(
        self, hass, coordinator
    ):
        client = FakeDirectClient()
        poller = DirectPoller(hass, coordinator, client, household_id=HID)
        await poller.async_poll_once()

        client.failures_left = DIRECT_OFFLINE_AFTER_FAILURES
        for _ in range(DIRECT_OFFLINE_AFTER_FAILURES - 1):
            await poller.async_poll_safely()
            assert coordinator.get_node(NID).online is True

        await poller.async_poll_safely()
        node = coordinator.get_node(NID)
        assert node.online is False
        assert node.available is False

    async def test_a_recovered_node_comes_back_online(self, hass, coordinator):
        client = FakeDirectClient()
        poller = DirectPoller(hass, coordinator, client, household_id=HID)
        await poller.async_poll_once()

        client.failures_left = DIRECT_OFFLINE_AFTER_FAILURES
        for _ in range(DIRECT_OFFLINE_AFTER_FAILURES):
            await poller.async_poll_safely()
        assert coordinator.get_node(NID).online is False

        await poller.async_poll_safely()
        assert coordinator.get_node(NID).online is True

    async def test_a_rehoused_node_is_refused_not_relabelled(
        self, hass, coordinator
    ):
        state = node_state(household_id=OTHER_HID)
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(state), household_id=HID
        )
        await poller.async_poll_safely()

        # Nothing is created under either household: the entry's entities are keyed
        # on the household recorded at setup, and quietly rewriting them would
        # relabel the device.
        assert coordinator.get_node(NID) is None
        assert coordinator.nodes == {}

    async def test_identity_mismatch_is_logged_once(self, hass, coordinator, caplog):
        state = node_state(household_id=OTHER_HID)
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(state), household_id=HID
        )
        with caplog.at_level("ERROR"):
            for _ in range(3):
                await poller.async_poll_safely()
        assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


class TestDirectLockControl:
    """Lock commands over the direct transport."""

    async def test_control_is_reported_as_available_without_a_command_key(
        self, hass, coordinator
    ):
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(), household_id=HID
        )
        coordinator.direct = poller
        # The direct transport authenticates with the device credential, so the
        # absence of a household command key says nothing about whether commands work.
        assert coordinator.command_key() is None
        assert coordinator.command_control_enabled is True

    async def test_without_a_transport_control_is_unavailable(self, coordinator):
        assert coordinator.command_control_enabled is False

    async def test_command_reaches_the_node_and_refreshes_state(
        self, hass, coordinator
    ):
        client = FakeDirectClient()
        poller = DirectPoller(hass, coordinator, client, household_id=HID)
        coordinator.direct = poller

        await coordinator.async_lock_node(NID)
        assert client.lock_actions == ["lock"]

        await coordinator.async_unlock_node(NID)
        assert client.lock_actions == ["lock", "unlock"]

        # The poll after the command is what makes the entity reflect what the
        # mechanism did, rather than what it was asked to do.
        assert coordinator.get_node(NID) is not None

    async def test_a_failed_refresh_does_not_hide_a_delivered_command(
        self, hass, coordinator
    ):
        client = FakeDirectClient()
        poller = DirectPoller(hass, coordinator, client, household_id=HID)
        await poller.async_poll_once()
        coordinator.direct = poller
        # The command succeeds; only the follow-up poll fails.
        client.failures_left = 1

        result = await coordinator.async_lock_node(NID)
        assert client.lock_actions == ["lock"]
        assert result["state"] == "unlocked"

    async def test_unsupported_action_is_refused(self, hass, coordinator):
        poller = DirectPoller(
            hass, coordinator, FakeDirectClient(), household_id=HID
        )
        coordinator.direct = poller
        with pytest.raises(ValidationError):
            await coordinator.async_send_lock_command(NID, "unlatch")
