"""Step 12 (continued) - telemetry topics and payload contract.

Every assertion here is against the firmware 0.10.0 contract:
``B = homekey/household/<hid>/nodes/<nid>`` with ``state``/``status``/``health``/
``security`` subtopics. Payloads are validated, not merely observed.
"""

from __future__ import annotations

import json

import pytest

from tests_hardware.helpers import Evidence, HardwareBlocked

pytestmark = pytest.mark.telemetry

REQUIRED_STATE_FIELDS = ("household_id", "node_id", "firmware", "lock_current")


def _by_suffix(messages: list[dict]) -> dict[str, dict]:
    return {m["topic"].rsplit("/", 1)[-1]: m for m in messages}


def test_state_payload_has_required_identity_fields(
    observer, evidence: Evidence, household_id: str, node_id: str
) -> None:
    """B/state must carry the identity fields HA keys its entities on."""
    messages = observer.collect(seconds=10.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    state = _by_suffix(messages).get("state")
    if not state:
        raise HardwareBlocked(
            "no retained 'state' message arrived; the node is not publishing. "
            f"Observed topics: {sorted(m['topic'] for m in messages)}"
        )

    try:
        payload = json.loads(state["payload"])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"B/state is not valid JSON: {state['payload']!r}"
        ) from exc

    missing = [field for field in REQUIRED_STATE_FIELDS if field not in payload]
    assert not missing, f"B/state is missing required field(s): {missing}"

    assert payload["household_id"] == household_id, (
        f"B/state reports household_id={payload['household_id']!r} but this "
        f"household is {household_id!r}"
    )
    assert payload["node_id"] == node_id, (
        f"B/state reports node_id={payload['node_id']!r} but this node is {node_id!r}"
    )


def test_status_is_online_literal(observer, evidence: Evidence) -> None:
    """B/status is the retained availability literal 'online'."""
    messages = observer.collect(seconds=8.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    status = _by_suffix(messages).get("status")
    if not status:
        raise HardwareBlocked("no retained 'status' message arrived")

    assert status["payload"].strip().strip('"') == "online", (
        f"B/status should be the literal 'online', got {status['payload']!r}"
    )


def test_security_is_raw_literal_not_json(observer, evidence: Evidence) -> None:
    """B/security is a RAW string ('OK'/'WARNING'), not a JSON document.

    Getting this wrong silently breaks the security entity, so it is asserted
    explicitly rather than assumed.
    """
    messages = observer.collect(seconds=8.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    security = _by_suffix(messages).get("security")
    if not security:
        raise HardwareBlocked("no retained 'security' message arrived")

    raw = security["payload"].strip()
    assert raw.upper() in {"OK", "WARNING"}, (
        f"B/security should be the raw literal 'OK' or 'WARNING', got {raw!r}"
    )
    assert not raw.startswith("{"), (
        "B/security was JSON-encoded; the contract requires a raw string"
    )


def test_health_payload_shape(observer, evidence: Evidence) -> None:
    """B/health carries health metrics; identity fields are NOT required here.

    ``B/health`` deliberately omits household_id/node_id in the firmware, so a
    parser that demands them there is wrong.
    """
    messages = observer.collect(seconds=20.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    health = _by_suffix(messages).get("health")
    if not health:
        raise HardwareBlocked(
            "no 'health' message observed; health is non-retained and periodic, "
            "so it may simply not have been published during this window"
        )

    payload = json.loads(health["payload"])
    assert "firmware_version" in payload, (
        f"B/health should report firmware_version; keys={sorted(payload)}"
    )
