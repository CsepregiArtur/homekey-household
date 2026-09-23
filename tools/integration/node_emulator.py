"""ESP32 node emulator for REAL integration validation.

Publishes the **exact documented firmware 0.10.0 payloads** over a real MQTT
connection. This is test tooling; it is never imported by the integration.

Payloads below are copied verbatim from the authoritative contract:
  * docs/content/mqtt_household_api.md
  * docs/content/mqtt_api_contract_matrix.md  (section 4, "Payload schemas")

If a payload here drifts from those documents, the test is invalid.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import aiomqtt
from mqtt_broker import DEFAULT_HOST, DEFAULT_PORT


def node_base(household_id: str, node_id: str) -> str:
    return f"homekey/household/{household_id}/nodes/{node_id}"


# ---------------------------------------------------------------------------
# Exact documented payload builders
# ---------------------------------------------------------------------------
def state_payload(
    household_id: str,
    node_id: str,
    node_name: str,
    node_role: str = "gate",
    node_state: str = "ACTIVE",
    generation: int = 1,
    firmware_version: str = "0.10.0",
) -> str:
    """``B/state`` — documented schema (retained, QoS 0)."""
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
    """``B/health`` — documented schema (NON-retained, QoS 0, no identity fields)."""
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
            "security": {
                "all_ok": security_all_ok,
                "warnings": security_warnings,
            },
        },
        separators=(",", ":"),
    )


def security_payload(state: str = "OK") -> str:
    """``B/security`` — documented plain string ``OK`` / ``WARNING``."""
    return state


def backup_last_payload(status: str = "completed", timestamp: int = 1760000000) -> str:
    """``B/backup/last`` — documented metadata-only schema."""
    return json.dumps({"status": status, "timestamp": timestamp}, separators=(",", ":"))


def last_auth_payload(
    result: str = "SUCCESS",
    auth_type: str = "HomeKey",
    timestamp: int = 1760000000,
) -> str:
    """``B/last_auth`` — documented safe-metadata schema."""
    return json.dumps(
        {"type": auth_type, "result": result, "timestamp": timestamp},
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Emulator
# ---------------------------------------------------------------------------
@dataclass
class EmulatedNode:
    """Publishes the documented household payloads for one node."""

    household_id: str
    node_id: str
    node_name: str
    node_role: str = "gate"
    # Retained and QoS per the contract's telemetry table.
    _publish_log: list[tuple[str, str, int, bool]] = field(default_factory=list)

    @property
    def base(self) -> str:
        return node_base(self.household_id, self.node_id)

    async def publish_all(
        self,
        client: aiomqtt.Client,
        *,
        lock_current: int = 1,
        security: str = "OK",
        status: str = "online",
        last_auth_result: str = "SUCCESS",
        backup_status: str = "completed",
    ) -> list[tuple[str, str, int, bool]]:
        """Publish the full documented telemetry set; returns what was sent."""
        sent: list[tuple[str, str, int, bool]] = []

        async def pub(topic: str, payload: str, qos: int, retain: bool) -> None:
            await client.publish(topic, payload, qos=qos, retain=retain)
            sent.append((topic, payload, qos, retain))

        # B/state: retained, QoS 0
        await pub(
            f"{self.base}/state",
            state_payload(
                self.household_id,
                self.node_id,
                self.node_name,
                self.node_role,
            ),
            0,
            True,
        )
        # B/status: retained, QoS 1, payload "online"
        await pub(f"{self.base}/status", status, 1, True)
        # B/health: NON-retained, QoS 0
        await pub(
            f"{self.base}/health",
            health_payload(lock_current=lock_current),
            0,
            False,
        )
        # B/security: retained, QoS 0, plain string
        await pub(f"{self.base}/security", security_payload(security), 0, True)
        # B/backup/status: retained, QoS 0, plain string
        await pub(f"{self.base}/backup/status", backup_status, 0, True)
        # B/backup/last: retained, QoS 0, metadata JSON
        await pub(f"{self.base}/backup/last", backup_last_payload(), 0, True)
        # B/last_auth: retained, QoS 0, safe metadata JSON
        await pub(
            f"{self.base}/last_auth",
            last_auth_payload(result=last_auth_result),
            0,
            True,
        )

        self._publish_log.extend(sent)
        return sent

    async def publish_status(
        self, client: aiomqtt.Client, status: str, *, retained: bool = True
    ) -> None:
        await client.publish(
            f"{self.base}/status", status, qos=1, retain=retained
        )

    async def publish_health(
        self, client: aiomqtt.Client, *, lock_current: int
    ) -> None:
        """Republish health after a lock state change (non-retained)."""
        await client.publish(
            f"{self.base}/health",
            health_payload(lock_current=lock_current),
            qos=0,
            retain=False,
        )

    async def publish_last_auth(
        self, client: aiomqtt.Client, result: str = "SUCCESS"
    ) -> None:
        await client.publish(
            f"{self.base}/last_auth",
            last_auth_payload(result=result),
            qos=0,
            retain=True,
        )


async def connect_node(
    household_id: str,
    node_id: str,
    node_name: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    node_role: str = "gate",
    lwt_prefix: str | None = None,
) -> tuple[aiomqtt.Client, EmulatedNode]:
    """Connect an emulated node with the documented shared LWT (one will only).

    The firmware configures its single will on the legacy availability topic
    ``<CLIENT_ID>/status``. ``lwt_prefix`` mirrors that (e.g. ``ESP_``).
    """
    will = None
    if lwt_prefix:
        will = aiomqtt.Will(
            topic=f"{lwt_prefix}{node_id}/status",
            payload="offline",
            qos=1,
            retain=True,
        )
    client = aiomqtt.Client(host, port, will=will)
    await client.__aenter__()
    node = EmulatedNode(
        household_id=household_id, node_id=node_id, node_name=node_name, node_role=node_role
    )
    # The firmware publishes "online" to the legacy LWT topic on connect.
    if lwt_prefix:
        await client.publish(
            f"{lwt_prefix}{node_id}/status", "online", qos=1, retain=True
        )
    return client, node


async def main() -> None:  # pragma: no cover - manual smoke helper
    """Manually publish one node's telemetry (smoke test for the emulator)."""
    client, node = await connect_node(
        "HOME-TEST", "GATE-TEST-001", "Gate Test", lwt_prefix="ESP_"
    )
    try:
        sent = await node.publish_all(client)
        for topic, _payload, qos, retain in sent:
            print(f"published {topic} (qos={qos}, retain={retain})")
        await asyncio.sleep(1)
    finally:
        await client.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
