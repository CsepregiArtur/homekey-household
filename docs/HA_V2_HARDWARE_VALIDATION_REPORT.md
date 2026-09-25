# HomeKey Household V2 — Hardware Validation Report

Status of validation against **real ESP32 hardware**.

Terminology matches `docs/HA_V2_REAL_INTEGRATION_TEST_REPORT.md`.

| Verdict | Meaning |
|---|---|
| **PASS** | Verified against the real device. |
| **FAIL** | A real defect or misconfiguration was proven. |
| **BLOCKED** | Could not be tested. **Not a pass.** |
| **NOT TESTED** | Not attempted. |

---

## A. Hardware environment (detected, not assumed)

Everything in this section was read from the machine and the device — nothing is
inferred from documentation.

| Item | Detected value | How it was detected |
|---|---|---|
| ESP32 model | Classic **ESP32** (`board = esp32dev`) | `platformio.ini`; `build/project_description.json` → `"target": "esp32"` |
| USB-serial bridge | **Silicon Labs CP2102** (`0x10C4:0xEA60`) | `ioreg -p IOUSB` |
| Serial device | `/dev/cu.usbserial-0001`, **115200** baud | `ls /dev/cu.*`; `monitor_speed` |
| Firmware (repo) | `HK_APP_VERSION = "0.10.0"` | `CMakeLists.txt:7` |
| Firmware (last build) | **`v0.10.0-9-gf76cc50-dirty`** | `build/project_description.json` |
| Firmware (running) | **Could not be read — see B/FAIL-1** | Not exposed on the serial console |
| Framework | **ESP-IDF** 5.5.5 (local symlinked checkout) | `platformio.ini` `platform_packages` |
| NFC reader | **PN532 over SPI** (runtime-selected) | `main/include/defaults.h:78` |
| NFC pins (PN532 default) | SS=5, SCK=18, MISO=19, MOSI=23 | `variants/esp32/pins_arduino.h` |
| Actuator pin | **`GPIO_ACTION_PIN = 255` (unset)** | `main/include/defaults.h:134` |
| Actuator polarity | LOCK = LOW, UNLOCK = HIGH | `defaults.h:135-136` |
| Actuator drive mode | **Latched** via `gpio_hold_en()`, not pulsed | `main/HardwareManager.cpp:514-521` |
| Momentary mode | Disabled by default; 5000 ms if enabled | `defaults.h:137-138` |
| MQTT TLS | **Disabled** (`MQTT_USE_SSL false`) | `defaults.h`; confirmed on console |
| MQTT broker | `192.168.1.135:1883` **OPEN**; `:8883` closed | TCP connect test |
| MQTT auth policy | **Requires credentials** | Anonymous → *Not authorized*; bogus → *Not authorized* |
| Home Assistant | 2026.9.3 at `192.168.1.135` | User's instance |
| HA integration | `homekey_household` **2.2.3** | Integrations page |
| Node ID / household ID | **Unknown — not discoverable** | Device web UI not on this subnet (see C) |
| Working tree (firmware) | **DIRTY**: `main/CMakeLists.txt`, `main/main.cpp` modified; `main/DeviceCert.{cpp,hpp}` untracked | `git status --porcelain` |

### Hardware configuration that could NOT be determined

These live in device NVS and are not readable without the web UI or a working
MQTT connection. They must be confirmed by the operator rather than assumed:

- the **actual** configured NFC reader type (PN532 / PN7160 / ST25R3916)
- the **actual** `gpioActionPin` (255 means commands are ignored entirely:
  `HardwareManager.cpp:499-502`)
- the **actual** MQTT broker host/port and credentials stored on the device
- the **actual** household/node identity

---

## B. Test results

| Phase | Result | Evidence |
|---|---|---|
| ESP32 boot + application bring-up | **PASS** | Boot banner + `ConfigManager` output on console; `tests_hardware/test_boot.py::test_esp32_boots_and_reaches_application_code` |
| Wi-Fi connection | **PASS** | `[W][wifi] FT-PSK present but FT disabled, falling back to WPA2-PSK`; MQTT activity implies link up |
| MQTT TLS connection | **NOT TESTED** | TLS is disabled by design; 8883 closed |
| **MQTT authentication** | **FAIL** | Device: `[W][mqtt_client] Connection refused, not authorized`. See FAIL-1. |
| Household node in HA / entities | **BLOCKED** | Requires MQTT. No node publishes without it. |
| `B/state`, `B/status`, `B/security` retained | **BLOCKED** | Requires broker credentials (`HK_MQTT_USERNAME`). |
| `B/health` non-retained | **BLOCKED** | Requires broker credentials. |
| Firmware version verified on device | **BLOCKED** | See FAIL-1; version is only exposed over MQTT/web UI. |
| Physical lock state (step 13) | **BLOCKED** | Needs an operator to confirm the physical bolt. |
| HA → ESP32 lock (step 14) | **BLOCKED** | Needs MQTT + `HK_RECOVERY_SECRET` + operator. |
| HA → ESP32 unlock (step 14) | **BLOCKED** | As above. |
| HomeKey authentication (step 15) | **BLOCKED** | Requires a physical credential tap. |
| Invalid credential (step 16) | **BLOCKED** | Requires an operator; security is not weakened to automate. |
| ESP32 reboot (step 17 A) | **BLOCKED** | Reboot performed; HA-side checks need MQTT + identities. |
| Power interruption (step 17 B) | **NOT TESTED** | Requires `HK_ALLOW_POWER_LOSS_TEST=1` and a safe lock state. |
| MQTT independence (step 18) | **BLOCKED** | Requires an operator + physical tap. |
| HA independence (step 18) | **BLOCKED** | As above. |
| Internet independence (step 18) | **BLOCKED** | As above. |
| Console secret scan (step 9) | **PASS** (partial) | Zero 64-hex tokens on the boot console. Cross-check with the real secret BLOCKED (secret not supplied). |
| Failure injection (step 10) | **BLOCKED** | Requires MQTT + the real command key. |
| MQTT outage resilience | **PASS** | Device stays up and retries MQTT every ~10 s rather than resetting. |

Automated suite: **41 tests collected** — `tests_hardware/`.

---

## C. Failures and blockers

### FAIL-1 — MQTT authentication is rejected by the broker

- **Reproduction**: reset the ESP32 and capture the serial console.
  ```
  [W][MqttManager] MQTT TLS is disabled: credentials and lock commands are
      sent in the clear. ...
  [W][mqtt_client] Connection refused, not authorized
  [E][MqttManager] MQTT_EVENT_ERROR: Connection or protocol error occurred
  [E][MqttManager] MQTT Error Type: 2
  [E][MqttManager] Connection refused - broker may be down or rejecting connection
  [E][mqtt_client] MQTT connect failed
  ```
- **Expected**: `MQTT_EVENT_CONNECTED`.
- **Actual**: CONNACK refusal, retried every ~10 s indefinitely.
- **Broker is not at fault**: independently probed from the workstation —
  `192.168.1.135:1883` accepts TCP, and both an anonymous CONNECT and a CONNECT
  with bogus credentials return **Not authorized**. The broker is healthy and
  correctly requiring authentication.
- **Root cause**: the credentials configured on the ESP32 (NVS) are missing or
  do not match a valid broker user. The reply is a CONNACK rejection, not a
  timeout, which proves the device reached the broker — so this is purely a
  credential mismatch, not a network or firewall problem.
- **Classification**: **hardware/device configuration** — *not* a firmware
  defect, *not* an integration defect. No code change is warranted, and none was
  made.
- **Fix**: configure a valid MQTT username/password on the device (device web UI
  → MQTT settings, or the setup portal). In a Home Assistant Mosquitto add-on
  deployment this means creating a Home Assistant user and entering those
  credentials on the device.
- **Regression test**: `tests_hardware/test_boot.py::test_mqtt_authentication_succeeds`
  — currently **FAIL**, and will flip to PASS once valid credentials are set.
- **Why this blocks everything**: without MQTT there is no node, no entity, no
  `B/state`, and no command path. Steps 12–17 and 10 all depend on it.

### BLOCKED-2 — Device identity and web UI not reachable

- **Finding**: a port-80 sweep of `192.168.1.0/24` found `192.168.1.110` and
  `192.168.1.111` serving **ESPHome** (`<esp-app>` + `oi.esphome.io/v2/www.js`),
  `192.168.1.249` running Apache, and `192.168.1.135` being Home Assistant.
  The HomeKey device's own web UI (`/household`, `/node`, `/config`) was **not**
  found.
- **Impact**: the device's configured GPIO/NFC/MQTT settings and its
  household/node identity cannot be read, so the household topic tree cannot be
  reconstructed for observation.
- **Classification**: environment/discovery.
- **Next step**: confirm which network the ESP32 has joined and its IP. The
  firmware's default log level suppressed INFO lines (it logged
  `GlobalLogLevel not found in NVS. Returning default level.`), so the usual
  "got IP" line was not printed; raising the log level via the web UI would make
  the address visible.

### BLOCKED-3 — Operator-dependent physical tests

Steps 13, 15, 16, 17-B and 18 cannot be automated: they need a human to observe
a bolt, tap a credential, cut power, or stop a service. These are reported as
BLOCKED **by design** rather than simulated, because a simulated unlock proves
nothing about a door.

### Observation — firmware working tree is dirty

`main/CMakeLists.txt` and `main/main.cpp` are modified, and
`main/DeviceCert.cpp` / `main/include/DeviceCert.hpp` are untracked. The last
build is therefore `v0.10.0-9-gf76cc50-dirty`, and **the flashed image cannot be
tied to a clean commit**. This does not indicate a defect, but it means the
running firmware is not reproducible from `main`. Adding the `DeviceCert`
sources implies in-progress certificate work (relevant to the disabled MQTT
TLS).

---

## D. Final decision

```
HARDWARE VALIDATION NOT READY — BLOCKERS REMAIN
```

Reasons, in priority order:

1. **MQTT authentication fails on the device** (FAIL-1). This alone blocks the
   entire node/telemetry/command/HA surface.
2. **Device identity and GPIO/NFC/actuator configuration are unknown**, because
   the device web UI was not reachable and the settings live in NVS (BLOCKED-2).
   Notably the actuator pin defaults to `255` = unset, which would silently
   ignore every lock command.
3. **Operator-required physical tests are unperformed** (BLOCKED-3).

The software phase passing says nothing about the physical device: no claim of
production-readiness is made here, and none should be inferred from this report.

### To make progress

1. Set valid MQTT credentials on the device → re-run
   `pytest tests_hardware -m boot` until it is green.
2. Locate the device's web UI and record: household id, node id, NFC reader
   type, actuator pin, MQTT broker settings.
3. Export the identities and credentials into the environment (see
   `tests_hardware/README.md`) and re-run telemetry, command and security tests.
4. With an operator present, complete steps 13/15/16/17-B/18.

---

## E. Production-code changes

**None.** No firmware, integration, or contract change was made during this
phase. `custom_components/` is untouched, `git status` shows no modifications
there, and the firmware working tree was already dirty before this work began
and was not touched.

Additions: the test harness (`tests_hardware/`, 41 tests) and this report.

### Test-harness defect found and fixed (not a product defect)

`tests_integration/test_resilience.py` spawned its LWT helper subprocess with
`os.environ.get("LWT_HELPER_PYTHON", "python3")`. That resolves to PATH's
`python3` — the *system* interpreter, which does not have `paho-mqtt` installed —
rather than the interpreter actually running the tests (the venv, which does).
The helper therefore died immediately with `ModuleNotFoundError: No module named
'paho'`, and two resilience tests failed plus one errored:

```
AssertionError: LWT helper did not come online; events=[...
  'import paho.mqtt.client as mqtt',
  "ModuleNotFoundError: No module named 'paho'"] rc=1
```

This was reproducible on a clean broker port, so it was a genuine harness defect
rather than a stale-process artefact. It is independent of anything in this
phase, but it must be fixed for the regression baseline to be trustworthy.

**Fix**: default to `sys.executable`, so the helper always runs under the same
interpreter as the test session. This is a test-only change; no product code was
altered.

**Verification**: `test_resilience.py` 10/10 passed; the full integration suite
**88 passed, 0 failed, 0 errors** (exit 0).

