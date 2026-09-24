"""Config-flow tests for the MQTT guided / express setup paths.

These exercise the real HA config-flow engine (``hass.config_entries.flow``), not
a hand-rolled mock, so the menu, the abort-free behaviour and the express MQTT
entry creation are all validated end to end.

Most tests patch ``async_validate_mqtt`` so "MQTT available" can be toggled
deterministically; the express test additionally uses the real disposable broker
because the official MQTT flow validates broker reachability.
"""

from __future__ import annotations

from unittest.mock import patch

DOMAIN = "homekey_household"
MQTT_DOMAIN = "mqtt"


async def _start_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    # Let the flow settle (it awaits MQTT validation).
    await hass.async_block_till_done()
    return result


async def test_offers_menu_when_mqtt_missing(hass):
    """With MQTT unavailable the flow shows a menu, not an abort."""
    with patch(
        "custom_components.homekey_household.config_flow.async_validate_mqtt",
        return_value=False,
    ):
        result = await _start_flow(hass)

    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"mqtt_guide", "mqtt_express"}


async def test_flow_starts_with_no_mqtt_entry_at_all(hass):
    """REGRESSION: the flow must start even when MQTT has NO config entry.

    This is the real-world first-install case. If ``mqtt`` were listed in the
    manifest ``dependencies``, Home Assistant would raise DependencyError before
    the flow ever ran, so the user would see nothing at all instead of the
    guided/express menu. Nothing is patched here: no MQTT entry exists, and the
    integration must still load and offer the menu.
    """
    # Precondition: no MQTT config entry in this fresh hass.
    assert not hass.config_entries.async_entries(MQTT_DOMAIN)

    result = await _start_flow(hass)

    assert result["type"] == "menu", (
        f"expected the setup menu without an MQTT entry, got {result['type']!r} "
        f"({result.get('reason') or result.get('errors')})"
    )
    assert set(result["menu_options"]) == {"mqtt_guide", "mqtt_express"}


async def test_guide_step_renders_form(hass):
    """The guided walkthrough renders instructions and re-checks on submit."""
    with patch(
        "custom_components.homekey_household.config_flow.async_validate_mqtt",
        return_value=False,
    ):
        await _start_flow(hass)
        flow_id = next(
            f
            for f in hass.config_entries.flow.async_progress()
            if f["handler"] == DOMAIN
        )["flow_id"]

        # Choosing the guide shows a form (no fields), with docs placeholder.
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "mqtt_guide"}
        )
        await hass.async_block_till_done()
        assert result["step_id"] == "mqtt_guide"
        assert result["type"] == "form"
        assert "docs" in (result.get("description_placeholders") or {})

        # Submitting while MQTT is still down surfaces an error, not a crash.
        result = await hass.config_entries.flow.async_configure(flow_id, {})
        await hass.async_block_till_done()
        assert result["type"] == "form"
        assert result["errors"] == {"base": "mqtt_not_configured"}


async def test_household_step_taken_when_mqtt_up(hass):
    """When MQTT is available the flow goes straight to the household form."""
    with patch(
        "custom_components.homekey_household.config_flow.async_validate_mqtt",
        return_value=True,
    ):
        result = await _start_flow(hass)

    assert result["type"] == "form"
    assert result["step_id"] == "household"


async def test_express_step_creates_mqtt_entry(hass, mosquitto_broker, socket_enabled):
    """The express path creates a real MQTT config entry and continues.

    Uses the real disposable broker because the official MQTT flow validates
    broker reachability before creating its entry.
    """
    with (
        patch(
            "custom_components.homekey_household.config_flow.async_validate_mqtt",
            return_value=False,
        ),
        patch(
            "custom_components.homekey_household.config_flow._async_mqtt_entry_exists",
            return_value=False,
        ),
    ):
        await _start_flow(hass)
        flow_id = next(
            f
            for f in hass.config_entries.flow.async_progress()
            if f["handler"] == DOMAIN
        )["flow_id"]

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "mqtt_express"}
        )
        await hass.async_block_till_done()
        assert result["step_id"] == "mqtt_express"
        assert result["type"] == "form"

        # From here the express step will try to create the MQTT entry and then
        # re-validate MQTT; make the creation succeed and validation pass.
        with patch(
            "custom_components.homekey_household.config_flow.async_validate_mqtt",
            return_value=True,
        ):
            result = await hass.config_entries.flow.async_configure(
                flow_id,
                {
                    "broker": mosquitto_broker["host"],
                    "port": mosquitto_broker["port"],
                },
            )
            await hass.async_block_till_done()

    # The flow advanced to the household form.
    assert result["type"] == "form", result
    assert result["step_id"] == "household"

    # A real MQTT config entry now exists, pointing at the broker we supplied.
    mqtt_entries = hass.config_entries.async_entries(MQTT_DOMAIN)
    assert mqtt_entries, "express path did not create an MQTT config entry"
    assert mqtt_entries[0].data["broker"] == mosquitto_broker["host"]
    assert mqtt_entries[0].data["port"] == mosquitto_broker["port"]


async def test_express_step_surfaces_real_error(hass, socket_enabled):
    """Express setup must show the MQTT flow's OWN error, not a generic one.

    Regression: an unreachable broker used to be reported as the generic
    "mqtt_setup_failed", which hid the real cause. It must now surface
    "cannot_connect" so the user knows to fix the host/port.

    No broker is used here: the address is deliberately unreachable.
    """
    with (
        patch(
            "custom_components.homekey_household.config_flow.async_validate_mqtt",
            return_value=False,
        ),
        patch(
            "custom_components.homekey_household.config_flow._async_mqtt_entry_exists",
            return_value=False,
        ),
    ):
        await _start_flow(hass)
        flow_id = next(
            f
            for f in hass.config_entries.flow.async_progress()
            if f["handler"] == DOMAIN
        )["flow_id"]
        await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "mqtt_express"}
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"broker": "127.0.0.1", "port": 59999}
        )
        await hass.async_block_till_done()

    assert result["type"] == "form"
    assert result["step_id"] == "mqtt_express"
    # The specific, actionable error is shown (not "mqtt_setup_failed").
    assert result["errors"] == {"base": "cannot_connect"}
