# HomeKey Household

A Home Assistant custom integration (`homekey_household`) that manages an entire
**household** of [HomeKey-ESP32](https://github.com/CsepregiArtur/HomeKey-ESP32)
nodes over the documented household MQTT API (**firmware 0.10.0**).

```
HomeKey Household
├── Household (HOME-001)
│   ├── Gate        (GATE-001)
│   ├── Main House  (HOUSE-001)
│   ├── Small House (SMALL-001)
│   └── Garage      (GARAGE-001)
```

Each node appears as its own Home Assistant **Device** with a lock and the six
documented household entities. The integration is a *client* of the ESP32 MQTT
API — it does not depend on firmware internals, and it reuses the Home Assistant
core MQTT integration as its transport.

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

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test,lint]"

.venv/bin/python -m pytest -q          # unit tests
.venv/bin/ruff check custom_components tests
.venv/bin/mypy custom_components/homekey_household
```

The test suite covers topic construction, node identity, entity unique ids,
multi-node and multi-household isolation, all documented payload parsers, the
HMAC canonical input and SHA-256 generation, timestamp/nonce/request-id handling,
lock and unlock commands, malformed data, missing credentials, legacy topic
rejection, and replacement-node identity.
