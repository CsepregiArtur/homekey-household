"""Independent firmware-compatible command verifier (validation tooling only).

This module re-implements the **firmware 0.10.0** command verification rules so a
captured HA-issued command can be checked *as the firmware would check it*.

It is deliberately independent from the Home Assistant integration: it does not
import ``custom_components.homekey_household``, and it does not import any
C++/firmware code. It is a from-scratch reimplementation of the documented
contract, used as a cross-check.

Rules mirrored from the firmware (read-only reference):

* ``main/MqttManager.cpp::handleSecureCommand``
  - action derived from the topic, never from the payload
  - required fields exactly ``ts`` (number), ``nonce``/``req_id``/``mac`` (strings)
  - ``mac = HMAC-SHA256(key, "{ts}{nonce}{req_id}{action}")``, compared in
    constant time
  - freshness: ``|ts - now| <= 300`` when a wall clock is available
  - replay: bounded 32-entry nonce window
* ``main/HouseholdManager.cpp::deriveCommandKey``
  - ``key = BLAKE2b(recovery_secret || salt, key="HK-HOUSEHOLD-CMD-v1", 32)``
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

COMMAND_KEY_LABEL = "HK-HOUSEHOLD-CMD-v1"
MAX_SKEW_SECONDS = 300
REPLAY_WINDOW = 32


class FirmwareReject(Exception):
    """Raised when the firmware would reject a command."""


def derive_command_key(recovery_secret: str, salt: str = "") -> bytes:
    """Firmware-equivalent key derivation (BLAKE2b keyed by the label)."""
    material = (recovery_secret + salt).encode("utf-8")
    if not material:
        raise FirmwareReject("empty key material")
    return hashlib.blake2b(
        material, key=COMMAND_KEY_LABEL.encode("utf-8"), digest_size=32
    ).digest()


def action_for_topic(base: str, topic: str) -> str:
    """Derive the action from the topic exactly as the firmware does."""
    if topic == f"{base}/command/unlock":
        return "unlock"
    if topic == f"{base}/command/lock":
        return "lock"
    raise FirmwareReject(f"unknown command topic: {topic}")


def expected_mac(key: bytes, ts: int, nonce: str, req_id: str, action: str) -> str:
    canonical = f"{ts}{nonce}{req_id}{action}"
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class FirmwareVerifier:
    """Stateful verifier with its own replay window (like the firmware)."""

    key: bytes
    now: int | None = None
    seen_nonces: list[str] = field(default_factory=list)

    def verify(self, base: str, topic: str, payload: str) -> str:
        """Return the accepted action, or raise :class:`FirmwareReject`."""
        action = action_for_topic(base, topic)

        try:
            data: Any = json.loads(payload)
        except ValueError as exc:
            raise FirmwareReject("malformed JSON") from exc
        if not isinstance(data, dict):
            raise FirmwareReject("payload is not an object")

        ts = data.get("ts")
        nonce = data.get("nonce")
        req_id = data.get("req_id")
        mac = data.get("mac")

        # Field types must match the firmware's cJSON checks exactly.
        if isinstance(ts, bool) or not isinstance(ts, int):
            raise FirmwareReject("ts must be a number")
        if not isinstance(nonce, str) or not isinstance(req_id, str):
            raise FirmwareReject("nonce/req_id must be strings")
        if not isinstance(mac, str):
            raise FirmwareReject("mac must be a string")

        expected = expected_mac(self.key, ts, nonce, req_id, action)
        if not hmac.compare_digest(expected, mac):
            raise FirmwareReject("MAC mismatch")

        if self.now is not None:
            skew = ts - self.now
            if skew < -MAX_SKEW_SECONDS or skew > MAX_SKEW_SECONDS:
                raise FirmwareReject("outside time window")

        if nonce in self.seen_nonces:
            raise FirmwareReject("replayed nonce")
        self.seen_nonces.append(nonce)
        if len(self.seen_nonces) > REPLAY_WINDOW:
            del self.seen_nonces[0]

        return action
