"""Attributing a lock change to whatever caused it.

Two things are being established here, and the second is the subtle one:

1. ``B/lock/last`` is parsed strictly, and only a cause that belongs to *this* change is
   used - never the previous one, which would blame whatever happened last for what
   happened now. The cause is recorded when the event arrives rather than when a later
   health snapshot confirms it: the snapshot samples the lock on a cadence, so a change
   that does not survive until the next sample - the usual case for a tap, which relocks a
   moment later - was never attributed at all, and the activity log reported that no cause
   was recorded for a door somebody had just opened.
2. A change Home Assistant asked for is left alone. Its service-call context is already
   pending on the entity and is consumed by the very write the change causes, which is
   what puts the user's name and "Action used: Lock lock" in the activity log. Overriding
   that with a vaguer description would be a regression, not an improvement.
"""

from __future__ import annotations

import json
import time

import pytest
from homeassistant.const import EVENT_LOGBOOK_ENTRY
from homeassistant.core import callback

from custom_components.homekey_household.const import (
    CONF_CAUSE_ENTITY,
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

    @callback
    def _capture(event) -> None:
        # The context lives on the event rather than in its data, and the context is the
        # whole point of the exercise, so it is folded in here. Declared as a callback so
        # it runs on the event loop in order: a plain listener is handed to an executor,
        # and what it collected may not have arrived by the time a test looks at it.
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

        seen: list[object] = []
        coordinator.async_add_listener(
            lambda: seen.append(coordinator.pending_context(NID))
        )
        # The event is what the entity reacts to: it is the update that changes the state
        # the entity writes, so it is the update that has to carry the cause.
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekey"))
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

        captured: list[object] = []
        coordinator.async_add_listener(
            lambda: captured.append(coordinator.pending_context(NID))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit"))
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

    async def test_a_cause_is_never_borrowed_by_a_later_snapshot(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        # An event that describes the *locked* state...
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_LOCKED, "homekit"))
        )
        # ...while the snapshot reports a move to unlocked, with no event of its own. The
        # cause for the event describes the event, not this.
        await coordinator.async_handle_message(
            make_message(TOPIC_HEALTH, health(LOCK_UNLOCKED))
        )

        assert len(logbook_entries) == 1
        assert "locked" in logbook_entries[0]["message"]

    async def test_the_first_reading_is_not_a_change(self, coordinator, logbook_entries):
        await register_node(coordinator)
        # No event, and nothing to compare the reading against yet: the node may simply
        # have been unlocked all along.
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

        # The event is announced once; the snapshot that agrees with it adds nothing.
        assert len(logbook_entries) == 1
        assert coordinator.pending_context(NID) is None

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
        assert logbook_entries[0]["message"] == (
            "Gate unlocked by Artur with a HomeKey credential"
        )

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


class TestChangeTheSnapshotNeverSees:
    """A change that is over before the next snapshot is taken.

    ``B/health`` reports the lock on a 30-second cadence. A tap opens the door and the lock
    closes again a moment later, so the sample taken afterwards says "locked" - exactly what
    the previous sample said. There is no state change for a snapshot-driven attribution to
    notice, and the activity log said "No cause was recorded" for a door that had just been
    opened by somebody. The event is the only record of what happened, and it is now enough
    on its own.
    """

    async def test_an_event_is_announced_without_waiting_for_a_snapshot(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))

        await coordinator.async_handle_message(
            make_message(
                TOPIC_LOCK_LAST,
                lock_last(LOCK_UNLOCKED, "homekey", timestamp=int(time.time())),
            )
        )

        assert len(logbook_entries) == 1
        assert "unlocked" in logbook_entries[0]["message"]

    async def test_a_brief_unlock_is_recorded_although_the_snapshot_misses_it(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))

        # Opened and closed again within the same sampling interval.
        now = int(time.time())
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit", timestamp=now))
        )
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_LOCKED, "homekit", timestamp=now + 1))
        )
        # The snapshot only ever sees the end state, which matches where it started.
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))

        assert [entry["message"] for entry in logbook_entries] == [
            "Gate unlocked by HomeKit",
            "Gate locked by HomeKit",
        ]

    async def test_the_state_follows_the_event_until_the_next_snapshot(self, coordinator):
        await register_node(coordinator)
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit"))
        )

        # The event is the fresher report of the lock, so the entity follows it at once
        # instead of waiting up to 30 seconds for the next sample.
        assert coordinator.get_node(NID).lock_state == LockState.UNLOCKED

        # The snapshot samples the hardware, so once it arrives it takes over again.
        await coordinator.async_handle_message(make_message(TOPIC_HEALTH, health(LOCK_LOCKED)))
        assert coordinator.get_node(NID).lock_state == LockState.LOCKED

    async def test_a_retained_event_from_hours_ago_is_history_not_news(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        # The retained event is delivered again on every connect. Announcing it as
        # something that just happened would put a false entry in the activity log.
        await coordinator.async_handle_message(
            make_message(
                TOPIC_LOCK_LAST,
                lock_last(LOCK_UNLOCKED, "homekey", timestamp=int(time.time()) - 3600),
            )
        )

        assert logbook_entries == []

    async def test_the_same_event_arriving_twice_is_announced_once(
        self, coordinator, logbook_entries
    ):
        await register_node(coordinator)
        payload = lock_last(LOCK_UNLOCKED, "homekey", timestamp=int(time.time()))
        await coordinator.async_handle_message(make_message(TOPIC_LOCK_LAST, payload))
        await coordinator.async_handle_message(make_message(TOPIC_LOCK_LAST, payload))

        assert len(logbook_entries) == 1

    async def test_a_second_lock_entity_can_be_told_the_cause_too(
        self, hass, logbook_entries
    ):
        """A node that also publishes its own MQTT discovery has two lock entities."""
        entry = FakeConfigEntry(options={CONF_CAUSE_ENTITY: "lock.hk_lock"})
        coordinator = HomeKeyHouseholdCoordinator(hass, entry, household_id=HID)
        await register_node(coordinator)

        await coordinator.async_handle_message(
            make_message(TOPIC_LOCK_LAST, lock_last(LOCK_UNLOCKED, "homekit"))
        )

        assert [item["entity_id"] for item in logbook_entries] == ["lock.hk_lock"]
