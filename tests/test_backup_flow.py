"""The backup/restore flow, as it is visible in Home Assistant.

The flow is three facts that only mean something together: is there a copy, does it
carry the node's keys, and can this entry reach the node at all. These tests hold the
entities and diagnostics to reporting those - and to never reporting the blobs
themselves.

The distinction that matters most, and that is easy to get wrong: what the **node**
says about its last backup is not what Home Assistant **holds**. ``backup/status:
completed`` means a backup was produced, not that one is stored anywhere - the device
keeps only the time and hash of the last one.
"""

from __future__ import annotations

import json

from custom_components.homekey_household.backup import (
    BACKUP_STORE_KEY,
    BackupStore,
    StoredBackup,
)
from custom_components.homekey_household.const import (
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    TOPIC_BACKUP_LAST,
    TOPIC_STATUS,
)
from custom_components.homekey_household.coordinator import (
    HomeKeyHouseholdCoordinator,
)
from custom_components.homekey_household.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.homekey_household.sensor import HomeKeyBackupSensor
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry, make_message

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"
ENTRY_ID = "entry-1"
BLOB = "ab" * 16

API_ACCESS = {
    CONF_HOST: "192.0.2.10",
    CONF_FINGERPRINT: "AA:BB",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "password",
}


class _FakeStorage:
    """Stands in for the HA store so no file is written."""

    async def async_load(self) -> dict | None:
        return None

    async def async_save(self, data: dict) -> None:
        return None


class _FakeCredentialStore:
    """No credential is configured: just enough for diagnostics to read."""

    def get(self, household_id: str):
        return None


class _FakeRuntime:
    """The part of HomeKeyRuntime the backup sensor and diagnostics read."""

    def __init__(self, entry: FakeConfigEntry, coordinator: HomeKeyHouseholdCoordinator) -> None:
        self.config_entry = entry
        self.coordinator = coordinator
        self.transport = None
        self.credential_store = _FakeCredentialStore()


def _wire(entity: HomeKeyBackupSensor, hass) -> HomeKeyBackupSensor:
    """Give a bare entity the hass an added entity would have.

    The entity reads the backup store out of ``hass.data``, which in production is set by
    the platform before any state is written.
    """
    entity.hass = hass
    return entity


def backup_payload(status: str = "completed") -> str:
    return json.dumps({"status": status, "timestamp": "2026-09-27T10:00:00+00:00"})


async def make_sensor(hass, node_id: str = NID) -> HomeKeyBackupSensor:
    """A backup sensor for a node, with the store wired the way the integration wires it."""
    entry = FakeConfigEntry(entry_id=ENTRY_ID, data={**API_ACCESS, "household_id": HID})
    coordinator = HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)
    hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = _FakeRuntime(entry, coordinator)
    store = BackupStore(hass, store=_FakeStorage())
    await store.async_load()
    hass.data.setdefault(DOMAIN, {})[BACKUP_STORE_KEY] = store

    await coordinator.async_handle_message(
        make_message(
            "state",
            json.dumps(
                {
                    "household_id": HID,
                    "node_id": node_id,
                    "node_name": "Gate",
                    "node_role": "gate",
                    "node_state": "ACTIVE",
                    "generation": 1,
                    "firmware_version": "0.11.0",
                }
            ),
        )
    )
    await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
    return _wire(HomeKeyBackupSensor(coordinator, node_id), hass)


async def add_copy(
    hass, *, includes_credentials: bool, created: str = "2026-09-27T10:00:00+00:00"
) -> None:
    store: BackupStore = hass.data[DOMAIN][BACKUP_STORE_KEY]
    await store.async_add(
        StoredBackup(
            node_id=NID,
            household_id=HID,
            created=created,
            node_time=1790370000,
            blob=BLOB,
            includes_credentials=includes_credentials,
        )
    )


class TestNodeReportVersusWhatIsHeld:
    """The node's verdict and the stored copies are different facts."""

    async def test_the_node_saying_completed_is_not_a_stored_backup(self, hass):
        sensor = await make_sensor(hass)
        await sensor.coordinator.async_handle_message(
            make_message(TOPIC_BACKUP_LAST, backup_payload("completed"))
        )

        attributes = sensor.extra_state_attributes
        assert sensor.native_value == "completed"
        # The node reported one; Home Assistant holds none. Reporting these as the same
        # thing is how a household ends up with no backup and a green light.
        assert attributes["stored_backups"] == 0
        assert attributes["restore"]["available"] is False
        assert attributes["restore"]["from_credentials_backup"] is None


class TestRestoreBlock:
    """What a restore would use, and what it would still need."""

    async def test_a_configuration_only_copy_is_reported_as_such(self, hass):
        sensor = await make_sensor(hass)
        await add_copy(hass, includes_credentials=False)

        restore = sensor.extra_state_attributes["restore"]
        assert restore["available"] is True
        assert restore["from_credentials_backup"] is False

    async def test_a_credential_copy_is_reported_as_such(self, hass):
        sensor = await make_sensor(hass)
        await add_copy(hass, includes_credentials=True)

        restore = sensor.extra_state_attributes["restore"]
        assert restore["available"] is True
        assert restore["from_credentials_backup"] is True

    async def test_the_secret_is_always_still_required(self, hass):
        """The integration never keeps the recovery secret, so it never claims to."""
        sensor = await make_sensor(hass)
        await add_copy(hass, includes_credentials=True)

        assert sensor.extra_state_attributes["restore"]["recovery_secret_required"] is True
        assert (
            sensor.extra_state_attributes["restore"]["service"]
            == "homekey_household.restore_backup"
        )


class TestStoredCopiesDetail:
    """The whole window is visible, not only its newest entry."""

    async def test_every_copy_is_listed_with_its_shape(self, hass):
        sensor = await make_sensor(hass)
        await add_copy(hass, includes_credentials=False, created="2026-09-25T10:00:00+00:00")
        await add_copy(hass, includes_credentials=True, created="2026-09-27T10:00:00+00:00")

        attributes = sensor.extra_state_attributes
        assert attributes["stored_backups"] == 2
        # The newest decides what a restore would use...
        assert attributes["stored_includes_credentials"] is True
        # ...but the older, configuration-only copy is still visible as such.
        detail = attributes["stored_backups_detail"]
        assert [copy["includes_credentials"] for copy in detail] == [False, True]

    async def test_no_blob_appears_in_the_attributes(self, hass):
        """The entity carries metadata; the encrypted backup stays in the store."""
        sensor = await make_sensor(hass)
        await add_copy(hass, includes_credentials=True)

        dump = json.dumps(sensor.extra_state_attributes, default=str)
        assert BLOB not in dump
        # The size is useful; the ciphertext is not offered.
        assert sensor.extra_state_attributes["stored_backups_detail"][0]["hex_bytes"] == len(
            BLOB
        ) // 2


class TestApiReachability:
    """A backup or a restore travels over the node's API, and that can be absent."""

    async def test_configured_when_the_entry_has_an_address_and_credentials(self, hass):
        sensor = await make_sensor(hass)
        assert sensor.extra_state_attributes["api_configured"] is True

    async def test_not_configured_without_them(self, hass):
        entry = FakeConfigEntry(entry_id=ENTRY_ID)  # MQTT entry: no API details
        coordinator = HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)
        hass.data.setdefault(DOMAIN, {})[ENTRY_ID] = _FakeRuntime(entry, coordinator)
        hass.data[DOMAIN][BACKUP_STORE_KEY] = BackupStore(hass, store=_FakeStorage())
        await coordinator.async_handle_message(
            make_message(TOPIC_STATUS, "online")
        )
        sensor = _wire(HomeKeyBackupSensor(coordinator, NID), hass)

        assert sensor.extra_state_attributes["api_configured"] is False


class TestDiagnostics:
    """Support can see the flow without being handed the backups."""

    async def test_the_store_is_summarised(self, hass):
        await make_sensor(hass)
        await add_copy(hass, includes_credentials=False, created="2026-09-25T10:00:00+00:00")
        await add_copy(hass, includes_credentials=True, created="2026-09-27T10:00:00+00:00")

        result = await async_get_config_entry_diagnostics(
            hass, hass.data[DOMAIN][ENTRY_ID].config_entry
        )
        summary = result["backup_store"]
        assert summary["available"] is True
        assert summary["count"] == 2
        assert summary["newest_created"] == "2026-09-27T10:00:00+00:00"
        assert summary["any_includes_credentials"] is True

    async def test_no_blob_or_key_material_is_reported(self, hass):
        await make_sensor(hass)
        await add_copy(hass, includes_credentials=True)

        result = await async_get_config_entry_diagnostics(
            hass, hass.data[DOMAIN][ENTRY_ID].config_entry
        )
        dump = json.dumps(result, default=str)
        assert BLOB not in dump
        # The option is reported - so support can see which shape the schedule
        # produces - while the secret never is.
        assert "backup_include_credentials" in result["entry"]["options"]
        assert "recovery_secret" not in dump

    async def test_the_option_is_reported_even_when_off(self, hass):
        """Off is a real answer, and the one most households should see.

        The key is always present in the output: an absent key would be
        indistinguishable from a diagnostic that forgot to report it.
        """
        await make_sensor(hass)
        result = await async_get_config_entry_diagnostics(
            hass, hass.data[DOMAIN][ENTRY_ID].config_entry
        )
        assert "backup_include_credentials" in result["entry"]["options"]
        # Absent from the entry's options, so reported as None rather than invented.
        assert result["entry"]["options"]["backup_include_credentials"] is None

    async def test_the_option_is_reported_when_on(self, hass):
        """The one fact that decides whether a copy is the keys to the door."""
        await make_sensor(hass)
        runtime = hass.data[DOMAIN][ENTRY_ID]
        runtime.config_entry.options = {"backup_include_credentials": True}

        result = await async_get_config_entry_diagnostics(hass, runtime.config_entry)

        assert result["entry"]["options"]["backup_include_credentials"] is True
