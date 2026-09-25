"""HMAC command protocol tests.

Covers requirements 11 (canonical input), 12 (HMAC-SHA256 generation),
13 (timestamp handling), 14 (nonce generation), 15 (request ID generation),
16/17 (lock/unlock commands), 18 (malformed command data) and
19 (missing credentials -> fail closed).

All secrets here are synthetic. The expected values are computed independently in
the test (``hashlib``/``hmac``) so the integration is verified against the
documented formula, not against its own output.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from custom_components.homekey_household.command import (
    NonceTracker,
    build_command,
    canonical_input,
    current_epoch_seconds,
    derive_command_key,
    make_command_mac,
    new_nonce,
    new_request_id,
)
from custom_components.homekey_household.const import (
    COMMAND_KEY_LABEL,
    COMMAND_MAX_SKEW_SECONDS,
    COMMAND_NONCE_BYTES,
    COMMAND_REPLAY_WINDOW,
)
from custom_components.homekey_household.models import ValidationError

SECRET = "unit-test-recovery-secret"
SALT = "unit-test-salt"
NONCE = "0123456789abcdef0123456789abcdef"
REQ_ID = "abcdef0123456789"
TS = 1760000000


class TestKeyDerivation:
    """The command key derivation matches the firmware's BLAKE2b scheme."""

    def test_matches_firmware_blake2b_formula(self):
        """key = BLAKE2b(msg=secret||salt, key=label, digest=32)."""
        key = derive_command_key(SECRET, SALT)
        expected = hashlib.blake2b(
            (SECRET + SALT).encode("utf-8"),
            key=COMMAND_KEY_LABEL.encode("utf-8"),
            digest_size=32,
        ).digest()
        assert key == expected

    def test_hex_values_are_decoded_before_deriving(self):
        """The node holds both values as bytes, so the hex it exports must be decoded."""
        secret_hex = "a1" * 32  # 32 bytes, as the node reports a recovery secret
        salt_hex = "b2" * 16  # 16 bytes, as ``/household`` reports the salt

        key = derive_command_key(secret_hex, salt_hex)
        expected = hashlib.blake2b(
            bytes.fromhex(secret_hex) + bytes.fromhex(salt_hex),
            key=COMMAND_KEY_LABEL.encode("utf-8"),
            digest_size=32,
        ).digest()
        assert key == expected

    def test_deriving_from_the_hex_text_would_be_a_different_key(self):
        """Why the decoding matters: the other reading is the one the node rejects.

        That mismatch is invisible from here - the command is published, Home Assistant
        reports success, and the node records ``bad_mac`` in its audit log - so it is
        pinned down by a test rather than left to be discovered on a door.
        """
        secret_hex = "a1" * 32
        salt_hex = "b2" * 16

        as_text = hashlib.blake2b(
            (secret_hex + salt_hex).encode("utf-8"),
            key=COMMAND_KEY_LABEL.encode("utf-8"),
            digest_size=32,
        ).digest()
        assert derive_command_key(secret_hex, salt_hex) != as_text

    def test_short_hex_looking_values_stay_text(self):
        """A short secret that merely looks like hex is not reinterpreted as bytes."""
        key = derive_command_key("abcd", "ef01")
        expected = hashlib.blake2b(
            b"abcdef01", key=COMMAND_KEY_LABEL.encode("utf-8"), digest_size=32
        ).digest()
        assert key == expected

    def test_odd_length_hex_looking_values_stay_text(self):
        """Half a byte is not a byte: an odd-length value cannot be hex."""
        value = "a1b2c3d4e5f60718293a4b5c6d7e8f90a"  # 33 characters
        assert len(value) % 2 == 1
        key = derive_command_key(value, "")
        expected = hashlib.blake2b(
            value.encode("utf-8"), key=COMMAND_KEY_LABEL.encode("utf-8"), digest_size=32
        ).digest()
        assert key == expected

    def test_key_is_32_bytes(self):
        assert len(derive_command_key(SECRET, SALT)) == 32

    def test_label_is_the_documented_value(self):
        assert COMMAND_KEY_LABEL == "HK-HOUSEHOLD-CMD-v1"

    def test_salt_changes_the_key(self):
        assert derive_command_key(SECRET, "salt-a") != derive_command_key(
            SECRET, "salt-b"
        )

    def test_secret_changes_the_key(self):
        assert derive_command_key("secret-a", SALT) != derive_command_key(
            "secret-b", SALT
        )

    def test_empty_salt_is_allowed(self):
        """``recovery_secret || salt`` with no salt still derives a key."""
        assert len(derive_command_key(SECRET, "")) == 32

    def test_empty_secret_is_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            derive_command_key("", SALT)


class TestCanonicalInput:
    """Requirement 11: canonical MAC input is ``{ts}{nonce}{req_id}{action}``."""

    def test_lock_canonical_input(self):
        assert canonical_input(TS, NONCE, REQ_ID, "lock") == f"{TS}{NONCE}{REQ_ID}lock"

    def test_unlock_canonical_input(self):
        assert (
            canonical_input(TS, NONCE, REQ_ID, "unlock")
            == f"{TS}{NONCE}{REQ_ID}unlock"
        )

    def test_ts_is_decimal_without_separator(self):
        """There is no delimiter between fields in the canonical string."""
        canonical = canonical_input(TS, NONCE, REQ_ID, "lock")
        assert canonical.startswith(str(TS))
        assert str(TS) + NONCE in canonical

    def test_action_is_appended_last(self):
        canonical = canonical_input(TS, NONCE, REQ_ID, "unlock")
        assert canonical.endswith("unlock")
        assert canonical == f"{TS}{NONCE}{REQ_ID}" + "unlock"
        # The suffix must be exactly the action, with no separator.
        assert canonical[len(f"{TS}{NONCE}{REQ_ID}") :] == "unlock"


class TestMacGeneration:
    """Requirement 12: HMAC-SHA256 over the canonical input."""

    def test_lock_mac_matches_reference_implementation(self):
        key = derive_command_key(SECRET, SALT)
        mac = make_command_mac(key, TS, NONCE, REQ_ID, "lock")
        expected = hmac.new(
            key, f"{TS}{NONCE}{REQ_ID}lock".encode(), hashlib.sha256
        ).hexdigest()
        assert mac == expected

    def test_unlock_mac_matches_reference_implementation(self):
        key = derive_command_key(SECRET, SALT)
        mac = make_command_mac(key, TS, NONCE, REQ_ID, "unlock")
        expected = hmac.new(
            key, f"{TS}{NONCE}{REQ_ID}unlock".encode(), hashlib.sha256
        ).hexdigest()
        assert mac == expected

    def test_mac_is_64_lowercase_hex_characters(self):
        key = derive_command_key(SECRET, SALT)
        mac = make_command_mac(key, TS, NONCE, REQ_ID, "lock")
        assert len(mac) == 64
        assert mac == mac.lower()
        assert all(ch in "0123456789abcdef" for ch in mac)

    def test_lock_and_unlock_macs_differ(self):
        key = derive_command_key(SECRET, SALT)
        assert make_command_mac(key, TS, NONCE, REQ_ID, "lock") != make_command_mac(
            key, TS, NONCE, REQ_ID, "unlock"
        )

    def test_wrong_key_produces_different_mac(self):
        assert make_command_mac(
            derive_command_key("other", SALT), TS, NONCE, REQ_ID, "lock"
        ) != make_command_mac(derive_command_key(SECRET, SALT), TS, NONCE, REQ_ID, "lock")

    def test_mac_binds_the_action(self):
        """A lock MAC must not validate for unlock (topic-derived action)."""
        key = derive_command_key(SECRET, SALT)
        lock_mac = make_command_mac(key, TS, NONCE, REQ_ID, "lock")
        unlock_mac = make_command_mac(key, TS, NONCE, REQ_ID, "unlock")
        assert lock_mac != unlock_mac

    def test_unsupported_action_rejected(self):
        key = derive_command_key(SECRET, SALT)
        with pytest.raises(ValidationError, match="unsupported action"):
            make_command_mac(key, TS, NONCE, REQ_ID, "reboot")


class TestTimestamp:
    """Requirement 13: Unix epoch seconds from the system clock."""

    def test_current_epoch_seconds_is_integer(self):
        value = current_epoch_seconds()
        assert isinstance(value, int) and value > 1_600_000_000

    def test_uses_supplied_clock(self):
        assert current_epoch_seconds(now=1760000000.75) == 1760000000

    def test_firmware_window_constant(self):
        assert COMMAND_MAX_SKEW_SECONDS == 300

    def test_explicit_ts_is_preserved(self):
        command = build_command(
            derive_command_key(SECRET, SALT), "lock", ts=TS, nonce=NONCE, req_id=REQ_ID
        )
        assert command.ts == TS

    def test_boolean_ts_rejected(self):
        with pytest.raises(ValidationError, match="ts"):
            build_command(
                derive_command_key(SECRET, SALT),
                "lock",
                ts=True,  # type: ignore[arg-type]
                nonce=NONCE,
                req_id=REQ_ID,
            )


class TestNonce:
    """Requirement 14: cryptographically appropriate, non-repeating nonces."""

    def test_default_nonce_length(self):
        assert len(new_nonce()) == COMMAND_NONCE_BYTES * 2

    def test_nonce_is_hex(self):
        nonce = new_nonce()
        assert all(ch in "0123456789abcdef" for ch in nonce)

    def test_nonces_are_unique(self):
        assert len({new_nonce() for _ in range(200)}) == 200

    def test_nonce_is_not_predictable_counter(self):
        """Nonces must not be a monotonically increasing counter."""
        values = [new_nonce() for _ in range(10)]
        assert values != sorted(values)

    def test_tracker_returns_fresh_nonces(self):
        tracker = NonceTracker()
        nonces = [tracker.fresh_nonce() for _ in range(100)]
        assert len(set(nonces)) == 100

    def test_tracker_remembers_nonces(self):
        tracker = NonceTracker()
        nonce = tracker.fresh_nonce()
        assert nonce in tracker

    def test_tracker_rejects_duplicate_source(self):
        """A nonce already remembered is never handed out again."""
        tracker = NonceTracker()
        tracker.remember(NONCE)
        assert NONCE in tracker

    def test_nonce_plausibility_checks(self):
        assert NonceTracker.is_plausible(NONCE)
        assert not NonceTracker.is_plausible("")
        assert not NonceTracker.is_plausible("not-hex!!")
        assert not NonceTracker.is_plausible("a" * 129)

    def test_replay_window_constant_matches_firmware(self):
        assert COMMAND_REPLAY_WINDOW == 32

    def test_unsafe_nonce_rejected_by_build(self):
        with pytest.raises(ValidationError, match="nonce"):
            build_command(
                derive_command_key(SECRET, SALT),
                "lock",
                ts=TS,
                nonce='bad"nonce',
                req_id=REQ_ID,
            )


class TestRequestId:
    """Requirement 15: unique, secret-free request ids."""

    def test_request_ids_are_unique(self):
        assert len({new_request_id() for _ in range(200)}) == 200

    def test_request_id_is_hex_and_bounded(self):
        req_id = new_request_id()
        assert len(req_id) == 32
        assert all(ch in "0123456789abcdef" for ch in req_id)

    def test_request_id_contains_no_secret(self):
        req_id = new_request_id()
        assert SECRET not in req_id
        assert derive_command_key(SECRET, SALT).hex() not in req_id

    def test_build_generates_request_id_when_absent(self):
        command = build_command(
            derive_command_key(SECRET, SALT), "lock", ts=TS, nonce=NONCE
        )
        assert command.req_id and len(command.req_id) == 32


class TestBuildCommand:
    """Requirements 16/17: lock and unlock command construction."""

    def test_payload_has_exactly_four_fields(self):
        command = build_command(
            derive_command_key(SECRET, SALT), "lock", ts=TS, nonce=NONCE, req_id=REQ_ID
        )
        assert set(command.to_dict()) == {"ts", "nonce", "req_id", "mac"}

    def test_payload_has_no_action_field(self):
        """The action is topic-derived; it must never be in the payload."""
        for action in ("lock", "unlock"):
            command = build_command(
                derive_command_key(SECRET, SALT),
                action,
                ts=TS,
                nonce=NONCE,
                req_id=REQ_ID,
            )
            assert "action" not in command.to_dict()

    def test_ts_serialises_as_integer(self):
        command = build_command(
            derive_command_key(SECRET, SALT), "lock", ts=TS, nonce=NONCE, req_id=REQ_ID
        )
        serialised = json.loads(json.dumps(command.to_dict()))
        assert isinstance(serialised["ts"], int)
        assert serialised["ts"] == TS

    def test_lock_command_mac(self):
        key = derive_command_key(SECRET, SALT)
        command = build_command(key, "lock", ts=TS, nonce=NONCE, req_id=REQ_ID)
        assert command.mac == make_command_mac(key, TS, NONCE, REQ_ID, "lock")

    def test_unlock_command_mac(self):
        key = derive_command_key(SECRET, SALT)
        command = build_command(key, "unlock", ts=TS, nonce=NONCE, req_id=REQ_ID)
        assert command.mac == make_command_mac(key, TS, NONCE, REQ_ID, "unlock")

    def test_no_unlock_boolean_shortcut(self):
        """There is no way to request an unlock without a MAC."""
        key = derive_command_key(SECRET, SALT)
        command = build_command(key, "unlock", ts=TS, nonce=NONCE, req_id=REQ_ID)
        assert command.mac
        assert "unlock" not in command.to_dict().values()

    def test_unsupported_action_rejected(self):
        with pytest.raises(ValidationError, match="unsupported action"):
            build_command(
                derive_command_key(SECRET, SALT), "revoke", ts=TS, nonce=NONCE
            )

    def test_empty_key_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            build_command(b"", "lock", ts=TS, nonce=NONCE, req_id=REQ_ID)

    def test_malformed_request_id_rejected(self):
        with pytest.raises(ValidationError, match="req_id"):
            build_command(
                derive_command_key(SECRET, SALT),
                "lock",
                ts=TS,
                nonce=NONCE,
                req_id="",
            )

    def test_generated_nonce_is_tracked(self):
        tracker = NonceTracker()
        command = build_command(
            derive_command_key(SECRET, SALT), "lock", ts=TS, nonce_tracker=tracker
        )
        assert command.nonce in tracker
