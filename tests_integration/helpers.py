"""Shared helpers for the REAL integration validation suite.

Every payload builder here mirrors the documented firmware 0.10.0 schemas. They
are intentionally duplicated (rather than imported from the integration) so a bug
in the integration's parsing cannot be masked by reusing the integration's own
encoders.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

HOST = "127.0.0.1"
PORT = 18830

# Synthetic test identities. Never production values.
HOUSEHOLD = "HOME-TEST"
NODE_GATE = "GATE-TEST-001"
NODE_HOUSE = "HOUSE-TEST-001"
NODE_SMALL = "SMALL-TEST-001"
HOUSEHOLD_OTHER = "HOME-OTHER"
TEST_SECRET = "integration-test-recovery-secret"
TEST_SALT = "integration-test-salt"


def node_base(household_id: str, node_id: str) -> str:
    return f"homekey/household/{household_id}/nodes/{node_id}"


def unique(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Documented payloads (verbatim schemas)
# ---------------------------------------------------------------------------
def state_payload(
    household_id: str,
    node_id: str,
    node_name: str,
    *,
    node_role: str = "gate",
    node_state: str = "ACTIVE",
    generation: int = 1,
    firmware_version: str = "0.10.0",
) -> str:
    """``B/state`` — retained, QoS 0."""
    return json.dumps(
        {
            "household_id": household_id,
            "node_id": node_id,
            "node_name": node_name,
            "node_role": node_role,
            "node_state": node_state,
            "generation": generation,
            "firmware_version": firmware_version,
        },
        separators=(",", ":"),
    )


def health_payload(
    *,
    network: str = "UNKNOWN",
    mqtt: str = "OK",
    mqtt_error: int = 0,
    nfc: str = "OK",
    lock_current: int = 1,
    lock_target: int = 1,
    backup: str = "ok",
    certificate: str = "unknown",
    firmware_version: str = "0.10.0",
    uptime: int = 1234,
    free_heap: int = 123456,
    reset_reason: str = "1",
    security_all_ok: bool = True,
    security_warnings: str = "",
) -> str:
    """``B/health`` — NON-retained, QoS 0, no identity fields (per contract)."""
    return json.dumps(
        {
            "network": network,
            "mqtt": mqtt,
            "mqtt_error": mqtt_error,
            "nfc": nfc,
            "lock_current": lock_current,
            "lock_target": lock_target,
            "backup": backup,
            "certificate": certificate,
            "firmware_version": firmware_version,
            "uptime": uptime,
            "free_heap": free_heap,
            "reset_reason": reset_reason,
            "security": {"all_ok": security_all_ok, "warnings": security_warnings},
        },
        separators=(",", ":"),
    )


def backup_last_payload(status: str = "completed", timestamp: int = 1760000000) -> str:
    """``B/backup/last`` — retained, metadata only."""
    return json.dumps({"status": status, "timestamp": timestamp}, separators=(",", ":"))


def last_auth_payload(result: str = "SUCCESS", timestamp: int = 1760000000) -> str:
    """``B/last_auth`` — retained, safe metadata only."""
    return json.dumps(
        {"type": "HomeKey", "result": result, "timestamp": timestamp},
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
async def publish_telemetry(
    client: Any,
    household_id: str,
    node_id: str,
    node_name: str,
    *,
    lock_current: int = 1,
    security: str = "OK",
    status: str = "online",
    last_auth_result: str = "SUCCESS",
    backup_status: str = "completed",
    node_role: str = "gate",
) -> list[tuple[str, str, int, bool]]:
    """Publish the full documented telemetry set for one node.

    QoS and retain flags match the contract's telemetry table exactly.
    """
    base = node_base(household_id, node_id)
    sent: list[tuple[str, str, int, bool]] = []

    async def pub(topic: str, payload: str, qos: int, retain: bool) -> None:
        await client.publish(topic, payload, qos=qos, retain=retain)
        sent.append((topic, payload, qos, retain))

    await pub(f"{base}/state", state_payload(household_id, node_id, node_name, node_role=node_role), 0, True)
    await pub(f"{base}/status", status, 1, True)
    await pub(f"{base}/health", health_payload(lock_current=lock_current), 0, False)
    await pub(f"{base}/security", security, 0, True)
    await pub(f"{base}/backup/status", backup_status, 0, True)
    await pub(f"{base}/backup/last", backup_last_payload(), 0, True)
    await pub(f"{base}/last_auth", last_auth_payload(result=last_auth_result), 0, True)
    return sent


async def wait_for_entity(
    hass,
    entity_id: str,
    *,
    timeout: float = 10.0,
    condition=None,
):
    """Wait until an entity exists (and optionally satisfies ``condition``)."""
    deadline = asyncio.get_running_loop().time() + timeout
    state = None
    while asyncio.get_running_loop().time() < deadline:
        state = hass.states.get(entity_id)
        if state is not None and (condition is None or condition(state)):
            return state
        await asyncio.sleep(0.05)
    return state


async def wait_for_entities(hass, entity_ids, *, timeout: float = 10.0) -> dict:
    """Wait until all given entity ids exist."""
    deadline = asyncio.get_running_loop().time() + timeout
    states: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        states = {eid: hass.states.get(eid) for eid in entity_ids}
        if all(s is not None for s in states.values()):
            return states
        await asyncio.sleep(0.05)
    return states


async def settle(hass, *, seconds: float = 1.0) -> None:
    """Let published MQTT messages be delivered and processed.

    Entity platforms add entities incrementally for newly discovered nodes, so
    there is no longer a debounced config-entry reload to wait for: a short drain
    of the HA task queue is enough.
    """
    await hass.async_block_till_done()
    await asyncio.sleep(seconds)
    await hass.async_block_till_done()
