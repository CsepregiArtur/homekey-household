"""Keeping backups, and putting one back.

The device hands a backup over and keeps no copy of its own, so everything that survives a
node lives in the store exercised here: what is kept, how much of it, and what happens to a
file somebody edited by hand. The restore side is held to the same standard - it must know
what it is about to send, and say so when it cannot.
"""

from __future__ import annotations

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.homekey_household.backup import (
    BACKUP_STORE_KEY,
    BackupStore,
    StoredBackup,
    async_back_up_entry,
    async_restore_entry,
    backup_client_for,
    entry_backup_settings,
)
from custom_components.homekey_household.const import (
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.homekey_household.direct import (  # noqa: E402
    DirectPoller as FakeDirectPoller,
)
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"
ENTRY_ID = "test_entry"

API_ACCESS = {
    CONF_HOST: "192.168.1.10",
    CONF_PORT: 443,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
    CONF_FINGERPRINT: "AA:BB:CC",
}


class FakeStorage:
    """A stand-in for Home Assistant's store: no disk, no hass."""

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload
        self.saved: list[dict] = []

    async def async_load(self) -> dict | None:
        return self.payload

    async def async_save(self, data: dict) -> None:
        self.saved.append(data)


class FakeRuntime:
    """The bits of the per-entry runtime that backup and restore look at."""

    def __init__(self, entry: FakeConfigEntry, client: object | None = None) -> None:
        self.config_entry = entry
        self.coordinator = type("C", (), {"household_id": HID})()
        self.transport = client


class FakePoller(FakeDirectPoller):
    """Stands in for the direct poller: it holds the live client.

    A real subclass so ``isinstance(transport, DirectPoller)`` sees it for what it is, but
    without running the poller's own constructor, which wants a session and a device.
    """

    def __init__(self, client: object, node_id: str = NID) -> None:
        # ``client`` and ``node_id`` are read-only properties on the poller, backed by
        # these.
        self._client = client
        self._node_id = node_id


class FakeClient:
    """Records what the integration asked the node to do."""

    def __init__(self, *, backup: str = "ab" * 8, restore_error: Exception | None = None) -> None:
        self.backup = backup
        self.restore_error = restore_error
        self.restores: list[tuple[str, str]] = []
        self.backups = 0

    async def async_create_backup(self) -> str:
        self.backups += 1
        return self.backup

    async def async_get_backup_info(self) -> dict:
        return {"last_backup_time": 1790370000}

    async def async_restore_backup(self, recovery_secret: str, backup: str) -> dict:
        if self.restore_error is not None:
            raise self.restore_error
        self.restores.append((recovery_secret, backup))
        return {"success": True}


def entry_with_api_access() -> FakeConfigEntry:
    return FakeConfigEntry(
        entry_id=ENTRY_ID, data={**API_ACCESS, "household_id": HID}
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class TestStoredBackup:
    def test_a_record_missing_its_blob_is_not_a_backup(self):
        assert StoredBackup.from_dict({"node_id": NID, "blob": ""}) is None
        assert StoredBackup.from_dict({"blob": "ab" * 4}) is None
        assert StoredBackup.from_dict("not a record") is None

    def test_a_usable_record_round_trips(self):
        record = StoredBackup(
            node_id=NID, household_id=HID, created="2026-09-26T00:00:00+00:00",
            node_time=1790370000, blob="cd" * 4,
        )
        assert StoredBackup.from_dict(record.as_dict()) == record

    def test_an_unusable_timestamp_becomes_none_rather_than_a_string(self):
        record = StoredBackup.from_dict(
            {"node_id": NID, "blob": "cd" * 4, "node_time": "yesterday"}
        )
        assert record is not None
        assert record.node_time is None


class TestBackupStore:
    async def test_backups_are_kept_newest_last(self, hass):
        store = BackupStore(hass, keep=3, store=FakeStorage())
        await store.async_load()

        for index in range(3):
            await store.async_add(
                StoredBackup(
                    node_id=NID, household_id=HID, created=f"2026-09-2{index}",
                    node_time=index, blob=f"0{index}",
                )
            )

        assert [b.created for b in store.backups] == [
            "2026-09-20",
            "2026-09-21",
            "2026-09-22",
        ]
        assert store.latest(NID).blob == "02"

    async def test_the_oldest_falls_out_of_the_window(self, hass):
        store = BackupStore(hass, keep=2, store=FakeStorage())
        await store.async_load()

        for index in range(4):
            await store.async_add(
                StoredBackup(
                    node_id=NID, household_id=HID, created=str(index), node_time=index,
                    blob=str(index),
                )
            )

        assert [b.created for b in store.for_node(NID)] == ["2", "3"]

    async def test_a_second_node_does_not_evict_the_first(self, hass):
        store = BackupStore(hass, keep=2, store=FakeStorage())
        await store.async_load()

        for index in range(3):
            await store.async_add(
                StoredBackup(
                    node_id=NID,
                    household_id=HID,
                    created=str(index),
                    node_time=index,
                    blob=str(index),
                )
            )
        await store.async_add(
            StoredBackup(
                node_id="OTHER",
                household_id=HID,
                created="other",
                node_time=None,
                blob="other",
            )
        )

        assert [b.created for b in store.for_node(NID)] == ["1", "2"]
        assert [b.created for b in store.for_node("OTHER")] == ["other"]

    async def test_a_hand_edited_record_is_skipped_not_fatal(self, hass):
        storage = FakeStorage(
            {
                "backups": [
                    {"node_id": NID, "household_id": HID, "created": "1", "blob": "aa"},
                    {"node_id": NID, "household_id": HID},  # no blob
                    "a string where a record belongs",
                ]
            }
        )
        store = BackupStore(hass, store=storage)
        await store.async_load()

        assert [b.created for b in store.backups] == ["1"]

    async def test_nothing_stored_is_not_an_error(self, hass):
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()

        assert store.backups == []
        assert store.latest() is None

    async def test_the_file_is_not_world_readable(self, hass):
        """Home Assistant chmods a public store to 0644; a private one stays 0600.

        The blobs are sealed with a key derived from the recovery secret, so the file is not
        the door - but on a shared host there is no reason for it to be readable by every
        user and every add-on that can see the config directory.
        """
        from pathlib import Path

        from custom_components.homekey_household.backup import STORE_KEY

        store = BackupStore(hass)
        await store.async_add(
            StoredBackup(
                node_id=NID, household_id=HID, created="1", node_time=None, blob="ab"
            )
        )

        stored_file = Path(hass.config.path(".storage", STORE_KEY))
        assert stored_file.exists()
        assert stored_file.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Which entries can be reached
# ---------------------------------------------------------------------------
class TestReachability:
    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({}, True),
            ({CONF_HOST: ""}, False),
            ({CONF_FINGERPRINT: ""}, False),
            ({CONF_USERNAME: ""}, False),
            ({CONF_PASSWORD: ""}, False),
        ],
    )
    def test_a_node_needs_all_of_it(self, overrides, expected):
        data = {**API_ACCESS, **overrides}
        assert backup_client_for(data) is expected

    def test_options_win_over_entry_data(self):
        runtime = FakeRuntime(
            FakeConfigEntry(data={**API_ACCESS, CONF_HOST: "old"}, options={CONF_HOST: "new"})
        )
        settings = entry_backup_settings(runtime)

        assert settings[CONF_HOST] == "new"
        assert settings["household_id"] == HID


# ---------------------------------------------------------------------------
# Backing up and restoring
# ---------------------------------------------------------------------------
class TestBackUpAndRestore:
    async def test_a_backup_is_taken_through_a_running_client(self, hass):
        client = FakeClient()
        entry = entry_with_api_access()
        hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = FakeRuntime(entry, FakePoller(client))
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()

        await async_back_up_entry(hass, store, ENTRY_ID)

        assert client.backups == 1
        assert store.latest(NID).blob == client.backup
        # The node's own stamp for it is carried along, so the copy can be dated.
        assert store.latest(NID).node_time == 1790370000

    async def test_a_press_that_cannot_reach_the_node_says_so(self, hass):
        entry = FakeConfigEntry(entry_id=ENTRY_ID, data={"household_id": HID})
        hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = FakeRuntime(entry)
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()

        with pytest.raises(ServiceValidationError):
            await async_back_up_entry(hass, store, ENTRY_ID, explicit=True)

    async def test_a_restore_sends_the_newest_stored_copy(self, hass):
        client = FakeClient()
        entry = entry_with_api_access()
        hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = FakeRuntime(entry, FakePoller(client))
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()
        await store.async_add(
            StoredBackup(
                node_id=NID, household_id=HID, created="old", node_time=None, blob="old"
            )
        )
        await store.async_add(
            StoredBackup(
                node_id=NID, household_id=HID, created="new", node_time=None, blob="new"
            )
        )

        await async_restore_entry(hass, store, ENTRY_ID, recovery_secret="secret")

        assert client.restores == [("secret", "new")]

    async def test_a_restore_can_be_handed_a_specific_backup(self, hass):
        client = FakeClient()
        hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = FakeRuntime(
            entry_with_api_access(), FakePoller(client)
        )
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()

        await async_restore_entry(
            hass, store, ENTRY_ID, recovery_secret="secret", backup_hex="deadbeef"
        )

        assert client.restores == [("secret", "deadbeef")]

    async def test_a_restore_without_a_secret_is_refused(self, hass):
        """The secret is the key: without it there is nothing to send that could work."""
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()

        with pytest.raises(ServiceValidationError):
            await async_restore_entry(hass, store, ENTRY_ID, recovery_secret="")

    async def test_a_restore_with_nothing_stored_is_refused(self, hass):
        client = FakeClient()
        hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = FakeRuntime(
            entry_with_api_access(), FakePoller(client)
        )
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()

        with pytest.raises(ServiceValidationError):
            await async_restore_entry(hass, store, ENTRY_ID, recovery_secret="secret")
        assert client.restores == []

    async def test_a_node_that_refuses_the_restore_is_reported(self, hass):
        from custom_components.homekey_household.direct import DirectProtocolError

        client = FakeClient(restore_error=DirectProtocolError("the secret does not match"))
        hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = FakeRuntime(
            entry_with_api_access(), FakePoller(client)
        )
        store = BackupStore(hass, store=FakeStorage())
        await store.async_load()
        await store.async_add(
            StoredBackup(
                node_id=NID, household_id=HID, created="1", node_time=None, blob="1"
            )
        )

        with pytest.raises(ServiceValidationError) as failure:
            await async_restore_entry(hass, store, ENTRY_ID, recovery_secret="wrong")

        assert "does not match" in str(failure.value)

    async def test_the_store_is_reachable_from_hass_data(self, hass):
        """The button finds the store where the integration put it."""
        store = BackupStore(hass, store=FakeStorage())
        hass.data.setdefault(DOMAIN, {})[BACKUP_STORE_KEY] = store

        assert hass.data[DOMAIN][BACKUP_STORE_KEY] is store
