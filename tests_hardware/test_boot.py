"""Step 12 - ESP32 connection and telemetry.

Verifies boot, Wi-Fi, MQTT authentication and node visibility from the device's
own console plus the broker's topic tree. Real hardware only: there is no
simulation path, and anything that cannot be observed is BLOCKED, not passed.
"""

from __future__ import annotations

import re

import pytest

from tests_hardware.device import SerialConsole
from tests_hardware.helpers import Evidence, HardwareBlocked

pytestmark = pytest.mark.boot

# Console patterns the firmware actually emits.
BOOT_MARKERS = {
    "boot banner": r"rst:0x[0-9a-f]+ \((POWERON|SW|TG[01]WDT)_RESET\)",
    "config load": r"ConfigManager:",
    "wifi association": r"\[W\]\[wifi\]",
    "mqtt tls warning": r"MqttManager\] MQTT TLS is disabled",
}
MQTT_AUTH_FAILURE = (
    r"Connection refused, not authorized|MQTT_CONNECTION_REFUSE_NOT_AUTHORIZED"
)
MQTT_CONNECTED = r"MQTT_EVENT_CONNECTED|MQTT connected"


def test_esp32_boots_and_reaches_application_code(
    console: SerialConsole, evidence: Evidence
) -> None:
    """The board must boot and run the HomeKey application, not just the ROM."""
    capture = console.capture(seconds=12.0, reset=True)
    evidence.serial(capture.text)

    found = capture.find(BOOT_MARKERS)
    assert "boot banner" in found, (
        "no boot banner on the serial console; the board is not booting or the "
        f"console is not the ESP32. Captured {len(capture.text)} bytes."
    )
    # Application code reached: the firmware's own ConfigManager runs.
    assert "config load" in found, (
        "the ROM booted but the HomeKey application did not start (no "
        "ConfigManager output). Evidence: " + capture.redacted[:500]
    )


def test_esp32_connects_to_wifi(console: SerialConsole, evidence: Evidence) -> None:
    """Wi-Fi must associate so the device can reach the broker."""
    capture = console.capture(seconds=20.0, reset=True)
    evidence.serial(capture.text)

    found = capture.find(BOOT_MARKERS)
    assert "wifi association" in found, (
        "no Wi-Fi activity on the console. Evidence: " + capture.redacted[:500]
    )
    # A DHCP failure would be explicit; absence of the Wi-Fi error plus MQTT
    # activity implies the link came up.
    assert not capture.has_error(r"wifi.*(fail|disconnect).*reason"), (
        "Wi-Fi reported a failure on the console"
    )


def test_mqtt_authentication_succeeds(
    console: SerialConsole, evidence: Evidence
) -> None:
    """MQTT must CONNECT. This is the gate for all downstream hardware tests."""
    capture = console.capture(seconds=25.0, reset=True)
    evidence.serial(capture.text)

    if capture.has_error(MQTT_AUTH_FAILURE):
        # Extract the firmware's own diagnosis for the failure report.
        detail = ", ".join(
            capture.find({"mqtt error": r"\[E\]\[[^\]]+\][^\n]*"}).get("mqtt error", [])
        )
        raise AssertionError(
            "MQTT authentication was REJECTED by the broker "
            "(firmware logged 'Connection refused, not authorized'). The "
            "broker requires credentials and the device's configured "
            "username/password are missing or invalid. "
            f"Firmware errors: {detail or 'n/a'}"
        )

    assert capture.has_error(MQTT_CONNECTED), (
        "no MQTT connection was observed on the console within the capture "
        "window. Evidence: " + capture.redacted[:500]
    )


def test_firmware_version_matches_contract(
    firmware_version: str, evidence: Evidence
) -> None:
    """The running firmware must report the contract version.

    The version is exposed by the firmware over MQTT (``/health``) and the web
    UI, not on the boot console, so it cannot be read here without a working
    MQTT connection. Reporting a version we cannot read would be a fabricated
    pass, so this is explicitly BLOCKED until the node is reachable.
    """
    raise HardwareBlocked(
        f"cannot verify the running firmware version ({firmware_version}) from "
        "the console: the firmware only reports it via MQTT /health and the web "
        "UI. Requires a working MQTT connection and HK_HOUSEHOLD_ID/HK_NODE_ID."
    )


def test_household_node_appears_in_home_assistant(observer, evidence: Evidence) -> None:
    """The node must publish retained state so HA discovers it."""
    messages = observer.collect(seconds=12.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    assert messages, (
        "no messages on the household topic tree; the node is not publishing "
        "(most likely MQTT never connected). No HA entity can exist without "
        "these retained messages."
    )

    suffixes = {m["topic"].rsplit("/", 1)[-1] for m in messages}
    assert "state" in suffixes, (
        f"no retained 'state' message; node discovery is impossible. Saw: {sorted(suffixes)}"
    )


@pytest.mark.parametrize("suffix", ["status", "security"])
def test_retained_topics_are_retained(
    observer, evidence: Evidence, suffix: str
) -> None:
    """status/security are retained per the contract; health is not."""
    messages = observer.collect(seconds=8.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    matching = [m for m in messages if m["topic"].endswith(f"/{suffix}")]
    assert matching, f"no '{suffix}' topic published; cannot verify retain flag"
    assert any(m["retain"] for m in matching), (
        f"'{suffix}' was published but not retained, which breaks late-joining "
        "recovery in Home Assistant"
    )


def test_health_topic_is_not_retained(observer, evidence: Evidence) -> None:
    """B/health is explicitly NON-retained; a retained flag is a contract break."""
    messages = observer.collect(seconds=8.0)
    for message in messages:
        evidence.mqtt(message["topic"], message["payload"])

    health = [m for m in messages if m["topic"].endswith("/health")]
    if not health:
        raise HardwareBlocked(
            "no 'health' message observed during the window; health is "
            "non-retained and only published periodically, so it cannot be "
            "verified passively in this capture"
        )
    assert not any(m["retain"] for m in health), (
        "B/health was published with retain=1 but the contract requires non-retained"
    )


def test_no_secret_material_in_boot_log(
    evidence: Evidence, console: SerialConsole
) -> None:
    """Step 9 applied to the boot path: no 64-hex secrets on the console."""
    capture = console.capture(seconds=12.0, reset=True)
    evidence.serial(capture.text)

    leaks = re.findall(r"\b[0-9a-f]{64}\b", capture.text)
    assert not leaks, (
        f"{len(leaks)} 64-character hex token(s) appeared in the boot log; "
        "these are indistinguishable from command keys/MACs and must never be "
        "logged. Values were redacted before storage."
    )
