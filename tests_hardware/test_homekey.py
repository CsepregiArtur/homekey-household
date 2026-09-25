"""Steps 15-16 - real HomeKey/NFC authentication and invalid credentials.

Step 15 uses the actual NFC reader and credential. The critical assertion is
that authentication reaches the existing LockManager and that ``B/last_auth``
updates — but the tap itself is physical, so an operator is required.

Step 16 must never weaken security to make a test possible: an invalid
credential must be rejected, and the firmware must not be coaxed into accepting
it. There is deliberately no "skip verification" path here.
"""

from __future__ import annotations

import json

import pytest

from tests_hardware.device import SerialConsole, topic_suffix
from tests_hardware.helpers import Evidence, HardwareBlocked

pytestmark = pytest.mark.homekey


def test_nfc_reader_initialises(console: SerialConsole, evidence: Evidence) -> None:
    """The NFC reader must come up. This can be observed on the console."""
    capture = console.capture(seconds=20.0, reset=True)
    evidence.serial(capture.text)

    reader_errors = [
        token
        for token in (
            "NfcManager] Failed to start",
            "Pn532Reader] Failed",
            "Failed to init",
            "reader not found",
        )
        if token in capture.text
    ]
    assert not reader_errors, (
        f"the NFC reader did not initialise ({reader_errors}). The reader model "
        "is selected at runtime from NVS (0=PN532, 1=PN7160, 2=ST25R3916)."
    )


def test_invalid_credential_is_rejected_without_exposing_material(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Step 16 - an unprovisioned credential must fail closed.

    Requires physically presenting an unknown tag. What CAN be asserted without
    the tap is that no credential material leaks during a rejection, which is
    checked here on whatever the console produced.
    """
    capture = console.capture(seconds=20.0, reset=True)
    evidence.serial(capture.text)

    import re

    leaks = re.findall(r"\b[0-9a-f]{64}\b", capture.text)
    assert not leaks, (
        "a 64-hex value appeared on the console while idle; credential or key "
        "material must never be logged (redacted before storage)"
    )

    raise HardwareBlocked(
        "requires a human to present an invalid/unprovisioned credential at the "
        "reader and to confirm the bolt does NOT move. Security is not weakened "
        "to automate this."
    )


def test_last_auth_topic_updates_after_successful_tap(
    observer, evidence: Evidence
) -> None:
    """B/last_auth must be retained and updated after a successful authentication."""
    messages = observer.collect(seconds=20.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    last_auth = [m for m in messages if topic_suffix(m["topic"]) == "last_auth"]
    if not last_auth:
        raise HardwareBlocked(
            "no B/last_auth message observed. It is retained, so it should appear "
            "immediately once the device has ever authenticated a credential — "
            "which also means it is absent until a physical tap has occurred."
        )

    payload = json.loads(last_auth[-1]["payload"])
    assert last_auth[-1]["retain"], "B/last_auth must be retained per the contract"
    # The contract must never expose credential material in this payload.
    lowered = json.dumps(payload).lower()
    for forbidden in ("secret", "private_key", "key_material"):
        assert forbidden not in lowered, (
            f"B/last_auth exposes {forbidden!r}; authentication telemetry must "
            "never carry credential material"
        )


def test_local_authentication_accepted_by_lockmanager(
    console: SerialConsole, evidence: Evidence
) -> None:
    """The tap must reach LockManager, not terminate in the reader layer."""
    capture = console.capture(seconds=25.0, reset=False)
    evidence.serial(capture.text)

    raise HardwareBlocked(
        "requires a physical credential tap while observing the console for the "
        "LockManager accept path. This cannot be triggered in software, and "
        "simulating it would defeat the purpose of the hardware validation."
    )
