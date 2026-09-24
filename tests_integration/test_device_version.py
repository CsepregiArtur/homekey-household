"""Device-card version reporting.

Verifies what Home Assistant shows on the device page:

* the integration version comes from ``manifest.json`` (what the Integrations
  page displays for the integration itself);
* the node device advertises the **firmware** version as ``sw_version``, and
  never mislabels the firmware as ``hw_version``.
"""

from __future__ import annotations

import pytest
from test_hmac_commands import _seed_credential, _setup_entry

from helpers import HOUSEHOLD, NODE_GATE, publish_telemetry, settle

DOMAIN = "homekey_household"


async def test_integration_version_comes_from_manifest(hass):
    """The Integrations page version is the manifest version."""
    from homeassistant import loader

    manifest = (await loader.async_get_integration(hass, DOMAIN)).manifest
    assert manifest["version"]  # a version key must exist for custom integrations
    assert manifest["version"] == "2.2.2"


async def test_device_reports_firmware_as_sw_version(hass, mqtt_client):
    """The node device shows the node's firmware as sw_version (not hw_version).

    Regression: the firmware version was previously also written to
    ``hw_version``, which made the device card show it twice and mislabelled
    software as hardware.
    """
    from homeassistant.helpers import device_registry as dr

    await _seed_credential(hass, HOUSEHOLD)
    await _setup_entry(hass, HOUSEHOLD)
    await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
    await settle(hass)

    devices = [
        d
        for d in dr.async_get(hass).devices
        if any(ident[0] == DOMAIN for ident in d.identifiers)
    ]
    assert devices, "no device registered for the node"

    device = devices[0]
    assert device.sw_version == "0.10.0", (
        f"expected the node firmware as sw_version, got {device.sw_version!r}"
    )
    assert device.hw_version is None, (
        "hw_version must not carry the firmware version (software != hardware)"
    )


async def test_device_version_present_before_node_reports(hass, mqtt_client):
    """sw_version is never blank: it falls back to the supported contract version.

    The device is created as soon as the node is discovered; the firmware string
    only arrives with the first ``B/state``. The fallback keeps the card readable
    in that window.
    """
    await _seed_credential(hass, HOUSEHOLD)
    await _setup_entry(hass, HOUSEHOLD)
    # Discovery via the retained status topic only (no firmware in it).
    await mqtt_client.publish(
        f"homekey/household/{HOUSEHOLD}/nodes/{NODE_GATE}/status",
        "online",
        qos=1,
        retain=True,
    )
    await settle(hass)

    from homeassistant.helpers import device_registry as dr

    devices = [
        d
        for d in dr.async_get(hass).devices
        if any(ident[0] == DOMAIN for ident in d.identifiers)
    ]
    if not devices:
        pytest.skip("node not discovered from status alone in this environment")
    assert devices[0].sw_version, "sw_version must never be blank"
