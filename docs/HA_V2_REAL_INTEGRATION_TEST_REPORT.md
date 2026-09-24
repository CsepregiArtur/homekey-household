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
| Final HEAD | `9bef9e15f5648d9b3ec0ff1dc22c1b470e0e670a` (`v0.10.0-4-g9bef9e1`) |
| Baseline `git status --porcelain` | ` M sdkconfig.defaults`, ` M with_ota.csv`, `?? keys/` |
| Final `git status --porcelain` | **empty (clean)** |

**I did not modify the firmware.** The repository advanced through commits authored
by the user (all CI/docs/build plumbing). The tag `v0.10.0` is unchanged, i.e. the
MQTT contract version was **not bumped**.
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
| Real HMAC lock/unlock over MQTT (step 10) | **PASS** | `tests_integration/test_hmac_commands.py` (11 tests) |
| Negative command tests (step 11) | **PASS** | `tests_integration/test_negative_commands.py` (29 tests) |
| Resilience: disconnect/reconnect, HA restart, retained vs non-retained (steps 16/18) | **PASS 10/10** | `tests_integration/test_resilience.py` |

### Suite totals

```
=== BROKER SEMANTICS ===
8/8 broker checks passed

=== UNIT / CONTRACT SUITE ===
209 passed

=== INTEGRATION: HA BOOTSTRAP ===
6 passed

=== INTEGRATION: NODE ENTITIES (real MQTT + real HA) ===
14 passed

=== INTEGRATION: HMAC COMMANDS (real MQTT + real HA) ===
11 passed

=== INTEGRATION: NEGATIVE / SECURITY COMMANDS ===
29 passed

=== INTEGRATION: RESILIENCE (disconnect/reconnect, HA restart) ===
10 passed
```

**Integration suite total: 70 tests** (`pytest tests_integration -c
tests_integration/pytest.ini` collects 70 = 6 + 14 + 11 + 29 + 10; all passed).

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
| MQTT disconnect/reconnect (step 16) | **PASS 10/10** — `tests_integration/test_resilience.py` |
| LWT offline then recovery | **PASS** — broker published the will; HA marked the node offline (see below) |
| ESP32 reboot (step 17) | **NOT TESTED** — no hardware |
| Home Assistant restart (step 18) | **PASS 3/3** — `TestHaRestartPersistence` |

LWT behaviour **PASS** at broker level (will fires on unclean disconnect) *and*
end-to-end in HA.

### D.1 LWT (Last Will) test — how the unclean drop is produced

A Last Will is published by a **live broker** when it detects that a client
vanished *without* sending an MQTT `DISCONNECT`. Two approaches do **not** work,
and both were ruled out experimentally:

* Killing the **broker** cannot deliver a will — nothing is left to publish it.
* A graceful `client.disconnect()` **suppresses** the will.
* Force-closing an `aiomqtt`/`paho` socket from inside the test **corrupts that
client**: paho still owns the socket and later calls
`loop.add_writer(sock.fileno(), ...)`. After a close, `fileno()` is `-1`, so
teardown dies with `ValueError: Invalid file descriptor: -1`.

The working mechanism is **process isolation**: `tests_integration/lwt_node_helper.py`
is a disposable helper process that connects, registers the will, publishes the
retained `online` status, then parks. The test `SIGKILL`s it — the MQTT equivalent
of a cable pull — so no `DISCONNECT` is sent and the broker publishes `offline`.

The test explicitly proves all seven required steps:

1. node initially available (HA `on`)
2. unexpected disconnect occurs (`SIGKILL`, no `DISCONNECT`)
3. broker publishes the `offline` will (independent broker-side subscriber)
4. HA reflects it (`off` — see note)
5. client reconnects
6. node republishes `online`
7. HA recovers (`on`)

**Semantics note (verified, not assumed):** the node-online entity is a
**connectivity** binary sensor, so a node that *reports* `offline` reads **`off`**
— `unavailable` is reserved for a node the integration cannot read at all. An
earlier draft of this test asserted `unavailable` and failed against correct
product behaviour; the assertion was corrected to `off`. This is a test fix, not
a product change.

**Fidelity of "unclean":** `SIGKILL` tears the process down with no TCP `FIN` and
no MQTT `DISCONNECT`; the broker then holds the connection until the keepalive
expires and fires the will. This is the same path a real ESP32 crash/cable-pull
takes.

Verification: isolated run **3/3 PASS**; full module **10 passed, 0 errors**
(no teardown exception, no interrupted loop, no invalid file descriptor, no
lingering task).

---

## E. Security

| Check | Result |
|---|---|
| Secret exposure in logs (step 19) | **PASS (product)** — see E.1; one HA-core DEBUG finding |
| Legacy topic usage | **PASS** — integration never publishes `P/homekit/set_*`; verified by test |
| HMAC verification over MQTT (step 10) | **PASS** — 11 tests; lock and unlock accepted only with a valid MAC |
| Fail-closed behaviour | **PASS** — 29 negative tests: missing credential, tampered ts/nonce/req_id/mac, wrong action/topic, stale/future ts, replayed nonce, malformed/typed payloads |

### E.1 Secret-exposure log scan (step 19) — method and result

A dedicated scanner (`tools/integration/scan_logs_for_secrets.py`) runs the real
HMAC + negative-command tests while capturing **all** log output at DEBUG, then
searches the captured text for the **actual values** of every secret present
during the run and **attributes each match to the logger that emitted it**.
Raw evidence is written to `/tmp/step19_scan.log`. The scan exits non-zero only
for integration-emitted secret material, so it can gate CI.

Secrets searched (actual values, not names):

* the recovery secret (`integration-test-recovery-secret`)
* the salt value (`integration-test-salt`)
* the derived command key, hex form (re-derived independently via the documented
  BLAKE2b KDF — the scanner does not import the integration)
* command MACs (the `"mac"` field of the command payload)

Result, attributed by logger:

| Value | Integration (`custom_components.*`) | Test harness | HA core MQTT client |
|---|---|---|---|
| recovery secret | **absent** | absent | absent |
| salt value | **absent** | present | absent |
| derived command key (hex) | **absent** | present | absent |
| command MAC | **absent** | absent | **present (238×)** |

**Product verdict: PASS.** The integration never logs the recovery secret, the
derived command key, the salt or the MAC at any level. This was verified both
statically (every `_LOGGER.*` call site in `custom_components/**` inspected; all
emit only node/req_id/subtopic identifiers) and dynamically (the scan above).
`diagnostics.py` additionally redacts key material and exposes only a one-way
fingerprint (`SHA-256(key)[:8]`).

**F1 — Test-harness exposure (not a product defect).**
`pytest_homeassistant_custom_component.common` logs the **full payload** of every
`Store` read/write at DEBUG (`common.py:1550` `"Loading data for %s: %s"`, and
`:1558` `"Writing data to %s: %s"`). Because the integration persists the derived
command key via `Store`, the harness prints the storage JSON — key hex and salt
included. Home Assistant's **real** `Store`
(`homeassistant/helpers/storage.py`) logs only the store key and the file path
(`:614`), never the contents, so this exposure exists **only in tests**.
Severity: test-infrastructure. No product change is warranted or made.

**F2 — HA core MQTT client logs command MACs at DEBUG (core behaviour, not an
integration defect).**
`homeassistant.components.mqtt.client` logs the entire payload of every
publish/receive at DEBUG (`client.py:772` "Transmitting message on …" and
`client.py:1337` "Received message on …"), so the 4-field command payload —
including `mac` — appears in the log (238 occurrences in this run). The MAC is
**not** a stored secret and cannot be replayed (the firmware bounds the
timestamp to ±300 s and tracks a bounded nonce window), but it is
command-authentication material. The integration logs only `req_id`
(`mqtt.py`, `_LOGGER.info("Published authenticated %s command … (req_id=%s)")`).
Its own transport log cannot be suppressed by the integration.

**Operational recommendation (no contract change): do not run Home Assistant with
`homeassistant.components.mqtt.client` at DEBUG in production.** This is a
deployment note, not a code defect.

---

## F. Bugs found by real-environment testing

Real MQTT + real HA exposed **six defects that the hermetic unit tests could
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
| `tests_integration/test_hmac_commands.py` | Real HMAC lock/unlock over MQTT (11 tests) |
| `tests_integration/test_negative_commands.py` | Fail-closed / tamper / replay security tests (29 tests) |
| `tests_integration/test_resilience.py` | Disconnect/reconnect, LWT, HA restart, retained vs non-retained (10 tests) |
| `tests_integration/lwt_node_helper.py` | Disposable process that is `SIGKILL`ed to trigger a real LWT (see D.1) |
| `tools/integration/firmware_verifier.py` | Independent reimplementation of the firmware HMAC verification (no shared code with the integration) |
| `tools/integration/scan_logs_for_secrets.py` | Step 19: captures DEBUG logs from a real run and attributes any secret/MAC match to its logger (see E.1) |

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
3. **Software/integration scope is complete.** Step 19 (secret-exposure log scan)
   is now executed (section E.1); every remaining gap is the absence of physical
   hardware.

---

## I. Final decision

# SOFTWARE INTEGRATION READY — HARDWARE VALIDATION REMAINS

Scope of this decision: **software and integration only**. Hardware is explicitly
excluded (see C and H.1).

Every software/integration phase has now been executed against a **real MQTT
broker and a real Home Assistant instance**:

| Area | Result |
|---|---|
| Broker semantics | PASS 8/8 |
| HA bootstrap / integration load | PASS 6/6 |
| Node entities, identity, multi-node, multi-household, replacement | PASS 14/14 |
| HMAC lock over real MQTT | PASS |
| HMAC unlock over real MQTT | PASS |
| Negative / security (fail-closed, tamper, replay, nonce/req-id) | PASS 29/29 |
| MQTT disconnect/reconnect + LWT | PASS |
| Home Assistant restart persistence | PASS |
| Retained vs non-retained state behaviour | PASS |
| Secret-exposure log scan (step 19) | PASS (product) — see E.1 |
| Task/resource cleanup (no lingering task in the integration) | PASS |
| Static analysis | `ruff` clean, `mypy` clean (14 files) |
| Hermetic unit / contract suite | 209 passed |

Six genuine, previously-shipping defects were found by real-environment testing
and fixed (section F); the LWT test harness was independently corrected without
touching production code (section D.1); and a HA-core DEBUG logging behaviour was
documented (section E.1, F2) without a contract change.

**What this does NOT cover.** No ESP32, no lock actuator and no HomeKey reader
were ever present. Steps 12–15 and 17 remain NOT TESTED / BLOCKED (section C).
The full system is therefore **not** production-ready: the next phase is
validation with **real hardware** (real ESP32 + real lock + real HomeKey
credential), repeating the whole suite end-to-end.

---
