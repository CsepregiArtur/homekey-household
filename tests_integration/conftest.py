"""Pytest configuration for the REAL integration validation suite.

This suite is separate from ``tests/`` (the fast unit/contract suite) because it
needs the official Home Assistant test harness enabled, plus a live MQTT broker.

Run with::

    .venv/bin/python -m pytest tests_integration -c tests_integration/pytest.ini

The ``pytest_homeassistant_custom_component`` plugin provides a genuine Home
Assistant bootstrap (the ``hass`` fixture) — it is *not* a hand-rolled mock.

IMPORTANT: the HA harness blocks real sockets by default (``socket.socket`` is
guarded) to keep unit tests hermetic. This suite exists specifically to talk to a
real broker, so real sockets are re-enabled via the ``socket_enabled`` fixture.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The HA harness plugin must be enabled before HA imports happen.
pytest_plugins = ["pytest_homeassistant_custom_component"]

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools" / "integration"

# Make ``custom_components.homekey_household`` importable and discoverable by the
# HA loader (it scans ``custom_components`` relative to the config dir and repo
# root on sys.path).
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18830
MOSQUITTO_CONF = TOOLS / "mosquitto.test.conf"


@pytest.fixture(autouse=True)
def _allow_real_sockets(socket_enabled):
    """Re-enable real sockets for every test in this suite.

    The HA harness guards ``socket.socket``; this suite must reach a real broker.
    """
    return None


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Allow Home Assistant's own MQTT client periodic timer at teardown.

    ``homeassistant.components.mqtt`` schedules a periodic "misc" task on the
    event loop. That is core HA behaviour, not a leak in the integration under
    test, so this suite tolerates it. Genuine leaks in the integration are still
    caught: its own debounced reload timer was found and fixed this way.
    """
    return True


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Allow the HA loader to discover ``custom_components.homekey_household``."""
    return None


def _mosquitto_binary() -> str | None:
    """Locate the mosquitto broker binary (Homebrew paths included)."""
    from shutil import which

    candidates = [
        "mosquitto",
        "/opt/homebrew/opt/mosquitto/bin/mosquitto",
        "/opt/homebrew/sbin/mosquitto",
        "/usr/local/opt/mosquitto/bin/mosquitto",
        "/usr/sbin/mosquitto",
    ]
    for candidate in candidates:
        if os.path.isabs(candidate):
            if Path(candidate).exists():
                return candidate
        else:
            found = which(candidate)
            if found:
                return found
    return None


def _link_integration_into_harness_config() -> None:
    """Prepare the HA harness config dir for a REAL MQTT connection.

    The HA harness uses its own ``testing_config`` directory as the Home
    Assistant config dir. Two things are required there:

    1. ``custom_components/homekey_household`` must be discoverable by the loader
       (otherwise ``ModuleNotFoundError`` / "Integration not found").
    2. A minimal ``configuration.yaml`` must exist, because HA's MQTT component
       reads the homeassistant YAML config during setup.
    """
    try:
        from pytest_homeassistant_custom_component.common import get_test_config_dir
    except Exception:  # pragma: no cover - harness unavailable
        return

    config_dir = Path(get_test_config_dir())
    if not config_dir.is_dir():
        return

    # 1. Link the integration under test.
    target_dir = config_dir / "custom_components"
    if target_dir.is_dir():
        link = target_dir / "homekey_household"
        source = REPO_ROOT / "custom_components" / "homekey_household"
        needs_link = not (link.is_symlink() and link.resolve() == source.resolve())
        if needs_link:
            with contextlib.suppress(OSError):
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(source, target_is_directory=True)

    # 2. Provide a minimal configuration.yaml when the harness ships none.
    config_yaml = config_dir / "configuration.yaml"
    if not config_yaml.exists():
        with contextlib.suppress(OSError):
            config_yaml.write_text(
                "homeassistant:\n  name: HA Integration Test\n  unit_system: metric\n",
                encoding="utf-8",
            )


_link_integration_into_harness_config()


def broker_available() -> bool:
    """True when a real mosquitto binary is present."""
    return _mosquitto_binary() is not None


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with (
            contextlib.suppress(OSError),
            socket.create_connection((host, port), timeout=1),
        ):
            return True
        time.sleep(0.15)
    return False


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Start a disposable, loopback-only mosquitto broker for the whole session.

    Skips (rather than fails) when mosquitto is unavailable, so a missing broker
    is reported as NOT TESTED rather than a false FAIL.
    """
    binary = _mosquitto_binary()
    if binary is None:
        pytest.skip(
            "mosquitto not installed -- real MQTT validation NOT TESTED "
            "(install with: brew install mosquitto)"
        )

    proc = subprocess.Popen(  # noqa: S603 - fixed binary + fixed conf path
        [binary, "-c", str(MOSQUITTO_CONF)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not _wait_for_port(BROKER_HOST, BROKER_PORT):
        proc.terminate()
        out = ""
        with contextlib.suppress(Exception):
            out = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"mosquitto did not start on {BROKER_HOST}:{BROKER_PORT}\n{out}")

    yield {"host": BROKER_HOST, "port": BROKER_PORT, "proc": proc}

    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)
    if proc.poll() is None:
        proc.kill()


@pytest.fixture
async def mqtt_client(mosquitto_broker):
    """A connected aiomqtt client against the disposable broker."""
    import aiomqtt

    async with aiomqtt.Client(BROKER_HOST, BROKER_PORT) as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_pooled_state(hass):
    """Clear pooled household state so tests cannot inherit each other's nodes.

    The integration pools discovered node state in ``hass.data`` so a
    discovery-triggered reload can reattach non-retained telemetry. Within a test
    the ``hass`` fixture is fresh, but this guard makes the isolation explicit.
    """
    for key in list(hass.data):
        if isinstance(key, str) and key.startswith("homekey_household_state_"):
            hass.data.pop(key, None)
    yield
    for key in list(hass.data):
        if isinstance(key, str) and key.startswith("homekey_household_state_"):
            hass.data.pop(key, None)


@pytest.fixture(autouse=True)
async def _purge_retained_topics(mosquitto_broker, mqtt_client, hass):
    """Clear retained household topics before each test.

    Retained messages are broker state and outlive a single test, so without this
    a later test would immediately receive an earlier test's telemetry.
    """
    import aiomqtt

    topics = [
        "homekey/household/#",
        "ESP_+/status",
    ]
    # Publish an empty retained payload over the household tree to clear it.
    async with aiomqtt.Client(BROKER_HOST, BROKER_PORT) as cleaner:
        for household, node in (
            ("HOME-TEST", "GATE-TEST-001"),
            ("HOME-TEST", "GATE-TEST-002"),
            ("HOME-TEST", "HOUSE-TEST-001"),
            ("HOME-TEST", "SMALL-TEST-001"),
            ("HOME-OTHER", "GATE-TEST-001"),
        ):
            base = f"homekey/household/{household}/nodes/{node}"
            for sub in (
                "state",
                "status",
                "health",
                "security",
                "backup/status",
                "backup/last",
                "last_auth",
            ):
                await cleaner.publish(f"{base}/{sub}", "", qos=1, retain=True)
        for client_id in (
            "ESP_GATETEST",
            "ESP_HOUSETEST",
            "ESP_SMALLTEST",
            "ESP_GATENEW",
        ):
            await cleaner.publish(f"{client_id}/status", "", qos=1, retain=True)
    await hass.async_block_till_done()
    del topics
    yield


@pytest.fixture
def broker_endpoint(mosquitto_broker) -> tuple[str, int]:
    return BROKER_HOST, BROKER_PORT


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "hardware: test requires a physical ESP32 / lock actuator"
    )
