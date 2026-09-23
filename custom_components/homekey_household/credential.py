"""Secure storage for household command credentials.

Boundary (documented, per the firmware contract)
------------------------------------------------

The firmware derives the household command key as::

    key = BLAKE2b(recovery_secret || salt, key="HK-HOUSEHOLD-CMD-v1", 32)

The **recovery secret never leaves the ESP32 and is never sent over MQTT**. It is
used *once*, locally, to derive the 32-byte command key. Home Assistant stores
that derived key via :class:`homeassistant.helpers.storage.Store` (a JSON file
under ``<config>/.storage/``) and discards the raw secret immediately.

Because the firmware's own scheme is a *keyed hash of the recovery secret*, HA
must be able to reproduce the command key. To do so the integration accepts the
recovery secret at configuration time and derives the command key in memory. The
secret itself is only retained for the lifetime of the config-flow step; only the
derived key (and the salt) is persisted.

What is guaranteed
------------------

* The secret/key is never written to MQTT, entity state, attributes, logs,
  diagnostics, or the config entry.
* The stored value is the *derived command key*, not the recovery secret and not
  the raw backup.
* Diagnostics redact any key material (see :mod:`diagnostics`).
* If no key material is available, commands **fail closed**: no unauthenticated
  command is ever published.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .command import derive_command_key
from .const import (
    COMMAND_KEY_STORAGE_KEY,
    COMMAND_KEY_STORAGE_VERSION,
)
from .models import ValidationError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandCredential:
    """A derived household command key plus its (non-secret) metadata."""

    key: bytes
    salt: str | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValidationError("command credential: empty key")

    @property
    def command_key_hex(self) -> str:
        """Hex form of the derived key.

        Only ever used for storage; never logged or surfaced to entities.
        """
        return self.key.hex()


def key_fingerprint(key: bytes) -> str:
    """A short, non-reversible fingerprint for diagnostics and key comparison.

    Uses the first 8 hex characters of a SHA-256 hash. It cannot be used to
    reconstruct the key, so it is safe to show in logs and diagnostics.
    """
    import hashlib

    return hashlib.sha256(key).hexdigest()[:8]


class CommandKeyStore:
    """Persists derived command keys, one per household, via HA ``Store``.

    ``Store`` writes to ``<config>/.storage/<key>``. Keys are stored in a single
    file keyed by ``household_id``.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, COMMAND_KEY_STORAGE_VERSION, COMMAND_KEY_STORAGE_KEY
        )
        self._cache: dict[str, CommandCredential] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load stored credentials into the in-memory cache."""
        if self._loaded:
            return
        raw = await self._store.async_load() or {}
        entries = raw.get("households", {})
        if not isinstance(entries, dict):
            _LOGGER.warning("Ignoring malformed command-key storage")
            entries = {}
        for household_id, payload in entries.items():
            if not isinstance(payload, dict):
                continue
            key_hex = payload.get("key")
            if not isinstance(key_hex, str) or not key_hex:
                continue
            try:
                key = bytes.fromhex(key_hex)
            except ValueError:
                _LOGGER.warning(
                    "Ignoring malformed stored command key for household %s",
                    household_id,
                )
                continue
            salt = payload.get("salt")
            self._cache[household_id] = CommandCredential(
                key=key,
                salt=salt if isinstance(salt, str) else None,
                fingerprint=key_fingerprint(key),
            )
        self._loaded = True

    def get(self, household_id: str) -> CommandCredential | None:
        """Return the cached credential for a household, if any."""
        return self._cache.get(household_id)

    async def async_set(
        self,
        household_id: str,
        key: bytes,
        salt: str | None = None,
    ) -> CommandCredential:
        """Store a derived command key for a household."""
        credential = CommandCredential(
            key=key, salt=salt, fingerprint=key_fingerprint(key)
        )
        self._cache[household_id] = credential
        await self._async_save()
        _LOGGER.debug(
            "Stored command key for household %s (fingerprint %s)",
            household_id,
            credential.fingerprint,
        )
        return credential

    async def async_set_from_secret(
        self,
        household_id: str,
        recovery_secret: str,
        salt: str | None = None,
    ) -> CommandCredential:
        """Derive and store a command key from the raw recovery secret.

        The raw secret is not persisted and is not referenced after derivation.
        """
        key = derive_command_key(recovery_secret, salt or "")
        try:
            return await self.async_set(household_id, key, salt)
        finally:
            del key

    async def async_remove(self, household_id: str) -> None:
        """Remove a household's stored credential."""
        if self._cache.pop(household_id, None) is not None:
            await self._async_save()

    async def _async_save(self) -> None:
        payload = {
            "households": {
                household_id: {
                    "key": credential.command_key_hex,
                    **(
                        {"salt": credential.salt}
                        if credential.salt is not None
                        else {}
                    ),
                    "fingerprint": credential.fingerprint,
                }
                for household_id, credential in self._cache.items()
            }
        }
        await self._store.async_save(payload)


__all__ = [
    "CommandCredential",
    "CommandKeyStore",
    "key_fingerprint",
]
