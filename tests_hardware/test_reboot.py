"""Step 17 - ESP32 reboot and power-interruption resilience.

Test A reboots the device normally; Test B removes power and relies on the MQTT
Last Will and Testament to report the node offline.

SAFETY: a power-loss test can leave a physical lock in an indeterminate state.
The Test B case therefore refuses to run unless the operator explicitly opts in
via HK_ALLOW_POWER_LOSS_TEST=1 after confirming the lock is safe.
"""

from __future__ import annotations

import os

import pytest

from tests_hardware.device import SerialConsole, topic_suffix
from tests_hardware.helpers import Evidence, HardwareBlocked

pytestmark = pytest.mark.reboot


def test_normal_reboot_returns_to_service(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Test A - after a normal reboot the device must rejoin and republish."""
    capture = console.capture(seconds=30.0, reset=True)
    evidence.serial(capture.text)

    assert "ConfigManager:" in capture.text, (
        "the device did not reach application code after reboot"
    )
    # Retained topics are restored by the broker on resubscribe; the device must
    # also republish its own state after a reboot.
    assert "rst:0x" in capture.text, "no reset banner observed; reboot not confirmed"

    raise HardwareBlocked(
        "the reboot itself was performed and the device returned to application "
        "code, but confirming that HA entities survived, retained state was "
        "restored, health was republished and no duplicate entities appeared "
        "requires observation of Home Assistant and HK_HOUSEHOLD_ID/HK_NODE_ID."
    )


def test_power_interruption_triggers_lwt_offline(observer, evidence: Evidence) -> None:
    """Test B - power loss must make the broker publish the LWT 'offline'."""
    if os.environ.get("HK_ALLOW_POWER_LOSS_TEST") != "1":
        raise HardwareBlocked(
            "power-loss test not enabled. Set HK_ALLOW_POWER_LOSS_TEST=1 only "
            "after confirming the physical lock is in a SAFE state; a power cut "
            "during a transition could leave the bolt indeterminate."
        )
    raise HardwareBlocked(
        "requires physically removing and restoring power while observing the "
        "broker's last-will message. Not automated: cutting power under software "
        "control risks an unsafe lock state."
    )


def test_no_duplicate_entities_after_reboot(observer, evidence: Evidence) -> None:
    """A reboot must not create duplicate nodes/entities."""
    messages = observer.collect(seconds=15.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    if not messages:
        raise HardwareBlocked("no messages observed, so duplicates cannot be assessed")

    node_paths = {"/".join(m["topic"].split("/")[:5]) for m in messages}
    assert len(node_paths) == 1, (
        "more than one node path appeared after reboot, which would create "
        f"duplicate entities in Home Assistant: {sorted(node_paths)}"
    )


def test_availability_transitions_are_reported(observer, evidence: Evidence) -> None:
    """B/status must reflect online state so HA availability is correct."""
    messages = observer.collect(seconds=10.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    status = [m for m in messages if topic_suffix(m["topic"]) == "status"]
    if not status:
        raise HardwareBlocked("no B/status message observed to assess availability")
    assert status[-1]["retain"], (
        "B/status must be retained so availability survives restarts"
    )
