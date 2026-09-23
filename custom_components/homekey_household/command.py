"""HMAC-SHA256 command authentication for the household lock/unlock API.

This module implements the *client* half of the firmware 0.10.0 authenticated
command protocol exactly as implemented in ``main/MqttManager.cpp`` and
``main/HouseholdManager.cpp``:

Command key derivation (``HouseholdManager::deriveCommandKey``)::

    key = BLAKE2b(message = recovery_secret || salt,
                  key     = "HK-HOUSEHOLD-CMD-v1",
                  digest  = 32 bytes)

Canonical input (``MqttManager::makeCommandMac``)::

    canonical = f"{ts}{nonce}{req_id}{action}"
    mac       = HMAC-SHA256(key, canonical)   # lowercase hex

The ``action`` is derived from the MQTT topic; the payload contains no ``action``
field. Note that the firmware hashes ``crypto_auth_hmacsha256`` over the UTF-8
bytes of the canonical string, so ``ts`` is rendered in its decimal form.

Security boundaries:

* The recovery secret is never transmitted, logged, or stored in entity state.
* The command key is derived in memory and stored (if at all) via Home
  Assistant's encrypted-ish ``Store``; see :mod:`credential`.
* Nonces are generated from a CSPRNG and never reused within the firmware replay
  window.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import uuid

from .const import (
    ACTION_LOCK,
    ACTION_UNLOCK,
    COMMAND_KEY_LABEL,
    COMMAND_KEY_LENGTH,
    COMMAND_NONCE_BYTES,
    COMMAND_REPLAY_WINDOW,
)
from .models import AuthenticatedCommand, ValidationError

_LOGGER = logging.getLogger(__name__)

# BLAKE2b rejects digests larger than 64 bytes.
_MAX_BLAKE2B_DIGEST = 64


def _as_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def derive_command_key(
    recovery_secret: str | bytes,
    salt: str | bytes = b"",
) -> bytes:
    """Derive the 32-byte household command key.

    Mirrors ``HouseholdManager::deriveCommandKey``: ``crypto_generichash`` is
    libsodium's BLAKE2b, where the ``key`` parameter is the label and the message
    is ``recovery_secret || salt``.

    Raises :class:`ValidationError` if the secret is empty (the firmware also
    rejects an empty derivation, returning no key).
    """
    secret_bytes = _as_bytes(recovery_secret)
    if not secret_bytes:
        raise ValidationError("recovery_secret: must not be empty")
    label_bytes = COMMAND_KEY_LABEL.encode("utf-8")
    if len(label_bytes) > _MAX_BLAKE2B_DIGEST:
        raise ValidationError("command key label too long for BLAKE2b")

    material = secret_bytes + _as_bytes(salt)
    try:
        return hashlib.blake2b(
            material,
            key=label_bytes,
            digest_size=COMMAND_KEY_LENGTH,
        ).digest()
    finally:
        # Best-effort scrub of the intermediate buffer holding the secret.
        del material


def canonical_input(ts: int, nonce: str, req_id: str, action: str) -> str:
    """Build the canonical MAC input string ``f"{ts}{nonce}{req_id}{action}"``."""
    return f"{ts}{nonce}{req_id}{action}"


def make_command_mac(
    key: bytes, ts: int, nonce: str, req_id: str, action: str
) -> str:
    """Return the lowercase-hex HMAC-SHA256 for a command."""
    if action not in (ACTION_LOCK, ACTION_UNLOCK):
        raise ValidationError(f"unsupported action: {action!r}")
    canonical = canonical_input(ts, nonce, req_id, action)
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def new_nonce(num_bytes: int = COMMAND_NONCE_BYTES) -> str:
    """Generate a cryptographically random lowercase-hex nonce.

    Uses ``secrets.token_hex`` (CSPRNG). Never a counter: predictable nonces
    would weaken the firmware's replay window.
    """
    if num_bytes <= 0:
        raise ValidationError("nonce size must be positive")
    return secrets.token_hex(num_bytes)


def new_request_id() -> str:
    """Generate a unique, secret-free request id for local correlation."""
    return uuid.uuid4().hex


def current_epoch_seconds(now: float | None = None) -> int:
    """Current Unix epoch seconds (the HA system clock; no arbitrary offset)."""
    return int(time.time() if now is None else now)


class NonceTracker:
    """Tracks recently used nonces so the client never reuses one.

    The firmware keeps a bounded 32-entry replay window; reusing any nonce still
    inside that window would be rejected. This tracker retains two windows' worth
    to be safe.
    """

    def __init__(self, window: int = COMMAND_REPLAY_WINDOW) -> None:
        self._window = max(1, window)
        self._seen: list[str] = []

    def __contains__(self, nonce: str) -> bool:
        return nonce in self._seen

    def remember(self, nonce: str) -> None:
        self._seen.append(nonce)
        if len(self._seen) > self._window * 2:
            del self._seen[: self._window]

    def fresh_nonce(self, num_bytes: int = COMMAND_NONCE_BYTES) -> str:
        """Generate a nonce that has not been used by this process."""
        for _ in range(8):
            nonce = new_nonce(num_bytes)
            if nonce not in self._seen:
                self.remember(nonce)
                return nonce
        # Astronomically unlikely; fail closed rather than risk a replay.
        raise ValidationError("could not generate a fresh nonce")

    @staticmethod
    def is_plausible(nonce: str, *, expected_bytes: int = COMMAND_NONCE_BYTES) -> bool:
        """Validate the nonce shape expected by the firmware (hex, non-empty).

        The firmware only requires a non-empty string; we additionally require
        hex so we never emit a nonce containing quotes/control characters.
        """
        if not nonce:
            return False
        if len(nonce) > 128:
            return False
        return all(ch in "0123456789abcdefABCDEF" for ch in nonce)


def build_command(
    key: bytes,
    action: str,
    *,
    ts: int | None = None,
    nonce: str | None = None,
    req_id: str | None = None,
    nonce_tracker: NonceTracker | None = None,
) -> AuthenticatedCommand:
    """Build a complete, MAC-signed command payload.

    ``ts`` defaults to the current system time, and ``nonce``/``req_id`` are
    generated when not supplied. All values are validated before signing.
    """
    if action not in (ACTION_LOCK, ACTION_UNLOCK):
        raise ValidationError(f"unsupported action: {action!r}")
    if not key:
        raise ValidationError("command key: must not be empty")

    if ts is None:
        ts = current_epoch_seconds()
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValidationError("ts: expected integer epoch seconds")

    if nonce is None:
        nonce = (
            nonce_tracker.fresh_nonce() if nonce_tracker else new_nonce()
        )
    if not NonceTracker.is_plausible(nonce):
        raise ValidationError("nonce: must be a bounded hex string")

    if req_id is None:
        req_id = new_request_id()
    if not isinstance(req_id, str) or not req_id or len(req_id) > 128:
        raise ValidationError("req_id: must be a non-empty bounded string")

    mac = make_command_mac(key, ts, nonce, req_id, action)
    return AuthenticatedCommand(ts=ts, nonce=nonce, req_id=req_id, mac=mac)


__all__ = [
    "NonceTracker",
    "build_command",
    "canonical_input",
    "current_epoch_seconds",
    "derive_command_key",
    "make_command_mac",
    "new_nonce",
    "new_request_id",
]
