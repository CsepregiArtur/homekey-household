"""Shared test constants and helpers (importable without a package).

Kept separate from ``conftest.py`` so test modules can import the synthetic
identities and the message builder directly.
"""

from __future__ import annotations

from custom_components.homekey_household.const import (
    CONF_COMMAND_CONTROL,
    CONF_HOUSEHOLD_ID,
    CONF_HOUSEHOLD_NAME,
    DOMAIN,
)
from custom_components.homekey_household.mqtt import HomeKeyMessage

# Synthetic test identities (never production values).
TEST_HOUSEHOLD_ID = "HOME-TEST"
TEST_HOUSEHOLD_NAME = "Test Household"
TEST_NODE_ID = "GATE-001"
TEST_NODE_ID_2 = "HOUSE-001"
TEST_SECRET = "test-only-recovery-secret"
TEST_SALT = "test-only-salt"


class FakeTransport:
    """In-memory MQTT transport capturing subscriptions and publishes."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, object] = {}
        self.published: list[tuple[str, str, int, bool]] = []

    async def subscribe(self, topic, callback, qos=0):
        self.subscriptions[topic] = callback
        return lambda: self.subscriptions.pop(topic, None)

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def last_payload(self) -> str:
        return self.published[-1][1]

    def last_topic(self) -> str:
        return self.published[-1][0]


class FakeConfigEntry:
    """Minimal ConfigEntry stand-in."""

    def __init__(
        self,
        entry_id: str = "test_entry",
        data: dict | None = None,
        options: dict | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.domain = DOMAIN
        self.data = (
            data
            if data is not None
            else {
                CONF_HOUSEHOLD_ID: TEST_HOUSEHOLD_ID,
                CONF_HOUSEHOLD_NAME: TEST_HOUSEHOLD_NAME,
                CONF_COMMAND_CONTROL: True,
            }
        )
        self.options = options if options is not None else {}
        self.title = TEST_HOUSEHOLD_NAME
        self.version = 1
        self._unloads: list = []

    def async_on_unload(self, callback) -> None:
        self._unloads.append(callback)


def make_message(
    subtopic: str,
    payload: str,
    *,
    household_id: str = TEST_HOUSEHOLD_ID,
    node_id: str | None = TEST_NODE_ID,
    retain: bool = False,
    legacy: bool = False,
) -> HomeKeyMessage:
    """Build a parsed MQTT message for coordinator tests."""
    return HomeKeyMessage(
        household_id=household_id,
        node_id=node_id,
        subtopic=subtopic,
        payload=payload,
        retain=retain,
        legacy=legacy,
    )
