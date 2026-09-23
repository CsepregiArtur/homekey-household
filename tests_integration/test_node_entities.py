"""End-to-end entity validation with a real MQTT broker and real HA (steps 7-9).

Path under test:

    emulated ESP32 -> real mosquitto -> HA core MQTT -> integration -> entities

These tests publish the **exact documented firmware 0.10.0 payloads** and assert
that Home Assistant creates the documented entities with stable unique ids and
correct device identity, with no duplicates on republish.

Covers:
  * step 7  single emulated node: all 7 entities, unique ids, device identity
  * step 8  multi-node and multi-household isolation
  * step 9  replacement node (GATE-TEST-001 -> GATE-TEST-002)
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import (
    HOUSEHOLD,
    HOUSEHOLD_OTHER,
    NODE_GATE,
    NODE_HOUSE,
    NODE_SMALL,
    node_base,
    publish_telemetry,
    settle,
    wait_for_entity,
)

DOMAIN = "homekey_household"

pytestmark = pytest.mark.usefixtures("mosquitto_broker")

# The documented entity unique-id suffixes (contract section 5).
ENTITY_SUFFIXES = ("online", "health", "backup", "security", "firmware", "last_auth")


MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 18830


async def _setup_mqtt(hass):
    """Set up HA core MQTT as a REAL config entry pointing at the test broker.

    HA 2026.9 configures MQTT through a config entry (YAML broker config was
    removed). The integration reuses this transport, so a real connection to the
    disposable mosquitto broker is established here — never a second one inside
    the integration.

    Idempotent: calling this more than once per test (e.g. when several
    households are configured) reuses the existing MQTT entry.
    """
    from homeassistant.components import mqtt

    existing = hass.config_entries.async_entries("mqtt")
    if existing:
        mqtt_entry = existing[0]
    else:
        mqtt_entry = MockConfigEntry(
            domain="mqtt",
            data={
                mqtt.CONF_BROKER: MQTT_BROKER,
                mqtt.CONF_PORT: MQTT_PORT,
                mqtt.CONF_DISCOVERY: False,
            },
            options={mqtt.CONF_BIRTH_MESSAGE: {}},
            title="MQTT (test broker)",
        )
        mqtt_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mqtt_entry.entry_id)
        await hass.async_block_till_done()

    # Confirm the real MQTT client is connected before the integration starts.
    assert await mqtt.async_wait_for_mqtt_client(hass), "MQTT client did not connect"
    return mqtt_entry


async def _setup_entry(hass, household_id: str, *, command_control: bool = True):
    """Create and set up a real config entry for a household."""
    await _setup_mqtt(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "household_id": household_id,
            "household_name": household_id,
            "command_control": command_control,
        },
        unique_id=f"{DOMAIN}_{household_id}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_ids(household_id: str, node_id: str) -> dict[str, str]:
    """Map the documented suffix -> the unique_id an entity must carry."""
    return {s: f"{household_id}_{node_id}_{s}" for s in ENTITY_SUFFIXES}


def _find_entity_id_by_unique_id(hass, unique_id: str) -> str | None:
    """Locate a live entity_id from the entity registry by unique_id."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity in registry.entities.values():
        if entity.unique_id == unique_id and entity.platform == DOMAIN:
            return entity.entity_id
    return None


async def _wait_for_registry_entries(hass, unique_ids, timeout: float = 10.0):
    """Wait until all unique_ids appear in the real entity registry."""
    deadline = asyncio.get_running_loop().time() + timeout
    found: dict[str, str | None] = {}
    while asyncio.get_running_loop().time() < deadline:
        found = {u: _find_entity_id_by_unique_id(hass, u) for u in unique_ids}
        if all(v is not None for v in found.values()):
            return found
        await asyncio.sleep(0.1)
    return found


class TestSingleEmulatedNode:
    """Step 7: entities, unique ids, device identity, no duplicates."""

    async def test_all_documented_entities_are_created(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)

        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        # Discovery triggers a debounced reload; give it time to register entities.
        expected = _entity_ids(HOUSEHOLD, NODE_GATE)
        expected["lock"] = f"{HOUSEHOLD}_{NODE_GATE}_lock"
        found = await _wait_for_registry_entries(hass, expected.values())

        missing = [uid for uid, eid in found.items() if eid is None]
        assert not missing, f"missing entities for unique ids: {missing}"

    async def test_entity_unique_ids_match_contract(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        expected = _entity_ids(HOUSEHOLD, NODE_GATE)
        expected["lock"] = f"{HOUSEHOLD}_{NODE_GATE}_lock"
        found = await _wait_for_registry_entries(hass, expected.values())

        for unique_id, entity_id in found.items():
            assert entity_id is not None, f"unique_id not registered: {unique_id}"

    async def test_device_identity_is_household_plus_node(self, hass, mqtt_client):
        """Device identifiers must be (DOMAIN, household_id, node_id)."""
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        found = await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{NODE_GATE}_online"]
        )
        entity_id = found[f"{HOUSEHOLD}_{NODE_GATE}_online"]
        assert entity_id is not None

        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entity = er.async_get(hass).async_get(entity_id)
        assert entity is not None and entity.device_id

        device = dr.async_get(hass).async_get(entity.device_id)
        assert device is not None
        # Stable logical identity; never MAC or HomeKit deviceID.
        assert (DOMAIN, HOUSEHOLD, NODE_GATE) in device.identifiers
        for _domain, hid, nid in device.identifiers:
            assert ":" not in hid and ":" not in nid
            assert not hid.startswith("HK-")

    async def test_node_online_reflects_status(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        found = await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{NODE_GATE}_online"]
        )
        entity_id = found[f"{HOUSEHOLD}_{NODE_GATE}_online"]
        assert entity_id is not None

        state = await wait_for_entity(hass, entity_id, condition=lambda s: s.state == "on")
        assert state is not None and state.state == "on"

    async def test_security_sensor_exposes_raw_documented_value(
        self, hass, mqtt_client
    ):
        """Security must be the raw OK/WARNING string, never a numeric score."""
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(
            mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test", security="WARNING"
        )
        await settle(hass)

        found = await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{NODE_GATE}_security"]
        )
        entity_id = found[f"{HOUSEHOLD}_{NODE_GATE}_security"]
        assert entity_id is not None

        state = await wait_for_entity(
            hass, entity_id, condition=lambda s: s.state == "WARNING"
        )
        assert state is not None and state.state == "WARNING"

    async def test_last_auth_sensor_reflects_result(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(
            mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test", last_auth_result="SUCCESS"
        )
        await settle(hass)

        found = await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{NODE_GATE}_last_auth"]
        )
        entity_id = found[f"{HOUSEHOLD}_{NODE_GATE}_last_auth"]
        assert entity_id is not None

        state = await wait_for_entity(
            hass, entity_id, condition=lambda s: s.state == "SUCCESS"
        )
        assert state is not None and state.state == "SUCCESS"

    async def test_firmware_sensor_reflects_state_payload(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        found = await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{NODE_GATE}_firmware"]
        )
        entity_id = found[f"{HOUSEHOLD}_{NODE_GATE}_firmware"]
        assert entity_id is not None

        state = await wait_for_entity(
            hass, entity_id, condition=lambda s: s.state == "0.10.0"
        )
        assert state is not None and state.state == "0.10.0"

    async def test_republished_retained_messages_create_no_duplicates(
        self, hass, mqtt_client
    ):
        """Retained republish / reconnect must not duplicate entities."""
        await _setup_entry(hass, HOUSEHOLD)

        for _ in range(3):
            await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
            await settle(hass, seconds=1.0)

        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        online_matches = [
            e
            for e in registry.entities.values()
            if e.unique_id == f"{HOUSEHOLD}_{NODE_GATE}_online"
            and e.platform == DOMAIN
        ]
        assert len(online_matches) == 1, "duplicate entities after republish"

        # And only one device for the node.
        from homeassistant.helpers import device_registry as dr

        devices = [
            d
            for d in dr.async_get(hass).devices
            if (DOMAIN, HOUSEHOLD, NODE_GATE) in d.identifiers
        ]
        assert len(devices) == 1, "duplicate devices after republish"


class TestMultiNodeAndMultiHousehold:
    """Step 8: nodes and households never collide."""

    async def test_three_nodes_in_one_household(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)

        for node_id, name in (
            (NODE_GATE, "Gate"),
            (NODE_HOUSE, "Main House"),
            (NODE_SMALL, "Small House"),
        ):
            await publish_telemetry(mqtt_client, HOUSEHOLD, node_id, name)
            await hass.async_block_till_done()

        online_ids = [f"{HOUSEHOLD}_{n}_online" for n in (NODE_GATE, NODE_HOUSE, NODE_SMALL)]
        found = await _wait_for_registry_entries(hass, online_ids)
        assert all(v is not None for v in found.values()), found
        # Distinct entity ids: no collision.
        assert len(set(found.values())) == 3

    async def test_three_nodes_produce_three_devices(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        for node_id, name in (
            (NODE_GATE, "Gate"),
            (NODE_HOUSE, "Main House"),
            (NODE_SMALL, "Small House"),
        ):
            await publish_telemetry(mqtt_client, HOUSEHOLD, node_id, name)
            await hass.async_block_till_done()

        await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{NODE_GATE}_online", f"{HOUSEHOLD}_{NODE_HOUSE}_online", f"{HOUSEHOLD}_{NODE_SMALL}_online"]
        )

        from homeassistant.helpers import device_registry as dr

        devices = [
            d
            for d in dr.async_get(hass).devices
            if any(i[0] == DOMAIN and i[1] == HOUSEHOLD for i in d.identifiers)
        ]
        assert len(devices) == 3, f"expected 3 devices, got {len(devices)}"

    async def test_same_node_id_in_two_households_are_separate(self, hass, mqtt_client):
        """HOME-OTHER/GATE-TEST-001 must be entirely separate from HOME-TEST's."""
        await _setup_entry(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD_OTHER)

        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate A")
        await publish_telemetry(mqtt_client, HOUSEHOLD_OTHER, NODE_GATE, "Gate B")
        await hass.async_block_till_done()

        ids = [
            f"{HOUSEHOLD}_{NODE_GATE}_online",
            f"{HOUSEHOLD_OTHER}_{NODE_GATE}_online",
        ]
        found = await _wait_for_registry_entries(hass, ids)
        assert all(v is not None for v in found.values()), found
        assert found[ids[0]] != found[ids[1]]

        from homeassistant.helpers import device_registry as dr

        devices = [
            d
            for d in dr.async_get(hass).devices
            if any(i[0] == DOMAIN and i[2] == NODE_GATE for i in d.identifiers)
        ]
        assert len(devices) == 2, "households must not share a device"


class TestReplacementNode:
    """Step 9: GATE-TEST-001 replaced by GATE-TEST-002 is a distinct node."""

    async def test_replacement_node_gets_distinct_identity(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Old")
        await hass.async_block_till_done()
        await _wait_for_registry_entries(hass, [f"{HOUSEHOLD}_{NODE_GATE}_online"])

        replacement = "GATE-TEST-002"
        await publish_telemetry(mqtt_client, HOUSEHOLD, replacement, "Gate New")
        await hass.async_block_till_done()

        found = await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{replacement}_online"]
        )
        new_entity = found[f"{HOUSEHOLD}_{replacement}_online"]
        assert new_entity is not None

        # Distinct unique ids and distinct devices.
        assert f"{HOUSEHOLD}_{NODE_GATE}_online" != f"{HOUSEHOLD}_{replacement}_online"

        from homeassistant.helpers import device_registry as dr

        devices = {
            d.id: d
            for d in dr.async_get(hass).devices
            if any(i[0] == DOMAIN and i[1] == HOUSEHOLD for i in d.identifiers)
        }
        idents = {tuple(sorted(d.identifiers)) for d in devices.values()}
        assert len(idents) == 2, f"replacement must be a new device: {idents}"

        # Distinct MQTT namespaces (topics), asserted structurally.
        assert node_base(HOUSEHOLD, NODE_GATE) != node_base(HOUSEHOLD, replacement)

    async def test_old_node_can_go_offline_independently(self, hass, mqtt_client):
        """The old node can become unavailable without affecting the new one."""
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Old")
        replacement = "GATE-TEST-002"
        await publish_telemetry(mqtt_client, HOUSEHOLD, replacement, "Gate New")
        await hass.async_block_till_done()

        found = await _wait_for_registry_entries(
            hass,
            [f"{HOUSEHOLD}_{NODE_GATE}_online", f"{HOUSEHOLD}_{replacement}_online"],
        )
        old_entity = found[f"{HOUSEHOLD}_{NODE_GATE}_online"]
        new_entity = found[f"{HOUSEHOLD}_{replacement}_online"]
        assert old_entity and new_entity

        # Old node publishes offline; new node stays online.
        await mqtt_client.publish(
            f"{node_base(HOUSEHOLD, NODE_GATE)}/status", "offline", qos=1, retain=True
        )
        await hass.async_block_till_done()

        old_state = await wait_for_entity(
            hass, old_entity, condition=lambda s: s.state == "off"
        )
        new_state = hass.states.get(new_entity)
        assert old_state is not None and old_state.state == "off"
        assert new_state is not None and new_state.state == "on"


class TestHealthStubsPreserved:
    """Documented stub fields must be preserved verbatim, never fabricated."""

    async def test_health_attributes_include_unknown_stubs(self, hass, mqtt_client):
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        found = await _wait_for_registry_entries(
            hass, [f"{HOUSEHOLD}_{NODE_GATE}_health"]
        )
        entity_id = found[f"{HOUSEHOLD}_{NODE_GATE}_health"]
        assert entity_id is not None

        state = await wait_for_entity(hass, entity_id, condition=lambda s: s.state == "OK")
        assert state is not None
        attrs = state.attributes
        assert attrs.get("network") == "UNKNOWN"
        assert attrs.get("certificate") == "unknown"
        assert attrs.get("household_id") == HOUSEHOLD
        assert attrs.get("node_id") == NODE_GATE
