"""Strongly typed, strictly validated data models for HomeKey Household.

Every ``from_*`` helper validates its input and raises :class:`ValidationError`
on malformed data. The integration never trusts MQTT payloads blindly.

Parsing rules derived from the firmware 0.10.0 household contract:

* The firmware does **not** send a ``schema`` field on household payloads, so
  none is required. Unknown fields are ignored; required fields are strict.
* ``household_id`` / ``node_id`` inside a node payload must match the topic.
* Missing fields are *not* silently coerced to zero/false: a required field that
  is absent is a validation error, and an optional field stays ``None`` so the
  coordinator can preserve the previous valid state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import (
    KEY_AUTH_ISSUER,
    KEY_AUTH_RESULT,
    KEY_AUTH_TYPE,
    KEY_BACKUP,
    KEY_BACKUP_STATUS,
    KEY_CERTIFICATE,
    KEY_FIRMWARE,
    KEY_FREE_HEAP,
    KEY_GENERATION,
    KEY_HOUSEHOLD_ID,
    KEY_LOCK_CHANGE_CURRENT,
    KEY_LOCK_CHANGE_TARGET,
    KEY_LOCK_CURRENT,
    KEY_LOCK_SOURCE,
    KEY_LOCK_TARGET,
    KEY_MQTT,
    KEY_MQTT_ERROR,
    KEY_NETWORK,
    KEY_NFC,
    KEY_NODE_ID,
    KEY_NODE_NAME,
    KEY_NODE_ROLE,
    KEY_NODE_STATE,
    KEY_SECURITY_ALL_OK,
    KEY_SECURITY_OBJECT,
    KEY_SECURITY_WARNINGS,
    KEY_TIMESTAMP,
    KEY_UPTIME,
    LOCK_STATE_MAP,
    AuthResult,
    BackupOutcome,
    LockSource,
    LockState,
    NodeRole,
)


class ValidationError(ValueError):
    """Raised when an MQTT payload fails validation."""


# ---------------------------------------------------------------------------
# Primitive validators
# ---------------------------------------------------------------------------
def _require_dict(data: Any, where: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValidationError(f"{where}: expected object, got {type(data).__name__}")
    return data


def _require_str(data: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"{key}: expected string, got {type(value).__name__}")
    if not allow_empty and not value:
        raise ValidationError(f"{key}: must not be empty")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{key}: expected string, got {type(value).__name__}")
    return value


def _optional_bool(data: dict[str, Any], key: str) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValidationError(f"{key}: expected boolean, got {type(value).__name__}")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    # bool is a subclass of int in Python — reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{key}: expected integer, got {type(value).__name__}")
    return value


def as_optional_str(value: Any, where: str) -> str | None:
    """Coerce an arbitrary JSON value to an optional string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{where}: expected string, got {type(value).__name__}")
    return value


def normalise_timestamp(value: Any, where: str) -> str | None:
    """Normalise an epoch-seconds timestamp to a UTC ISO-8601 string.

    The firmware publishes unix epoch seconds on ``B/backup/last`` and
    ``B/last_auth`` (with a monotonic-uptime-seconds fallback when no wall clock
    is available). ISO-8601 strings are accepted for forward compatibility.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{where}: invalid timestamp")
    if isinstance(value, int | float):
        from datetime import UTC, datetime

        try:
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValidationError(f"{where}: invalid timestamp") from exc
    if isinstance(value, str):
        # Accept ISO strings and the bare-numeric uptime fallback verbatim.
        return value
    raise ValidationError(f"{where}: invalid timestamp type")


def _optional_enum(data: dict[str, Any], key: str, enum: type[StrEnum]) -> str | None:
    """Validate an optional string against a known enum's allowed values."""
    value = _optional_str(data, key)
    if value is None:
        return None
    if value not in set(enum):
        raise ValidationError(f"{key}: unknown value {value!r}")
    return value


# ---------------------------------------------------------------------------
# ID validation
# ---------------------------------------------------------------------------
_ID_SAFE_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


def validate_id(value: str, field_name: str, *, max_len: int = 64) -> str:
    """Validate a household/node id against the contract safe charset."""
    if not value:
        raise ValidationError(f"{field_name}: must not be empty")
    if len(value) > max_len:
        raise ValidationError(f"{field_name}: too long (max {max_len})")
    if any(ch not in _ID_SAFE_CHARS for ch in value):
        raise ValidationError(
            f"{field_name}: contains invalid characters (allowed: [A-Za-z0-9_-])"
        )
    return value


def validate_identity(
    data: dict[str, Any], household_id: str, node_id: str | None, where: str
) -> None:
    """Ensure ``household_id``/``node_id`` match the topic *when present*.

    The firmware includes identity fields in ``state``, ``backup/last`` and
    ``last_auth`` but **not** in ``health``. Fields that are absent are therefore
    allowed; fields that are present must match the topic (a mismatch is always
    rejected).
    """
    if KEY_HOUSEHOLD_ID in data:
        payload_household = _require_str(data, KEY_HOUSEHOLD_ID)
        if payload_household != household_id:
            raise ValidationError(
                f"{where}.household_id does not match topic "
                f"({payload_household!r} != {household_id!r})"
            )
    if node_id is not None and KEY_NODE_ID in data:
        payload_node = _require_str(data, KEY_NODE_ID)
        if payload_node != node_id:
            raise ValidationError(
                f"{where}.node_id does not match topic "
                f"({payload_node!r} != {node_id!r})"
            )


def require_identity(
    data: dict[str, Any], household_id: str, node_id: str | None, where: str
) -> None:
    """Require identity fields to be present *and* match the topic.

    Used for payloads where the contract always includes identity (``state``).
    """
    payload_household = _require_str(data, KEY_HOUSEHOLD_ID)
    if payload_household != household_id:
        raise ValidationError(
            f"{where}.household_id does not match topic "
            f"({payload_household!r} != {household_id!r})"
        )
    if node_id is not None:
        payload_node = _require_str(data, KEY_NODE_ID)
        if payload_node != node_id:
            raise ValidationError(
                f"{where}.node_id does not match topic "
                f"({payload_node!r} != {node_id!r})"
            )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeState:
    """Parsed ``B/state`` payload (retained, QoS 0)."""

    household_id: str
    node_id: str
    node_name: str
    node_role: str
    node_state: str
    generation: int | None = None
    firmware_version: str | None = None

    @classmethod
    def from_dict(cls, payload: Any, household_id: str, node_id: str) -> NodeState:
        data = _require_dict(payload, "state")
        # ``state`` always carries identity per the contract.
        require_identity(data, household_id, node_id, "state")
        return cls(
            household_id=household_id,
            node_id=node_id,
            node_name=_optional_str(data, KEY_NODE_NAME) or node_id,
            node_role=_optional_str(data, KEY_NODE_ROLE) or NodeRole.OTHER,
            node_state=_optional_str(data, KEY_NODE_STATE) or "UNKNOWN",
            generation=_optional_int(data, KEY_GENERATION),
            firmware_version=_optional_str(data, KEY_FIRMWARE),
        )


@dataclass(frozen=True)
class NodeHealth:
    """Parsed ``B/health`` payload (non-retained, QoS 0).

    ``network`` and ``certificate`` are documented firmware stubs: ``network`` is
    always ``"UNKNOWN"`` and ``certificate`` is always ``"unknown"``. Both are
    preserved verbatim and never fabricated from unrelated Home Assistant data.
    """

    network: str
    mqtt: str
    nfc: str
    certificate: str
    mqtt_error: int | None = None
    lock_current: int | None = None
    lock_target: int | None = None
    backup: str | None = None
    firmware_version: str | None = None
    uptime: int | None = None
    free_heap: int | None = None
    reset_reason: str | None = None
    security_all_ok: bool | None = None
    security_warnings: str | None = None

    @classmethod
    def from_dict(cls, payload: Any, household_id: str, node_id: str) -> NodeHealth:
        data = _require_dict(payload, "health")
        # ``health`` omits identity fields in firmware 0.10.0; validate only when
        # they are present so a stale/mismatched sibling is still rejected.
        validate_identity(data, household_id, node_id, "health")

        security = data.get(KEY_SECURITY_OBJECT)
        if security is None:
            sec_obj: dict[str, Any] = {}
        elif isinstance(security, dict):
            sec_obj = security
        else:
            raise ValidationError("health.security: expected object")

        return cls(
            # Preserve the documented stubs verbatim; default only when absent.
            network=_optional_str(data, KEY_NETWORK) or "UNKNOWN",
            # ``mqtt`` is the documented state value for the health entity.
            mqtt=_require_str(data, KEY_MQTT),
            nfc=_optional_str(data, KEY_NFC) or "unknown",
            certificate=_optional_str(data, KEY_CERTIFICATE) or "unknown",
            mqtt_error=_optional_int(data, KEY_MQTT_ERROR),
            lock_current=_optional_int(data, KEY_LOCK_CURRENT),
            lock_target=_optional_int(data, KEY_LOCK_TARGET),
            backup=_optional_str(data, KEY_BACKUP),
            firmware_version=_optional_str(data, KEY_FIRMWARE),
            uptime=_optional_int(data, KEY_UPTIME),
            free_heap=_optional_int(data, KEY_FREE_HEAP),
            reset_reason=as_optional_str(data.get("reset_reason"), "reset_reason"),
            security_all_ok=_optional_bool(sec_obj, KEY_SECURITY_ALL_OK),
            security_warnings=as_optional_str(
                sec_obj.get(KEY_SECURITY_WARNINGS), "security.warnings"
            ),
        )

    @property
    def lock_current_state(self) -> str | None:
        """Lock state enum for ``lock_current`` (``None`` when absent)."""
        if self.lock_current is None:
            return None
        return LOCK_STATE_MAP.get(self.lock_current, LockState.UNKNOWN)


@dataclass(frozen=True)
class BackupRecord:
    """Parsed ``B/backup/last`` payload (metadata only, retained).

    Encrypted backup contents are never published over MQTT; this model only ever
    carries the outcome and timestamp.
    """

    status: str
    timestamp: str | None = None

    @classmethod
    def from_dict(cls, payload: Any, household_id: str, node_id: str) -> BackupRecord:
        data = _require_dict(payload, "backup/last")
        # ``backup/last`` is metadata only; identity is validated when present.
        validate_identity(data, household_id, node_id, "backup/last")
        status = _require_str(data, KEY_BACKUP_STATUS)
        if status not in set(BackupOutcome):
            raise ValidationError(f"backup/last.status: unknown value {status!r}")
        return cls(
            status=status,
            timestamp=normalise_timestamp(
                data.get(KEY_TIMESTAMP), "backup/last.timestamp"
            ),
        )


@dataclass(frozen=True)
class LastAuth:
    """Parsed ``B/last_auth`` payload (safe metadata only, retained).

    Contains only ``type``/``result``/``timestamp``. Credential identifiers,
    APDU data and cryptographic material are never published to this topic.
    """

    auth_type: str
    result: str
    timestamp: str | None = None
    # The name the user gave the controller that authenticated, when they gave one. The
    # firmware never sends the issuer id, so an unnamed issuer is simply unnamed here.
    issuer: str | None = None

    @classmethod
    def from_dict(cls, payload: Any, household_id: str, node_id: str) -> LastAuth:
        data = _require_dict(payload, "last_auth")
        # ``last_auth`` carries safe metadata only; identity is checked if present.
        validate_identity(data, household_id, node_id, "last_auth")
        result = _require_str(data, KEY_AUTH_RESULT)
        if result not in set(AuthResult):
            raise ValidationError(f"last_auth.result: unknown value {result!r}")
        return cls(
            auth_type=_optional_str(data, KEY_AUTH_TYPE) or "HomeKey",
            result=result,
            timestamp=normalise_timestamp(
                data.get(KEY_TIMESTAMP), "last_auth.timestamp"
            ),
            issuer=_optional_str(data, KEY_AUTH_ISSUER),
        )


@dataclass(frozen=True)
class LockChange:
    """Parsed ``B/lock/last`` payload: what changed the lock, and to what.

    Separate from ``B/health`` because a state is a property and an origin is an event:
    re-announcing a state must not re-announce an old cause.
    """

    current: int
    source: str
    target: int | None = None
    timestamp: str | None = None

    @classmethod
    def from_dict(cls, payload: Any, household_id: str, node_id: str) -> LockChange:
        data = _require_dict(payload, "lock/last")
        validate_identity(data, household_id, node_id, "lock/last")

        # ``current`` is what ties this cause to a state change. Without it the entry
        # cannot be matched to the change it describes, so it is required rather than
        # optional - a cause that cannot be attributed is worse than none.
        current = _optional_int(data, KEY_LOCK_CHANGE_CURRENT)
        if current is None:
            raise ValidationError("lock/last.current: missing")

        source = _optional_str(data, KEY_LOCK_SOURCE)
        if source not in set(LockSource):
            raise ValidationError(f"lock/last.source: unknown value {source!r}")

        return cls(
            current=current,
            source=source,
            target=_optional_int(data, KEY_LOCK_CHANGE_TARGET),
            timestamp=normalise_timestamp(data.get(KEY_TIMESTAMP), "lock/last.timestamp"),
        )


@dataclass
class Node:
    """Mutable per-node state maintained by the coordinator.

    The logical identity is ``household_id + node_id``. A replacement node
    (``GATE-001`` -> ``GATE-002``) is a *different* node with its own entities.
    """

    node_id: str
    household_id: str
    node_name: str = ""
    node_role: str = NodeRole.OTHER
    node_state: str = "UNKNOWN"
    generation: int | None = None
    firmware: str | None = None
    # Availability from the retained ``B/status`` topic (``online``).
    online: bool = False
    # Legacy shared-LWT availability topic (``<CLIENT_ID>/status``). Tracked
    # separately because it is the only *broker-driven* offline signal.
    lwt_online: bool | None = None
    last_seen: str | None = None
    health: NodeHealth | None = None
    security: str | None = None
    backup_status: str | None = None
    backup: BackupRecord | None = None
    last_auth: LastAuth | None = None
    # The most recent change the node reported, including what asked for it. Kept so a
    # lock state change can be attributed to a cause rather than to nothing.
    lock_change: LockChange | None = None
    # True while ``lock_change`` is newer than the health snapshot. ``B/lock/last`` is
    # published the moment the lock changes; ``B/health`` samples it on a cadence, so
    # between the two the event is the fresher word on the lock.
    lock_change_is_fresh: bool = False
    # ``(timestamp, current, source)`` of the change whose cause has already been recorded,
    # so a retained replay of the same event is not announced as news a second time.
    attributed_lock_change: tuple[str | None, int, str] | None = None

    def __post_init__(self) -> None:
        if not self.node_name:
            self.node_name = self.node_id

    @property
    def lock_change_key(self) -> tuple[str | None, int, str] | None:
        """Identity of the most recent lock event, for de-duplication."""
        if self.lock_change is None:
            return None
        change = self.lock_change
        return (change.timestamp, change.current, change.source)

    @property
    def lock_state(self) -> str:
        """The lock's state, from the freshest report of it.

        Two documents describe the lock. ``B/lock/last`` is published the moment it
        changes; ``B/health`` is a snapshot taken on a cadence. Where they disagree the
        event wins, but only until the next snapshot arrives: a snapshot samples the
        hardware, so it is the one that knows about a jam, while the event only knows what
        was asked for.
        """
        if self.lock_change_is_fresh and self.lock_change is not None:
            return LOCK_STATE_MAP.get(self.lock_change.current, LockState.UNKNOWN)
        if self.health is None:
            return LockState.UNKNOWN
        return self.health.lock_current_state or LockState.UNKNOWN

    @property
    def available(self) -> bool:
        """Entity availability.

        Available when the retained ``B/status`` says online, and — when the
        shared broker LWT has been observed — that LWT also says online. This
        honours the documented clean-disconnect limitation without inventing a
        timer-based fake offline state.
        """
        if not self.online:
            return False
        return self.lwt_online is not False

    @classmethod
    def from_state(cls, household_id: str, node_id: str, payload: Any) -> Node:
        """Build/refresh a node from a validated ``state`` payload."""
        state = NodeState.from_dict(payload, household_id, node_id)
        return cls(
            node_id=node_id,
            household_id=household_id,
            node_name=state.node_name,
            node_role=state.node_role,
            node_state=state.node_state,
            generation=state.generation,
            firmware=state.firmware_version,
        )


# ---------------------------------------------------------------------------
# Command models
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuthenticatedCommand:
    """The exact 4-field HMAC command payload required by the firmware.

    ``action`` is **not** part of the payload: it is derived from the MQTT topic
    (``.../command/lock`` -> ``lock``, ``.../command/unlock`` -> ``unlock``).
    """

    ts: int
    nonce: str
    req_id: str
    mac: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise in the contract's field order (``ts`` is an integer)."""
        from .const import KEY_MAC, KEY_NONCE, KEY_REQ_ID, KEY_TS

        return {
            KEY_TS: self.ts,
            KEY_NONCE: self.nonce,
            KEY_REQ_ID: self.req_id,
            KEY_MAC: self.mac,
        }
