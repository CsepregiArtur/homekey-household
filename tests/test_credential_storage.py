"""Credential storage, redaction, and diagnostics tests.

Covers requirement 8 (secure credential storage) and 22 (logging/diagnostics
never expose secrets).
"""

from __future__ import annotations

import json

import pytest

from custom_components.homekey_household.command import derive_command_key
from custom_components.homekey_household.credential import (
    CommandKeyStore,
    key_fingerprint,
)
from custom_components.homekey_household.models import ValidationError
from helpers import TEST_HOUSEHOLD_ID, TEST_SALT, TEST_SECRET

HID = TEST_HOUSEHOLD_ID


class TestKeyFingerprint:
    """The fingerprint must be one-way and short."""

    def test_fingerprint_length(self):
        assert len(key_fingerprint(derive_command_key(TEST_SECRET, TEST_SALT))) == 8

    def test_fingerprint_is_not_the_key(self):
        key = derive_command_key(TEST_SECRET, TEST_SALT)
        assert key_fingerprint(key) != key.hex()
        assert key_fingerprint(key) not in key.hex()

    def test_fingerprint_differs_per_key(self):
        assert key_fingerprint(derive_command_key("a", TEST_SALT)) != key_fingerprint(
            derive_command_key("b", TEST_SALT)
        )


class TestCommandKeyStore:
    """Keys are stored via HA Store; the raw secret is never persisted."""

    async def test_set_and_get(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        key = derive_command_key(TEST_SECRET, TEST_SALT)
        await store.async_set(HID, key, TEST_SALT)
        credential = store.get(HID)
        assert credential is not None
        assert credential.key == key

    async def test_set_from_secret_derives_key(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        credential = await store.async_set_from_secret(HID, TEST_SECRET, TEST_SALT)
        assert credential.key == derive_command_key(TEST_SECRET, TEST_SALT)

    async def test_empty_secret_rejected(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        with pytest.raises(ValidationError):
            await store.async_set_from_secret(HID, "", TEST_SALT)

    async def test_get_unknown_household_is_none(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        assert store.get("NOPE") is None

    async def test_remove(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        await store.async_set_from_secret(HID, TEST_SECRET, TEST_SALT)
        await store.async_remove(HID)
        assert store.get(HID) is None

    async def test_salt_is_preserved(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        await store.async_set_from_secret(HID, TEST_SECRET, TEST_SALT)
        assert store.get(HID).salt == TEST_SALT

    async def test_reload_from_storage(self, hass):
        """A fresh store instance must recover the persisted key."""
        store = CommandKeyStore(hass)
        await store.async_load()
        key = derive_command_key(TEST_SECRET, TEST_SALT)
        await store.async_set(HID, key, TEST_SALT)

        reopened = CommandKeyStore(hass)
        await reopened.async_load()
        credential = reopened.get(HID)
        assert credential is not None
        assert credential.key == key

    async def test_malformed_storage_ignored(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        await store._store.async_save(  # noqa: SLF001 - exercising malformed input
            {"households": {"BAD": {"key": "not-hex"}, "ALSO_BAD": "not-a-dict"}}
        )
        reopened = CommandKeyStore(hass)
        await reopened.async_load()
        assert reopened.get("BAD") is None
        assert reopened.get("ALSO_BAD") is None

    async def test_stored_payload_contains_no_raw_secret(self, hass):
        """The raw recovery secret must never be written to storage."""
        store = CommandKeyStore(hass)
        await store.async_load()
        await store.async_set_from_secret(HID, TEST_SECRET, TEST_SALT)
        saved = await store._store.async_load()  # noqa: SLF001
        serialised = json.dumps(saved)
        assert TEST_SECRET not in serialised


class TestCredentialModel:
    """The credential object never leaks the key through str/repr."""

    async def test_empty_key_rejected(self, hass):
        from custom_components.homekey_household.credential import CommandCredential

        with pytest.raises(ValidationError):
            CommandCredential(key=b"")

    async def test_hex_property_matches_key(self, hass):
        store = CommandKeyStore(hass)
        await store.async_load()
        credential = await store.async_set_from_secret(HID, TEST_SECRET, TEST_SALT)
        assert credential.command_key_hex == credential.key.hex()


class TestDiagnosticsRedaction:
    """Requirement 22: diagnostics never contain secret material."""

    def test_redacts_command_key(self):
        from custom_components.homekey_household.diagnostics import _redact

        result = _redact({"command_key": "deadbeef", "key": "cafe"})
        assert result == {"command_key": "[redacted]", "key": "[redacted]"}

    def test_redacts_nested_secrets(self):
        from custom_components.homekey_household.diagnostics import _redact

        result = _redact(
            {"a": {"recovery_secret": "s", "mac": "m", "backup": "b", "data": "d"}}
        )
        assert result["a"] == {
            "recovery_secret": "[redacted]",
            "mac": "[redacted]",
            "backup": "[redacted]",
            "data": "[redacted]",
        }

    def test_keeps_safe_fields(self):
        from custom_components.homekey_household.diagnostics import _redact

        result = _redact({"node_id": "GATE-001", "mqtt": "OK", "uptime": 12})
        assert result == {"node_id": "GATE-001", "mqtt": "OK", "uptime": 12}

    def test_redacts_lists(self):
        from custom_components.homekey_household.diagnostics import _redact

        result = _redact({"items": [{"mac": "x"}, {"node_id": "y"}]})
        assert result["items"] == [{"mac": "[redacted]"}, {"node_id": "y"}]

    def test_redacted_key_set_includes_secret_names(self):
        from custom_components.homekey_household.diagnostics import redacted_keys

        keys = redacted_keys()
        for expected in (
            "recovery_secret",
            "command_key",
            "private_key",
            "provisioning_token",
            "mac",
        ):
            assert expected in keys
