# HomeKey Household — Hardware Validation Suite

Real-hardware tests for the HomeKey ESP32 against the `homekey_household`
Home Assistant integration. These tests require a **physical ESP32**.

> These tests are **not** part of the software suite. A green software run says
> nothing about the hardware, and a blocked hardware test says nothing about the
> device. Do not cite this suite as software coverage.

## Outcome model

| Result | Meaning |
|---|---|
| `PASS` | The device behaved as the firmware 0.10.0 contract requires. |
| `FAIL` | The device did **not** behave as required. This is a real defect. |
| `BLOCKED` | The test could not run — hardware, credentials, or an operator was missing. **Never a pass.** |
| `NOT TESTED` | Not attempted. |

`BLOCKED` is rendered in its own red section at the end of every run, separate
from the pass/fail counts, so a missing device can never be mistaken for success.

## Running

```bash
# From the repository root:
.venv/bin/python -m pytest tests_hardware -c tests_hardware/pytest.ini -q

# Only the tests that need no operator (boot, telemetry, security):
.venv/bin/python -m pytest tests_hardware -c tests_hardware/pytest.ini -q -m "boot or security"
```

Requires `pyserial` (serial console) and `paho-mqtt` (topic observation):

```bash
.venv/bin/pip install pyserial paho-mqtt
```

## Configuration (environment)

Nothing is hardcoded, and no secret is ever written to the repo or to evidence.

| Variable | Purpose |
|---|---|
| `HK_SERIAL_PORT` | Serial device. Auto-detected from `/dev/cu.usbserial-*` if unset. |
| `HK_MQTT_HOST` / `HK_MQTT_PORT` | Broker address (default `127.0.0.1:1883`). |
| `HK_MQTT_USERNAME` / `HK_MQTT_PASSWORD` | **Required** — the broker refuses anonymous connections, so nothing can be observed without these. |
| `HK_HOUSEHOLD_ID` / `HK_NODE_ID` | Required to build the household topic tree. |
| `HK_RECOVERY_SECRET` / `HK_SALT` | Enables the independent command-MAC verification and the secret cross-check. |
| `HK_FIRMWARE_VERSION` | Expected firmware version (default `0.10.0`). |
| `HK_ALLOW_POWER_LOSS_TEST` | Must be `1` to permit the power-interruption test. |

Missing configuration produces `BLOCKED` with an actionable reason — it is never
worked around by weakening a check.

## Evidence

Every test writes a redacted JSON bundle to `tests_hardware/evidence/`
(timestamp, firmware version, node/household, MQTT payloads, serial log, HA
state, physical result). Secrets are removed by **key name and by shape**: any
64-character lowercase hex value is redacted, because a command MAC can leak
inside a free-text log line, not only as a named field.

## What is automated vs. what needs a person

Automated (no operator):

- boot, application bring-up, Wi-Fi association
- MQTT connect/auth outcome
- topic presence, retain flags, payload shape
- console secret-exposure scan (step 9)
- MQTT retry behaviour under an outage

Requires an operator at the device (reported as `BLOCKED` with a procedure):

- confirming the **physical bolt** matches the reported state (step 13)
- tapping a **physical credential** (steps 15–16)
- cutting **power** (step 17 Test B)
- removing MQTT / Home Assistant / internet for the independence tests (step 18)

These are blocked rather than simulated on purpose: a simulated unlock proves
nothing about a door.

## Constraints

- The firmware is **read-only** in this suite. Nothing here modifies it.
- The MQTT contract (firmware 0.10.0), HKDF parameters, and HMAC semantics are
  asserted, never changed.
- Security is never weakened to make a test pass — including for the invalid
  credential test, which has no bypass path.

## Independent verification

The command MAC is recomputed with Python's stdlib `hmac`/`hashlib`:

```
key = BLAKE2b(recovery_secret || salt, key="HK-HOUSEHOLD-CMD-v1", digest=32)
mac = HMAC-SHA256(key, f"{ts}{nonce}{req_id}{action}")   # lowercase hex
```

This shares no code with the firmware (libsodium) or the integration, so a bug
in either cannot validate itself.
