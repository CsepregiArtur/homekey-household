"""A lock command reports whether the node actually carried it out.

Home Assistant writes the requested state the moment the call returns, so before this the
activity read exactly the same for a command the node rejected and one it performed - until
the entity snapped back to what the node really reports. These tests hold the integration to
answering the question instead: did the node reach the state it was asked for?
"""

from __future__ import annotations

import json

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.homekey_household import lock as lock_module
from custom_components.homekey_household.const import (
    TOPIC_HEALTH,
    TOPIC_LOCK_LAST,
    TOPIC_STATE,
    TOPIC_STATUS,
)
from custom_components.homekey_household.coordinator import (
    HomeKeyHouseholdCoordinator,
)
from custom_components.homekey_household.lock import HomeKeyLock
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry, make_message

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"


def health(current: int) -> str:
    return json.dumps(
        {
            "network": "UNKNOWN",
            "mqtt": "OK",
            "mqtt_error": 0,
            "nfc": "OK",
            "lock_current": current,
            "lock_target": current,
            "backup": "ok",
            "certificate": "unknown",
            "firmware_version": "0.11.0",
            "uptime": 100,
            "free_heap": 100000,
            "reset_reason": "1",
            "security": {"all_ok": True, "warnings": ""},
        }
    )


def state() -> str:
    return json.dumps(
        {
            "household_id": HID,
            "node_id": NID,
            "node_name": "Gate",
            "node_role": "gate",
            "node_state": "ACTIVE",
            "generation": 1,
            "firmware_version": "0.11.0",
        }
    )


@pytest.fixture(autouse=True)
def fast_confirmation(monkeypatch):
    """Keep the confirmation window out of the test's runtime, not out of its logic."""
    monkeypatch.setattr(lock_module, "COMMAND_CONFIRM_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lock_module, "COMMAND_CONFIRM_POLL_SECONDS", 0.01)


@pytest.fixture
async def lock(hass):
    coordinator = HomeKeyHouseholdCoordinator(
        hass, FakeConfigEntry(), household_id=HID
    )
    await coordinator.async_handle_message(make_message(TOPIC_STATE, state()))
    await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
    await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(1)))
    return HomeKeyLock(coordinator, NID)


async def test_a_command_the_node_carries_out_is_quiet(lock):
    """The node reports the new state, so the call returns without complaint."""
    sent: list[str] = []

    async def send(node_id, action):
        sent.append(action)
        # The device answering: the state it reports moves to what was asked for.
        await lock.coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, json.dumps({"current": 0, "source": "mqtt"}))
        )

    lock.coordinator.async_send_lock_command = send  # type: ignore[method-assign]

    await lock.async_unlock()

    assert sent == ["unlock"]


async def test_a_command_the_node_ignores_is_reported(lock):
    """Published, no answer: the call must not claim success.

    The node reports itself locked and is asked to unlock, and it never says otherwise -
    which is what a rejected command looks like from here.
    """
    sent: list[str] = []

    async def send(node_id, action):
        sent.append(action)

    lock.coordinator.async_send_lock_command = send  # type: ignore[method-assign]

    assert lock.is_locked is True
    with pytest.raises(HomeAssistantError) as failure:
        await lock.async_unlock()

    assert sent == ["unlock"]
    assert "did not report" in str(failure.value)
    assert "audit log" in str(failure.value)


async def test_a_command_the_node_already_satisfied_is_quiet(lock):
    """Asking for the state the lock is already in needs no confirmation round trip."""
    sent: list[str] = []

    async def send(node_id, action):
        sent.append(action)

    lock.coordinator.async_send_lock_command = send  # type: ignore[method-assign]

    assert lock.is_locked is True
    await lock.async_lock()

    assert sent == ["lock"]


async def test_a_missing_credential_still_fails_closed(lock):
    """No credential means nothing is published, which is reported as such."""
    from custom_components.homekey_household.models import ValidationError

    async def send(node_id, action):
        raise ValidationError("command credential unavailable")

    lock.coordinator.async_send_lock_command = send  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError) as failure:
        await lock.async_unlock()

    assert "credential" in str(failure.value)
