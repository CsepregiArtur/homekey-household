# HomeKey Household

A Home Assistant custom integration (`homekey_household`) that manages a
**household** of [HomeKey-ESP32](https://github.com/CsepregiArtur/HomeKey-ESP32)
nodes over one of two transports:

| Transport | How | Covers | Needs a broker |
|---|---|---|---|
| `mqtt` | The documented household MQTT API (firmware 0.10.0), reusing Home Assistant's core MQTT integration | a whole household | yes |
| `direct` | The node's own HTTPS API (`/api/ha/*`), with its certificate pinned to an exact fingerprint | one node | no |

```
HomeKey Household
├── Household (HOME-001)
│   ├── Gate        (GATE-001)
│   ├── Main House  (HOUSE-001)
│   ├── Small House (SMALL-001)
│   └── Garage      (GARAGE-001)
```

Both produce identical coordinator state, so each node appears as its own Home
Assistant **Device** with a lock and the six documented household entities either
way, and nothing downstream can tell which transport is in use.

The integration is a *client* of the ESP32 — it does not depend on firmware
internals, and it never opens a broker connection of its own.

> **Important:** Home Assistant is **not required** for local HomeKey unlocking.
> NFC → ESP32 → HomeKey authentication → lock works independently of Home
> Assistant, MQTT, and the internet. This integration is a management and control
> plane, not a dependency for local access.

---

## Documentation

📖 **[Full integration documentation →](docs/HA_V2_INTEGRATION.md)**

It covers architecture, installation, configuration, household setup, node
discovery, entities, lock control, HMAC command security, credential storage,
multi-node behaviour, replacement-node behaviour, troubleshooting, the known MQTT
LWT clean-disconnect limitation, and the reserved/HTTP-only backup APIs.

The authoritative MQTT contract lives in the firmware repository:

* `docs/content/mqtt_household_api.md`
* `docs/content/mqtt_api_contract_matrix.md`

---

## Quick start

**Broker-less (the node is discovered automatically).** A node advertising
`_homekey._tcp` on the local network is offered as a discovered card under
**Settings → Devices & Services**. Confirm that the certificate fingerprint shown
matches the one on the node's own Web UI (Misc → Security), then enter the node's
Web UI credentials. No broker, no household id to look up: the node reports its
own identity.

**Over MQTT.**

1. Install: copy `custom_components/homekey_household/` into
   `<config>/custom_components/` (or install via HACS) and restart Home Assistant.
2. Configure the core **MQTT** integration (reused by this integration).
3. **Settings → Devices & Services → Add Integration → HomeKey Household**.
4. Enter the **Household ID** and, to enable lock control, the household
   **recovery secret** (used once to derive the command key; never stored or
   transmitted).

Nodes are discovered automatically from the household namespace — you do not add
them manually.

---

## The broker-less transport

Why it exists: the MQTT path needs a broker to be running, correct and reachable
before a single node shows up. A node on the same LAN does not need any of that.

**Trust.** The node generates its own key and self-signed certificate on first
boot, so every unit is unique. There is no CA to chain to and no subject that can
match a DHCP address, which means the SHA-256 fingerprint shown in the node's Web
UI and advertised over mDNS *is* the trust anchor. The integration pins it: the
certificate must match exactly, and the check is repeated on every Home Assistant
start, so a swapped or factory-reset device is refused rather than trusted
because it once was.

**Authorisation.** The node authorises commands with its own Web UI credential,
over that pinned TLS connection. The credential is stored in the config entry —
the same way the core MQTT integration stores a broker password — because the node
has to be authenticated on every poll. If it is ever rejected, Home Assistant
starts a reauthentication flow instead of failing silently.

**Polling.** MQTT pushes; this transport polls every 30 s. A single slow response
does not mark the node unavailable: the ESP32 serves TLS from the same chip that
runs HomeKit, so three consecutive failures are required before the entities go
unavailable, and the node's last known state is kept until then.

**What it does not do.** One entry covers one node — a household reaching HA over
the direct transport produces one entry per node, while the MQTT transport covers
a whole household in one. Backup, restore, audit and provisioning remain
HTTP-only on the firmware and are not exposed as entities either way.

---

## Topics used

Household base `B = homekey/household/<household_id>/nodes/<node_id>`

| Direction | Topic |
|---|---|
| Subscribe | `B/state`, `B/status`, `B/health`, `B/security` |
| Subscribe | `B/backup/status`, `B/backup/last`, `B/last_auth` |
| Subscribe | `<CLIENT_ID>/status` (shared broker LWT availability only) |
| Publish | `B/command/lock`, `B/command/unlock` (HMAC-SHA256 authenticated) |

Reserved/not-implemented topics (`B/events`, `B/backup/{request,data}`,
`B/restore/*`) are never used. Backup/restore/audit/provisioning are HTTP-only on
the firmware.

---

## Entities

| Entity | Platform | Unique id |
|---|---|---|
| Lock | `lock` | `<hid>_<nid>_lock` |
| Node online | `binary_sensor` | `<hid>_<nid>_online` |
| Node health | `sensor` | `<hid>_<nid>_health` |
| Backup status | `sensor` | `<hid>_<nid>_backup` |
| Security status | `sensor` | `<hid>_<nid>_security` |
| Firmware version | `sensor` | `<hid>_<nid>_firmware` |
| Last HomeKey authentication | `sensor` | `<hid>_<nid>_last_auth` |

Device identity is `household_id + node_id` (never the MAC address or HomeKit
`deviceID`).

---

## Knowing who opened the door

Home Assistant can only credit a change it was told the reason for. A change asked for
*from* Home Assistant carries your service call's context — which is why the activity log
names you and says "Action used: Lock lock". A change made at the door had nobody to
attribute to: the node reported a number and nothing else.

The firmware now publishes `B/lock/last` immediately before the state it explains, saying
what asked for the change — `homekit`, `homekey`, `mqtt`, `api` or `device`. The integration
turns a device-originated change into a cause that shares the context of the state change,
so the activity log reads *"Gate unlocked by HomeKit"* instead of *"No cause was
recorded"*.

A HomeKey tap can say more than the mechanism, because the node also publishes `B/last_auth`
with **the name you gave that controller**. When the node stamps the authorisation and the
change it produced from the same reading of its clock, the activity log names the person:
*"Gate unlocked by Artur"*. If the stamps differ, the mechanism is named instead — less
specific, and never wrong.

A change Home Assistant asked for is deliberately left alone. Its context is already
pending on the entity and is consumed by the write the change causes, which is what puts
your name there; overriding it with a source word would replace a real person with a vaguer
description.

The device is the authority on this, and it is careful about it: a cause is only used for
the change it actually describes. If the node reports a cause for a different change — or
none at all, on older firmware — nothing is claimed rather than guessed at.

### Naming a paired controller

A node's Web UI (Dashboard → HomeKey → an issuer) lets you give a paired controller a name,
because HomeKit never tells the accessory one: HAP exposes only an opaque pairing id and a
public key, so there is nothing to derive a name from.

The name you type is published with `B/last_auth` and appears as the `issuer` attribute on
the **Last HomeKey authentication** sensor. Only the name is sent — never the issuer id —
and only when you have given one, so a node with unnamed issuers publishes exactly what it
always did.

---

## The direct transport's HTTP surface

Documented by the firmware; listed here so the contract the client implements is
visible in one place.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/ha/info` | none | Identity, protocol version, actual transport, port, certificate fingerprint |
| GET | `/api/ha/state` | Basic | Identity plus the documented `B/health` document |
| GET/POST | `/api/ha/config` | Basic | The Web UI's own configuration handlers |
| POST | `/api/ha/lock` | Basic | `{"action":"lock"}` or `{"action":"unlock"}` |

`/api/ha/info` is deliberately unauthenticated: it is what a client fetches to
learn the fingerprint it is about to ask its user to confirm, and it cannot ask
for a password before knowing what it is talking to. Everything it returns is
already broadcast in the mDNS TXT record. The household id is *not* among it — that
is only reported once authenticated, because it forms part of the MQTT topic path.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test,lint]"

.venv/bin/python -m pytest -q          # unit tests (hermetic, no sockets)
.venv/bin/ruff check custom_components tests tests_integration
.venv/bin/mypy custom_components/homekey_household

# Real Home Assistant, real broker, real TLS: ~6 minutes
.venv/bin/python -m pytest tests_integration -c tests_integration/pytest.ini
```

`tests/` is hermetic and fast. `tests_integration/` boots a genuine Home Assistant
with a real broker, and for the broker-less transport it stands up a real HTTPS
server with a genuine self-signed certificate — a mocked client would verify none
of the pinning, which is the entire trust anchor of that transport.

`tests_hardware/` drives a physical node and needs the device on the network.

The test suite covers topic construction, node identity, entity unique ids,
multi-node and multi-household isolation, all documented payload parsers, the
HMAC canonical input and SHA-256 generation, timestamp/nonce/request-id handling,
lock and unlock commands over both transports, malformed data, missing
credentials, legacy topic rejection, replacement-node identity, certificate
pinning, credential reauthentication, and poll-failure handling.
