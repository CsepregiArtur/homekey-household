"""Sensor platform for HomeKey Household.

Implements the five documented household sensors from the firmware discovery
contract, each with a stable unique id ``<household_id>_<node_id>_<entity>``:

===========================  =========================  ========================
Entity                       Unique-id suffix           State source
===========================  =========================  ========================
Node health                  ``health``                 ``B/health`` (``mqtt``)
Backup status                ``backup``                 ``B/backup/last`` status
Security status              ``security``               ``B/security`` (raw)
Firmware version             ``firmware``               ``B/state.firmware_version``
Last HomeKey authentication  ``last_auth``              ``B/last_auth.result``
===========================  =========================  ========================

Semantics are preserved exactly:

* ``security`` is the raw documented string ``OK`` / ``WARNING`` — never a
  numeric score and never an invented classification.
* ``health`` uses the documented ``mqtt`` field as its value.
* ``backup`` uses ``B/backup/last.status``; the full JSON is exposed as
  attributes. Backup *contents* are never expected or accepted over MQTT.
* ``last_auth`` uses ``B/last_auth.result``; the full safe JSON is exposed as
  attributes. No credential identifiers or key material are ever expected.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .backup import (
    BACKUP_STORE_KEY,
    BackupStore,
    StoredBackup,
    backup_client_for,
    entry_backup_settings,
)
from .const import (
    DOMAIN,
    ENTITY_SENSOR_BACKUP,
    ENTITY_SENSOR_FIRMWARE,
    ENTITY_SENSOR_GUEST_TAGS,
    ENTITY_SENSOR_HEALTH,
    ENTITY_SENSOR_LAST_AUTH,
    ENTITY_SENSOR_SECURITY,
    AuthResult,
    unique_id,
)
from .coordinator import HomeKeyHouseholdCoordinator
from .discovery import async_add_entities_for_nodes
from .entity import HomeKeyBaseEntity
from .models import Node

# The documented allowed values for each enumerated sensor.
_HEALTH_OPTIONS = ["OK", "ERROR"]
_SECURITY_OPTIONS = ["OK", "WARNING", "ERROR"]
_BACKUP_OPTIONS = ["completed", "failed"]
_LAST_AUTH_OPTIONS = ["SUCCESS", "FAILURE"]


def _sensors_for_node(
    coordinator: HomeKeyHouseholdCoordinator, node_id: str
) -> list[HomeKeyBaseSensor]:
    """Build the documented sensors for one node, plus the guest tag count."""
    return [
        HomeKeyHealthSensor(coordinator, node_id),
        HomeKeyBackupSensor(coordinator, node_id),
        HomeKeySecuritySensor(coordinator, node_id),
        HomeKeyFirmwareSensor(coordinator, node_id),
        HomeKeyLastAuthSensor(coordinator, node_id),
        HomeKeyGuestTagsSensor(coordinator, node_id),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for known nodes and for nodes discovered later."""
    coordinator: HomeKeyHouseholdCoordinator = hass.data[entry.domain][
        entry.entry_id
    ].coordinator
    async_add_entities_for_nodes(
        coordinator,
        async_add_entities,
        lambda node_id: _sensors_for_node(coordinator, node_id),
    )


class HomeKeyBaseSensor(HomeKeyBaseEntity, SensorEntity):
    """A sensor backed by a node accessor with a declared option set."""

    def __init__(
        self,
        coordinator: HomeKeyHouseholdCoordinator,
        node_id: str,
        entity_type: str,
        value_fn: Callable[[Node], str | datetime | None],
        *,
        device_class: SensorDeviceClass | None = None,
        options: list[str] | None = None,
    ) -> None:
        super().__init__(coordinator, node_id)
        self.entity_type = entity_type
        self._value_fn = value_fn
        self._attr_unique_id = unique_id(coordinator.household_id, node_id, entity_type)
        self._attr_translation_key = entity_type
        if options is not None:
            # HA requires the ENUM device class whenever options are declared,
            # otherwise the entity raises "providing enum options, but is missing
            # the enum device class" when its state is read.
            self._attr_options = options
            self._attr_device_class = SensorDeviceClass.ENUM
        else:
            self._attr_device_class = device_class

    @property
    def native_value(self) -> str | datetime | None:
        node = self._node()
        if node is None:
            return None
        return self._value_fn(node)


class HomeKeyHealthSensor(HomeKeyBaseSensor):
    """Node health, using the documented ``B/health`` ``mqtt`` field."""

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(
            coordinator,
            node_id,
            ENTITY_SENSOR_HEALTH,
            lambda node: node.health.mqtt if node.health else None,
            options=_HEALTH_OPTIONS,
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = super().extra_state_attributes
        node = self._node()
        if node is None or node.health is None:
            return attributes
        health = node.health
        # Surface the documented health snapshot. ``network`` is always
        # "UNKNOWN" and ``certificate`` always "unknown" in firmware 0.10.0;
        # both are reported verbatim, never fabricated.
        attributes.update(
            {
                "network": health.network,
                "nfc": health.nfc,
                "lock_current": health.lock_current,
                "lock_target": health.lock_target,
                "backup": health.backup,
                "certificate": health.certificate,
                "firmware_version": health.firmware_version,
                "uptime": health.uptime,
                "free_heap": health.free_heap,
                "reset_reason": health.reset_reason,
                "mqtt_error": health.mqtt_error,
                "security_all_ok": health.security_all_ok,
                "security_warnings": health.security_warnings,
            }
        )
        return attributes


class HomeKeyBackupSensor(HomeKeyBaseSensor):
    """What the node says about its last backup, and what Home Assistant holds.

    The node's own report is metadata - an outcome and a timestamp - because the device
    keeps no copy of the backup itself. So the attributes also state how many copies this
    integration has taken and when the newest one was made, which is the part that still
    exists after the node does not.
    """

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(
            coordinator,
            node_id,
            ENTITY_SENSOR_BACKUP,
            lambda node: node.backup.status if node.backup else node.backup_status,
            options=_BACKUP_OPTIONS,
        )

    def _stored_backups(self) -> list[StoredBackup]:
        """Copies held for this node, newest last."""
        store = self.hass.data.get(DOMAIN, {}).get(BACKUP_STORE_KEY)
        if not isinstance(store, BackupStore):
            return []
        return store.for_node(self._node_id)

    def _api_configured(self) -> bool:
        """Whether this entry has what a backup or a restore needs.

        Both travel over the node's own HTTPS API, not over MQTT, so an entry without
        an address, a fingerprint and Web UI credentials cannot do either - and that is
        worth saying on the entity rather than only in a log line when a button is
        pressed.
        """
        runtime = self.hass.data.get(DOMAIN, {}).get(self.coordinator.entry_id)
        if runtime is None:
            return False
        return backup_client_for(entry_backup_settings(runtime))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = super().extra_state_attributes
        node = self._node()
        if node is None:
            return attributes
        if node.backup is not None:
            # Full metadata JSON (status + timestamp) as documented; never the
            # encrypted backup contents.
            attributes["status"] = node.backup.status
            parsed = _parse_dt(node.backup.timestamp)
            attributes["backup_timestamp"] = node.backup.timestamp
            attributes["backup_age_seconds"] = _age_seconds(parsed)
        if node.backup_status is not None:
            attributes["last_event"] = node.backup_status

        stored = self._stored_backups()
        attributes["stored_backups"] = len(stored)
        if stored:
            latest = stored[-1]
            attributes["stored_created"] = latest.created
            attributes["stored_age_seconds"] = _age_seconds(_parse_dt(latest.created))
            # Which kind of backup the newest copy is decides what it can do: with the
            # node's keys inside, a replacement node needs no tag enrolled again.
            attributes["stored_includes_credentials"] = latest.includes_credentials
            if latest.node_time is not None:
                attributes["stored_node_time"] = latest.node_time
            # Every copy, so the rolling window is visible rather than only its newest
            # entry. Metadata only - the blobs themselves stay in the store.
            attributes["stored_backups_detail"] = [
                {
                    "created": entry.created,
                    "age_seconds": _age_seconds(_parse_dt(entry.created)),
                    "includes_credentials": entry.includes_credentials,
                    "hex_bytes": len(entry.blob) // 2,
                    "node_time": entry.node_time,
                }
                for entry in stored
            ]

        # The restore flow, in one place, because it is three separate facts that only
        # mean something together: is there a copy, does it carry the node's keys, and
        # what does it still need.
        attributes["restore"] = {
            "available": bool(stored),
            # Only a copy taken with the node's keys inside can bring a *replacement*
            # node back without every tag being enrolled again. Without it, the file
            # restores membership and configuration, and the enrolled devices have to
            # be provisioned afresh. ``None`` when there is nothing to restore from.
            "from_credentials_backup": (
                stored[-1].includes_credentials if stored else None
            ),
            # Always required, and never kept here by design: the secret is the key the
            # backup was sealed with *and* the proof of the right to rejoin, so it is
            # supplied per restore and passed straight to the node.
            "recovery_secret_required": True,
            "service": "homekey_household.restore_backup",
        }
        # Whether a backup or a restore can reach the node at all.
        attributes["api_configured"] = self._api_configured()
        return attributes


class HomeKeySecuritySensor(HomeKeyBaseSensor):
    """Security status from ``B/security``, with the reasons the node gave for it.

    The state is a single word, and a word on its own does not answer *why*. The node
    reports its findings right next to the verdict - which hardening features are switched
    off, in its own words - so they are exposed here: a card that says WARNING without
    saying why is a card that gets ignored, and the answer already arrived with the state.
    """

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(
            coordinator,
            node_id,
            ENTITY_SENSOR_SECURITY,
            lambda node: node.security,
            options=_SECURITY_OPTIONS,
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = super().extra_state_attributes
        node = self._node()
        health = node.health if node is not None else None
        if health is None:
            # No snapshot yet: the state is unknown rather than OK, and inventing findings
            # for it would be worse than saying nothing.
            return attributes
        findings = _finding_lines(health.security_warnings)
        attributes["security_all_ok"] = health.security_all_ok
        # The raw text as published, for templates that want it verbatim...
        attributes["security_warnings"] = health.security_warnings
        # ...and the same findings one per line, which is what a card can show.
        attributes["security_findings"] = findings
        attributes["warning_count"] = len(findings)
        return attributes


class HomeKeyFirmwareSensor(HomeKeyBaseSensor):
    """Firmware version from ``B/state.firmware_version``."""

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(
            coordinator,
            node_id,
            ENTITY_SENSOR_FIRMWARE,
            lambda node: node.firmware,
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = super().extra_state_attributes
        node = self._node()
        if node is None:
            return attributes
        attributes["generation"] = node.generation
        return attributes


class HomeKeyLastAuthSensor(HomeKeyBaseSensor):
    """Last HomeKey authentication result from ``B/last_auth.result``."""

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(
            coordinator,
            node_id,
            ENTITY_SENSOR_LAST_AUTH,
            lambda node: node.last_auth.result if node.last_auth else None,
            options=_LAST_AUTH_OPTIONS,
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = super().extra_state_attributes
        node = self._node()
        if node is None or node.last_auth is None:
            return attributes
        # Only the safe metadata published on this topic is surfaced.
        attributes["auth_type"] = node.last_auth.auth_type
        attributes["auth_timestamp"] = node.last_auth.timestamp
        # The name the user gave the controller that authenticated. Present only when the
        # device sent one, which happens only when the user named that issuer: the device
        # never publishes the underlying issuer id.
        attributes["issuer"] = node.last_auth.issuer
        attributes["auth_age_seconds"] = _age_seconds(
            _parse_dt(node.last_auth.timestamp)
        )
        if node.last_auth.result == AuthResult.SUCCESS:
            attributes["last_success_timestamp"] = node.last_auth.timestamp
        return attributes


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _age_seconds(when: datetime | None) -> int | None:
    if when is None:
        return None
    return max(0, int((datetime.now(UTC) - when).total_seconds()))


def _finding_lines(warnings: str | None) -> list[str]:
    """A node's security findings, one per line, in the node's own words.

    The firmware joins them with real newlines. Splitting them here is the point of the
    attribute that uses this: Home Assistant renders a list readably and a single run-on
    line not at all, and the wording is passed through untouched - translating it here
    would let this file drift away from what the node actually reported.
    """
    if not warnings:
        return []
    return [line.strip() for line in warnings.splitlines() if line.strip()]


class HomeKeyGuestTagsSensor(HomeKeyBaseSensor):
    """How many guest cards are taught to this node.

    The card list is an attribute, because a sensor's state has to be one value and the
    list is what actually makes the entity useful.

    Only non-secret fields are exposed. The node never publishes a card's per-tag
    secret - not on ``guest/status`` and not on ``/api/ha/guest`` - so there is nothing
    here that could be used to clone a card, and nothing this file has to remember to
    strip.

    ``None`` (unknown) until the node reports: an older firmware, or a poll that has not
    happened yet, is not the same claim as "no guest tags".
    """

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(
            coordinator, node_id, ENTITY_SENSOR_GUEST_TAGS, lambda node: None
        )
        self._attr_native_unit_of_measurement = "tags"

    @property
    def native_value(self) -> int | None:
        node = self._node()
        guest = node.guest if node else None
        return guest.count if guest is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attrs = dict(super().extra_state_attributes)
        node = self._node()
        guest = node.guest if node else None
        if guest is None:
            return attrs
        attrs.update(
            {
                "tags": [
                    {
                        "tag_id": tag.tag_id,
                        "label": tag.label,
                        "uid": tag.uid,
                        "enabled": tag.enabled,
                        "expires": tag.expires,
                        "last_used_at": tag.last_used_at or None,
                        "use_count": tag.use_count,
                    }
                    for tag in guest.tags
                ],
                "capacity": guest.capacity,
                "default_validity_days": round(guest.default_validity_days, 2),
                # The card-writer state, so the UI can explain why a teach command did
                # nothing: armed and waiting, out of slots, or unsupported reader.
                "write_armed": guest.write_armed,
                "write_supported": guest.can_write,
                "last_write_result": guest.last_write_result,
                "last_write_message": guest.last_write_message,
                "node_has_wall_clock": guest.has_wall_clock,
            }
        )
        return attrs
