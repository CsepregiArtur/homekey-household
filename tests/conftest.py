"""Shared pytest fixtures for HomeKey Household tests.

All fixtures use synthetic values (see ``tests/helpers.py``). No real household,
node, recovery secret, or command key ever appears in the test suite.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.homekey_household.credential import CommandKeyStore
from helpers import (
    TEST_HOUSEHOLD_ID,
    TEST_SALT,
    TEST_SECRET,
    FakeConfigEntry,
    FakeTransport,
)


@pytest.fixture
def test_secret() -> str:
    return TEST_SECRET


@pytest.fixture
def test_salt() -> str:
    return TEST_SALT


@pytest.fixture
async def hass() -> HomeAssistant:
    """A real, unstarted HomeAssistant instance with a temp config dir."""
    instance = HomeAssistant(config_dir="/tmp/homekey_household_test")
    yield instance


@pytest.fixture
def fake_entry() -> FakeConfigEntry:
    return FakeConfigEntry()


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def command_key(test_secret: str, test_salt: str) -> bytes:
    """A deterministic, synthetic command key derived from test-only material."""
    from custom_components.homekey_household.command import derive_command_key

    return derive_command_key(test_secret, test_salt)


@pytest.fixture
async def credential_store(
    hass: HomeAssistant, command_key: bytes, test_salt: str
) -> CommandKeyStore:
    """A command-key store seeded with a synthetic key."""
    store = CommandKeyStore(hass)
    await store.async_load()
    await store.async_set(TEST_HOUSEHOLD_ID, command_key, test_salt)
    return store
