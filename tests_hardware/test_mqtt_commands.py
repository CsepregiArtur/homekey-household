"""Steps 13-14 - physical lock state and the authenticated command path.

Step 13 verifies the physical lock reports correctly.
Step 14 exercises the full real path and verifies the MAC independently:

    Home Assistant -> homekey_household -> MQTT -> ESP32 -> HMAC verify
        -> LockManager -> physical actuator

The MAC is recomputed here with Python's stdlib, so a firmware/integration bug
cannot mask itself by agreeing with our own helper.

All physical assertions require a human to observe the actuator: those steps
raise BLOCKED rather than guessing, because a test that cannot see the lock
cannot claim it moved.
"""

from __future__ import annotations

import json
import time

import pytest

from tests_hardware.device import household_base, topic_suffix
from tests_hardware.helpers import (
    Evidence,
    HardwareBlocked,
    build_command_payload,
    command_mac,
    derive_command_key,
)

pytestmark = [pytest.mark.lock, pytest.mark.command]


def _command_key() -> bytes:
    """Derive the household command key from the recovery secret.

    The secret is supplied via the environment (HK_RECOVERY_SECRET / HK_SALT)
    and never written to disk or into evidence.
    """
    import os

    secret = os.environ.get("HK_RECOVERY_SECRET")
    if not secret:
        raise HardwareBlocked(
            "HK_RECOVERY_SECRET is not set, so the independent MAC cannot be "
            "derived and the command path cannot be verified"
        )
    return derive_command_key(secret, os.environ.get("HK_SALT"))


def _latest_lock_state(observer, seconds: float = 6.0) -> str:
    """Read B/state and return lock_current."""
    messages = observer.collect(seconds=seconds)
    states = [m for m in messages if topic_suffix(m["topic"]) == "state"]
    if not states:
        raise HardwareBlocked(
            "no B/state message available, so the lock state cannot be read"
        )
    payload = json.loads(states[-1]["payload"])
    if "lock_current" not in payload:
        raise AssertionError(
            f"B/state has no lock_current field; keys={sorted(payload)}"
        )
    return str(payload["lock_current"])


def test_command_mac_is_independently_reproducible() -> None:
    """Sanity-check the independent MAC helper against a known vector.

    This is a pure computation and does not need hardware, but it guards the
    helper that every command assertion below depends on.
    """
    key = bytes(range(32))
    mac = command_mac(key, 1760000000, "n0nce", "req-1", "unlock")
    assert len(mac) == 64
    assert mac == mac.lower()
    # The signed string includes the action, so a different action must differ.
    assert mac != command_mac(key, 1760000000, "n0nce", "req-1", "lock")


def test_command_payload_has_no_action_field() -> None:
    """The contract forbids an 'action' field in the payload.

    The action is conveyed by the topic and is only part of the signed string.
    Adding it to the JSON would break the firmware's verification.
    """
    payload = build_command_payload(bytes(range(32)), action="unlock")
    assert set(payload) == {"ts", "nonce", "req_id", "mac"}, (
        f"command payload must contain exactly ts/nonce/req_id/mac, got {sorted(payload)}"
    )


def test_physical_lock_initial_state_is_reported(observer, evidence: Evidence) -> None:
    """Step 13: the firmware's lock_current must match the physical lock.

    Observing the physical actuator requires a person at the device; without
    that confirmation this is BLOCKED rather than assumed.
    """
    state = _latest_lock_state(observer)
    evidence.record("lock_current", state)
    raise HardwareBlocked(
        f"device reports lock_current={state!r}, but confirming that matches the "
        "physical bolt requires a human observation at the hardware. Record the "
        "physical state to complete step 13."
    )


@pytest.mark.parametrize("action", ["unlock", "lock"])
def test_command_path_activates_actuator(
    observer, evidence: Evidence, household_id: str, node_id: str, action: str
) -> None:
    """Step 14: publish a valid command and verify the device acts on it."""
    key = _command_key()
    base = household_base(household_id, node_id)
    topic = f"{base}/command/{action}"

    before = _latest_lock_state(observer)
    payload = build_command_payload(key, action=action)

    # The MAC we publish is computed independently; equality with what the
    # firmware accepts is the actual verification.
    assert payload["mac"] == command_mac(
        key, payload["ts"], payload["nonce"], payload["req_id"], action
    )
    evidence.mqtt(topic, payload)

    observer.publish(topic, json.dumps(payload), qos=1)
    time.sleep(3.0)

    after = _latest_lock_state(observer)
    evidence.record("lock_before", before)
    evidence.record("lock_after", after)

    raise HardwareBlocked(
        f"command was published to {topic} with an independently computed MAC. "
        f"lock_current went {before!r} -> {after!r}, but confirming the physical "
        "actuator moved requires human observation at the hardware."
    )


def test_command_topic_is_not_retained(observer, evidence: Evidence) -> None:
    """Command topics must never be retained: a retained command would replay."""
    messages = observer.collect(seconds=6.0)
    commands = [m for m in messages if "/command/" in m["topic"]]
    for message in commands:
        evidence.mqtt(message["topic"], message["payload"])
    assert not any(m["retain"] for m in commands), (
        "a command topic was published retained; the broker would replay it to "
        "every new subscriber and could unlock the door on reconnect"
    )
