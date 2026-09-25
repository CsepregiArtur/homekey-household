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

from .backup import BACKUP_STORE_KEY, BackupStore, StoredBackup
from .const import (
    DOMAIN,
    ENTITY_SENSOR_BACKUP,
    ENTITY_SENSOR_FIRMWARE,
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
    """Build the five documented sensors for one node."""
    return [
        HomeKeyHealthSensor(coordinator, node_id),
        HomeKeyBackupSensor(coordinator, node_id),
        HomeKeySecuritySensor(coordinator, node_id),
        HomeKeyFirmwareSensor(coordinator, node_id),
        HomeKeyLastAuthSensor(coordinator, node_id),
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
            lambda node: (
                node.backup.status
                if node.backup
                else node.backup_status
            ),
            options=_BACKUP_OPTIONS,
        )

    def _stored_backups(self) -> list[StoredBackup]:
        """Copies held for this node, newest last."""
        store = self.hass.data.get(DOMAIN, {}).get(BACKUP_STORE_KEY)
        if not isinstance(store, BackupStore):
            return []
        return store.for_node(self._node_id)

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
            if latest.node_time is not None:
                attributes["stored_node_time"] = latest.node_time
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
