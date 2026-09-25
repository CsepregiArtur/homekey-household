"""Step 13 - physical lock state fidelity.

Verifies the physical bolt, the firmware's reported ``lock_current`` and Home
Assistant's view all agree, and that repeated telemetry does not create
oscillation or duplicate entities.

The firmware latches the actuator pin with ``gpio_hold_en`` (it does not pulse),
and a momentary mode exists but defaults to disabled with a 5000 ms timeout.
These tests check the *observable* consequence of that configuration rather than
assuming which mode is active.
"""

from __future__ import annotations

import json

import pytest

from tests_hardware.device import topic_suffix
from tests_hardware.helpers import Evidence, HardwareBlocked

pytestmark = pytest.mark.lock

LOCK_LITERALS = {"locked", "unlocked", "locking", "unlocking", "jammed"}


def _state_payloads(observer, seconds: float = 12.0) -> list[dict]:
    messages = observer.collect(seconds=seconds)
    payloads = []
    for message in messages:
        if topic_suffix(message["topic"]) != "state":
            continue
        try:
            payloads.append(json.loads(message["payload"]))
        except json.JSONDecodeError:
            continue
    return payloads


def test_lock_current_is_a_known_literal(observer, evidence: Evidence) -> None:
    """lock_current must be one of the contract's lock literals."""
    payloads = _state_payloads(observer)
    if not payloads:
        raise HardwareBlocked(
            "no B/state payload available; lock state cannot be validated"
        )

    value = str(payloads[-1].get("lock_current", "")).lower()
    evidence.record("lock_current", value)
    assert value in LOCK_LITERALS, (
        f"lock_current={value!r} is not a recognised lock literal "
        f"({sorted(LOCK_LITERALS)})"
    )


def test_state_is_stable_across_repeated_reports(observer, evidence: Evidence) -> None:
    """Repeated telemetry must not oscillate between lock states.

    A flapping lock_current would make the entity unusable and could indicate the
    actuator is being re-driven on every publish.
    """
    payloads = _state_payloads(observer, seconds=20.0)
    if len(payloads) < 2:
        raise HardwareBlocked(
            "fewer than two state reports observed, so stability cannot be assessed"
        )

    sequence = [str(p.get("lock_current", "")).lower() for p in payloads]
    evidence.record("sequence", sequence)

    transitions = sum(1 for a, b in zip(sequence, sequence[1:], strict=False) if a != b)
    assert transitions == 0, (
        f"lock_current oscillated {transitions} time(s) without any command being "
        f"sent: {sequence}. The device must not change reported state on its own."
    )


def test_no_duplicate_node_registrations(observer, evidence: Evidence) -> None:
    """One physical node must map to exactly one identity in the topic tree."""
    messages = observer.collect(seconds=12.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])
    if not messages:
        raise HardwareBlocked("no messages observed; duplicates cannot be assessed")

    pairs = set()
    for message in messages:
        parts = message["topic"].split("/")
        # homekey/household/<hid>/nodes/<nid>/...
        if len(parts) >= 5:
            pairs.add((parts[2], parts[4]))

    assert len(pairs) == 1, (
        f"expected exactly one (household, node) identity, found {sorted(pairs)}; "
        "duplicate identities create duplicate Home Assistant entities"
    )


def test_transition_reports_are_ordered(observer, evidence: Evidence) -> None:
    """A locked->unlocked transition must be reflected in subsequent state."""
    payloads = _state_payloads(observer, seconds=25.0)
    if len(payloads) < 2:
        raise HardwareBlocked(
            "requires at least two state reports across a physical transition; "
            "the transition itself needs an operator or a valid command"
        )

    first = str(payloads[0].get("lock_current", "")).lower()
    last = str(payloads[-1].get("lock_current", "")).lower()
    evidence.record("transition", {"from": first, "to": last})
    raise HardwareBlocked(
        f"observed lock_current {first!r} -> {last!r}, but confirming this "
        "matches the physical bolt requires an observation at the hardware."
    )
