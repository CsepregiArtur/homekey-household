"""Pulling node backups into Home Assistant, on a schedule, and keeping them.

A backup exists only as the reply to ``POST /backup/create`` on the node's own API. The
device keeps the time and hash of the last one and nothing else, and the household MQTT
namespace has no topic that carries one - ``backup/request`` and ``backup/data`` are
documented in the topic list but nothing implements them. So a backup is only ever held by
whoever asked for it, which means a backup that depends on somebody remembering to open the
Web UI and click Download is not a backup at all. This module asks on a schedule instead.

What is stored is the encrypted blob exactly as the node produced it. It is encrypted and
signed under a key derived from the household recovery secret, and that secret is *not*
stored here, so this file on its own is ciphertext and configuration - it cannot be turned
back into a device without the secret the user keeps offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    BACKUP_INTERVAL_SECONDS,
    BACKUP_KEEP,
    BACKUP_STORE_VERSION,
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    DEFAULT_HTTPS_PORT,
    DOMAIN,
    SERVICE_CREATE_BACKUP,
)
from .direct import (
    DirectClient,
    DirectPoller,
    DirectTransportError,
    async_connect_node,
)

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.backups"

# Reserves one key in hass.data[DOMAIN] alongside the per-entry runtimes. Config entry ids
# are ULIDs, so this can never collide with one.
BACKUP_STORE_KEY = "backup_store"


@dataclass(frozen=True)
class StoredBackup:
    """One backup, kept as the node produced it."""

    node_id: str
    household_id: str
    created: str
    """When Home Assistant stored it, ISO-8601 UTC."""
    node_time: int | None
    """The node's own stamp for the backup it handed over, when it reported one."""
    blob: str
    """The encrypted backup, hex, byte for byte as the node returned it."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "household_id": self.household_id,
            "created": self.created,
            "node_time": self.node_time,
            "blob": self.blob,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> StoredBackup | None:
        """Build from stored data, or ``None`` when it is not usable.

        Defensive on purpose: this file is user-visible and hand-editable, and one bad
        record must not stop the rest of a household's backups from loading.
        """
        if not isinstance(raw, dict):
            return None
        blob = raw.get("blob")
        node_id = raw.get("node_id")
        if not isinstance(blob, str) or not blob or not isinstance(node_id, str) or not node_id:
            return None
        node_time = raw.get("node_time")
        return cls(
            node_id=node_id,
            household_id=str(raw.get("household_id") or ""),
            created=str(raw.get("created") or ""),
            node_time=node_time if isinstance(node_time, int) else None,
            blob=blob,
        )


class BackupStore:
    """The newest few backups per node, newest last."""

    def __init__(
        self,
        hass: HomeAssistant,
        keep: int = BACKUP_KEEP,
        store: Store | None = None,
    ) -> None:
        # ``store`` is injectable so the retention rules can be exercised without a running
        # Home Assistant; nothing but the persistence layer is substituted.
        self._store: Store = store if store is not None else Store(hass, BACKUP_STORE_VERSION, STORE_KEY)
        self._keep = keep
        self._backups: list[StoredBackup] = []

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        entries = raw.get("backups") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            self._backups = []
            return
        loaded = [backup for backup in (StoredBackup.from_dict(e) for e in entries) if backup]
        dropped = len(entries) - len(loaded)
        if dropped:
            _LOGGER.warning("Ignored %d unreadable backup record(s) in %s", dropped, STORE_KEY)
        self._backups = loaded

    @property
    def backups(self) -> list[StoredBackup]:
        return list(self._backups)

    def for_node(self, node_id: str) -> list[StoredBackup]:
        return [backup for backup in self._backups if backup.node_id == node_id]

    def latest(self, node_id: str | None = None) -> StoredBackup | None:
        """The newest backup, for one node or across all of them."""
        candidates = (
            self._backups
            if node_id is None
            else [b for b in self._backups if b.node_id == node_id]
        )
        return candidates[-1] if candidates else None

    async def async_add(self, backup: StoredBackup) -> None:
        """Keep a backup and drop the ones that fell out of the window.

        Every backup is stored, including one identical in effect to its predecessor: the
        node's blobs always differ (nonce and timestamp), and a rolling window is only
        useful if it is a window over *time*. A schedule that skipped "unchanged" backups
        would keep whatever was newest the last time something changed, which is exactly
        the copy that is least likely to be current when it is needed.
        """
        self._backups.append(backup)
        self._prune(backup.node_id)
        await self._store.async_save({"backups": [b.as_dict() for b in self._backups]})

    def _prune(self, node_id: str) -> None:
        """Keep the newest ``keep`` for one node, leaving other nodes untouched."""
        for_node = [b for b in self._backups if b.node_id == node_id]
        excess = len(for_node) - self._keep
        if excess <= 0:
            return
        doomed = {id(b) for b in for_node[:excess]}
        self._backups = [b for b in self._backups if id(b) not in doomed]


def backup_client_for(entry_data: dict[str, Any]) -> bool:
    """Whether these settings are enough to ask the node for a backup.

    The node's own API is a separate thing from the transport that carries its events: a
    backup needs an address, the Web UI credentials and the certificate fingerprint to pin,
    and an entry created over MQTT has none of those. Rather than guess or reach the node
    unpinned, such an entry simply is not backed up, and the service says so.
    """
    return all(
        [
            entry_data.get(CONF_HOST),
            entry_data.get(CONF_FINGERPRINT),
            entry_data.get(CONF_USERNAME),
            entry_data.get(CONF_PASSWORD),
        ]
    )


def entry_backup_settings(runtime: Any) -> dict[str, Any]:
    """The settings a backup needs, from the entry that owns the node.

    Options win over data: for an entry created over MQTT, the node's address and Web UI
    credentials have nowhere else to live.
    """
    entry = getattr(runtime, "config_entry", None)
    data: dict[str, Any] = {}
    if entry is not None:
        data.update(entry.data)
        data.update(entry.options)
    data["household_id"] = getattr(getattr(runtime, "coordinator", None), "household_id", "")
    return data


async def async_fetch_backup(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    *,
    node_id: str | None = None,
    running_client: DirectClient | None = None,
) -> StoredBackup:
    """Ask a node for a backup and return it.

    ``running_client`` is the client of a live direct entry, whose certificate has already
    been re-verified at startup. Without one, the connection is established here and the
    fingerprint is checked against the pinned value before a credential is sent - the same
    order of operations the config flow and entry setup use.
    """
    if running_client is not None:
        client = running_client
    else:
        probe = await async_connect_node(
            hass.async_add_executor_job,
            _session_for(hass),
            host=str(entry_data[CONF_HOST]),
            port=int(entry_data.get(CONF_PORT, DEFAULT_HTTPS_PORT)),
            expected_fingerprint=str(entry_data[CONF_FINGERPRINT]),
            username=str(entry_data[CONF_USERNAME]),
            password=str(entry_data[CONF_PASSWORD]),
        )
        client = probe.client
        node_id = probe.node_id

    blob = await client.async_create_backup()
    return StoredBackup(
        node_id=node_id or "",
        household_id=str(entry_data.get("household_id") or ""),
        created=datetime.now(UTC).isoformat(),
        node_time=await _async_node_backup_time(client),
        blob=blob,
    )


async def _async_node_backup_time(client: DirectClient) -> int | None:
    """The node's own stamp for the last backup, or ``None`` if it will not say.

    A backup's own timestamp is inside the encrypted payload, so the node's ``/backup``
    summary is the only way to date one without the recovery secret. Best-effort: a node
    that does not answer must not lose us the backup we already hold.
    """
    try:
        info = await client.async_get_backup_info()
    except DirectTransportError as err:
        _LOGGER.debug("Could not read the node's backup summary: %s", err)
        return None
    value = info.get("last_backup_time")
    return value if isinstance(value, int) and value > 0 else None


async def async_setup_backups(hass: HomeAssistant) -> None:
    """Register the backup service and start the schedule."""
    store = BackupStore(hass)
    await store.async_load()
    hass.data.setdefault(DOMAIN, {})[BACKUP_STORE_KEY] = store

    async def _back_up_now(call: Any) -> None:
        entry_ids = _target_entry_ids(hass, call.data)
        if not entry_ids:
            _LOGGER.warning(
                "No HomeKey entry can be backed up: one needs the node's address, its "
                "certificate fingerprint and its Web UI credentials (%s)",
                "set them under Configure on the entry",
            )
            return
        for entry_id in entry_ids:
            await async_back_up_entry(hass, store, entry_id)

    hass.services.async_register(DOMAIN, SERVICE_CREATE_BACKUP, _back_up_now)

    async def _scheduled(_now: datetime) -> None:
        for entry_id in _target_entry_ids(hass, {}):
            await async_back_up_entry(hass, store, entry_id)

    # Held for the life of the integration: unsubscribing would stop the schedule, and
    # nothing else cancels it, because the store and the entries outlive any single one.
    async_track_time_interval(hass, _scheduled, timedelta(seconds=BACKUP_INTERVAL_SECONDS))


def _target_entry_ids(hass: HomeAssistant, data: dict[str, Any]) -> list[str]:
    """Entries a backup can be taken for, or just the one that was asked for."""
    requested = data.get("config_entry_id")
    entry_ids: list[str] = []
    for entry_id, runtime in hass.data.get(DOMAIN, {}).items():
        if entry_id == BACKUP_STORE_KEY:
            continue
        if requested and entry_id != requested:
            continue
        if backup_client_for(entry_backup_settings(runtime)):
            entry_ids.append(entry_id)
    return entry_ids


async def async_back_up_entry(hass: HomeAssistant, store: BackupStore, entry_id: str) -> None:
    """Take one backup for one entry, logging rather than raising on failure.

    A scheduled job that raises is a job that stops being useful, and a node that is
    briefly unreachable is not an error worth failing a service call over.
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry_id)
    entry = getattr(runtime, "config_entry", None)
    if runtime is None or entry is None:
        return
    entry_data = entry_backup_settings(runtime)

    transport = getattr(runtime, "transport", None)
    running_client = transport.client if isinstance(transport, DirectPoller) else None

    try:
        backup = await async_fetch_backup(
            hass,
            entry_data,
            node_id=getattr(transport, "node_id", None),
            running_client=running_client,
        )
    except DirectTransportError as err:
        _LOGGER.warning("Could not back up the node for entry %s: %s", entry_id, err)
        return

    await store.async_add(backup)
    _LOGGER.info(
        "Stored a backup of %s (%d bytes, %d kept)",
        backup.node_id or backup.household_id,
        len(backup.blob),
        len(store.for_node(backup.node_id)),
    )


def _session_for(hass: HomeAssistant) -> aiohttp.ClientSession:
    """The shared client session, imported lazily to keep this module HA-optional."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return async_get_clientsession(hass)
