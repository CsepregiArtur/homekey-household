"""The security sensor says why, not just that something is wrong.

A verdict of ``WARNING`` on its own is not actionable - the question it raises is "warning
about what?", and the node answers that question in the very same payload. These tests hold
the integration to passing that answer on, in the node's own words, one finding per line.
"""

from __future__ import annotations

import json

import pytest

from custom_components.homekey_household.const import (
    TOPIC_HEALTH,
    TOPIC_SECURITY,
    TOPIC_STATE,
    TOPIC_STATUS,
)
from custom_components.homekey_household.coordinator import (
    HomeKeyHouseholdCoordinator,
)
from custom_components.homekey_household.sensor import HomeKeySecuritySensor
from helpers import TEST_HOUSEHOLD_ID, FakeConfigEntry, make_message

HID = TEST_HOUSEHOLD_ID
NID = "GATE-001"

# The exact wording a node with nothing enabled reports.
HARDENING_OFF = (
    "secure_boot: Secure boot disabled.\n"
    "flash_encryption: Flash encryption disabled.\n"
    "nvs_encryption: NVS encryption disabled.\n"
    "ota_signature: OTA signature verification disabled.\n"
    "mqtt_tls: MQTT TLS disabled; MQTT command topics can unlock the door."
)


def health_payload(*, all_ok: bool, warnings: str) -> str:
    """Build a firmware-faithful ``B/health`` payload with a given security block."""
    return json.dumps(
        {
            "network": "UNKNOWN",
            "mqtt": "OK",
            "mqtt_error": 0,
            "nfc": "OK",
            "lock_current": 1,
            "lock_target": 1,
            "backup": "ok",
            "certificate": "unknown",
            "firmware_version": "0.11.0",
            "uptime": 100,
            "free_heap": 100000,
            "reset_reason": "1",
            "security": {"all_ok": all_ok, "warnings": warnings},
        }
    )


def state_payload() -> str:
    return json.dumps(
        {
            "household_id": HID,
            "node_id": NID,
            "node_name": "Gate",
            "node_role": "gate",
            "node_state": "ACTIVE",
            "generation": 1,
            "firmware_version": "0.11.0",
        }
    )


@pytest.fixture
async def sensor(hass):
    """A security sensor for a node that has already been announced."""
    coordinator = HomeKeyHouseholdCoordinator(
        hass, FakeConfigEntry(), household_id=HID
    )
    await coordinator.async_handle_message(make_message(TOPIC_STATE, state_payload()))
    await coordinator.async_handle_message(make_message(TOPIC_STATUS, "online"))
    return HomeKeySecuritySensor(coordinator, NID)


async def feed(
    sensor: HomeKeySecuritySensor, *, all_ok: bool, warnings: str, verdict: str = "WARNING"
) -> None:
    """Send what the node sends: the verdict, and the findings beside it.

    The state comes from ``B/security``; the reasons come from the health snapshot. They
    travel on different topics, so a test that feeds only one of them is not testing what
    the device actually publishes.
    """
    await sensor.coordinator.async_handle_message(make_message(TOPIC_SECURITY, verdict))
    await sensor.coordinator.async_handle_message(
        make_message(TOPIC_HEALTH, health_payload(all_ok=all_ok, warnings=warnings))
    )


async def test_the_reasons_ride_along_with_the_verdict(sensor):
    await feed(sensor, all_ok=False, warnings=HARDENING_OFF)

    assert sensor.native_value == "WARNING"
    attributes = sensor.extra_state_attributes
    assert attributes["security_all_ok"] is False
    assert attributes["warning_count"] == 5
    assert len(attributes["security_findings"]) == 5


async def test_the_findings_keep_the_nodes_own_words(sensor):
    await feed(sensor, all_ok=False, warnings=HARDENING_OFF)

    findings = sensor.extra_state_attributes["security_findings"]
    # Verbatim, including the semicolon and the reason why it matters: the point of the
    # attribute is to carry the node's explanation, not a paraphrase of it.
    assert findings[4] == (
        "mqtt_tls: MQTT TLS disabled; MQTT command topics can unlock the door."
    )
    assert sensor.extra_state_attributes["security_warnings"] == HARDENING_OFF


async def test_a_node_with_nothing_to_report_lists_nothing(sensor):
    await feed(sensor, all_ok=True, warnings="", verdict="OK")

    assert sensor.native_value == "OK"
    attributes = sensor.extra_state_attributes
    assert attributes["security_all_ok"] is True
    assert attributes["security_findings"] == []
    assert attributes["warning_count"] == 0


async def test_blank_lines_are_not_findings(sensor):
    await feed(sensor, all_ok=False, warnings="\n  \n")

    attributes = sensor.extra_state_attributes
    assert attributes["security_findings"] == []
    assert attributes["warning_count"] == 0


async def test_a_node_without_a_snapshot_gets_no_invented_findings(hass):
    coordinator = HomeKeyHouseholdCoordinator(
        hass, FakeConfigEntry(), household_id=HID
    )
    await coordinator.async_handle_message(make_message(TOPIC_STATE, state_payload()))
    fresh = HomeKeySecuritySensor(coordinator, NID)

    # Nothing has been published yet, so there is nothing to be said about it - and an
    # unknown posture must not be dressed up as a clean one.
    assert fresh.native_value is None
    assert "security_findings" not in fresh.extra_state_attributes
    assert "security_all_ok" not in fresh.extra_state_attributes
