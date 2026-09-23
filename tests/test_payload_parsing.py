"""Payload parsing tests using the exact documented firmware 0.10.0 payloads.

Covers requirements 6 (state), 7 (health), 8 (backup), 9 (security) and
10 (last-auth) parsing, plus malformed-payload resilience (requirement 18) and
the documented stub fields (network / certificate).
"""

from __future__ import annotations

import json

import pytest

from custom_components.homekey_household.const import (
    HEALTH_CERTIFICATE_UNKNOWN,
    HEALTH_NETWORK_UNKNOWN,
    LockState,
    SecurityState,
)
from custom_components.homekey_household.models import (
    BackupRecord,
    LastAuth,
    NodeHealth,
    NodeState,
    ValidationError,
)

HID = "HOUSE-7F42"
NID = "GATE-001"

# The exact documented example payloads from mqtt_household_api.md.
STATE_JSON = (
    '{"household_id":"HOUSE-7F42","node_id":"GATE-001","node_name":"Gate",'
    '"node_role":"gate","node_state":"ACTIVE","generation":1,'
    '"firmware_version":"0.10.0"}'
)

HEALTH_JSON = (
    '{"network":"UNKNOWN","mqtt":"OK","mqtt_error":0,"nfc":"OK",'
    '"lock_current":1,"lock_target":1,"backup":"ok","certificate":"unknown",'
    '"firmware_version":"0.10.0","uptime":1234,"free_heap":123456,'
    '"reset_reason":"1","security":{"all_ok":false,"warnings":"..."}}'
)

HEALTH_JSON_NO_SECURITY = (
    '{"network":"UNKNOWN","mqtt":"ERROR","mqtt_error":5,"nfc":"ERROR",'
    '"lock_current":2,"lock_target":1,"backup":"failed","certificate":"unknown",'
    '"firmware_version":"0.10.0","uptime":42,"free_heap":1000,"reset_reason":"3"}'
)

BACKUP_LAST_JSON = '{"status":"completed","timestamp":1760000000}'
LAST_AUTH_JSON = '{"type":"HomeKey","result":"SUCCESS","timestamp":1760000000}'


class TestStateParsing:
    """Requirement 6: state payload parsing."""

    def test_documented_state_payload(self):
        state = NodeState.from_dict(json.loads(STATE_JSON), HID, NID)
        assert state.household_id == HID
        assert state.node_id == NID
        assert state.node_name == "Gate"
        assert state.node_role == "gate"
        assert state.node_state == "ACTIVE"
        assert state.generation == 1
        assert state.firmware_version == "0.10.0"

    def test_state_requires_no_schema_field(self):
        """The firmware does not send ``schema``; parsing must not require it."""
        payload = {
            "household_id": HID,
            "node_id": NID,
            "node_name": "Gate",
        }
        state = NodeState.from_dict(payload, HID, NID)
        assert state.node_name == "Gate"
        assert state.node_role == "other"
        assert state.firmware_version is None

    def test_state_identity_mismatch_rejected(self):
        payload = {"household_id": "OTHER", "node_id": NID}
        with pytest.raises(ValidationError, match="household_id does not match"):
            NodeState.from_dict(payload, HID, NID)

    def test_state_node_id_mismatch_rejected(self):
        payload = {"household_id": HID, "node_id": "OTHER"}
        with pytest.raises(ValidationError, match="node_id does not match"):
            NodeState.from_dict(payload, HID, NID)

    def test_state_missing_identity_rejected(self):
        with pytest.raises(ValidationError):
            NodeState.from_dict({"node_name": "Gate"}, HID, NID)

    def test_state_not_an_object_rejected(self):
        with pytest.raises(ValidationError, match="expected object"):
            NodeState.from_dict(["not", "an", "object"], HID, NID)


class TestHealthParsing:
    """Requirement 7: health payload parsing, preserving documented stubs."""

    def test_documented_health_payload(self):
        health = NodeHealth.from_dict(json.loads(HEALTH_JSON), HID, NID)
        assert health.mqtt == "OK"
        assert health.mqtt_error == 0
        assert health.nfc == "OK"
        assert health.lock_current == 1
        assert health.lock_target == 1
        assert health.backup == "ok"
        assert health.firmware_version == "0.10.0"
        assert health.uptime == 1234
        assert health.free_heap == 123456
        assert health.reset_reason == "1"
        assert health.security_all_ok is False
        assert health.security_warnings == "..."

    def test_network_stub_preserved_verbatim(self):
        """``network`` is always ``UNKNOWN``; never fabricated from HA state."""
        health = NodeHealth.from_dict(json.loads(HEALTH_JSON), HID, NID)
        assert health.network == HEALTH_NETWORK_UNKNOWN == "UNKNOWN"

    def test_certificate_stub_preserved_verbatim(self):
        """``certificate`` is always ``unknown``; never fabricated."""
        health = NodeHealth.from_dict(json.loads(HEALTH_JSON), HID, NID)
        assert health.certificate == HEALTH_CERTIFICATE_UNKNOWN == "unknown"

    def test_stubs_preserved_even_when_missing(self):
        """Missing stub fields still resolve to the documented literals."""
        health = NodeHealth.from_dict({"mqtt": "OK"}, HID, NID)
        assert health.network == "UNKNOWN"
        assert health.certificate == "unknown"

    def test_lock_current_maps_to_enum(self):
        health = NodeHealth.from_dict(json.loads(HEALTH_JSON), HID, NID)
        assert health.lock_current_state == LockState.LOCKED

    def test_jammed_lock_current(self):
        health = NodeHealth.from_dict(
            {"mqtt": "OK", "lock_current": 2},
            HID,
            NID,
        )
        assert health.lock_current_state == LockState.JAMMED

    def test_missing_lock_current_is_none_not_zero(self):
        """A missing field must not be interpreted as locked (0/1)."""
        health = NodeHealth.from_dict({"mqtt": "OK"}, HID, NID)
        assert health.lock_current is None
        assert health.lock_current_state is None

    def test_health_has_no_identity_fields_in_firmware(self):
        """Firmware 0.10.0 ``B/health`` omits household_id/node_id entirely."""
        health = NodeHealth.from_dict(json.loads(HEALTH_JSON), HID, NID)
        assert health.mqtt == "OK"

    def test_health_identity_mismatch_rejected_when_present(self):
        """A mismatched identity, if present, is still rejected."""
        with pytest.raises(ValidationError, match="does not match"):
            NodeHealth.from_dict(
                {
                    "household_id": "OTHER",
                    "node_id": NID,
                    "mqtt": "OK",
                },
                HID,
                NID,
            )

    def test_missing_mqtt_required_field_rejected(self):
        with pytest.raises(ValidationError, match="mqtt"):
            NodeHealth.from_dict({"household_id": HID, "node_id": NID}, HID, NID)

    def test_health_without_security_object(self):
        health = NodeHealth.from_dict(json.loads(HEALTH_JSON_NO_SECURITY), HID, NID)
        assert health.mqtt == "ERROR"
        assert health.security_all_ok is None
        assert health.security_warnings is None

    def test_security_object_must_be_object(self):
        with pytest.raises(ValidationError, match="health.security"):
            NodeHealth.from_dict(
                {"mqtt": "OK", "security": "x"},
                HID,
                NID,
            )

    def test_boolean_is_not_accepted_as_integer(self):
        """``True`` must not silently become ``lock_current == 1``."""
        with pytest.raises(ValidationError, match="lock_current"):
            NodeHealth.from_dict(
                {"mqtt": "OK", "lock_current": True},
                HID,
                NID,
            )


class TestBackupParsing:
    """Requirement 8: backup payload parsing (metadata only)."""

    def test_documented_backup_last_payload(self):
        record = BackupRecord.from_dict(json.loads(BACKUP_LAST_JSON), HID, NID)
        assert record.status == "completed"
        assert record.timestamp is not None
        assert record.timestamp.startswith("2025-")  # 1760000000 -> 2025

    def test_failed_backup(self):
        record = BackupRecord.from_dict(
            {"status": "failed", "timestamp": 1760000000}, HID, NID
        )
        assert record.status == "failed"

    def test_unknown_backup_status_rejected(self):
        with pytest.raises(ValidationError, match="unknown value"):
            BackupRecord.from_dict({"status": "maybe"}, HID, NID)

    def test_backup_never_expects_blob(self):
        """There is no model field for encrypted backup contents."""
        record = BackupRecord.from_dict(json.loads(BACKUP_LAST_JSON), HID, NID)
        assert not hasattr(record, "data")
        assert set(record.__dataclass_fields__) == {"status", "timestamp"}

    def test_identity_mismatch_rejected_when_present(self):
        with pytest.raises(ValidationError, match="does not match"):
            BackupRecord.from_dict(
                {"household_id": "OTHER", "node_id": NID, "status": "completed"},
                HID,
                NID,
            )


class TestSecurityParsing:
    """Requirement 9: security payload parsing (raw OK/WARNING, never numeric)."""

    @pytest.mark.parametrize("value", ["OK", "WARNING"])
    def test_documented_values_accepted(self, value):
        from custom_components.homekey_household.coordinator import (
            _VALID_SECURITY,
        )

        assert value in _VALID_SECURITY

    def test_security_values_are_the_documented_set(self):
        from custom_components.homekey_household.coordinator import _VALID_SECURITY

        # ERROR is reserved by the firmware but tolerated defensively.
        assert {"OK", "WARNING", "ERROR"} == _VALID_SECURITY

    def test_no_numeric_security_score_exists(self):
        """No numeric security classification may be modelled."""
        assert {s.value for s in SecurityState} == {"OK", "WARNING", "ERROR"}
        for value in ("ok", "warning", "100", "5", "SECURE"):
            assert value not in {s.value for s in SecurityState}


class TestLastAuthParsing:
    """Requirement 10: last-auth parsing (safe metadata only)."""

    def test_documented_last_auth_payload(self):
        auth = LastAuth.from_dict(json.loads(LAST_AUTH_JSON), HID, NID)
        assert auth.auth_type == "HomeKey"
        assert auth.result == "SUCCESS"
        assert auth.timestamp is not None
        assert auth.timestamp.startswith("2025-")

    def test_failure_result(self):
        auth = LastAuth.from_dict(
            {"type": "HomeKey", "result": "FAILURE", "timestamp": 1760000000},
            HID,
            NID,
        )
        assert auth.result == "FAILURE"

    def test_unknown_result_rejected(self):
        with pytest.raises(ValidationError, match="unknown value"):
            LastAuth.from_dict({"result": "MAYBE"}, HID, NID)

    def test_does_not_expect_credential_identifiers(self):
        """issuerId/endpointId/APDU are not part of this topic's contract."""
        record = LastAuth.from_dict(json.loads(LAST_AUTH_JSON), HID, NID)
        for forbidden in ("issuerId", "endpointId", "apdu", "credential_id"):
            assert not hasattr(record, forbidden)
        assert set(record.__dataclass_fields__) == {"auth_type", "result", "timestamp"}

    def test_extra_identifier_fields_are_ignored(self):
        """Even if a broker retained stale identifiers, they are not surfaced."""
        auth = LastAuth.from_dict(
            {
                "type": "HomeKey",
                "result": "SUCCESS",
                "timestamp": 1760000000,
                "issuerId": "DEADBEEF",
                "endpointId": "CAFEBABE",
            },
            HID,
            NID,
        )
        assert auth.result == "SUCCESS"
        assert "DEADBEEF" not in json.dumps(auth.__dict__)
