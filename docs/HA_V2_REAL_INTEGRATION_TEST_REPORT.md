# HA V2 Real Integration Test Report

HomeKey Household V2 — Home Assistant integration vs. HomeKey-ESP32 **0.10.0**
household MQTT contract.

Generated: 2026-09-22
Scope: real MQTT broker + real Home Assistant. **No hardware was available.**

---

## A. Environment

| Item | Value |
|---|---|
| OS | macOS 26.6 (build 25G72), arm64 |
| Home Assistant | **2026.9.3** (real instance, bootstrapped via the official test harness) |
| MQTT broker | **Mosquitto 2.1.2** (Homebrew), loopback-only, port 18830, disposable |
| Docker | NOT AVAILABLE (used a venv + Homebrew instead) |
| ESP-IDF | `/Users/artur/esp/esp-idf` (present, unused — no hardware) |
| ESP32 model | **NONE CONNECTED** (no USB/serial device detected) |
| Firmware version | 0.10.0 (tag `v0.10.0`; working tree clean) |
| Python | 3.14.6 (`.venv`) |
| Test extras | `pytest-homeassistant-custom-component` 0.13.366, `aiomqtt` 2.5.1 |

### Firmware integrity (step 3 / step 20)

| | Value |
|---|---|
| Baseline HEAD | `328a6c35311c6e3bee317b8439ad5e8d51316f57` |
| Final HEAD | `5ace9d624702e51a0115823982ab96258a1eee43` (`v0.10.0-2-g5ace9d6`) |
| Baseline `git status --porcelain` | ` M sdkconfig.defaults`, ` M with_ota.csv`, `?? keys/` |
| Final `git status --porcelain` | **empty (clean)** |

**I did not modify the firmware.** During the session the repository advanced by
**5 commits authored by the user** (`7861ea8`, `9bb9548`, `31387d7`, `ae3eb43`,
`5ace9d6`), which also committed the previously-dirty `sdkconfig.defaults`,
`with_ota.csv`, and gitignored `keys/`.

**MQTT contract files verified byte-identical** between baseline and final HEAD:

```
IDENTICAL  main/MqttManager.cpp
IDENTICAL  main/include/MqttManager.hpp
IDENTICAL  main/HouseholdManager.cpp
IDENTICAL  main/include/HouseholdManager.hpp
IDENTICAL  main/include/household_types.hpp
IDENTICAL  main/HealthManager.cpp
```

No `MqttManager.cpp`, HMAC, topic, or payload-schema change occurred. No firmware
version bump was made by this task.

---

## B. Software tests (real broker + real HA)

| Test group | Result | Evidence |
|---|---|---|
| Broker semantics (step 5) | **PASS 8/8** | see below |
| HA bootstrap + integration load (step 6) | **PASS 6/6** | `tests_integration/test_ha_bootstrap.py` |
| Emulated single node — entities/unique IDs/device identity (step 7) | **PASS** | `tests_integration/test_node_entities.py` |
| Multi-node (3 nodes in one household) (step 8) | **PASS** | same |
| Multi-household (`HOME-OTHER/GATE-TEST-001` separate) (step 8) | **PASS** | same |
| Replacement node (`GATE-TEST-001` → `GATE-TEST-002`) (step 9) | **PASS** | same |
| No duplicates on repeated retained publishes (step 7) | **PASS** | same |
| Real HMAC lock/unlock over MQTT (step 10) | **NOT TESTED** | phase not reached this session |
| Negative command tests (step 11) | **NOT TESTED** | phase not reached this session |

### Suite totals

```
=== BROKER SEMANTICS ===
[PASS] connection over TCP
[PASS] QoS 0 + QoS 1 publish/subscribe
[PASS] retained delivered to late subscriber
[PASS] retained survives subscriber reconnect
[PASS] retained delivery respects subscription filter
[PASS] retained respects '+' wildcard
[PASS] empty retained payload clears the topic
[PASS] LWT fires on unclean disconnect
8/8 broker checks passed

=== UNIT / CONTRACT SUITE ===
209 passed

=== INTEGRATION: HA BOOTSTRAP ===
6 passed

=== INTEGRATION: NODE ENTITIES (real MQTT + real HA) ===
14 passed
```

Static checks: `ruff check` **All checks passed**;
`mypy custom_components/homekey_household` **no issues in 14 source files**.

### Broker semantics detail (step 5)

The retained-delivery checks matter most: every important firmware topic
(`B/state`, `B/status`, `B/security`, `B/backup/status`, `B/backup/last`,
`B/last_auth`) is **retained**. Mosquitto passed the filter-isolation check,
including that a subscription to an *empty* topic receives nothing.

---

## C. Hardware tests

| Test | Result |
|---|---|
| ESP32 connection (step 12) | **NOT TESTED** — no device detected |
| State telemetry from real node (step 12) | **NOT TESTED** — no device |
| Physical lock state / `lock_current` (step 13) | **BLOCKED** — no actuator |
| HA → ESP32 lock command (step 14) | **BLOCKED** — no ESP32 to receive it |
| ESP32 → HA state (step 14) | **NOT TESTED** — no device |
| HomeKey authentication (step 15) | **NOT TESTED** — no reader/credential |

Hardware phases were **not** simulated into a pass. No hardware result is
claimed.

---

## D. Resilience

| Test | Result |
|---|---|
| MQTT disconnect/reconnect (step 16) | **NOT TESTED** — phase not reached this session |
| ESP32 reboot (step 17) | **NOT TESTED** — no hardware |
| Home Assistant restart (step 18) | **NOT TESTED** — phase not reached this session |

LWT behaviour itself **PASS** at broker level (will fires on unclean disconnect).

---

## E. Security

| Check | Result |
|---|---|
| Secret exposure in logs (step 19) | **NOT TESTED** — full cross-phase log scan not completed |
| Legacy topic usage | **PASS** — integration never publishes `P/homekit/set_*`; verified by test |
| HMAC verification over MQTT (step 10) | **NOT TESTED** — phase not reached |
| Fail-closed behaviour | **PASS** (unit level) — 19 tests cover missing credential/transport |

---

## F. Bugs found by real-environment testing

Real MQTT + real HA exposed **six defects that the 208 hermetic unit tests could
not detect**. All six are fixed and re-verified.

### 1. Invalid MQTT subscription filter — CRITICAL
* **Component**: `custom_components/homekey_household/const.py`
  → `legacy_status_subscribe()`
* **Defect**: returned `ESP_+/status`. MQTT requires `+` to occupy an **entire**
  topic level; paho raises `Invalid subscription filter` and the subscription
  **never happens**.
* **Impact**: against a real broker the integration could never receive the
  shared LWT availability signal. Only reproducible with a real client/broker.
* **Fix**: return `+/status` (valid, matches `ESP_A1B2C3D4/status`); legacy prefix
  scoping remains enforced by the topic parser.
* **Regression test**: `tests/test_topics_identity.py::TestLegacyTopicRejection`
  now validates the filter with paho's own wildcard validator.

### 2. Zero entities created on first setup — CRITICAL
* **Component**: `custom_components/homekey_household/__init__.py`,
  `coordinator.py`
* **Defect**: retained MQTT messages are delivered *asynchronously* after
  `async_subscribe`, so `async_forward_entry_setups()` ran against an **empty
  node registry** and created no entities. The debounced reload then rebuilt the
  coordinator from scratch, so it never converged.
* **Impact**: a user would see the integration load successfully but produce
  **no entities at all** until a restart.
* **Fix**: `coordinator.async_wait_for_initial_nodes()` waits for retained
  messages before platform setup; platforms now register entities incrementally
  for later-discovered nodes (no config-entry reload).

### 3. Enum sensors missing device class — HIGH
* **Component**: `sensor.py`
* **Defect**: declared `_attr_options` without `SensorDeviceClass.ENUM`.
* **Impact**: HA raises `Sensor ... is providing enum options, but is missing the
  enum device class` when the state is read → health/security/backup/last_auth
  sensors unusable.
* **Fix**: setting `options` now also sets `SensorDeviceClass.ENUM`.

### 4. Reload discarded non-retained state — HIGH
* **Component**: `coordinator.py`
* **Defect**: a discovery reload rebuilt the coordinator, losing `B/health`
  (non-retained — the broker never replays it).
* **Impact**: lock state and health went permanently unknown after discovery.
* **Fix**: node state pooled in `hass.data` and reattached on reload; reloads
  only for genuinely new nodes (now not needed at all).

### 5. Lingering reload timer leak — MEDIUM
* **Component**: `coordinator.py`, `__init__.py`
* **Defect**: the debounced reload timer was never cancelled on unload.
* **Impact**: leaked timer; could reload a config entry that is being removed.
* **Fix**: `async_cancel_pending_reload()` called on unload.

### 6. `extra_state_attributes` returned `None` — MEDIUM
* **Component**: `entity.py`
* **Defect**: returned `None` for a known node, so HA dropped **all** attributes
  (and subclasses would have `.update()` on `None`).
* **Fix**: always returns a dict.

### Tooling note (not a product defect)
ha-mqtt integration testing also required: symlinking the integration into the
harness config dir, `enable_custom_integrations`, real sockets enabled, an MQTT
*config entry* (HA 2026.9 removed YAML broker config), and a
`configuration.yaml`. The `pytest_homeassistant_custom_component` plugin must be
disabled for the hermetic unit suite
(`addopts = "-p no:homeassistant"`).

---

## G. Test infrastructure added

| Path | Purpose |
|---|---|
| `tools/integration/mosquitto.test.conf` | Disposable loopback-only broker config (port 18830) |
| `tools/integration/verify_broker_semantics.py` | 8 broker-semantics checks against real Mosquitto |
| `tests_integration/conftest.py` | Real-broker + real-HA harness fixtures |
| `tests_integration/pytest.ini` | Separate config for the integration suite |
| `tests_integration/helpers.py` | Documented payload builders + wait helpers |
| `tests_integration/test_ha_bootstrap.py` | HA bootstrap / integration load (6 tests) |
| `tests_integration/test_node_entities.py` | Entities, multi-node, multi-household, replacement (14 tests) |

Run:

```bash
# hermetic unit suite
.venv/bin/python -m pytest -q

# real broker + real HA
.venv/bin/python -m pytest tests_integration -c tests_integration/pytest.ini -q
.venv/bin/python tools/integration/verify_broker_semantics.py
```

---

## H. Blockers

1. **No ESP32 hardware** — all physical/hardware phases are NOT TESTED / BLOCKED.
   Nothing hardware-related was faked or inferred.
2. **amqtt unusable as a validation broker** (encountered and discarded):
   amqtt 0.12.1 and 0.11.4 replay retained messages to subscribers whose topic
   filter does **not** match (a client subscribed to an unrelated empty topic
   still received retained payloads). Because every key firmware topic is
   retained, amqtt results would be meaningless. Replaced with Mosquitto 2.1.2.
3. **Remaining phases not yet executed**: HMAC command over MQTT (step 10),
   negative commands (step 11), MQTT disconnect/reconnect (step 16), HA restart
   (step 18), full secret-exposure log scan (step 19).

---

## I. Final decision

# NOT READY — BLOCKERS REMAIN

Rationale, based only on executed tests:

* The integration is now proven to work **end-to-end against a real broker and a
  real Home Assistant instance** for discovery, entities, identity and state —
  and six genuine, previously-shipping defects were found and fixed.
* However, the **authoritative lock/unlock HMAC command path over MQTT was not
  executed**, the resilience phases (disconnect/reconnect, HA restart) were not
  executed, and the security log scan was not completed.
* **No physical hardware was tested at all.**

A pilot cannot be declared from these results. The next step is to run the
remaining phases (steps 10, 11, 16, 18, 19), and to repeat the whole suite with a
real ESP32 to cover steps 12–15 and 17.
