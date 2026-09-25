"""Shared helpers for the HomeKey Household hardware-validation suite.

Design rules (from the validation plan):

* PASS / FAIL / BLOCKED / NOT TESTED are distinct outcomes. Missing hardware or
  missing configuration must surface as BLOCKED — never as a simulated PASS.
* Evidence is captured for every test and secrets are always redacted.
* The command MAC is recomputed here with an INDEPENDENT implementation
  (Python's stdlib ``hmac``/``hashlib``) so it does not share code with either
  the firmware (libsodium) or the integration under test.

Nothing in this module modifies the firmware, the MQTT contract, or HMAC
semantics. It only observes and verifies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_TESTED = "NOT TESTED"


class HardwareBlocked(RuntimeError):
    """Raised when a test cannot run because hardware/config is unavailable.

    The runner converts this into a BLOCKED result. It must never be treated as
    a pass: a blocked test proves nothing about the device.
    """


class HardwareMismatch(AssertionError):
    """Raised when the device does not behave as the contract requires."""


# --------------------------------------------------------------------------
# Independent command-MAC implementation
# --------------------------------------------------------------------------

# Must match the firmware's derivation, but implemented independently.
_KDF_PERSONALISATION = b"HK-HOUSEHOLD-CMD-v1"
_MAC_MSG_FIELDS = ("ts", "nonce", "req_id", "action")


def derive_command_key(recovery_secret: str, salt: str | None = None) -> bytes:
    """Recompute the household command key exactly as the firmware does.

    ``key = BLAKE2b(recovery_secret || salt, key="HK-HOUSEHOLD-CMD-v1", 32)``

    Implemented with hashlib so it shares no code with the device.
    """
    material = recovery_secret.encode()
    if salt:
        material += salt.encode()
    return hashlib.blake2b(material, digest_size=32, key=_KDF_PERSONALISATION).digest()


def command_mac(key: bytes, ts: int, nonce: str, req_id: str, action: str) -> str:
    """Compute the lowercase-hex command MAC independently of the firmware.

    ``mac = HMAC-SHA256(key, f"{ts}{nonce}{req_id}{action}")`` — note the action
    is part of the signed string but is NOT a field in the JSON payload.
    """
    message = f"{ts}{nonce}{req_id}{action}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def build_command_payload(
    key: bytes,
    *,
    action: str,
    ts: int | None = None,
    nonce: str | None = None,
    req_id: str | None = None,
    mac: str | None = None,
) -> dict[str, Any]:
    """Build a command payload; override any field to craft a negative test.

    The payload shape is fixed by the contract: exactly ``ts, nonce, req_id,
    mac`` with no ``action`` field.
    """
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or os.urandom(8).hex()
    req_id = req_id or f"hw-{int(time.time() * 1000) % 100000}"
    mac = mac if mac is not None else command_mac(key, ts, nonce, req_id, action)
    return {"ts": ts, "nonce": nonce, "req_id": req_id, "mac": mac}


# --------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------

_SECRET_KEYS = (
    "mac",
    "key",
    "command_key",
    "recovery_secret",
    "secret",
    "salt",
    "password",
    "passwd",
    "token",
    "prov",
    "private_key",
    "client_key",
    "psk",
)

# A 64-char lowercase hex string is almost certainly a command MAC or key.
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def redact(value: Any) -> Any:
    """Recursively replace secret-looking values with a placeholder.

    Two layers: by key name, and by shape (64-hex). The shape rule matters
    because a MAC can leak inside a log line or an error string, not just as a
    named field.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in _SECRET_KEYS):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _HEX64.sub("[REDACTED]", value)
    return value


def redact_text(text: str) -> str:
    """Redact secret-shaped tokens from a free-text log blob."""
    return _HEX64.sub("[REDACTED]", text)


def find_secret_leaks(text: str) -> list[str]:
    """Return every 64-hex token found in ``text`` (candidates for leaks).

    Used by the security tests: an empty list is the expected product result.
    """
    return _HEX64.findall(text)


# --------------------------------------------------------------------------
# Evidence collection
# --------------------------------------------------------------------------

EVIDENCE_DIR = Path(__file__).parent / "evidence"


@dataclass
class Evidence:
    """Accumulates redacted evidence for one hardware test."""

    test_name: str
    firmware_version: str | None = None
    esp32_model: str | None = None
    node_id: str | None = None
    household_id: str | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, kind: str, payload: Any) -> None:
        """Record one evidence item, redacted before it is stored."""
        self.entries.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": kind,
                "payload": redact(payload),
            }
        )

    def mqtt(self, topic: str, payload: Any) -> None:
        self.record("mqtt", {"topic": topic, "payload": payload})

    def serial(self, text: str) -> None:
        self.record("serial", redact_text(text))

    def ha_state(self, entity_id: str, state: Any) -> None:
        self.record("ha_state", {"entity_id": entity_id, "state": state})

    def physical(self, description: str) -> None:
        self.record("physical", description)

    def write(self, result: str) -> Path:
        """Persist the evidence bundle and return its path."""
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.test_name)
        path = EVIDENCE_DIR / f"{stamp}_{safe}.json"
        path.write_text(
            json.dumps(
                {
                    "test": self.test_name,
                    "result": result,
                    "firmware_version": self.firmware_version,
                    "esp32_model": self.esp32_model,
                    "node_id": self.node_id,
                    "household_id": self.household_id,
                    "entries": self.entries,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path


def blocked(reason: str) -> HardwareBlocked:
    """Create a BLOCKED outcome with an actionable reason."""
    return HardwareBlocked(reason)
