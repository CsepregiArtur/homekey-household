# Backup and full restore

How a node's backup is taken, how to make that backup *also* carry the node's keys,
and how to put a node — or a replacement node — back from one.

This is the description of the flow. The same facts are visible in Home Assistant
itself; see [Seeing the flow in Home Assistant](#seeing-the-flow-in-home-assistant).

---

## 1. Where the backups are

Home Assistant's `Store`, key `homekey_household.backups`:

```
<config>/.storage/homekey_household.backups        # /config or /homeassistant, depending on install
```

Written `0o600` (owner-only) because the store is created with `private=True`.
Home Assistant's default is `0o644`, which would make it readable by anything else
on the host; there is no reason for a file holding node backups to be world-readable.

Newest **7 copies per node** are kept (`BACKUP_KEEP`), and the retention window is
over *time*: every scheduled backup is stored even if it looks unchanged, because the
node's blobs always differ (nonce and timestamp) and a window that skipped
"unchanged" copies would keep whatever was newest the last time something changed —
exactly the copy least likely to be current when it is needed.

> [!IMPORTANT]
> **The file does not contain usable keys.**
>
> The blob in it is the node's backup exactly as the node produced it: sealed with
> XChaCha20-Poly1305 and signed with Ed25519, under a key derived from the **household
> recovery secret**. That secret is *not* stored here — not in `.storage`, not in the
> config entry, not anywhere in Home Assistant. So this file on its own is ciphertext
> plus metadata. It cannot be turned back into a device without the secret you kept
> offline.
>
> It can *carry* the node's keys, but only inside that encryption, and only if the
> backup was taken with them requested. Which brings us to:

## 2. Including the credentials

A backup is taken in one of two shapes:

| Shape | Restores | A replacement node needs |
|---|---|---|
| **Configuration only** (default) | household membership, node configuration, issuers (public) | every HomeKey device and tag **re-enrolled / re-paired** |
| **With credentials** | the above **plus** the node's reader credential store and its HomeKit pairing state | **nothing** — it comes back as the same device |

A credential-carrying backup is the keys to the door. It is therefore **off by
default**, and turning it on is a deliberate act.

**Turn it on permanently** (applies to the daily schedule *and* the button), in
Home Assistant:

> **Settings → Devices & Services → HomeKey Household → Configure →**
> *"Include the node's keys in each backup (it becomes the keys to the door)"* → on

That writes the entry option `backup_include_credentials`, and it stays on until you
clear it.

**Or ask for one copy**, without changing the setting:

```yaml
action: homekey_household.create_backup
data:
  include_credentials: true      # this file only
```

The node reports back what it actually put in, and **that** is what gets recorded —
not what was asked for. A node whose firmware predates the option will produce a
configuration-only file even when asked, and the stored record says so. Check
`stored_includes_credentials` on the backup sensor (or `includes_credentials` in
diagnostics) before trusting a copy.

## 3. The full restore

### What it needs

1. A **stored backup** for the household (or one passed inline), and
2. the **household recovery secret** — the one the node exported once and you stored
   offline, and
3. an entry that can **reach the node's own HTTPS API**: address, certificate
   fingerprint (pinned) and Web UI credentials. A backup or a restore travels over the
   node's API, never over MQTT — so an entry created over MQTT with no API details
   configured cannot do either. That is what `api_configured: false` on the backup
   sensor means.

The secret is required *every* time and is never kept: it is simultaneously the key
the backup was sealed with and the proof of the right to rejoin the household. It is
passed to the node and dropped.

### The call

```yaml
action: homekey_household.restore_backup
data:
  config_entry_id: 01J...            # which node to restore
  recovery_secret: 9f2c...           # required
  # backup: 0107a1b2...             # optional: a specific copy as hex.
                                    # omitted = the newest stored copy
```

Omit `backup` and the newest stored copy for that household is used, which is the one
most likely to describe the household as you last left it.

### What happens, in order

1. The secret and the blob are sent to the node (`POST /backup/restore`).
2. The node decrypts the payload, verifies the signature, checks the household, and
   applies: household membership, configuration, and — if the copy carried them — its
   reader credential store and HomeKit pairing state.
3. If credentials were applied, the node answers `reboot_required: true` **and
   restarts itself** to put them into use. Expect it to be unreachable for a short
   while; the integration logs a warning saying so, because a silent gap looks exactly
   like a failed restore.
4. The integration re-reads the node's identity (`/api/ha/info`) and **reloads the
   config entry**. A restore is the one thing that can change who the node says it is —
   it may have just taken on a household, or a different node id, from the backup — and
   entities describing the identity it no longer has would otherwise linger.

### Restoring onto a replacement node

1. Set up the **new** hardware and flash the firmware.
2. Add it to Home Assistant as a new entry (address + fingerprint + Web UI credentials).
3. Call `homekey_household.restore_backup` for **that new entry**, with:
   - `recovery_secret`: the household secret you kept offline, and
   - `backup`: the hex of the **credential-carrying** copy
     (`stored_includes_credentials: true`).
4. The node applies the household, then restarts to apply the credentials.
5. The entry reloads. If the copy carried credentials, the existing Apple Home setup
   and enrolled tags continue to work — nothing is re-paired.

> [!WARNING]
> **A node's own identity is never cloned.** The household's *membership and trust* are
> restorable; a node's private identity key is not, and a replacement node keeps its own
> (`generation` is bumped). What a credential-carrying backup restores is the **reader**
> identity and HomeKit pairing, which is what the enrolled devices actually check.

## 4. Seeing the flow in Home Assistant

### The backup sensor

`sensor.<node>_backup_status` carries the flow as attributes:

| Attribute | Meaning |
|---|---|
| `status`, `backup_timestamp`, `backup_age_seconds` | what the **node** says about its last backup (metadata only — the node keeps no copy) |
| `stored_backups` | how many copies **Home Assistant** holds for this node |
| `stored_created`, `stored_age_seconds` | when the newest stored copy was taken |
| `stored_includes_credentials` | whether the newest copy carries the node's keys |
| `stored_backups_detail` | every copy: `created`, `age_seconds`, `includes_credentials`, `hex_bytes`, `node_time` |
| `restore` | the flow in one place: `available`, `from_credentials_backup`, `recovery_secret_required`, `service` |
| `api_configured` | whether a backup or restore can reach the node at all |

`restore.available` is false when there is nothing to restore from.
`restore.recovery_secret_required` is always true — the secret is never stored.

### Diagnostics

**Settings → Devices & Services → HomeKey Household → ⋮ → Download diagnostics** adds:

* `backup_api_configured` — can this entry reach the node's API,
* `backup_include_credentials` — the entry's option,
* `backup_store` — `count`, `newest_created`, `any_includes_credentials`, and one entry
  per copy (`node_id`, `created`, `includes_credentials`, `hex_bytes`, `node_time`).

No blob, no secret and no key material appears in diagnostics; the backup blobs are
never reported there.

### The services

Both services are documented in **Developer Tools → Actions**, including the
`include_credentials` and `recovery_secret` fields and what they do:
`homekey_household.create_backup`, `homekey_household.restore_backup`.

## 5. Limits worth knowing

* **The recovery secret is the whole thing.** Lose it and every stored backup is
  ciphertext forever. It is exported exactly once by the node, by design.
* **A backup is taken over the API, not MQTT.** An MQTT-only entry (no address,
  fingerprint or Web UI credentials) is skipped for backups; `api_configured` says so
  rather than a button silently doing nothing.
* **The store is per household, and pruned per node**, newest 7. Copies of other nodes
  are never touched by one node's pruning.
* **`backup/status: completed` on MQTT** means "a backup was produced", not "a backup is
  stored somewhere". The stored copies are the ones in the store, and only the backup
  sensor's `stored_*` attributes describe those.
* **A restore cannot be undone by a restore.** If you restore the wrong copy, restore
  the right one — but a credential-carrying restore also rewrites the node's reader
  identity, so the enrolled devices match whatever that copy held.
