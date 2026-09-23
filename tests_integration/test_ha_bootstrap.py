"""Verify a real Home Assistant instance bootstraps and loads the integration.

Step 6 capability probe, using the genuine HA harness ``hass`` fixture together
with ``enable_custom_integrations`` so the real HA loader discovers the
integration from ``custom_components/``.

Every test here requests ``enable_custom_integrations``.
"""

from __future__ import annotations

import pytest

DOMAIN = "homekey_household"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    """Enable custom integration discovery for every test in this module."""
    return None


async def test_ha_bootstraps(hass):
    """A real Home Assistant instance is available."""
    from homeassistant.const import __version__

    assert hass is not None
    assert __version__


async def test_integration_resolves_via_ha_loader(hass):
    """The integration is discoverable as a real HA custom component."""
    from homeassistant import loader

    integration = await loader.async_get_integration(hass, DOMAIN)
    assert integration.domain == DOMAIN
    assert integration.manifest


async def test_manifest_contract(hass):
    """Manifest declares config flow, reuses MQTT, and has no requirements."""
    from homeassistant import loader

    manifest = (await loader.async_get_integration(hass, DOMAIN)).manifest
    assert manifest.get("config_flow") is True
    assert "mqtt" in (manifest.get("dependencies") or [])
    assert not (manifest.get("requirements") or [])


async def test_entity_platforms_import(hass):
    """Entity platforms import cleanly inside a real HA process."""
    import importlib

    for platform in ("lock", "binary_sensor", "sensor"):
        importlib.import_module(f"custom_components.{DOMAIN}.{platform}")


async def test_diagnostics_imports(hass):
    """Diagnostics module loads and exposes its entry point."""
    import importlib

    diag = importlib.import_module(f"custom_components.{DOMAIN}.diagnostics")
    assert hasattr(diag, "async_get_config_entry_diagnostics")


async def test_config_entry_registers(hass):
    """A real config entry can be created for the integration."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"household_id": "HOME-TEST", "household_name": "HOME-TEST"},
        unique_id=f"{DOMAIN}_HOME-TEST",
    )
    entry.add_to_hass(hass)
    assert entry.entry_id
