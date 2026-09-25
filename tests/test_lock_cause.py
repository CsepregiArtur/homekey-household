"""Attributing a lock change to whatever caused it.

Two things are being established here, and the second is the subtle one:

1. ``B/lock/last`` is parsed strictly, and only a cause that belongs to *this* change is
   used - never the previous one, which would blame whatever happened last for what
   happened now.
2. A change Home Assistant asked for is left alone. Its service-call context is already
   pending on the entity and is consumed by the very write the change causes, which is
   what puts the user's name and "Action used: Lock lock" in the activity log. Overriding
   that with a vaguer description would be a regression, not an improvement.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.const import EVENT_LOGBOOK_ENTRY

from custom_components.homekey_household.const import (
    TOPIC_HEALTH,
    TOPIC_LAST_AUTH,
    TOPIC_LOCK_LAST,
    TOPIC_STATE,
    TOPIC_STATUS,
    LockSource,
    LockState,
)
from custom_components.homekey_household.coordinator import (
    HomeKeyHouseholdCoordinator,
)
from custom_components.homekey_household.models import (
    LastAuth,
    LockChange,
    ValidationError,
)
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry, make_message

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"

LOCK_UNLOCKED = 0
LOCK_LOCKED = 1


def lock_last(
    current: int, source: str, *, target: int | None = None, timestamp: int | None = None
) -> str:
    """Build a firmware-faithful ``B/lock/last`` payload."""
    payload: dict[str, object] = {"current": current, "source": source}
    if target is not None:
        payload["target"] = target
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return json.dumps(payload)


def last_auth(
    issuer: str | None = None, *, timestamp: int | None = None, result: str = "SUCCESS"
) -> str:
    """Build a firmware-faithful ``B/last_auth`` payload."""
    payload: dict[str, object] = {"type": "HomeKey", "result": result}
    if issuer is not None:
        payload["issuer"] = issuer
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return json.dumps(payload)


def health(current: int, **overrides: object) -> str:
    """Build a firmware-faithful ``B/health`` payload."""
    payload: dict[str, object] = {
        "network": "UNKNOWN",
        "mqtt": "OK",
        "mqtt_error": 0,
        "nfc": "OK",
        "lock_current": current,
        "lock_target": current,
        "backup": "ok",
        "certificate": "unknown",
        "firmware_version": "0.11.0",
        "uptime": 100,
        "free_heap": 100000,
        "reset_reason": "1",
        "security": {"all_ok": True, "warnings": ""},
    }
    payload.update(overrides)
    return json.dumps(payload)


def state(node_name: str = "Gate") -> str:
    return json.dumps(
        {
            "household_id": HID,
            "node_id": NID,
            "node_name": node_name,
            "node_role": "gate",
            "node_state": "ACTIVE",
            "generation": 1,
            "firmware_version": "0.11.0",
        }
    )


@pytest.fixture
def coordinator(hass):
    entry = FakeConfigEntry()
    return HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)


@pytest.fixture
def logbook_entries(hass):
    """Every logbook entry fired while the test runs, with its context."""
    captured: list[dict] = []

    def _capture(event) -> None:
        # The context lives on the event rather than in its data, and the context is the
        # whole point of the exercise, so it is folded in here.
        captured.append({**event.data, "context": event.context})

    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, _capture)
    return captured


async def register_node(coordinator) -> None:
    """Put the node on the books the way a retained state message would."""
    await coordinator.async_handle_message(make_message(TOPIC_STATE, state()))
    await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))


class TestLockChangeParsing:
    def test_parses_a_full_payload(self):
        change = LockChange.from_dict(
            {
                "current": 0,
                "target": 0,
                "source": "homekit",
                "timestamp": 1700000000,
            },
            HID,
            NID,
        )
        assert change.current == 0
        assert change.target == 0
        assert change.source == LockSource.HOMEKIT
        assert change.timestamp is not None

    def test_timestamp_and_target_are_optional(self):
        change = LockChange.from_dict({"current": 1, "source": "device"}, HID, NID)
        assert change.target is None
        assert change.timestamp is None

    @pytest.mark.parametrize("source", ["homekit", "homekey", "mqtt", "api", "device", "unknown"])
    def test_every_documented_source_is_accepted(self, source):
        assert LockChange.from_dict({"current": 0, "source": source}, HID, NID).source

    def test_an_unknown_source_is_rejected(self):
        # Named rather than quietly mapped onto a real origin: crediting the wrong thing
        # is worse than admitting the payload was not understood.
        with pytest.raises(ValidationError):
            LockChange.from_dict({"current": 0, "source": "telepathy"}, HID, NID)

    def test_current_is_required(self):
        # Without it the entry cannot be matched to the change it describes.
        with pytest.raises(ValidationError):
            LockChange.from_dict({"source": "homekit"}, HID, NID)

    def test_identity_is_checked_when_present(self):
        with pytest.raises(ValidationError):
            LockChange.from_dict(
                {"current": 0, "source": "homekit", "node_id": "OTHER"}, HID, NID
            )


class TestLastAuthIssuer:
    def test_issuer_name_is_read(self):
        auth = LastAuth.from_dict(
            {"type": "HomeKey", "result": "SUCCESS", "issuer": "Artur's iPhone"}, HID, NID
        )
        assert auth.issuer == "Artur's iPhone"

    def test_absent_issuer_is_none(self):
        # A device that has not been told any names publishes exactly what it always did.
        auth = LastAuth.from_dict({"type": "HomeKey", "result": "SUCCESS"}, HID, NID)
        assert auth.issuer is None

    def test_a_quote_in_the_name_is_not_a_payload_fault(self):
        auth = LastAuth.from_dict(
            {"type": "HomeKey", "result": "SUCCESS", "issuer": 'He said "hi"'}, HID, NID
        )
        assert auth.issuer == 'He said "hi"'


class TestAttribution:
    async def test_a_change_from_the_door_is_attributed(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        # Baseline: the node is locked, as it was the last time we looked.
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))

        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit"))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert len(logbook_entries) == 1
        assert "unlocked" in logbook_entries[0]["message"]
        assert "HomeKit" in logbook_entries[0]["message"]
        assert logbook_entries[0]["domain"] == "homekey_household"

    async def test_the_cause_is_available_to_the_entities_during_the_update(
        self, coordinator
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekey"))
        )

        seen: list[object] = []
        coordinator.async_add_listener(
            lambda: seen.append(coordinator.pending_context(NID))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        # Present while the entities write, so their state change carries it...
        assert seen == [seen[0]]
        assert seen[0] is not None

    async def test_the_cause_does_not_survive_the_update(self, coordinator):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit"))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        # ...and gone afterwards, so it cannot be attached to an unrelated change later.
        assert coordinator.pending_context(NID) is None

    async def test_the_context_shared_with_the_state_change_is_the_one_logged(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit"))
        )

        captured: list[object] = []
        coordinator.async_add_listener(
            lambda: captured.append(coordinator.pending_context(NID))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        # The logbook entry and the state change must carry the *same* context, or the
        # activity view has nothing to join them by and the cause does not attach.
        assert logbook_entries[0]["context"] is captured[0]

    async def test_a_change_home_assistant_asked_for_is_left_to_home_assistant(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "mqtt"))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        # Nothing is logged and no context is set, so the service call's own context
        # survives to be attached - which is what names the user in the activity log.
        assert logbook_entries == []
        assert coordinator.pending_context(NID) is None

    async def test_no_cause_on_record_is_not_guessed_at(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        # No B/lock/last at all: firmware predating the topic.
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert logbook_entries == []
        assert coordinator.pending_context(NID) is None

    async def test_a_stale_cause_is_not_applied_to_a_different_change(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        # A cause from an earlier change that produced the *locked* state...
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_LOCKED, "homekit"))
        )
        # ...while the state moves to unlocked. The cause does not describe this change.
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert logbook_entries == []
        assert coordinator.pending_context(NID) is None

    async def test_the_first_reading_is_not_a_change(self, coordinator, logbook_entries):
        await register_node(coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit"))
        )
        # Nothing to compare against yet: the node may simply have been unlocked all along.
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert logbook_entries == []

    async def test_a_repeated_reading_is_not_a_change(self, coordinator, logbook_entries):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_LOCKED, "homekit"))
        )
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))

        assert logbook_entries == []

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("homekey", "HomeKey credential"), ("device", "the device")],
    )
    async def test_each_source_is_named_in_its_own_words(
        self, coordinator, logbook_entries, source, expected
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, source))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert expected in logbook_entries[0]["message"]

    async def test_a_jammed_lock_is_reported_as_such(self, coordinator, logbook_entries):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(2, "device"))
        )
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(2)))

        assert LockState.JAMMED.value in logbook_entries[0]["message"]

    async def test_locking_is_attributed_too(self, coordinator, logbook_entries):
        """Not only unlocks: a lock closed by someone else is just as worth naming."""
        await register_node(coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_LOCKED, "homekit"))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_LOCKED))
        )

        assert "locked" in logbook_entries[0]["message"]
        assert "HomeKit" in logbook_entries[0]["message"]


class TestNamingThePerson:
    """The activity log names whoever the node says it was, but only when it can prove it.

    A name is used only when the node stamped the authorisation and the change it caused
    from the same reading of its clock. Anything looser risks putting a person's name on a
    change they had nothing to do with, which is worse than naming nobody at all.
    """

    async def test_a_named_tap_is_attributed_to_the_person(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_LOCKED))
        )

        # The node stamps a tap and the change it produced identically.
        await coordinator.async_handle_message(
            make_message(TOPIC_LAST_AUTH, last_auth("Artur", timestamp=106))
        )
        await coordinator.async_handle_message(
            make_message(
                TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekey", timestamp=106)
            )
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert len(logbook_entries) == 1
        assert logbook_entries[0]["message"] == "Gate unlocked by Artur"

    async def test_an_unnamed_tap_names_the_mechanism(
        self, coordinator, logbook_entries
    ):
        """A device nobody has named yet still says how the lock opened."""
        await register_node(coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_LOCKED))
        )

        await coordinator.async_handle_message(
            make_message(TOPIC_LAST_AUTH, last_auth(timestamp=106))
        )
        await coordinator.async_handle_message(
            make_message(
                TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekey", timestamp=106)
            )
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert logbook_entries[0]["message"] == "Gate unlocked by a HomeKey credential"

    async def test_an_earlier_authorisation_is_not_borrowed(
        self, coordinator, logbook_entries
    ):
        """The tap is stamped 100 and the change 106: those are different events."""
        await register_node(coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_LOCKED))
        )

        await coordinator.async_handle_message(
            make_message(TOPIC_LAST_AUTH, last_auth("Artur", timestamp=100))
        )
        await coordinator.async_handle_message(
            make_message(
                TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekey", timestamp=106)
            )
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert logbook_entries[0]["message"] == "Gate unlocked by a HomeKey credential"

    async def test_a_homekit_change_never_borrows_a_persons_name(
        self, coordinator, logbook_entries
    ):
        """HAP does not say which controller asked, so the mechanism is all there is."""
        await register_node(coordinator)
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_LOCKED))
        )

        await coordinator.async_handle_message(
            make_message(TOPIC_LAST_AUTH, last_auth("Artur", timestamp=106))
        )
        await coordinator.async_handle_message(
            make_message(
                TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit", timestamp=106)
            )
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert logbook_entries[0]["message"] == "Gate unlocked by HomeKit"
