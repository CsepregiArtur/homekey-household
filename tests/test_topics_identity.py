"""Topic construction, node identity, unique IDs, and isolation tests.

Covers requirements 1 (topic construction), 2 (node identity), 3 (entity unique
IDs), 4 (multi-node isolation), 5 (multi-household isolation), 20 (legacy topic
rejection) and 21 (replacement-node identity).
"""

from __future__ import annotations

from custom_components.homekey_household.const import (
    DOMAIN,
    ENTITY_LOCK,
    ENTITY_SENSOR_BACKUP,
    ENTITY_SENSOR_FIRMWARE,
    ENTITY_SENSOR_HEALTH,
    ENTITY_SENSOR_LAST_AUTH,
    ENTITY_SENSOR_ONLINE,
    ENTITY_SENSOR_SECURITY,
    HOUSEHOLD_ENTITY_SUFFIXES,
    LEGACY_COMMAND_TOPICS,
    RESERVED_TOPICS,
    TOPIC_BACKUP_LAST,
    TOPIC_BACKUP_STATUS,
    TOPIC_CMD_LOCK,
    TOPIC_CMD_UNLOCK,
    TOPIC_HEALTH,
    TOPIC_LAST_AUTH,
    TOPIC_SECURITY,
    TOPIC_STATE,
    TOPIC_STATUS,
    TOPIC_SUBSCRIBE_ALL,
    device_identifiers,
    node_base,
    node_topic,
    unique_id,
)


class TestTopicConstruction:
    """Requirement 1: topic construction matches the documented contract."""

    def test_node_base(self):
        assert (
            node_base("HOME-001", "GATE-001")
            == "homekey/household/HOME-001/nodes/GATE-001"
        )

    def test_each_documented_topic(self):
        b = node_base("HOME-001", "GATE-001")
        assert node_topic("HOME-001", "GATE-001", TOPIC_STATE) == f"{b}/state"
        assert node_topic("HOME-001", "GATE-001", TOPIC_STATUS) == f"{b}/status"
        assert node_topic("HOME-001", "GATE-001", TOPIC_HEALTH) == f"{b}/health"
        assert node_topic("HOME-001", "GATE-001", TOPIC_SECURITY) == f"{b}/security"
        assert (
            node_topic("HOME-001", "GATE-001", TOPIC_BACKUP_STATUS)
            == f"{b}/backup/status"
        )
        assert (
            node_topic("HOME-001", "GATE-001", TOPIC_BACKUP_LAST) == f"{b}/backup/last"
        )
        assert node_topic("HOME-001", "GATE-001", TOPIC_LAST_AUTH) == f"{b}/last_auth"

    def test_command_topics(self):
        b = node_base("HOME-001", "GATE-001")
        assert node_topic("HOME-001", "GATE-001", TOPIC_CMD_LOCK) == f"{b}/command/lock"
        assert (
            node_topic("HOME-001", "GATE-001", TOPIC_CMD_UNLOCK)
            == f"{b}/command/unlock"
        )

    def test_security_topic_is_not_security_status(self):
        """The firmware publishes ``/security``, not ``/security/status``."""
        topic = node_topic("HOME-001", "GATE-001", TOPIC_SECURITY)
        assert topic.endswith("/security")
        assert not topic.endswith("/security/status")

    def test_subscribe_pattern_covers_household(self):
        assert TOPIC_SUBSCRIBE_ALL == "homekey/household/#"

    def test_reserved_topics_are_not_declared(self):
        """Reserved topics must not be reachable through the constants."""
        from custom_components.homekey_household import const

        assert {
            "events",
            "backup/request",
            "backup/data",
            "restore/request",
            "restore/status",
        } == RESERVED_TOPICS
        # No constant should expose these as usable subtopics.
        for name in dir(const):
            if name.startswith("TOPIC_"):
                value = getattr(const, name)
                if isinstance(value, str):
                    assert value not in RESERVED_TOPICS


class TestNodeIdentity:
    """Requirement 2/21: stable identity is household_id + node_id."""

    def test_unique_id_matches_contract_format(self):
        assert unique_id("HOME-001", "GATE-001", "online") == "HOME-001_GATE-001_online"

    def test_all_documented_entity_unique_ids(self):
        hid, nid = "HOME-001", "GATE-001"
        assert unique_id(hid, nid, ENTITY_SENSOR_ONLINE) == "HOME-001_GATE-001_online"
        assert unique_id(hid, nid, ENTITY_SENSOR_HEALTH) == "HOME-001_GATE-001_health"
        assert unique_id(hid, nid, ENTITY_SENSOR_BACKUP) == "HOME-001_GATE-001_backup"
        assert (
            unique_id(hid, nid, ENTITY_SENSOR_SECURITY) == "HOME-001_GATE-001_security"
        )
        assert (
            unique_id(hid, nid, ENTITY_SENSOR_FIRMWARE) == "HOME-001_GATE-001_firmware"
        )
        assert (
            unique_id(hid, nid, ENTITY_SENSOR_LAST_AUTH)
            == "HOME-001_GATE-001_last_auth"
        )

    def test_entity_suffixes_match_contract(self):
        assert HOUSEHOLD_ENTITY_SUFFIXES == (
            "online",
            "health",
            "backup",
            "security",
            "firmware",
            "last_auth",
        )

    def test_device_identity_is_not_mac_or_device_id(self):
        """Device identity must not be a MAC or HomeKit deviceID."""
        identifiers = device_identifiers("HOME-001", "GATE-001")
        assert identifiers == {(DOMAIN, "HOME-001", "GATE-001")}
        for _, hid, nid in identifiers:
            # MAC-ish values contain ':' or 'HK-'; deviceID is bare 12 hex chars.
            assert ":" not in hid and ":" not in nid
            assert not hid.startswith("HK-")

    def test_replacement_node_is_a_new_identity(self):
        """GATE-001 -> GATE-002 must produce distinct identities."""
        old = unique_id("HOME-001", "GATE-001", ENTITY_LOCK)
        new = unique_id("HOME-001", "GATE-002", ENTITY_LOCK)
        assert old != new
        assert device_identifiers("HOME-001", "GATE-001") != device_identifiers(
            "HOME-001", "GATE-002"
        )


class TestMultiNodeIsolation:
    """Requirement 4: nodes within a household never collide."""

    def test_sibling_nodes_have_distinct_topics(self):
        gate = node_base("HOME-A", "GATE-001")
        house = node_base("HOME-A", "HOUSE-001")
        small = node_base("HOME-A", "SMALL-001")
        assert len({gate, house, small}) == 3

    def test_sibling_nodes_have_distinct_entity_ids(self):
        ids = {
            unique_id("HOME-A", node, ENTITY_SENSOR_ONLINE)
            for node in ("GATE-001", "HOUSE-001", "SMALL-001")
        }
        assert len(ids) == 3

    def test_multi_node_registry_keys(self):
        registry = {
            (hid, nid): node_base(hid, nid)
            for hid, nid in (
                ("HOME-A", "GATE-001"),
                ("HOME-A", "HOUSE-001"),
                ("HOME-A", "SMALL-001"),
            )
        }
        assert len(set(registry.values())) == 3


class TestMultiHouseholdIsolation:
    """Requirement 5: identical node ids in different households never collide."""

    def test_same_node_id_different_household(self):
        a = node_base("HOME-A", "GATE-001")
        b = node_base("HOME-B", "GATE-001")
        assert a != b

    def test_same_node_id_different_household_unique_ids(self):
        a = unique_id("HOME-A", "GATE-001", ENTITY_SENSOR_ONLINE)
        b = unique_id("HOME-B", "GATE-001", ENTITY_SENSOR_ONLINE)
        assert a != b
        assert a == "HOME-A_GATE-001_online"
        assert b == "HOME-B_GATE-001_online"

    def test_same_node_id_different_household_device_identifiers(self):
        assert device_identifiers("HOME-A", "GATE-001") != device_identifiers(
            "HOME-B", "GATE-001"
        )


class TestLegacyTopicRejection:
    """Requirement 20: legacy topics must not be used authoritatively."""

    def test_legacy_command_topics_are_enumerated(self):
        assert "homekit/set_state" in LEGACY_COMMAND_TOPICS
        assert "homekit/set_target_state" in LEGACY_COMMAND_TOPICS
        assert "homekit/set_current_state" in LEGACY_COMMAND_TOPICS

    def test_no_constant_builds_a_legacy_command_topic(self):
        """No helper in const/mqtt may emit a legacy set_* command topic."""
        from custom_components.homekey_household import const, mqtt

        for module in (const, mqtt):
            for name in dir(module):
                value = getattr(module, name)
                if isinstance(value, str) and "set_" in value:
                    assert "set_target_state" not in value
                    assert "set_state" not in value

    def test_legacy_state_and_auth_not_subscribed_as_authoritative(self):
        """Only the LWT status suffix is consumed from the legacy namespace."""
        from custom_components.homekey_household.const import (
            legacy_status_subscribe,
        )
        from custom_components.homekey_household.mqtt import (
            KIND_LEGACY_STATUS,
            parse_topic,
        )

        # The filter must be VALID MQTT: ``+`` occupies a whole topic level.
        assert legacy_status_subscribe("ESP_") == "+/status"
        parsed = parse_topic("ESP_AABBCCDD/status", "ESP_")
        assert parsed is not None and parsed.subtopic == KIND_LEGACY_STATUS

        # Legacy state/auth are parsed only so they can be explicitly rejected.
        state = parse_topic("ESP_AABBCCDD/homekit/state", "ESP_")
        assert state is not None and state.subtopic != KIND_LEGACY_STATUS
        auth = parse_topic("ESP_AABBCCDD/homekey/auth", "ESP_")
        assert auth is not None and auth.subtopic != KIND_LEGACY_STATUS

    def test_legacy_filter_is_valid_mqtt_wildcard(self):
        """Regression: ``ESP_+/status`` is INVALID MQTT (+ must be its own level).

        A real broker rejects that filter with "Invalid subscription filter",
        so the integration could never receive the shared LWT. Verified against
        paho's own wildcard validator.
        """
        from paho.mqtt.client import Client

        from custom_components.homekey_household.const import (
            legacy_status_subscribe,
        )

        validator = Client()
        filter_ = legacy_status_subscribe("ESP_")
        assert validator._filter_wildcard_len_check(filter_.encode()) == 0  # noqa: SLF001
        # The invalid embedded-wildcard form must never be produced again.
        assert "+" not in filter_.replace("+/status", "")
