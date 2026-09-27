"""Guest NFC tag tests.

Guest tags are a locally verified credential on an ordinary card, so the things
worth testing are the claims that could quietly be wrong:

1. The status document is parsed exactly as the firmware emits it - including the
   difference between "guest access is off" and "the node has not told us yet".
2. Nothing that could clone a card is ever modelled. The node does not publish a
   card's per-tag secret, and the integration must not invent or expect one.
3. Guest *state* arrives over both transports, but the writes need the direct API
   and say so rather than failing obscurely on MQTT.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from custom_components.homekey_household.button import HomeKeyGuestTeachButton
from custom_components.homekey_household.const import (
    DOMAIN,
    GUEST_SECONDS_PER_DAY,
    TOPIC_GUEST_STATUS,
    TOPIC_STATUS,
)
from custom_components.homekey_household.coordinator import (
    HomeKeyHouseholdCoordinator,
)
from custom_components.homekey_household.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.homekey_household.models import (
    GuestState,
    GuestTag,
    ValidationError,
)
from custom_components.homekey_household.sensor import HomeKeyGuestTagsSensor
from custom_components.homekey_household.switch import HomeKeyGuestAccessSwitch
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry, make_message

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"

# The token-free document the firmware returns from GET /api/ha/guest and publishes
# on the guest/status subtopic. Copied from the firmware's own projection, so this
# test fails if the two drift apart.
GUEST_STATUS: dict = {
    "enabled": True,
    "default_validity_seconds": 604800,
    "capacity": 16,
    "count": 1,
    "wall_clock": 1780000000,
    "write": {
        "armed": False,
        "can_write": True,
        "last_result": "success",
        "last_message": "Card written and verified",
        "last_tag_id": "A1B2C3D4",
    },
    "tags": [
        {
            "tag_id": "A1B2C3D4",
            "uid": "04A1B2C3D4E5F6",
            "label": "Cleaner",
            "enabled": True,
            "valid_from": 1779990000,
            "valid_until": 1780600000,
            "created_at": 1779990000,
            "last_used_at": 0,
            "use_count": 0,
        }
    ],
}

# Two taught cards, deliberately different: one labelled with an expiry, one with
# ``valid_until`` 0 (never expires). The list is per node, and each card keeps its own
# window.
TWO_CARDS: dict = dict(
    GUEST_STATUS,
    count=2,
    tags=[
        dict(GUEST_STATUS["tags"][0]),
        dict(
            GUEST_STATUS["tags"][0],
            tag_id="11223344",
            uid="0411223344",
            label="Dog walker",
            valid_from=0,
            valid_until=0,
        ),
    ],
)


@pytest.fixture
def coordinator(hass):
    entry = FakeConfigEntry()
    return HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)


class TestGuestStateParsing:
    """The document is read exactly as the firmware writes it."""

    def test_parses_firmware_document(self):
        state = GuestState.from_dict(GUEST_STATUS)
        assert state.enabled is True
        assert state.count == 1
        assert state.capacity == 16
        assert state.has_wall_clock is True
        assert state.write_armed is False
        assert state.can_write is True
        assert state.last_write_result == "success"
        assert len(state.tags) == 1

        tag = state.tags[0]
        assert tag.tag_id == "A1B2C3D4"
        assert tag.label == "Cleaner"
        assert tag.uid == "04A1B2C3D4E5F6"
        assert tag.enabled is True

    def test_token_is_never_modelled(self):
        """A card's secret must not have a field to be filled in.

        The node never publishes it, so any field for it here would either be
        permanently empty or would have to be invented - and an invented one would be
        enough to clone a card.
        """
        fields = {f.name for f in dataclasses.fields(GuestTag)}
        assert "token" not in fields
        assert not hasattr(GuestState.from_dict(GUEST_STATUS).tags[0], "token")

    def test_absent_write_block_leaves_can_write_unknown(self):
        """Over MQTT there is no card-writer block, and that is not ``False``.

        "This transport does not report writing" and "this reader cannot write" are
        different claims; reporting the second for the first would make a PN532 node
        look like a reader without the feature.
        """
        payload = {k: v for k, v in GUEST_STATUS.items() if k != "write"}
        state = GuestState.from_dict(payload)
        assert state.can_write is None
        assert state.write_armed is False
        assert state.last_write_result is None

    def test_no_tags_is_zero_not_missing(self):
        payload = dict(GUEST_STATUS, count=0, tags=[])
        state = GuestState.from_dict(payload)
        assert state.count == 0
        assert state.tags == ()

    def test_default_validity_days(self):
        assert GuestState.from_dict(GUEST_STATUS).default_validity_days == 7.0

    def test_no_expiry_is_not_an_expiry_in_the_past(self):
        """A tag with ``valid_until`` 0 never expires - the sentinel is not a date."""
        payload = dict(GUEST_STATUS)
        payload["tags"] = [dict(GUEST_STATUS["tags"][0], valid_until=0)]
        tag = GuestState.from_dict(payload).tags[0]
        assert tag.expires is None
        assert tag.expired_at(2_000_000_000) is False

    def test_expiry_is_judged_against_the_given_time(self):
        tag = GuestState.from_dict(GUEST_STATUS).tags[0]
        assert tag.expired_at(1780599999) is False
        assert tag.expired_at(1780600001) is True

    @pytest.mark.parametrize(
        "payload",
        [
            "not a dict",
            [],
            {"tags": "not a list"},
            # A tag without an id cannot be revoked or displayed, so it is refused
            # rather than silently given a blank one.
            {"tags": [{"label": "Cleaner"}]},
        ],
    )
    def test_malformed_payloads_are_rejected(self, payload):
        with pytest.raises(ValidationError):
            GuestState.from_dict(payload)


class TestGuestDispatch:
    """The subtopic reaches the node, over whichever transport carried it."""

    async def test_unknown_until_reported(self, coordinator):
        """An empty document is a real reading: nothing enabled, nothing taught."""
        await coordinator.async_handle_message(make_message(TOPIC_GUEST_STATUS, "{}"))
        node = coordinator.get_node(NID)
        assert node is not None
        assert node.guest is not None
        assert node.guest.enabled is False
        assert node.guest.count == 0
        assert node.guest.can_write is None

    async def test_guest_status_sets_node_state(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(GUEST_STATUS))
        )
        node = coordinator.get_node(NID)
        assert node is not None
        assert node.guest is not None
        assert node.guest.enabled is True
        assert node.guest.count == 1
        assert node.guest.tags[0].label == "Cleaner"

    async def test_guest_status_updates_in_place(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(GUEST_STATUS))
        )
        revoked = dict(GUEST_STATUS, count=0, tags=[], enabled=False)
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(revoked))
        )
        node = coordinator.get_node(NID)
        assert node is not None
        assert node.guest is not None
        assert node.guest.enabled is False
        assert node.guest.count == 0

    async def test_malformed_guest_status_is_rejected_quietly(self, coordinator):
        """A bad guest document is dropped, and the last good one survives.

        The coordinator's ingestion contract is to reject a malformed payload without
        raising, so neither the node's other state nor its previous guest reading is
        disturbed. A node whose guest payload is garbage is still a node that can be
        locked.
        """
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(GUEST_STATUS))
        )
        # Rejected without raising: a parse failure is not an availability failure.
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, '{"tags": "nope"}')
        )
        node = coordinator.get_node(NID)
        assert node is not None
        # The last valid reading survives the bad one.
        assert node.guest is not None
        assert node.guest.count == 1


class _RecordingClient:
    """A DirectClient stand-in that records what the coordinator asked for."""

    def __init__(self) -> None:
        self.config_calls: list[dict] = []
        self.teach_calls: list[dict] = []
        self.revoked: list[str] = []
        self.cancelled = 0

    async def async_set_guest_config(self, **kwargs):
        self.config_calls.append(kwargs)
        return {"success": True}

    async def async_guest_teach(self, **kwargs):
        self.teach_calls.append(kwargs)
        return {"success": True, "tag_id": "A1B2C3D4"}

    async def async_guest_revoke(self, tag_id: str):
        self.revoked.append(tag_id)
        return {"success": True}

    async def async_guest_cancel(self):
        self.cancelled += 1
        return {"success": True}


class _RecordingPoller:
    """A DirectPoller stand-in: it only needs to expose the client and a refresh."""

    def __init__(self) -> None:
        self.client = _RecordingClient()
        self.node_id = NID
        self.polls = 0

    async def async_poll_once(self) -> None:
        self.polls += 1


class TestGuestManageability:
    """Writes need the direct transport; state does not."""

    def test_mqtt_transport_cannot_manage(self, coordinator):
        assert coordinator.direct is None
        assert coordinator.guest_manageable is False

    def test_direct_transport_can_manage(self, coordinator):
        coordinator.direct = _RecordingPoller()
        assert coordinator.guest_manageable is True

    async def test_set_access_raises_without_direct(self, coordinator):
        with pytest.raises(ValidationError):
            await coordinator.async_set_guest_access(NID, enabled=True)

    async def test_teach_raises_without_direct(self, coordinator):
        with pytest.raises(ValidationError):
            await coordinator.async_teach_guest_tag(NID, label="Cleaner")

    async def test_revoke_raises_without_direct(self, coordinator):
        with pytest.raises(ValidationError):
            await coordinator.async_revoke_guest_tag(NID, "A1B2C3D4")


class TestGuestWriteTranslation:
    """The interface speaks days or nothing; the firmware speaks seconds."""

    async def test_days_are_converted_to_seconds(self, coordinator):
        poller = _RecordingPoller()
        coordinator.direct = poller
        await coordinator.async_set_guest_access(
            NID, enabled=True, default_validity_days=7
        )
        assert poller.client.config_calls == [
            {"enabled": True, "default_validity_seconds": 7 * GUEST_SECONDS_PER_DAY}
        ]

    async def test_enable_only_sends_no_validity(self, coordinator):
        """Toggling the switch must not silently reset the validity window."""
        poller = _RecordingPoller()
        coordinator.direct = poller
        await coordinator.async_set_guest_access(NID, enabled=False)
        assert poller.client.config_calls == [
            {"enabled": False, "default_validity_seconds": None}
        ]

    async def test_teach_forwards_label_and_window(self, coordinator):
        poller = _RecordingPoller()
        coordinator.direct = poller
        await coordinator.async_teach_guest_tag(NID, label="Cleaner", valid_days=3)
        assert poller.client.teach_calls == [{"label": "Cleaner", "valid_days": 3.0}]

    async def test_write_refreshes_the_node(self, coordinator):
        """A teach arms a ~60 s window, so the UI must see it immediately."""
        poller = _RecordingPoller()
        coordinator.direct = poller
        await coordinator.async_teach_guest_tag(NID)
        assert poller.polls == 1

    async def test_a_failed_refresh_does_not_hide_the_command(self, coordinator):
        """The command was delivered; a refresh failure is not a command failure."""

        class _BrokenRefresh(_RecordingPoller):
            async def async_poll_once(self) -> None:
                raise RuntimeError("node busy")

        poller = _BrokenRefresh()
        coordinator.direct = poller
        result = await coordinator.async_teach_guest_tag(NID)
        assert result["tag_id"] == "A1B2C3D4"


class TestGuestControlsAreHiddenWithoutTheFeature:
    """A control that can only fail must not be offered.

    A node whose firmware predates guest tags answers 404 on /api/ha/guest and
    publishes no guest/status. Offering a switch and two buttons that error on every
    press would read as "broken", when the truth is "this node has no guest feature".
    """

    async def test_hidden_until_the_node_reports_guest_state(self, coordinator):
        poller = _RecordingPoller()
        coordinator.direct = poller
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))

        switch = HomeKeyGuestAccessSwitch(coordinator, NID)
        teach = HomeKeyGuestTeachButton(coordinator, NID)
        assert switch.available is False
        assert teach.available is False
        assert switch.is_on is None

    async def test_shown_once_the_node_reports_guest_state(self, coordinator):
        poller = _RecordingPoller()
        coordinator.direct = poller
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(GUEST_STATUS))
        )

        switch = HomeKeyGuestAccessSwitch(coordinator, NID)
        teach = HomeKeyGuestTeachButton(coordinator, NID)
        assert switch.available is True
        assert teach.available is True
        assert switch.is_on is True

    async def test_hidden_on_a_transport_that_cannot_manage_them(self, coordinator):
        """MQTT reports guest state but cannot teach or revoke, so the controls hide."""
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(GUEST_STATUS))
        )

        switch = HomeKeyGuestAccessSwitch(coordinator, NID)
        # The node reported guest state, but there is no direct transport to write over.
        assert coordinator.guest_manageable is False
        assert switch.available is False


class TestMultipleTaughtCards:
    """A node holds up to 16 cards, and all of them must be listed.

    One arming teaches one card, so teaching three cards means arm-and-tap three
    times. Re-teaching a card that is already known refreshes its own entry rather
    than spending a second slot.
    """

    def test_every_taught_card_is_listed(self):
        state = GuestState.from_dict(TWO_CARDS)
        assert state.count == 2
        assert [tag.label for tag in state.tags] == ["Cleaner", "Dog walker"]
        assert [tag.tag_id for tag in state.tags] == ["A1B2C3D4", "11223344"]

    def test_each_card_keeps_its_own_window(self):
        state = GuestState.from_dict(TWO_CARDS)
        # One card expires, the other never does: the window is per card, not per node.
        assert state.tags[0].expires is not None
        assert state.tags[1].expires is None

    def test_the_capacity_is_reported_so_a_full_table_is_visible(self):
        payload = dict(TWO_CARDS, count=16, capacity=16)
        state = GuestState.from_dict(payload)
        assert state.count == state.capacity == 16

    async def test_the_sensor_lists_the_cards_and_the_count(self, coordinator):
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(TWO_CARDS))
        )
        sensor = HomeKeyGuestTagsSensor(coordinator, NID)

        assert sensor.native_value == 2
        attributes = sensor.extra_state_attributes
        assert attributes["capacity"] == 16
        assert [card["label"] for card in attributes["tags"]] == [
            "Cleaner",
            "Dog walker",
        ]
        assert attributes["tags"][0]["tag_id"] == "A1B2C3D4"


class _DiagRuntime:
    """The part of the runtime diagnostics reads."""

    def __init__(self, entry, coordinator) -> None:
        self.config_entry = entry
        self.coordinator = coordinator
        self.transport = None
        self.credential_store = _NoCredentials()


class _NoCredentials:
    def get(self, household_id: str):
        return None


class TestGuestDiagnostics:
    """The taught cards are readable from diagnostics, not only entity attributes."""

    async def test_the_taught_cards_are_reported(self, hass):
        entry = FakeConfigEntry(entry_id="guest-entry")
        coordinator = HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)
        hass.data.setdefault(DOMAIN, {})["guest-entry"] = _DiagRuntime(entry, coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(TWO_CARDS))
        )

        result = await async_get_config_entry_diagnostics(hass, entry)

        guest = result["nodes"][0]["guest"]
        assert guest["count"] == 2
        assert guest["capacity"] == 16
        assert [card["label"] for card in guest["tags"]] == ["Cleaner", "Dog walker"]
        assert guest["write"]["last_result"] == "success"

    async def test_nothing_that_could_clone_a_card_is_reported(self, hass):
        entry = FakeConfigEntry(entry_id="guest-entry")
        coordinator = HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)
        hass.data.setdefault(DOMAIN, {})["guest-entry"] = _DiagRuntime(entry, coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_GUEST_STATUS, json.dumps(TWO_CARDS))
        )

        dump = json.dumps(await async_get_config_entry_diagnostics(hass, entry))

        # The node never publishes a per-tag secret, and the model has no field for it.
        assert "token" not in dump

    async def test_a_node_without_guest_support_reports_none(self, hass):
        """``None`` is "not reported", which is not the same as "no cards"."""
        entry = FakeConfigEntry(entry_id="guest-entry")
        coordinator = HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)
        hass.data.setdefault(DOMAIN, {})["guest-entry"] = _DiagRuntime(entry, coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["nodes"][0]["guest"] is None


class TestNoCoercion:
    """Guarding against the tempting shortcut of guessing what a field meant."""

    def test_enabled_requires_an_explicit_true(self):
        """Only a real ``true`` means enabled.

        A missing field must not become ``True`` - that is the dangerous direction for
        a value that gates a door - and a wrong type is refused rather than coerced,
        because guessing what ``"true"`` meant is how a typo opens a lock.
        """
        assert GuestState.from_dict({}).enabled is False
        assert GuestState.from_dict({"enabled": None}).enabled is False
        assert GuestState.from_dict({"enabled": True}).enabled is True
        with pytest.raises(ValidationError):
            GuestState.from_dict({"enabled": "true"})
        with pytest.raises(ValidationError):
            GuestState.from_dict({"enabled": 1})
