# HA V2 — Contract Compliance and Test Map

This document maps every required behaviour from the HomeKey Household task to the
code that implements it and the tests that verify it. It exists so the
implementation can be audited without reading the whole test suite.

Authoritative firmware contract (read-only, **not modified** by this work):

* `docs/content/mqtt_household_api.md`
* `docs/content/mqtt_api_contract_matrix.md`
* `main/MqttManager.cpp` — `handleSecureCommand`, `makeCommandMac`
* `main/HouseholdManager.cpp` — `deriveCommandKey`

Firmware version implemented against: **0.10.0**.

---

## 1. Requirement → implementation → test

| # | Requirement | Implementation | Tests |
|---|---|---|---|
| 1 | Topic construction | `const.node_base`, `const.node_topic` | `test_topics_identity.py::TestTopicConstruction` |
| 2 | Node identity | `const.unique_id`, `const.device_identifiers` | `TestNodeIdentity` |
| 3 | Entity unique ids | `const.unique_id`, `const.HOUSEHOLD_ENTITY_SUFFIXES` | `TestNodeIdentity::test_all_documented_entity_unique_ids` |
| 4 | Multi-node isolation | `coordinator.HomeKeyHouseholdCoordinator` keyed by `node_id` | `TestMultiNodeIsolation`, `test_coordinator_state.py::TestIsolation` |
| 5 | Multi-household isolation | `coordinator.household_id` gate + identity in unique ids | `TestMultiHouseholdIsolation`, `TestIsolation::test_other_household_messages_ignored` |
| 6 | State parsing | `models.NodeState` | `test_payload_parsing.py::TestStateParsing` |
| 7 | Health parsing | `models.NodeHealth` | `TestHealthParsing` |
| 8 | Backup parsing | `models.BackupRecord` | `TestBackupParsing` |
| 9 | Security parsing | `coordinator._handle_security`, `const.SecurityState` | `TestSecurityParsing` |
| 10 | Last-auth parsing | `models.LastAuth` | `TestLastAuthParsing` |
| 11 | HMAC canonical input | `command.canonical_input` | `test_command_hmac.py::TestCanonicalInput` |
| 12 | HMAC-SHA256 generation | `command.make_command_mac` | `TestMacGeneration` |
| 13 | Timestamp handling | `command.current_epoch_seconds`, `build_command` | `TestTimestamp` |
| 14 | Nonce generation | `command.new_nonce`, `command.NonceTracker` | `TestNonce` |
| 15 | Request id generation | `command.new_request_id` | `TestRequestId` |
| 16 | Lock command | `mqtt.HomeKeyMqttClient.async_lock`, `lock.HomeKeyLock` | `test_mqtt_client.py::TestCommandPublishing`, `TestBuildCommand` |
| 17 | Unlock command | `mqtt.HomeKeyMqttClient.async_unlock` | `TestCommandPublishing::test_unlock_publishes_to_unlock_topic` |
| 18 | Malformed command data | strict validators, fail-closed publish | `test_coordinator_state.py::TestMalformedPayloads`, `TestBuildCommand::test_malformed_request_id_rejected` |
| 19 | Missing credentials | `coordinator.async_send_lock_command` fail-closed | `test_coordinator_state.py::TestFailClosedCommands`, `TestCommandPublishing::test_missing_credential_fails_closed` |
| 20 | Legacy topic rejection | `mqtt.parse_topic`, `const.LEGACY_COMMAND_TOPICS` | `TestLegacyTopicRejection`, `test_mqtt_client.py::TestDispatch` |
| 21 | Replacement-node identity | `const.unique_id`/`device_identifiers` | `TestNodeIdentity::test_replacement_node_is_a_new_identity` |
| 22 | Logging / no secrets | `credential`, `diagnostics._redact` | `test_credential_storage.py` |
| 23 | Error handling | `coordinator.async_handle_message` | `TestMalformedPayloads`, `TestFailClosedCommands` |
| 24 | Tests | `tests/` | full suite |
| 25 | Documentation | `docs/HA_V2_INTEGRATION.md`, `README.md` | — |

---

## 2. Contract facts verified against the firmware

| Fact | Verified by |
|---|---|
| `B/health` contains **no** `household_id`/`node_id` | `HealthManager::toJson`; `test_health_has_no_identity_fields_in_firmware` |
| `B/state` always contains identity | `publishNodeStatus`; `TestStateParsing` |
| `B/security` is the raw string `OK`/`WARNING` | `publishNodeStatus`; `TestSecurityParsing` |
| `B/backup/status` is the raw string `completed`/`failed` | `publishBackupStatus`; `TestBackupParsing` |
| `B/backup/last` is `{status,timestamp}` | `publishBackupStatus`; `TestBackupParsing` |
| `B/last_auth` is `{type,result,timestamp}` | `publishLastAuth`; `TestLastAuthParsing` |
| `B/status` publishes only `online` | `publishNodeStatus`; `TestAvailability` |
| Command payload has no `action` field | `handleSecureCommand`; `TestBuildCommand::test_payload_has_no_action_field` |
| MAC input is `{ts}{nonce}{req_id}{action}` | `makeCommandMac`; `TestCanonicalInput` |
| Key derives via BLAKE2b(`secret‖salt`, key=label, 32) | `deriveCommandKey`; `TestKeyDerivation` |
| Replay window is 32 | `m_seenNonces`; `TestNonce::test_replay_window_constant_matches_firmware` |
| Freshness window is ±300 s | `handleSecureCommand`; `TestTimestamp::test_firmware_window_constant` |

---

## 3. Firmware integrity

No firmware file was modified. The ESP32 MQTT contract, `MqttManager.cpp`, the
HMAC protocol, topic names, payload schemas, and the firmware version are
unchanged. The firmware was inspected read-only to derive the contract this
integration implements.
