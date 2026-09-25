"""Fixtures and outcome reporting for the hardware-validation suite.

Outcome model
-------------
PASS        the device behaved as the contract requires
FAIL        the device did not behave as required
BLOCKED     the test could not run (hardware/config/credentials missing)
NOT TESTED  the test was not attempted

A BLOCKED test proves nothing about the device, so this suite renders it
distinctly and loudly instead of letting it look like a pass or a silent skip.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests_hardware.device import (
    MqttObserver,
    SerialConsole,
    broker_host,
    broker_port,
    expected_firmware_version,
    find_serial_port,
    mqtt_credentials,
)
from tests_hardware.helpers import Evidence, HardwareBlocked

# Every BLOCKED outcome is recorded here (nodeid, reason) so the summary can
# report it distinctly. A blocked test is NOT a pass and NOT a failure.
_BLOCKED_TESTS: list[tuple[str, str]] = []

BLOCKED_PREFIX = "BLOCKED:"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """Convert HardwareBlocked into a skip during the CALL phase.

    This must happen in ``pytest_runtest_call`` and not in a fixture: a fixture's
    ``yield`` resumes during teardown, by which point the call phase has already
    been recorded as a failure. Converting here keeps a blocked test out of the
    failure count while still being visible as BLOCKED in the summary.
    """
    try:
        return (yield)
    except HardwareBlocked as exc:
        raise pytest.skip.Exception(
            f"{BLOCKED_PREFIX} {exc}", _use_item_location=True
        ) from exc


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Print an explicit BLOCKED section so it can never be mistaken for PASS."""
    # Collect reasons from both sources: HardwareBlocked exceptions converted
    # above, and fixtures that skipped directly with a BLOCKED-prefixed reason.
    reason_to_tests: dict[str, list[str]] = {}
    for report in terminalreporter.stats.get("skipped", []):
        reason = ""
        if report.longrepr and isinstance(report.longrepr, tuple):
            reason = str(report.longrepr[2])
        elif report.longrepr:
            reason = str(report.longrepr).splitlines()[-1]
        if "BLOCKED" not in reason:
            continue
        clean = reason.split("BLOCKED:", 1)[-1].strip()
        reason_to_tests.setdefault(clean, []).append(report.nodeid)

    if not reason_to_tests:
        return

    total = sum(len(nodeids) for nodeids in reason_to_tests.values())
    terminalreporter.write_sep("=", "BLOCKED HARDWARE TESTS", red=True, bold=True)
    terminalreporter.write_line(
        "These tests did NOT run. BLOCKED is not a pass and is not evidence of "
        "correct device behaviour; it means the hardware or configuration "
        "needed to test was unavailable.",
        red=True,
    )
    for reason, nodeids in reason_to_tests.items():
        terminalreporter.write_line(f"\n  {reason}")
        for nodeid in nodeids:
            terminalreporter.write_line(f"    - {nodeid}")
    terminalreporter.write_line(f"\nTotal BLOCKED: {total}", red=True)


@pytest.fixture(scope="session")
def firmware_version() -> str:
    """Expected firmware version for this validation run."""
    return expected_firmware_version()


@pytest.fixture
def evidence(request: pytest.FixtureRequest) -> Iterator[Evidence]:
    """Evidence accumulator, written to tests_hardware/evidence/ on completion."""
    bundle = Evidence(test_name=request.node.name)
    yield bundle
    bundle.write(result="recorded")


@pytest.fixture(scope="session")
def serial_port() -> str:
    """The ESP32 serial port, or BLOCKED when nothing is attached."""
    port = find_serial_port()
    if not port:
        pytest.skip(
            "BLOCKED: no ESP32 serial device found (expected /dev/cu.usbserial-* "
            "or set HK_SERIAL_PORT)"
        )
    return port


@pytest.fixture(scope="session")
def console(serial_port: str) -> SerialConsole:
    """Serial console reader bound to the detected port."""
    return SerialConsole(port=serial_port)


@pytest.fixture(scope="session")
def hardware_available(console: SerialConsole) -> bool:
    """True when the board answers on the console at all."""
    capture = console.capture(seconds=3.0, reset=False)
    return bool(capture.text.strip())


@pytest.fixture(scope="session")
def household_id() -> str:
    """Household id under test (required to observe topics)."""
    value = os.environ.get("HK_HOUSEHOLD_ID")
    if not value:
        pytest.skip(
            "BLOCKED: HK_HOUSEHOLD_ID not set; the household topic tree is "
            "unknown, so topics cannot be observed"
        )
    return value


@pytest.fixture(scope="session")
def node_id() -> str:
    """Node id under test (required to observe topics)."""
    value = os.environ.get("HK_NODE_ID")
    if not value:
        pytest.skip(
            "BLOCKED: HK_NODE_ID not set; the node topic tree is unknown, so "
            "topics cannot be observed"
        )
    return value


@pytest.fixture(scope="session")
def broker_reachable() -> tuple[str, int]:
    """Broker address, or BLOCKED when it cannot be reached/authenticated."""
    if not mqtt_credentials():
        pytest.skip(
            f"BLOCKED: broker {broker_host()}:{broker_port()} requires "
            "authentication and HK_MQTT_USERNAME/HK_MQTT_PASSWORD are not set"
        )
    return broker_host(), broker_port()


@pytest.fixture
def observer(household_id: str, broker_reachable: tuple[str, int]) -> MqttObserver:
    """MQTT observer bound to the household under test."""
    host, port = broker_reachable
    return MqttObserver(household_id, host=host, port=port)
