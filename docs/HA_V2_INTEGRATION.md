# HomeKey Household — Home Assistant V2 Integration

Home Assistant custom integration (`homekey_household`) for **HomeKey-ESP32** nodes,
over one of two transports:

* **`mqtt`** — the documented household MQTT API, **firmware 0.10.0**. This is what
  the bulk of this document describes.
* **`direct`** — the node's own HTTPS API (`/api/ha/*`), with its self-signed
  certificate pinned by fingerprint. No broker involved. See §15.

Both produce identical coordinator state and therefore identical entities; nothing below
the coordinator knows which one is in use.

> **Local HomeKey unlock remains entirely on the ESP32.** Home Assistant, MQTT,
> and internet connectivity are *not* dependencies for local HomeKey access. The
> ESP32 authenticates HomeKey credentials and actuates the lock locally; the path
> `NFC → LockManager → HomeKit` has no MQTT dependency. This integration is a
> **management and control plane**, not part of the local access path.

---

## 1. Architecture

The integration is a **client of the documented household MQTT API**. It is
logically separated from the firmware implementation and does not depend on ESP32
C++ classes, `LockManager` internals, HomeKey credential storage, HomeKit
internals, raw NVS, the firmware filesystem, or legacy MQTT command paths.

```mermaid
flowchart TD
    HA[Home Assistant] --> INT[HomeKey Household V2 integration]
    INT --> MQTT[(MQTT broker<br/>reused from HA core MQTT)]
    MQTT --> T1["B/state, B/status, B/health, B/security"]
    MQTT --> T2["B/backup/status, B/backup/last, B/last_auth"]
    INT -.publish.-> C1["B/command/lock (HMAC)"]
    INT -.publish.-> C2["B/command/unlock (HMAC)"]
    MQTT --> ESP32[HomeKey-ESP32 node]
    ESP32 --> LOCK[Local lock actuation]
    NFC[NFC / HomeKey credential] --> ESP32
```

`B = homekey/household/<household_id>/nodes/<node_id>`

**Layering** — a single directional pipeline; entities never parse MQTT directly:

```
MQTT message → topic parser (mqtt.parse_topic)
             → payload validators (models)
             → coordinator (single source of truth)
             → entities (lock / binary_sensor / sensor)
```

### Module map

| Module | Responsibility |
|---|---|
| `const.py` | Domain, topic builders, HMAC constants, payload keys, enums |
| `models.py` | Strictly validated payload dataclasses |
| `command.py` | Key derivation, canonical input, HMAC-SHA256, nonce/request ids |
| `credential.py` | Secure command-key storage via HA `Store` |
| `mqtt.py` | Transport abstraction, topic parsing, command publication |
| `direct.py` | Broker-less transport: certificate pinning, `/api/ha` client, poller |
| `coordinator.py` | Node registry, state merge, availability, fail-closed commands |
| `entity.py` | Stable device identity + shared attributes |
| `lock.py` | Lock entity using the HMAC command topics |
| `binary_sensor.py` | Node online |
| `sensor.py` | Health, backup, security, firmware, last-auth |
| `config_flow.py` | Household + credential setup (reuses HA MQTT) |
| `diagnostics.py` | Redacted diagnostics |

**Transport**: by default the integration reuses the Home Assistant core **MQTT
integration**. It never opens a second broker connection and does not duplicate
broker configuration. The optional `direct` transport (§15) replaces the broker
entirely for a single node.

---

## 2. Installation

1. Copy `custom_components/homekey_household` into your Home Assistant
   `config/custom_components/` directory (or install via HACS).
2. Ensure the official **MQTT** integration is configured and connected.
3. Restart Home Assistant.
4. Add the integration: **Settings → Devices & Services → Add Integration →
   HomeKey Household**.

Requirements: Home Assistant 2024.x or newer (developed against 2026.9), and the
core MQTT integration. There are no third-party Python dependencies.

---

## 3. Configuration

### Config flow

The flow is a single step:

| Field | Required | Description |
|---|---|---|
| **Household ID** | yes | Matches `household_id` on the household topics, e.g. `HOME-001`. Charset `[A-Za-z0-9_-]{1,64}` |
| **Household name** | no | Friendly name; defaults to the household id |
| **Recovery secret** | no | Used **once** to derive the command key. Never stored, never logged, never sent over MQTT |
| **KDF salt** | no | Salt used by the firmware when deriving the command key |
| **Enable authenticated lock control** | no | Enables/disables lock commands (default on) |

MQTT broker details are **not** requested: the flow aborts with
`mqtt_not_configured` when the HA MQTT integration is unavailable, and otherwise
reuses it.

### Options flow

Re-open the integration and choose **Configure** to:

* rotate the command credential (supply the recovery secret again — leaving it
  blank keeps the current key),
* toggle authenticated lock control,
* change the legacy MQTT client-id prefix (default `ESP_`) used **only** to read
  the shared broker LWT availability topic.

### Credential boundary (important)

The firmware derives the command key as:

```
key = BLAKE2b( message = recovery_secret || salt,
               key     = "HK-HOUSEHOLD-CMD-v1",
               digest  = 32 bytes )
```

Home Assistant must be able to reproduce this key, so:

* the recovery secret is entered once, used **in memory** for derivation, then
  discarded;
* **only the derived 32-byte command key** is persisted, via the Home Assistant
  `Store` helper (`<config>/.storage/homekey_household.command_keys`);
* the raw recovery secret, raw backup, private keys, and HomeKey credentials are
  **never** stored in the config entry, MQTT topics, entity state, attributes,
  logs, or diagnostics.

If no credential is available and control is enabled, the lock entity is
read-only and every command **fails closed** — an unauthenticated command is
never published.

---

## 4. Household setup

A *household* is an independent namespace. Multiple households can be configured
in one Home Assistant instance and never collide.

Nodes are enrolled on the node side (via the firmware's HTTP provisioning
endpoints). Once enrolled, the node publishes under
`homekey/household/<household_id>/nodes/<node_id>/`.

---

## 5. Node discovery

**Authoritative registration path (chosen and documented):** the integration
registers nodes itself by subscribing to the household namespace
(`homekey/household/#`) and observing the documented node topics. The first
message seen for a `(household_id, node_id)` pair registers that node.

* Discovery is **deterministic** and **idempotent**: republished or retained
  messages never create duplicate entities.
* Adding a node triggers a debounced reload so the new device appears without a
  Home Assistant restart.
* The firmware's own HA MQTT Discovery messages are **not** used for
  registration. This is deliberate: the firmware's discovery payloads share a
  single device descriptor based on `deviceID`/MAC, which cannot express the
  required `household_id + node_id` identity model or multi-node isolation. The
  integration therefore implements one authoritative registration path and does
  not create duplicate entities for firmware-discovered ones.

Users do **not** configure nodes manually.

---

## 6. Entities

All entities are attached to a stable device per node. Unique ids follow the
documented contract: `<household_id>_<node_id>_<entity>`.

| Entity | Platform | Unique id | State source | Notes |
|---|---|---|---|---|
| **Lock** | `lock` | `<hid>_<nid>_lock` | `B/health.lock_current` | Controlled via HMAC `B/command/*` |
| **Node online** | `binary_sensor` | `<hid>_<nid>_online` | `B/status` (`online`/`offline`) | `availability_topic` = shared LWT |
| **Node health** | `sensor` | `<hid>_<nid>_health` | `B/health.mqtt` (`OK`/`ERROR`) | Full health JSON as attributes |
| **Backup status** | `sensor` | `<hid>_<nid>_backup` | `B/backup/last.status` | Full metadata JSON as attributes |
| **Security status** | `sensor` | `<hid>_<nid>_security` | `B/security` (`OK`/`WARNING`) | Raw string, never numeric |
| **Firmware version** | `sensor` | `<hid>_<nid>_firmware` | `B/state.firmware_version` | `generation` as an attribute |
| **Last HomeKey authentication** | `sensor` | `<hid>_<nid>_last_auth` | `B/last_auth.result` (`SUCCESS`/`FAILURE`) | Safe metadata only |

### Semantics preserved exactly

* **Security** is exposed as the documented string `OK` / `WARNING`. It is *not*
  converted into a numeric security score, and no additional classifications are
  invented. (`ERROR` is reserved by the firmware and tolerated defensively.)
* **Health** uses the documented `mqtt` field as its value. `network` is always
  `UNKNOWN` and `certificate` always `unknown` in firmware 0.10.0; both are
  reported **verbatim** and never fabricated from unrelated Home Assistant data.
* **Backup** uses the metadata topic only. The integration never expects or
  accepts backup *contents* over MQTT.
* **Last auth** expects only `type`, `result`, `timestamp`. Issuer/endpoint ids,
  APDU data, credential ids, and cryptographic material are never expected.

### Lock state

Lock state is derived from `B/health.lock_current` (the documented household
state source) mapped as `0=unlocked, 1=locked, 2=jammed, 3=unknown,
4=unlocking, 5=locking`. With no health snapshot yet, the state is `unknown`.

---

## 7. Lock control (HMAC-authenticated)

Authoritative control uses **only**:

```
B/command/lock
B/command/unlock
```

Legacy/internal topics are **not** used for control:

* `P/homekit/set_state`
* `P/homekit/set_target_state`
* `P/homekit/set_current_state`
* `P/homekit/set_battery_lvl`
* `P/homekit/set_custom_state`

### Command payload

Exactly four fields. **The action is not in the payload** — it is derived from
the MQTT topic.

```json
{"ts": 1760000000, "nonce": "<nonce>", "req_id": "<request-id>", "mac": "<64 lowercase hex characters>"}
```

### MAC computation

```
lock:   mac = HMAC-SHA256(key, f"{ts}{nonce}{req_id}lock")
unlock: mac = HMAC-SHA256(key, f"{ts}{nonce}{req_id}unlock")
```

* `ts` — Unix epoch seconds from the Home Assistant system clock (decimal, no
  separator). The firmware accepts roughly ±300 s.
* `nonce` — 16 random bytes rendered as lowercase hex (32 chars), from a CSPRNG
  (`secrets`). The client never reuses a nonce within the firmware's replay
  window. Nonces are **never** predictable counters.
* `req_id` — a unique, secret-free UUID hex string, for local correlation and
  debugging.
* The command is published at **QoS 1** and **not retained**.

### Fail-closed behaviour

* No credential → the command is not published and the entity raises
  `HomeAssistantError`.
* No MQTT transport → the command is not published.
* An `unlock=true` shortcut does not exist anywhere in the code path.

---

## 8. Multi-node and device identity

The stable logical identity is **`household_id + node_id`**.

```
Household HOME-A          Household HOME-B
├── GATE-001              └── GATE-001
├── HOUSE-001
└── SMALL-001
```

These never collide: entity unique ids, MQTT topics, and device identifiers all
incorporate both the household and the node id.

Device identifiers are `(homekey_household, household_id, node_id)`. The
integration deliberately **does not** use the MAC address or the HomeKit
`deviceID` as the primary identity, because:

* `deviceID` can change when a device is re-paired in Apple Home,
* a MAC-based identity cannot express the household/node model,
* the firmware's own discovery device descriptor collapses all household nodes
  onto one device.

### Replacement nodes

`GATE-001 → GATE-002` is treated as a **new node identity**. It produces a
distinct MQTT topic space, a distinct HA device, and distinct entities. The old
device becomes unavailable and can be removed from the device registry;
configuration and automations for the new node are separate. Re-provisioning
Apple Home is required on the device side (a documented firmware limitation).

---

## 9. Availability

Node availability follows the documented status/LWT semantics:

| Signal | Topic | Payload | Source |
|---|---|---|---|
| Node status | `B/status` | `online` / `offline` | Node publish (retained) |
| Shared LWT | `<CLIENT_ID>/status` | `online` / `offline` | Broker will on unexpected disconnect |

* The node-online entity is `on` when `B/status` reports `online`.
* The shared broker LWT is consumed as an availability signal, so an unexpected
  disconnect marks entities unavailable.
* **No second MQTT will is created** — MQTT allows only one will per connection.
* **Known limitation (documented, not worked around):** a *clean* MQTT disconnect
  can leave the retained `online` state until another connection or will event.
  The integration does **not** hide this by inventing a timer-based fake offline
  state.
* The LWT topic is MAC-derived and carries no household identity, so it can never
  create a household entity on its own.

---

## 10. State handling

Payloads are validated strictly and defensively:

* Malformed JSON, wrong types, and identity mismatches are rejected with a
  warning; the integration never crashes and never corrupts entity state.
* A rejected payload **preserves the previous valid state**.
* Missing fields are **not** interpreted as valid zero/false values unless the
  contract defines that behaviour. For example a missing `lock_current` yields
  `unknown`, not `locked`/`unlocked`; a missing optional boolean stays `None`.
* Booleans are never accepted where integers are required (Python's `bool`
  subclassing `int` is explicitly rejected).
* Identity fields are required on `state` (the contract always includes them) and
  validated-if-present on `health`, `backup/last`, and `last_auth` (the firmware
  omits them on `health`).

---

## 11. Reserved and HTTP-only APIs

The firmware marks these topics **RESERVED / NOT IMPLEMENTED**. The integration
never subscribes to or publishes them (the parser flags and drops them):

| Topic | Status |
|---|---|
| `B/events` | RESERVED / NOT IMPLEMENTED |
| `B/backup/request` | RESERVED / NOT IMPLEMENTED |
| `B/backup/data` | RESERVED / NOT IMPLEMENTED |
| `B/restore/request` | RESERVED / NOT IMPLEMENTED |
| `B/restore/status` | RESERVED / NOT IMPLEMENTED |

**Backup, restore, audit, and provisioning are HTTP-only** on the firmware
(`POST /backup/create`, `POST /backup/restore`, `GET /audit`). If backup or
restore integration is added later it must use those documented HTTP APIs — not
invented MQTT topics. Encrypted backup blobs are never published over MQTT, and
this integration never expects them there.

---

## 12. Logging

Logged (safe): `household_id`, `node_id`, command type, success/failure, MQTT
topic, request id.

**Never logged:** the HMAC command key, the recovery secret, the raw command MAC,
backup plaintext, private keys, HomeKey credential material.

Diagnostics output is redacted recursively (`recovery_secret`, `command_key`,
`key`, `mac`, `backup`, `data`, `private_key`, tokens, …). A one-way key
fingerprint (first 8 hex chars of SHA-256 over the key) may be shown; it cannot
be used to sign commands.

---

## 13. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Setup aborts with *MQTT not configured* | Configure and connect the core MQTT integration first |
| No nodes appear | Node not enrolled in the household; check `homekey/household/<hid>/nodes/+/state` is being published (retained) |
| Entities unavailable | `B/status` is `offline`, or the shared LWT published `offline` |
| Entities stay available after a clean disconnect | Documented MQTT clean-disconnect limitation — not a bug (see §9) |
| `Cannot lock/unlock … credential unavailable` | No command key stored. Open **Configure** and supply the recovery secret |
| Lock never changes state | Node not publishing `B/health`; lock state comes from `lock_current` |
| Security shows `WARNING` | Firmware security posture has a finding — check the Web UI / firmware security checks |
| `network` is `UNKNOWN` / `certificate` is `unknown` | Documented firmware stubs (not wired); working as intended |
| Backup entity shows no timestamp | The node has not published `B/backup/last` yet |
| Commands appear ignored | Check `req_id` in the logs; the firmware rejects bad MACs, replayed nonces, and timestamps outside ±300 s |

Debug logging:

```yaml
logger:
  logs:
    custom_components.homekey_household: debug
```

---

## 14. Contract compliance summary

| Contract requirement | Implementation |
|---|---|
| Household-only topics | `const.py` declares only documented topics; reserved topics are explicitly rejected |
| No `schema` field required | `models.py` does not require one |
| `B/security` raw string | Stored and exposed as `OK` / `WARNING`, never numeric |
| `B/health` stubs preserved | `network`/`certificate` reported verbatim |
| Backup metadata only | Only `status`/`timestamp` modelled |
| `last_auth` safe metadata | Only `type`/`result`/`timestamp` modelled |
| HMAC command payload | Exactly 4 fields, no `action` |
| Topic-derived action | `COMMAND_ACTIONS` maps topic → action |
| Key derivation | BLAKE2b(`secret || salt`, key=label, 32) |
| MAC | HMAC-SHA256 over `{ts}{nonce}{req_id}{action}`, lowercase hex |
| Timestamp | Unix epoch seconds from the HA clock |
| Freshness/replay | CSPRNG nonce, never reused; ~±300 s window respected |
| Fail closed | No credential ⇒ no publish |
| One MQTT will | Shared LWT reused, no second will |
| Multi-node / multi-household | Identity is `household_id + node_id` throughout |
| Legacy topics excluded | No `P/*` command topic published; `P/homekey/auth` unused for V2 auth |

---

## 16. Knowing who opened the door

### The problem

Home Assistant attributes a state change to whatever `Context` was attached when the state
was written. A change asked for from Home Assistant already has one: `helpers/service.py`
calls `entity.async_set_context(<the service call's context>)` before the service handler
runs, and this integration's lock does not write a new state at that moment — it publishes a
command. The context therefore stays pending and is consumed by the **next** state write,
which is the one the node's report causes. That is why those read *"artur — Action used:
Lock lock"*.

A change made at the door had nobody to attribute to. The node reported a number in
`B/health` and nothing that identified a person, so "No cause was recorded" was accurate
rather than a bug.

### What the node reports

`B/lock/last` (retained, published immediately *before* the state it explains):

```json
{"current":0,"target":0,"source":"homekit","timestamp":1760000000}
```

`source` is `homekit`, `homekey`, `mqtt`, `api`, `device`, or `unknown`. `current`/`target`
use the same numbering as `lock_current`/`lock_target` in `B/health`.

`LockManager` carries the origin on every `LOCK_STATE_CHANGED` event. It used to overwrite it
with `INTERNAL` in three places, which is why every change previously looked identical.

### How the cause is applied

The coordinator keeps the reported cause on the node. When a health document arrives whose
`lock_current` **differs** from the previous reading, and the recorded cause's `current`
matches the new state, and the source is one Home Assistant did not cause
(`homekit`/`homekey`/`device`), it:

1. creates a fresh `Context`,
2. records a logbook entry for the node's lock entity with that context, and
3. hands the same context to the entities, which write their state with it via
   `async_set_context()` — the same mechanism a service call uses.

Because the logbook entry and the state change share a context, the activity view can join
them and name the cause.

When the cause is a HomeKey tap, the entry names the **person** rather than the mechanism.
The name the user gave that controller is on `B/last_auth`, and the node stamps both records
from the same reading of its clock, so a stamp that matches is what identifies the
authorisation as the cause of this particular change. A HomeKey change with no matching
authorisation — and every `homekit` change, since HAP never says which controller asked —
names the mechanism instead.

The context is dropped once the update has been delivered, so it cannot be attached to an
unrelated later change.

### What is deliberately *not* done

* **A change Home Assistant asked for is left alone.** `mqtt` and `api` sources are ignored:
the service call's own context is already pending, and overriding it would replace a real
person with a source word.
* **A stale cause is never used.** If the recorded cause's `current` does not match the new
state, it describes a different change and is discarded.
* **Nothing is inferred.** No timing windows, no guessing. The person's name is used only
  when the node stamps `B/last_auth` and `B/lock/last` *identically* — the node's own
  statement that the authorisation and the change are one event — and not because two
  messages arrived close together and this integration decided that was close enough.
* **Attribution cannot break ingestion.** The work is wrapped so a failure to write a
logbook entry can never stop a real reading from being published.

### Naming a paired controller

`HomeSpan`'s `Controller` exposes only `getID()` and `getLTPK()` — HAP never tells an
accessory a controller's name — so an issuer cannot be labelled automatically. The node's
Web UI (Dashboard → HomeKey → an issuer) allows naming one by hand, stored in the
reader-data blob and dropped when that pairing ends.

The name is published with `B/last_auth` and surfaced as the `issuer` attribute on the
**Last HomeKey authentication** sensor. Only the name is sent, never the issuer id, and only
when one has been given — so naming is an explicit opt-in to sharing it, and a node with
unnamed issuers publishes exactly what it always did.

---

## 15. The broker-less (direct) transport

### Why it exists

The MQTT transport requires a broker to be installed, running, reachable and
correctly configured before a single node appears. A node sitting on the same LAN
does not need any of that, and if the broker is the only reason Home Assistant
cannot see it, then the broker is the problem.

### Discovery

The node advertises `_homekey._tcp` over mDNS with TXT records `id`, `name`,
`model`, `ver`, `proto`, `fp`, `cfg` and `tls`. The integration declares
`zeroconf: ["_homekey._tcp.local."]` in its manifest, which is what makes Home
Assistant offer it as a discovered card.

Discovery leads to the **direct** transport: a node that announces itself on the
LAN can be talked to without a broker, so making broker setup the first thing it
asks for would defeat the point. The MQTT transport remains available from the
manual "Add integration" path.

A node advertising `proto` other than `1`, `tls` other than `1`, or no `fp` is
refused at discovery with its own reason. Refusing early is deliberate: the
firmware will not serve state in the clear, so a node without TLS would be
discovered and then fail every request, and a node without a fingerprint has
nothing to pin it to.

### Trust: the fingerprint *is* the trust anchor

The node generates its own key and self-signed certificate on first boot, so every
unit is unique. There is no CA to chain to and no subject that can match a DHCP
address. That means:

* the fingerprint shown on the discovery card must be compared against the value
  in the node's own Web UI (Misc → Security) — if they differ, stop;
* `pinned_ssl_context()` loads the confirmed certificate as the **only** trusted
  root with `CERT_REQUIRED`, so a node presenting anything else is rejected by the
  TLS layer itself, before any credential is sent. Comparing fingerprints after a
  request has already been sent would be too late;
* the check is repeated on **every startup**, so a device that was swapped or
  factory reset is refused rather than trusted because it once was.

The startup distinction matters for the error the user sees: a fingerprint
mismatch stops the entry with a permanent error naming both values, while an
unreachable node is retried with backoff, and a rejected credential starts the
reauthentication flow.

### Authorisation

Commands are authorised by the node's own Web UI credential, over that pinned TLS
connection — the same boundary that already protects `/reboot_device`,
`/recovery/export` and `/backup/restore`. The credential is stored in the config
entry, exactly as the core MQTT integration stores a broker password, because the
node has to be authenticated on every poll.

HMAC command signing is *not* used on this path. The MQTT path signs commands
because a broker is untrusted; here the peer is pinned and the channel is
authenticated, so a second signature scheme would add code without adding a
guarantee — and would mean asking for a recovery secret the transport does not
need.

### Reading state

`GET /api/ha/state` returns the node's identity plus the documented health
document, byte for byte the same JSON the MQTT transport publishes on `B/health`.
A `direct.py` helper restates that response as the *messages MQTT would have
delivered* and hands them to the same coordinator ingestion path, so every parser,
validator and merge rule is shared rather than reimplemented. Two transports
cannot read the same firmware differently, because there is only one reader.

The firmware's documented stubs (`network`, `certificate`) and its
"lock not reported" sentinel are preserved or omitted, never filled in with
something plausible-looking.

### Availability and polling

MQTT pushes; this transport polls every 30 s. A single slow response does **not**
mark the node unavailable — the ESP32 serves TLS from the same chip that runs
HomeKit, so an occasional slow handshake is normal, and flapping a lock entity to
unavailable on it would be a worse lie than reporting the last known state. Three
consecutive failures are required, and the node is marked offline through the same
path a retained `B/status` message would use.

### Limits

* One entry covers **one node**. A household reaching Home Assistant this way
  produces one entry per node, where the MQTT transport covers a whole household in
  one.
* Backup, restore, audit and provisioning are HTTP-only on the firmware and are not
  exposed as entities on either transport.
* An entry is refused if its household is already configured, over either
  transport: entity unique ids are `<household>_<node>_<entity>`, so a second entry
  would produce a duplicate set that the user could not tell apart.
