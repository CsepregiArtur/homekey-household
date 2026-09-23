"""Coordinator tests: state handling, multi-node, resilience, fail-closed.

Covers requirement 12 (defensive state handling), 18 (malformed data), 19
(missing credentials), 4/5 (multi-node / multi-household isolation) and the
availability semantics of requirement 11.
"""

from __future__ import annotations

import pytest

from custom_components.homekey_household.const import (
    TOPIC_BACKUP_LAST,
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
from custom_components.homekey_household.models import ValidationError
from custom_components.homekey_household.mqtt import KIND_LEGACY_STATUS
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry, make_message

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"
NID_2 = "HOUSE-001"


def state_payload(node_id: str, **overrides) -> str:
    import json

    payload = {
        "household_id": HID,
        "node_id": node_id,
        "node_name": f"Node {node_id}",
        "node_role": "gate",
        "node_state": "ACTIVE",
        "generation": 1,
        "firmware_version": "0.10.0",
    }
    payload.update(overrides)
    return json.dumps(payload)


def health_payload(node_id: str, **overrides) -> str:
    """Build a firmware-faithful ``B/health`` payload.

    Firmware 0.10.0 does **not** include ``household_id``/``node_id`` in health.
    """
    import json

    payload = {
        "network": "UNKNOWN",
        "mqtt": "OK",
        "mqtt_error": 0,
        "nfc": "OK",
        "lock_current": 1,
        "lock_target": 1,
        "backup": "ok",
        "certificate": "unknown",
        "firmware_version": "0.10.0",
        "uptime": 1234,
        "free_heap": 123456,
        "reset_reason": "1",
        "security": {"all_ok": True, "warnings": ""},
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.fixture
def coordinator(hass, command_key):
    entry = FakeConfigEntry()
    return HomeKeyHouseholdCoordinator(
        hass,
        entry,
        household_id=HID,
        command_key_provider=lambda: command_key,
    )


@pytest.fixture
def no_key_coordinator(hass):
    entry = FakeConfigEntry()
    return HomeKeyHouseholdCoordinator(
        hass,
        entry,
        household_id=HID,
        command_key_provider=None,
    )


class TestNodeRegistration:
    """Requirement 20: deterministic, idempotent node discovery."""

    async def test_state_message_registers_node(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, state_payload(NID))
        )
        node = coordinator.get_node(NID)
        assert node is not None
        assert node.node_name == f"Node {NID}"
        assert node.firmware == "0.10.0"

    async def test_registration_is_idempotent(self, coordinator):
        for _ in range(5):
            await coordinator.async_handle_message(
                make_message(TOPIC_STATE, state_payload(NID))
            )
        assert list(coordinator.nodes) == [NID]

    async def test_health_before_state_creates_node(self, coordinator):
        """A node may announce itself through any documented topic."""
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID))
        )
        assert coordinator.get_node(NID) is not None

    async def test_republished_retained_state_does_not_duplicate(self, coordinator):
        for _ in range(3):
            await coordinator.async_handle_message(
                make_message(TOPIC_STATE, state_payload(NID), retain=True)
            )
        assert len(coordinator.nodes) == 1


class TestAvailability:
    """Requirement 11: ``B/status`` + shared LWT, no fake timers."""

    async def test_status_online(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_STATUS, "online")
        )
        assert coordinator.get_node(NID).online is True
        assert coordinator.get_node(NID).available is True

    async def test_status_offline(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "offline"))
        node = coordinator.get_node(NID)
        assert node.online is False
        assert node.available is False

    async def test_shared_lwt_offline_makes_unavailable(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, state_payload(NID))
        )
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
        await coordinator.async_handle_message(
            make_message(KIND_LEGACY_STATUS, "offline", legacy=True)
        )
        node = coordinator.get_node(NID)
        assert node.lwt_online is False
        assert node.available is False

    async def test_shared_lwt_online_restores_availability(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
        await coordinator.async_handle_message(
            make_message(KIND_LEGACY_STATUS, "offline", legacy=True)
        )
        await coordinator.async_handle_message(
            make_message(KIND_LEGACY_STATUS, "online", legacy=True)
        )
        assert coordinator.get_node(NID).available is True

    async def test_lwt_does_not_create_a_node(self, coordinator):
        """The MAC-derived LWT topic carries no household identity."""
        await coordinator.async_handle_message(
            make_message(KIND_LEGACY_STATUS, "online", node_id="ESP_AABB", legacy=True)
        )
        assert coordinator.nodes == {}

    async def test_clean_disconnect_leaves_retained_online(self, coordinator):
        """Documented limitation: no timer-based fake offline state."""
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
        # No further messages (clean disconnect emits no will).
        assert coordinator.get_node(NID).online is True

    async def test_status_rejects_unexpected_payload(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "ON"))
        assert coordinator.nodes == {}


class TestStateHandling:
    """Requirements 6/7/12: healthy parsing and preserved semantics."""

    async def test_health_snapshot_parsed(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID))
        )
        health = coordinator.get_node(NID).health
        assert health is not None
        assert health.network == "UNKNOWN"
        assert health.certificate == "unknown"
        assert health.lock_current_state == LockState.LOCKED

    async def test_lock_state_derived_from_health(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID, lock_current=0))
        )
        assert coordinator.get_node(NID).lock_state == LockState.UNLOCKED

    async def test_jammed_lock_state(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID, lock_current=2))
        )
        node = coordinator.get_node(NID)
        assert node.lock_state == LockState.JAMMED

    async def test_unknown_lock_state_without_health(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, state_payload(NID))
        )
        assert coordinator.get_node(NID).lock_state == LockState.UNKNOWN

    async def test_security_raw_ok(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_SECURITY, "OK"))
        assert coordinator.get_node(NID).security == "OK"

    async def test_security_raw_warning(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_SECURITY, "WARNING"))
        assert coordinator.get_node(NID).security == "WARNING"

    async def test_security_not_converted_to_number(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_SECURITY, "OK"))
        assert coordinator.get_node(NID).security == "OK"
        assert not isinstance(coordinator.get_node(NID).security, int)

    async def test_backup_status_raw(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_BACKUP_STATUS, "completed")
        )
        assert coordinator.get_node(NID).backup_status == "completed"

    async def test_backup_last_metadata(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_BACKUP_LAST, '{"status":"completed","timestamp":1760000000}')
        )
        backup = coordinator.get_node(NID).backup
        assert backup is not None and backup.status == "completed"

    async def test_last_auth_metadata(self, coordinator):
        await coordinator.async_handle_message(
            make_message(
                TOPIC_LAST_AUTH,
                '{"type":"HomeKey","result":"SUCCESS","timestamp":1760000000}',
            )
        )
        auth = coordinator.get_node(NID).last_auth
        assert auth is not None and auth.result == "SUCCESS"

    async def test_state_merge_preserves_health(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, state_payload(NID, firmware_version="0.10.1"))
        )
        node = coordinator.get_node(NID)
        assert node.health is not None
        assert node.firmware == "0.10.1"


class TestMalformedPayloads:
    """Requirement 18: malformed data never crashes and preserves state."""

    async def test_invalid_json_rejected(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, "{not json")
        )
        # No node may be created from a payload that failed to parse.
        assert coordinator.nodes == {}

    async def test_missing_identity_rejected(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, '{"node_name":"Gate"}')
        )
        node = coordinator.get_node(NID)
        assert node is None or node.node_name != "Gate"

    async def test_identity_mismatch_rejected(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, state_payload(NID, household_id="OTHER"))
        )
        assert coordinator.nodes == {}

    async def test_previous_valid_state_preserved(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID))
        )
        before = coordinator.get_node(NID).health
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, '{"household_id":"' + HID + '"}')
        )
        assert coordinator.get_node(NID).health is before

    async def test_malformed_security_preserved(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_SECURITY, "OK"))
        await coordinator.async_handle_message(make_message(TOPIC_SECURITY, "BROKEN"))
        assert coordinator.get_node(NID).security == "OK"

    async def test_malformed_backup_preserved(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_BACKUP_STATUS, "completed")
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_BACKUP_STATUS, "half-done")
        )
        assert coordinator.get_node(NID).backup_status == "completed"

    async def test_non_object_json_rejected(self, coordinator):
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, "[1,2,3]"))
        node = coordinator.get_node(NID)
        assert node is None or node.health is None

    async def test_boolean_not_accepted_as_integer(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID, lock_current=True))
        )
        node = coordinator.get_node(NID)
        assert node is None or node.health is None


class TestIsolation:
    """Requirements 4/5: nodes and households never collide."""

    async def test_multiple_nodes_in_one_household(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, state_payload(NID), node_id=NID)
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_STATE, state_payload(NID_2), node_id=NID_2)
        )
        assert set(coordinator.nodes) == {NID, NID_2}

    async def test_node_state_is_independent(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health_payload(NID, lock_current=1), node_id=NID)
        )
        await coordinator.async_handle_message(
            make_message(
                TOPIC_HEALTH, health_payload(NID_2, lock_current=0), node_id=NID_2
            )
        )
        assert coordinator.get_node(NID).lock_state == LockState.LOCKED
        assert coordinator.get_node(NID_2).lock_state == LockState.UNLOCKED

    async def test_other_household_messages_ignored(self, coordinator):
        await coordinator.async_handle_message(
            make_message(
                TOPIC_STATE, state_payload(NID), household_id="HOME-OTHER"
            )
        )
        assert coordinator.nodes == {}


class TestFailClosedCommands:
    """Requirement 19/23: commands fail closed without credentials."""

    async def test_no_key_provider_raises(self, no_key_coordinator):
        with pytest.raises(ValidationError, match="credential unavailable"):
            await no_key_coordinator.async_lock_node(NID)

    async def test_empty_key_raises(self, hass):
        entry = FakeConfigEntry()
        coordinator = HomeKeyHouseholdCoordinator(
            hass, entry, household_id=HID, command_key_provider=lambda: b""
        )
        with pytest.raises(ValidationError, match="credential unavailable"):
            await coordinator.async_unlock_node(NID)

    async def test_command_control_disabled_without_key(self, no_key_coordinator):
        assert no_key_coordinator.command_control_enabled is False

    async def test_command_control_enabled_with_key(self, coordinator):
        assert coordinator.command_control_enabled is True

    async def test_missing_mqtt_client_fails_closed(self, coordinator):
        """A key alone is not enough; no transport means no publish."""
        coordinator.mqtt = None
        with pytest.raises(ValidationError, match="MQTT client unavailable"):
            await coordinator.async_lock_node(NID)
