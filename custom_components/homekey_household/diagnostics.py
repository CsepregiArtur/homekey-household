"""Diagnostics for HomeKey Household.

The output is safe to attach to bug reports: all secret material is redacted.

Redacted by design (never present in the output):

* the household recovery secret,
* the derived HMAC command key and any hex form of it,
* command MACs,
* backup plaintext or encrypted blobs,
* private keys and HomeKey credential material.

The key *fingerprint* (first 8 hex chars of SHA-256 over the key) is safe: it is
one-way and cannot be used to sign commands.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_COMMAND_CONTROL,
    CONF_LEGACY_CLIENT_ID_PREFIX,
    DOMAIN,
    TOPIC_SUBSCRIBE_ALL,
)

# Keys always redacted from diagnostics output.
_REDACT_KEYS = frozenset(
    {
        "password",
        "passwd",
        "mqtt_password",
        "web_password",
        "access_point_password",
        "private_key",
        "client_key",
        "recovery_key",
        "recovery_secret",
        "recovery_metadata",
        "command_key",
        "key",
        "mac",
        "token",
        "provisioning_token",
        "session_token",
        "backup",
        "data",
    }
)

REDACTED = "[redacted]"


def _redact(value: Any, key: str = "") -> Any:
    """Recursively redact secret values from an arbitrary structure."""
    if key.lower() in _REDACT_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_redact(v, key) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return {"configured": False, "error": "runtime not loaded"}

    coordinator = runtime.coordinator
    credential = runtime.credential_store.get(coordinator.household_id)

    nodes: list[dict[str, Any]] = []
    for node in coordinator.nodes.values():
        health = node.health
        nodes.append(
            {
                "node_id": node.node_id,
                "node_name": node.node_name,
                "node_role": node.node_role,
                "node_state": node.node_state,
                "generation": node.generation,
                "firmware_version": node.firmware,
                "online": node.online,
                "lwt_online": node.lwt_online,
                "available": node.available,
                "last_seen": node.last_seen,
                "security": node.security,
                "backup_status": node.backup_status,
                "backup": (
                    {
                        "status": node.backup.status,
                        "timestamp": node.backup.timestamp,
                    }
                    if node.backup
                    else None
                ),
                "last_auth": (
                    {
                        "type": node.last_auth.auth_type,
                        "result": node.last_auth.result,
                        "timestamp": node.last_auth.timestamp,
                    }
                    if node.last_auth
                    else None
                ),
                "health": (
                    {
                        "network": health.network,
                        "mqtt": health.mqtt,
                        "mqtt_error": health.mqtt_error,
                        "nfc": health.nfc,
                        "lock_current": health.lock_current,
                        "lock_target": health.lock_target,
                        "backup": health.backup,
                        "certificate": health.certificate,
                        "firmware_version": health.firmware_version,
                        "uptime": health.uptime,
                        "free_heap": health.free_heap,
                        "reset_reason": health.reset_reason,
                        "security_all_ok": health.security_all_ok,
                        "security_warnings": health.security_warnings,
                    }
                    if health
                    else None
                ),
            }
        )

    return _redact(  # type: ignore[no-any-return]
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
                "options": {
                    # Only non-secret options are reported.
                    CONF_COMMAND_CONTROL: entry.options.get(CONF_COMMAND_CONTROL),
                },
            },
            "household_id": coordinator.household_id,
            "command_control_enabled": coordinator.command_control_enabled,
            "command_key_present": credential is not None,
            # One-way fingerprint only; not the key itself.
            "command_key_fingerprint": credential.fingerprint if credential else None,
            "node_count": len(nodes),
            "nodes": nodes,
            "subscriptions": {
                "household": TOPIC_SUBSCRIBE_ALL,
                "legacy_availability_prefix": entry.options.get(
                    CONF_LEGACY_CLIENT_ID_PREFIX
                ),
            },
            "reserved_topics_consumed": [],
        }
    )


def redacted_keys() -> frozenset[str]:
    """Return the set of keys that diagnostics redacts."""
    return _REDACT_KEYS
