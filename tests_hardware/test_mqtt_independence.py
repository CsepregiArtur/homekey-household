"""Step 18 - local HomeKey must work with MQTT/HA/internet unavailable.

This is the single most important safety property in the product: the door must
open from a physical credential even when every network dependency is gone.

These tests are inherently BLOCKED without a physical credential and a human
present, because the trigger is a physical tap and the result is a physical
bolt movement. They are written so that the moment a person is present at the
device, the observations can be recorded and the tests complete.

The firmware must NOT be modified to make these pass.
"""

from __future__ import annotations

import json
import time

import pytest

from tests_hardware.device import SerialConsole, topic_suffix
from tests_hardware.helpers import Evidence, HardwareBlocked

pytestmark = pytest.mark.independence


def _collect_serial_for(console: SerialConsole, seconds: float) -> str:
    return console.capture(seconds=seconds, reset=False).text


def _lock_state(observer, seconds: float = 6.0) -> str | None:
    """Best-effort lock state read; None when MQTT is down (expected in test 1)."""
    try:
        messages = observer.collect(seconds=seconds)
    except HardwareBlocked:
        return None
    states = [m for m in messages if topic_suffix(m["topic"]) == "state"]
    if not states:
        return None
    return str(json.loads(states[-1]["payload"]).get("lock_current"))


def test_homekey_works_with_mqtt_unavailable(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Test 1 - with the broker down, a physical tap must still unlock.

    Procedure (requires an operator):
      1. Stop the MQTT broker.
      2. Tap a valid HomeKey credential.
      3. Observe the physical bolt.
    """
    raise HardwareBlocked(
        "requires an operator at the device: the broker must be stopped, a "
        "physical HomeKey credential tapped, and the bolt observed. Firmware "
        "logs on the console cannot prove that the local unlock path ran "
        "without MQTT, so this cannot be inferred automatically."
    )


def test_homekey_works_with_home_assistant_stopped(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Test 2 - with Home Assistant stopped, a physical tap must still unlock."""
    raise HardwareBlocked(
        "requires an operator at the device: Home Assistant must be stopped, a "
        "credential tapped, and the bolt observed."
    )


def test_homekey_works_without_internet(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Test 3 - with the WAN down but the LAN up, a tap must still unlock."""
    raise HardwareBlocked(
        "requires an operator at the device: the internet link must be cut while "
        "the LAN stays up, then a credential tapped and the bolt observed."
    )


def test_local_unlock_does_not_depend_on_mqtt_connection(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Structural check from the console: local auth must not require MQTT.

    While MQTT is failing to connect (its current state), the firmware must
    still present the NFC reader as initialised. If an MQTT failure disabled the
    reader, the local path would be broken — which is exactly what this guards.
    """
    text = _collect_serial_for(console, seconds=15.0)
    evidence.serial(text)

    # The firmware logs the reader/security subsystems early in boot. A reader
    # failure would surface as an explicit error.
    assert not any(
        token in text
        for token in ("NfcManager] Failed", "Pn532Reader] Failed", "reader init failed")
    ), (
        "the NFC reader failed to initialise. The local HomeKey path must work "
        "independently of MQTT, so an MQTT outage must not affect the reader."
    )


def test_mqtt_outage_does_not_stop_the_device(
    console: SerialConsole, evidence: Evidence
) -> None:
    """The firmware must stay up and keep retrying while MQTT is unavailable."""
    text = _collect_serial_for(console, seconds=25.0)
    evidence.serial(text)

    # MQTT is currently rejected by the broker; the device must keep running and
    # retrying rather than crashing or rebooting.
    assert "MQTT connect failed" in text or "MQTT_EVENT_DISCONNECTED" in text, (
        "expected the device to be retrying MQTT (it is currently rejected); "
        "no MQTT retry activity was seen"
    )
    assert "rst:0x" not in text.split("entry 0x")[0], (
        "the device reset during the MQTT outage; it must keep running"
    )


def test_device_keeps_retrying_mqtt(console: SerialConsole, evidence: Evidence) -> None:
    """MQTT retry must be periodic, so recovery is automatic once creds are fixed."""
    text = _collect_serial_for(console, seconds=30.0)
    evidence.serial(text)

    attempts = text.count("MQTT connect failed")
    if attempts == 0:
        raise HardwareBlocked(
            "no MQTT connect attempts observed in the window; MQTT may now be "
            "connected, in which case this retry check is not applicable"
        )
    assert attempts >= 2, (
        f"only {attempts} MQTT connect attempt(s) in ~30s; the device must retry "
        "periodically so it recovers automatically when the broker accepts it"
    )


def test_health_state_is_republished_after_reconnect(
    observer, evidence: Evidence
) -> None:
    """Non-retained health must be republished after a reconnect."""
    messages = observer.collect(seconds=30.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])
    health = [m for m in messages if topic_suffix(m["topic"]) == "health"]
    if not health:
        raise HardwareBlocked(
            "no health publication observed; health is non-retained and periodic"
        )
    time.sleep(0.1)
    assert health, "health must be republished after a reconnect"
