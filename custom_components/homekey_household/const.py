"""Constants for the HomeKey Household integration.

This integration is a *client* of the documented HomeKey-ESP32 **household MQTT
API** (firmware 0.10.0). The authoritative contract lives in the firmware
repository:

* ``docs/content/mqtt_household_api.md``
* ``docs/content/mqtt_api_contract_matrix.md``

Only the topics listed in that contract are used here. Topics the firmware marks
``RESERVED / NOT IMPLEMENTED`` are deliberately **not** declared as usable
subtopics (see :data:`RESERVED_TOPICS`), so no code can accidentally depend on
them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "homekey_household"
NAME: Final = "HomeKey Household"
MANUFACTURER: Final = "HomeKey-ESP32"
MODEL: Final = "HomeKey Node"

# ---------------------------------------------------------------------------
# Configuration / options
# ---------------------------------------------------------------------------
CONF_HOUSEHOLD_ID: Final = "household_id"
CONF_HOUSEHOLD_NAME: Final = "household_name"
# Raw household recovery secret. Used only to derive the command key and never
# persisted in the config entry (see credential.py).
CONF_RECOVERY_SECRET: Final = "recovery_secret"
# Optional household KDF salt (part of the firmware ``recovery_metadata``).
CONF_SALT: Final = "salt"
# Whether this household opted in to HMAC-authenticated lock control.
CONF_COMMAND_CONTROL: Final = "command_control"
# Legacy MQTT client-id prefix, used only for the *shared LWT availability topic*.
CONF_LEGACY_CLIENT_ID_PREFIX: Final = "legacy_client_id_prefix"

DEFAULT_LEGACY_CLIENT_ID_PREFIX: Final = "ESP_"
DEFAULT_COMMAND_CONTROL: Final = True

# Firmware version whose household contract this integration implements.
TARGET_FIRMWARE_VERSION: Final = "0.10.0"

# ---------------------------------------------------------------------------
# MQTT topic construction (do NOT hardcode topic strings elsewhere)
# ---------------------------------------------------------------------------
TOPIC_ROOT: Final = "homekey"
TOPIC_HOUSEHOLD: Final = "household"
TOPIC_NODES: Final = "nodes"

# Node level subtopics (firmware 0.10.0 household contract)
TOPIC_STATUS: Final = "status"
TOPIC_STATE: Final = "state"
TOPIC_HEALTH: Final = "health"
TOPIC_SECURITY: Final = "security"
TOPIC_BACKUP_STATUS: Final = "backup/status"
TOPIC_BACKUP_LAST: Final = "backup/last"
TOPIC_LAST_AUTH: Final = "last_auth"

# Command subtopics (HMAC-authenticated, authoritative for HA V2)
TOPIC_CMD_LOCK: Final = "command/lock"
TOPIC_CMD_UNLOCK: Final = "command/unlock"

# Topics the firmware documents as RESERVED / NOT IMPLEMENTED. Declared here for
# documentation and rejection only: the integration never subscribes to or
# publishes them.
RESERVED_TOPICS: Final[frozenset[str]] = frozenset(
    {
        "events",
        "backup/request",
        "backup/data",
        "restore/request",
        "restore/status",
    }
)

# Subtopics carrying a JSON object payload.
JSON_SUBTOPICS: Final[frozenset[str]] = frozenset(
    {
        TOPIC_STATE,
        TOPIC_HEALTH,
        TOPIC_BACKUP_LAST,
        TOPIC_LAST_AUTH,
    }
)

# Subtopics carrying a plain-text payload (never JSON).
PLAIN_SUBTOPICS: Final[frozenset[str]] = frozenset(
    {
        TOPIC_STATUS,
        TOPIC_SECURITY,
        TOPIC_BACKUP_STATUS,
    }
)

# Legacy firmware topics (pre-household). Functional in firmware, but explicitly
# LEGACY/INTERNAL: HA V2 must not use them for authoritative functionality. The
# single exception is the *shared broker LWT* availability topic (see availability).
LEGACY_STATUS_SUFFIX: Final = "status"
LEGACY_STATE_SUFFIX: Final = "homekit/state"
LEGACY_AUTH_SUFFIX: Final = "homekey/auth"

# Legacy command topics HA V2 must never publish (unauthenticated numeric).
LEGACY_COMMAND_TOPICS: Final[frozenset[str]] = frozenset(
    {
        "homekit/set_state",
        "homekit/set_target_state",
        "homekit/set_current_state",
        "homekit/set_battery_lvl",
        "homekit/set_custom_state",
    }
)


def node_base(household_id: str, node_id: str) -> str:
    """Return the node base topic ``B`` (no trailing slash)."""
    return (
        f"{TOPIC_ROOT}/{TOPIC_HOUSEHOLD}/{household_id}"
        f"/{TOPIC_NODES}/{node_id}"
    )


def node_topic(
    household_id: str, node_id: str, subtopic: str | None = None
) -> str:
    """Build a node-level topic.

    Example: ``homekey/household/HOME001/nodes/GATE-001/health``
    """
    base = node_base(household_id, node_id)
    return f"{base}/{subtopic}" if subtopic else base


# Subscription pattern that captures the whole household tree. Topic filtering
# happens in the parser: only documented subtopics are acted upon.
TOPIC_SUBSCRIBE_ALL: Final = f"{TOPIC_ROOT}/{TOPIC_HOUSEHOLD}/#"
# Node discovery pattern: the first message on a node subtree registers a node.
TOPIC_SUBSCRIBE_NODES: Final = (
    f"{TOPIC_ROOT}/{TOPIC_HOUSEHOLD}/+/{TOPIC_NODES}/#"
)


def legacy_status_subscribe(prefix: str) -> str:
    """Build a VALID MQTT filter for the legacy shared-LWT availability topics.

    The firmware publishes its single will to ``<CLIENT_ID>/status``, where
    ``<CLIENT_ID>`` is e.g. ``ESP_A1B2C3D4`` (see ``MQTT_LWT_TOPIC "status"``).

    MQTT requires ``+`` to occupy an **entire** topic level, so a filter like
    ``ESP_+/status`` is invalid: paho rejects it with ``Invalid subscription
    filter`` and the subscription silently never happens. Since ``prefix`` is
    only a *fragment* of the client-id level, the wildcard is emitted as its own
    whole level, giving ``+/status``, which is valid and matches
    ``ESP_A1B2C3D4/status``.

    Callers that need to restrict to a specific prefix must filter the received
    client id themselves (the parser already scopes legacy handling).
    """
    return f"+/{LEGACY_STATUS_SUFFIX}"


# ---------------------------------------------------------------------------
# Command protocol (HMAC-SHA256 over the topic-derived action)
# ---------------------------------------------------------------------------
# BLAKE2b personalisation label used to derive the household command key from
# ``recovery_secret || salt``. Must match firmware ``household::kCommandKeyLabel``.
COMMAND_KEY_LABEL: Final = "HK-HOUSEHOLD-CMD-v1"
COMMAND_KEY_LENGTH: Final = 32
# Firmware accepts ``ts`` within +/- this many seconds of its wall clock.
COMMAND_MAX_SKEW_SECONDS: Final = 300
# Firmware replay window (bounded nonce deque).
COMMAND_REPLAY_WINDOW: Final = 32
# Nonce entropy in bytes, rendered as lowercase hex (16 bytes -> 32 hex chars).
COMMAND_NONCE_BYTES: Final = 16

ACTION_LOCK: Final = "lock"
ACTION_UNLOCK: Final = "unlock"

# topic -> topic-derived action (the payload has no ``action`` field)
COMMAND_ACTIONS: Final[dict[str, str]] = {
    TOPIC_CMD_LOCK: ACTION_LOCK,
    TOPIC_CMD_UNLOCK: ACTION_UNLOCK,
}


# ---------------------------------------------------------------------------
# Unique ID / naming helpers
# ---------------------------------------------------------------------------
def unique_id(household_id: str, node_id: str, entity_type: str) -> str:
    """Stable unique id matching the firmware contract: ``<hid>_<nid>_<entity>``."""
    return f"{household_id}_{node_id}_{entity_type}"


def device_identifiers(household_id: str, node_id: str) -> set[tuple[str, str, str]]:
    """Stable logical device identity: household id + node id (not MAC/deviceID)."""
    return {(DOMAIN, household_id, node_id)}


# ---------------------------------------------------------------------------
# Payload keys (firmware 0.10.0 household contract)
# ---------------------------------------------------------------------------
KEY_HOUSEHOLD_ID: Final = "household_id"
KEY_NODE_ID: Final = "node_id"
KEY_NODE_NAME: Final = "node_name"
KEY_NODE_ROLE: Final = "node_role"
KEY_NODE_STATE: Final = "node_state"
KEY_GENERATION: Final = "generation"
KEY_FIRMWARE: Final = "firmware_version"

KEY_NETWORK: Final = "network"
KEY_MQTT: Final = "mqtt"
KEY_MQTT_ERROR: Final = "mqtt_error"
KEY_NFC: Final = "nfc"
KEY_LOCK_CURRENT: Final = "lock_current"
KEY_LOCK_TARGET: Final = "lock_target"
KEY_BACKUP: Final = "backup"
KEY_CERTIFICATE: Final = "certificate"
KEY_UPTIME: Final = "uptime"
KEY_FREE_HEAP: Final = "free_heap"
KEY_RESET_REASON: Final = "reset_reason"
KEY_SECURITY_OBJECT: Final = "security"
KEY_SECURITY_ALL_OK: Final = "all_ok"
KEY_SECURITY_WARNINGS: Final = "warnings"

KEY_BACKUP_STATUS: Final = "status"
KEY_TIMESTAMP: Final = "timestamp"
KEY_AUTH_TYPE: Final = "type"
KEY_AUTH_RESULT: Final = "result"

# Command payload keys (exactly four; no ``action`` field).
KEY_TS: Final = "ts"
KEY_NONCE: Final = "nonce"
KEY_REQ_ID: Final = "req_id"
KEY_MAC: Final = "mac"

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
STORAGE_VERSION: Final = 1
COMMAND_KEY_STORAGE_KEY: Final = "homekey_household.command_keys"
COMMAND_KEY_STORAGE_VERSION: Final = 1

# ---------------------------------------------------------------------------
# Enums / value maps
# ---------------------------------------------------------------------------


class LockState(StrEnum):
    """Lock states reported by the ESP32 LockManager."""

    UNLOCKED = "unlocked"
    LOCKED = "locked"
    JAMMED = "jammed"
    UNKNOWN = "unknown"
    UNLOCKING = "unlocking"
    LOCKING = "locking"


# Numeric lock state -> LockState (matches LockManager.hpp enum). The household
# ``lock_current``/``lock_target`` health fields use the same numbering.
LOCK_STATE_MAP: Final[dict[int, str]] = {
    0: LockState.UNLOCKED,
    1: LockState.LOCKED,
    2: LockState.JAMMED,
    3: LockState.UNKNOWN,
    4: LockState.UNLOCKING,
    5: LockState.LOCKING,
}


class NodeRole(StrEnum):
    """Node roles defined by the firmware (``B/state.node_role``)."""

    GATE = "gate"
    MAIN_HOUSE = "main_house"
    SMALL_HOUSE = "small_house"
    GARAGE = "garage"
    WORKSHOP = "workshop"
    OTHER = "other"


class NodeState(StrEnum):
    """Node lifecycle state (``B/state.node_state``)."""

    UNCONFIGURED = "UNCONFIGURED"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class SecurityState(StrEnum):
    """Compact security state published on ``B/security`` (never numeric)."""

    OK = "OK"
    WARNING = "WARNING"
    # Reserved by the firmware and never emitted; tolerated defensively.
    ERROR = "ERROR"


class BackupOutcome(StrEnum):
    """Backup outcome published on ``B/backup/status`` / ``B/backup/last``."""

    COMPLETED = "completed"
    FAILED = "failed"


class AuthResult(StrEnum):
    """Result published on ``B/last_auth``."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


# Health field values.
HEALTH_OK: Final = "OK"
HEALTH_ERROR: Final = "ERROR"
# Documented firmware stubs (not wired). These exact literals must be preserved
# and never replaced with fabricated data.
HEALTH_NETWORK_UNKNOWN: Final = "UNKNOWN"
HEALTH_CERTIFICATE_UNKNOWN: Final = "unknown"

# ---------------------------------------------------------------------------
# Entities: one per documented firmware discovery entity, plus the lock
# ---------------------------------------------------------------------------
ENTITY_LOCK: Final = "lock"
ENTITY_SENSOR_ONLINE: Final = "online"
ENTITY_SENSOR_HEALTH: Final = "health"
ENTITY_SENSOR_BACKUP: Final = "backup"
ENTITY_SENSOR_SECURITY: Final = "security"
ENTITY_SENSOR_FIRMWARE: Final = "firmware"
ENTITY_SENSOR_LAST_AUTH: Final = "last_auth"

# The six documented household entity unique-id suffixes.
HOUSEHOLD_ENTITY_SUFFIXES: Final[tuple[str, ...]] = (
    ENTITY_SENSOR_ONLINE,
    ENTITY_SENSOR_HEALTH,
    ENTITY_SENSOR_BACKUP,
    ENTITY_SENSOR_SECURITY,
    ENTITY_SENSOR_FIRMWARE,
    ENTITY_SENSOR_LAST_AUTH,
)
