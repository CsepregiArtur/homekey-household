"""Direct (broker-less) transport for HomeKey nodes.

The primary transport is MQTT, which needs a broker. This module talks to the
node's own HTTPS API (``/api/ha/*``) instead, so a household works with no broker
at all. It is an *addition*: everything here produces the same shapes the MQTT
path produces, so the coordinator, models and entities are shared unchanged.

Trust model
-----------
The node serves a certificate it generated itself, so there is no certificate
authority to chain to and the subject cannot match a DHCP address. The SHA-256
fingerprint, advertised over mDNS and shown in the node's Web UI, is the whole of
the trust anchor. Pinning is therefore load-bearing rather than a hardening
extra: without it, TLS would only mean the bytes are scrambled, with nothing
establishing who is on the other end.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import aiohttp

from .const import (
    ACTION_LOCK,
    ACTION_UNLOCK,
    DIRECT_OFFLINE_AFTER_FAILURES,
    DIRECT_POLL_INTERVAL_SECONDS,
    HA_API_PROTOCOL,
    HEALTH_CERTIFICATE_UNKNOWN,
    HEALTH_ERROR,
    HEALTH_NETWORK_UNKNOWN,
    HEALTH_OK,
    TOPIC_BACKUP_STATUS,
    TOPIC_HEALTH,
    TOPIC_LAST_AUTH,
    TOPIC_LOCK_LAST,
    TOPIC_SECURITY,
    TOPIC_STATE,
    TOPIC_STATUS,
    AuthResult,
    BackupOutcome,
    SecurityState,
)
from .models import ValidationError
from .mqtt import HomeKeyMessage

_LOGGER = logging.getLogger(__name__)

# The node is an ESP32 also running HomeKit plus an HTTPS server, and its TLS
# handshake is not fast. This is deliberately generous.
REQUEST_TIMEOUT = 20
CERT_FETCH_TIMEOUT = 10

# The firmware's sentinel for "the lock was not reported" (the health snapshot's
# default when no lock manager is attached). Not a state the contract defines.
LOCK_STATE_NOT_REPORTED = 255


class DirectTransportError(Exception):
    """Base error for the direct transport."""


class DirectAuthError(DirectTransportError):
    """The node rejected the credentials."""


class DirectTlsUnavailableError(DirectTransportError):
    """The node is reachable but is not serving HTTPS.

    The firmware refuses to serve state or configuration over plain HTTP, so this
    is a deliberate refusal to be worked around, not a transient failure.
    """


class DirectProtocolError(DirectTransportError):
    """The node speaks a different API version than this integration understands."""


class DirectNoHouseholdError(DirectProtocolError):
    """The node is reachable and understood, but has no household.

    A distinct type because the remedy is entirely different from a protocol or
    connection problem: nothing is broken, the node simply has not been given the
    identity its entities would be keyed on.
    """


class DirectFingerprintMismatch(DirectTransportError):
    """The node presented a certificate other than the pinned one."""

    def __init__(
        self,
        message: str | None = None,
        *,
        expected: str = "",
        actual: str = "",
    ) -> None:
        # Both fingerprints are kept so a UI can show the user exactly what changed:
        # "the certificate differs" is not actionable on its own.
        super().__init__(
            message
            or (
                "The node presented a certificate that does not match the pinned "
                "fingerprint. It may have been factory reset or replaced."
            )
        )
        self.expected = expected
        self.actual = actual


def normalise_fingerprint(value: str) -> str:
    """Reduce a fingerprint to bare uppercase hex so formats can be compared."""
    return value.replace(":", "").replace(" ", "").replace("-", "").upper()


def format_fingerprint(digest: bytes) -> str:
    """Render a digest the way the node does: colon-separated uppercase hex."""
    return ":".join(f"{byte:02X}" for byte in digest)


def fingerprints_match(expected: str, actual: str) -> bool:
    return normalise_fingerprint(expected) == normalise_fingerprint(actual)


def fetch_peer_certificate(host: str, port: int, timeout: float = CERT_FETCH_TIMEOUT) -> bytes:
    """Return the DER certificate the node presents, without validating it.

    Deliberately unvalidated: this call is how we discover what we are about to
    pin. Trust is established by comparing the result against the fingerprint the
    user confirmed.

    Blocking socket work, so callers must run it in an executor.

    Raises OSError on a connection failure, ValueError when no certificate is
    offered.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with (
        socket.create_connection((host, port), timeout=timeout) as raw_socket,
        context.wrap_socket(raw_socket, server_hostname=host) as tls_socket,
    ):
        der = tls_socket.getpeercert(binary_form=True)

    if not der:
        raise ValueError("The node did not present a certificate")

    return der


def certificate_fingerprint(der: bytes) -> str:
    """SHA-256 of a DER certificate, formatted as the node reports it."""
    return format_fingerprint(hashlib.sha256(der).digest())


def pinned_ssl_context(der: bytes) -> ssl.SSLContext:
    """Build an SSL context that trusts only this exact certificate.

    ``check_hostname`` is off because the certificate's subject is the node's own
    name and no SAN can match a changing DHCP address; the pin is what actually
    provides the guarantee. ``CERT_REQUIRED`` stays on with only the pinned
    certificate as a trusted root, so a node presenting anything else is rejected
    by the TLS layer itself - a stronger check than comparing digests afterwards.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der))
    return context


def health_payload_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """Translate a ``/api/ha/state`` response into a documented health payload.

    The point of translating rather than parsing directly is that the result goes
    through the same ``NodeHealth.from_dict`` the MQTT transport uses, so both
    transports cannot drift apart in how a value is interpreted.

    Fields the firmware documents as stubs (``network``, ``certificate``) are
    passed through as the documented literal. They are never filled in from
    unrelated data: a plausible-looking value here would be a lie the firmware
    itself declines to tell.
    """
    lock = state.get("lock") or {}
    reader = state.get("reader") or {}
    mqtt = state.get("mqtt") or {}

    payload: dict[str, Any] = {
        "network": HEALTH_NETWORK_UNKNOWN,
        "certificate": HEALTH_CERTIFICATE_UNKNOWN,
        "nfc": HEALTH_OK if reader.get("connected") else HEALTH_ERROR,
        "mqtt": HEALTH_OK if mqtt.get("connected") else HEALTH_ERROR,
    }

    firmware = state.get("firmware")
    if firmware:
        payload["firmware_version"] = firmware

    # Absent when the node reports no lock manager; omit rather than invent a state.
    if lock.get("available") and lock.get("current") is not None:
        payload["lock_current"] = lock["current"]
    if lock.get("available") and lock.get("target") is not None:
        payload["lock_target"] = lock["target"]

    return payload


def health_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the documented health payload carried by a ``/api/ha/state`` response.

    Current firmware embeds the health document verbatim - the same JSON it publishes
    on ``B/health`` - so the common case needs no interpretation at all. Older firmware
    only has the structured fields, which :func:`health_payload_from_state` translates.
    """
    health = state.get("health")
    if isinstance(health, dict):
        payload = dict(health)
        # Drop the "not reported" sentinel rather than surfacing 255 as if it were a
        # lock state. The contract defines no such value, and a magic number in an
        # entity attribute is worse than an absent one.
        for key in ("lock_current", "lock_target"):
            if payload.get(key) == LOCK_STATE_NOT_REPORTED:
                payload.pop(key)
        return payload
    return health_payload_from_state(dict(state))


def resolve_identity(
    state: Mapping[str, Any], info: Mapping[str, Any]
) -> tuple[str, str]:
    """Return the ``(household_id, node_id)`` a node reports about itself.

    Both are required. The household id is what entities are keyed on and what the
    firmware the integration talks to has in common with the MQTT contract, so it is
    never invented here: a node that has no household is reported as a protocol
    problem, not silently assigned a made-up identity that would then diverge from
    the same node's MQTT entity ids.

    ``/api/ha/state`` carries identity; ``/api/ha/info`` carries the node id only, and
    exists as a fallback for firmware predating the identity fields.
    """
    device = info.get("device")
    device = device if isinstance(device, dict) else {}

    household_id = state.get("household_id") or ""
    node_id = state.get("node_id") or device.get("node_id") or ""

    if not node_id:
        raise DirectProtocolError(
            "The node did not report a node id, so there is nothing to identify it by"
        )
    if not household_id:
        raise DirectNoHouseholdError(
            f"Node {node_id} has no household. On the node's Web UI open Provision, issue "
            "a one-time code, then join with a household id - the Household page only "
            "shows what is already there. The household is what its entities are keyed "
            "on."
        )
    return str(household_id), str(node_id)


def state_to_messages(
    state: Mapping[str, Any],
    info: Mapping[str, Any],
    *,
    household_id: str,
    node_id: str,
) -> list[HomeKeyMessage]:
    """Describe a ``/api/ha/state`` response as the messages MQTT would have sent.

    The node's API is not MQTT, but the coordinator's ingestion path is pure data: it
    takes a subtopic and a payload and knows nothing about what carried them.
    Restating a direct response as those messages therefore reuses every parser,
    validator and merge rule rather than reimplementing them - which is the only way
    two transports cannot end up reading the same firmware differently.
    """
    device = info.get("device")
    device = device if isinstance(device, dict) else {}

    messages = [
        HomeKeyMessage(
            household_id=household_id,
            node_id=node_id,
            subtopic=TOPIC_STATUS,
            payload="online",
            retain=True,
        )
    ]

    # Sent before the health document that carries the resulting lock state, so the cause
    # is on record by the time the change it explains is seen. Absent on firmware
    # predating the field, in which case nothing is claimed rather than guessed.
    lock_last = state.get("lock_last")
    if isinstance(lock_last, dict) and lock_last.get("source"):
        messages.append(
            HomeKeyMessage(
                household_id=household_id,
                node_id=node_id,
                subtopic=TOPIC_LOCK_LAST,
                payload=json.dumps(lock_last),
                retain=True,
            )
        )

    # ``state`` payload: identity only, exactly the fields ``B/state`` carries. The
    # firmware omits an unspecified node name/role, so the defaults come from whatever
    # the identity endpoints did report and the model applies its own fallback.
    identity: dict[str, Any] = {"household_id": household_id, "node_id": node_id}
    node_name = state.get("node_name") or device.get("node_name")
    if node_name:
        identity["node_name"] = node_name
    node_role = state.get("node_role")
    if node_role:
        identity["node_role"] = node_role
    node_state = state.get("node_state")
    if node_state:
        identity["node_state"] = node_state
    generation = state.get("generation")
    if isinstance(generation, int) and not isinstance(generation, bool):
        identity["generation"] = generation
    firmware = state.get("firmware")
    if firmware:
        identity["firmware_version"] = firmware
    messages.append(
        HomeKeyMessage(
            household_id=household_id,
            node_id=node_id,
            subtopic=TOPIC_STATE,
            payload=json.dumps(identity),
            retain=True,
        )
    )

    messages.append(
        HomeKeyMessage(
            household_id=household_id,
            node_id=node_id,
            subtopic=TOPIC_HEALTH,
            payload=json.dumps(health_from_state(state)),
        )
    )

    # Optional topics. Each is only sent when the node actually reports a value the
    # contract defines, so a sensor shows "no data" rather than a plausible-looking
    # guess. The coordinator rejects unknown values anyway; filtering here keeps that
    # rejection from being logged as a payload fault on every poll.
    security = state.get("security")
    if isinstance(security, str) and security in set(SecurityState):
        messages.append(
            HomeKeyMessage(
                household_id=household_id,
                node_id=node_id,
                subtopic=TOPIC_SECURITY,
                payload=security,
                retain=True,
            )
        )

    backup_status = state.get("backup_status")
    if isinstance(backup_status, str) and backup_status in set(BackupOutcome):
        messages.append(
            HomeKeyMessage(
                household_id=household_id,
                node_id=node_id,
                subtopic=TOPIC_BACKUP_STATUS,
                payload=backup_status,
                retain=True,
            )
        )

    last_auth = state.get("last_auth")
    if isinstance(last_auth, dict) and last_auth.get("result") in set(AuthResult):
        messages.append(
            HomeKeyMessage(
                household_id=household_id,
                node_id=node_id,
                subtopic=TOPIC_LAST_AUTH,
                payload=json.dumps(last_auth),
                retain=True,
            )
        )

    return messages


class DirectClient:
    """Thin client for a single node's ``/api/ha`` endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._ssl_context = ssl_context
        self._auth = aiohttp.BasicAuth(username, password)

    @property
    def base_url(self) -> str:
        return f"https://{self._host}:{self._port}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                ssl=self._ssl_context,
                auth=self._auth,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                **kwargs,
            ) as response:
                if response.status == 401:
                    raise DirectAuthError("The node rejected the credentials")
                if response.status == 503:
                    raise DirectTlsUnavailableError(
                        (await response.text()).strip() or "HTTPS is not active"
                    )
                if response.status == 404:
                    # An older firmware without the /api/ha surface.
                    raise DirectProtocolError(
                        f"The node does not implement {path}"
                    )
                if response.status >= 400:
                    raise DirectTransportError(
                        f"HTTP {response.status}: {(await response.text()).strip()}"
                    )
                return await response.json(content_type=None)
        except aiohttp.ClientConnectorCertificateError as err:
            # The pinned certificate did not match. This is precisely the failure
            # the pin exists to produce, so it must not read as a generic
            # connection problem.
            raise DirectFingerprintMismatch(
                "The node presented a certificate that does not match the pinned "
                "fingerprint. It may have been factory reset or replaced."
            ) from err
        except aiohttp.ClientSSLError as err:
            raise DirectTransportError(f"TLS handshake failed: {err}") from err
        except aiohttp.ClientError as err:
            raise DirectTransportError(
                f"Could not reach the node at {self._host}: {err}"
            ) from err
        except TimeoutError as err:
            raise DirectTransportError("Timed out talking to the node") from err

    async def async_get_info(self) -> dict[str, Any]:
        """Identity and capabilities. Reachable without credentials by design."""
        return await self._request("GET", "/api/ha/info")

    async def async_get_state(self) -> dict[str, Any]:
        return await self._request("GET", "/api/ha/state")

    async def async_check_protocol(self, supported: int = HA_API_PROTOCOL) -> dict[str, Any]:
        """Fetch info and refuse a node this integration cannot interpret."""
        info = await self.async_get_info()
        reported = info.get("protocol")
        if reported != supported:
            raise DirectProtocolError(
                f"The node speaks API protocol {reported}, this integration "
                f"understands {supported}"
            )
        return info

    async def async_lock(self, action: str) -> dict[str, Any]:
        """Command the node's lock over the authenticated direct API.

        Unlike the MQTT transport this carries no HMAC: the node authorises the
        request with the device credential, over a TLS connection whose peer is pinned
        to an exact certificate. The command is what the node's own Web UI would
        perform, so it is no weaker than the interface the device already exposes.
        """
        if action not in (ACTION_LOCK, ACTION_UNLOCK):
            raise DirectProtocolError(f"Unsupported lock action: {action!r}")
        return await self._request("POST", "/api/ha/lock", json={"action": action})

    async def async_create_backup(self) -> str:
        """Ask the node for an encrypted backup and return it as hex.

        The backup is produced on demand and handed over in the reply; the node keeps only
        the time and hash of the last one. Whoever asked is therefore the only holder of it,
        which is why the integration asks on a schedule instead of trusting somebody to
        remember to open the Web UI and click Download.
        """
        result = await self._request("POST", "/backup/create")
        blob = result.get("backup")
        if not isinstance(blob, str) or not blob:
            raise DirectProtocolError("The node did not return a backup")
        return blob

    async def async_restore_backup(self, recovery_secret: str, backup: str) -> dict[str, Any]:
        """Hand a backup and the recovery secret to a node, and let it restore itself.

        The secret both authorises the restore and decrypts the backup - it is the key the
        backup was sealed with - so it is passed straight to the node and never stored.
        """
        return await self._request(
            "POST", "/backup/restore", json={"secret": recovery_secret, "backup": backup}
        )

    async def async_get_backup_info(self) -> dict[str, Any]:
        """The node's summary of its last backup: outcome, time and hash.

        Metadata only - it never carries the backup itself, which is the point of storing
        one somewhere else.
        """
        return await self._request("GET", "/backup")


@dataclass(frozen=True)
class DirectProbe:
    """Everything a successful connection to a node established.

    A single implementation shared by the config flow and by entry setup, so the
    checks performed when a node is first added are exactly the checks performed on
    every later start - the trust anchor is re-verified rather than assumed to still
    be true because it once was.
    """

    client: DirectClient
    der: bytes
    fingerprint: str
    household_id: str
    household_name: str
    node_id: str
    node_name: str
    model: str
    firmware: str
    info: dict[str, Any]
    state: dict[str, Any]


async def async_connect_node(
    to_executor: Callable[..., Awaitable[Any]],
    session: aiohttp.ClientSession,
    *,
    host: str,
    port: int,
    expected_fingerprint: str,
    username: str,
    password: str,
) -> DirectProbe:
    """Pin the node's certificate, authenticate, and read its identity.

    ``to_executor`` is a callable that runs a blocking function in a worker thread
    (Home Assistant's ``async_add_executor_job``). Injecting it rather than importing
    Home Assistant keeps this module usable, and testable, without it.

    Raises a :class:`DirectTransportError` subclass for every way this can fail, so
    callers only have to map exception types to something the user can act on.
    """
    try:
        der = await to_executor(fetch_peer_certificate, host, port)
    except OSError as err:
        raise DirectTransportError(f"Could not reach {host}:{port}: {err}") from err
    except ValueError as err:
        raise DirectTransportError(str(err)) from err

    actual = certificate_fingerprint(der)
    if not fingerprints_match(expected_fingerprint, actual):
        # Refuse before sending a credential. If the certificate is not the one the
        # node advertises, there is no reason to believe anything behind it.
        raise DirectFingerprintMismatch(
            expected=expected_fingerprint, actual=actual
        )

    client = DirectClient(session, host, port, pinned_ssl_context(der), username, password)
    info = await client.async_check_protocol(HA_API_PROTOCOL)
    state = await client.async_get_state()
    household_id, node_id = resolve_identity(state, info)

    device = info.get("device")
    device = device if isinstance(device, dict) else {}
    return DirectProbe(
        client=client,
        der=der,
        fingerprint=actual,
        household_id=household_id,
        household_name=str(state.get("household_name") or household_id),
        node_id=node_id,
        node_name=str(state.get("node_name") or device.get("node_name") or node_id),
        model=str(device.get("model") or ""),
        firmware=str(state.get("firmware") or device.get("firmware") or ""),
        info=info,
        state=state,
    )



class DirectPoller:
    """Keeps one node's coordinator state fresh over the direct API.

    MQTT pushes; this polls. Everything it learns is handed to the coordinator as the
    messages the MQTT transport would have delivered, so the two transports share one
    ingestion path.

    A run of failures - not a single slow response - marks the node offline. The node
    serves TLS from the same chip that is running HomeKit, so an occasional slow
    handshake is normal, and flapping a lock entity to unavailable on it would be a
    worse lie than reporting the last known state.
    """

    def __init__(
        self,
        hass: Any,
        coordinator: Any,
        client: DirectClient,
        *,
        household_id: str,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._client = client
        self._household_id = household_id
        self._node_id: str | None = None
        self._failures = 0
        self._identity_mismatch_logged = False
        self._unsubscribe: Any = None

    @property
    def node_id(self) -> str | None:
        """Node id reported by the device, once a poll has succeeded."""
        return self._node_id

    @property
    def client(self) -> DirectClient:
        """The client used for authenticated commands as well as reads."""
        return self._client

    @property
    def scheduled(self) -> bool:
        """True while the recurring poll is scheduled."""
        return self._unsubscribe is not None

    async def async_poll_once(self) -> None:
        """Fetch the node's state and feed it through the coordinator.

        Raises a :class:`DirectTransportError` subclass on failure; callers that must
        not fail (the scheduled poll) use :meth:`async_poll_safely`.
        """
        info = await self._client.async_get_info()
        state = await self._client.async_get_state()
        household_id, node_id = resolve_identity(state, info)

        if household_id != self._household_id:
            # The node has been re-homed since this entry was created. Its entities are
            # keyed on the household id recorded at setup, so its state cannot be
            # applied without silently relabelling the device. Say so once, and leave
            # the last known state in place rather than writing a confusing mixture.
            if not self._identity_mismatch_logged:
                self._identity_mismatch_logged = True
                _LOGGER.error(
                    "Node %s now belongs to household %s, but this entry was created "
                    "for household %s; delete and re-add the integration entry",
                    node_id,
                    household_id,
                    self._household_id,
                )
            return

        self._node_id = node_id
        for message in state_to_messages(
            state, info, household_id=household_id, node_id=node_id
        ):
            await self._coordinator.async_handle_message(message)
        self._failures = 0

    async def async_poll_safely(self) -> None:
        """Poll, converting a failure into availability instead of an exception."""
        try:
            await self.async_poll_once()
        except (DirectTransportError, ValidationError) as err:
            self._failures += 1
            _LOGGER.debug(
                "Direct poll of %s failed (%s/%s consecutive): %s",
                self._node_id or self._client.base_url,
                self._failures,
                DIRECT_OFFLINE_AFTER_FAILURES,
                err,
            )
            if self._failures == DIRECT_OFFLINE_AFTER_FAILURES and self._node_id:
                _LOGGER.warning(
                    "Node %s has not answered %s polls in a row (%s); marking it offline",
                    self._node_id,
                    self._failures,
                    err,
                )
                await self._async_mark_offline()
            return

        if self._failures >= DIRECT_OFFLINE_AFTER_FAILURES:
            _LOGGER.info("Node %s is answering again", self._node_id)
        self._failures = 0

    async def _async_mark_offline(self) -> None:
        """Report the node offline through the same path a retained status would use."""
        assert self._node_id is not None  # noqa: S101 - guarded by the caller
        await self._coordinator.async_handle_message(
            HomeKeyMessage(
                household_id=self._household_id,
                node_id=self._node_id,
                subtopic=TOPIC_STATUS,
                payload="offline",
                retain=True,
            )
        )

    async def async_start(self) -> None:
        """Keep polling on an interval. Callers poll once first via a safe poll."""
        # Imported here rather than at module scope so this module stays importable
        # without Home Assistant, which is what lets the protocol be tested standalone.
        from homeassistant.helpers.event import async_track_time_interval

        self._unsubscribe = async_track_time_interval(
            self._hass,
            self._async_tick,
            timedelta(seconds=DIRECT_POLL_INTERVAL_SECONDS),
        )

    async def _async_tick(self, now: Any = None) -> None:
        await self.async_poll_safely()

    async def async_stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None


__all__ = [
    "DirectAuthError",
    "DirectClient",
    "DirectFingerprintMismatch",
    "DirectNoHouseholdError",
    "DirectPoller",
    "DirectProbe",
    "DirectProtocolError",
    "DirectTlsUnavailableError",
    "DirectTransportError",
    "async_connect_node",
    "certificate_fingerprint",
    "fetch_peer_certificate",
    "fingerprints_match",
    "format_fingerprint",
    "health_from_state",
    "health_payload_from_state",
    "normalise_fingerprint",
    "pinned_ssl_context",
    "resolve_identity",
    "state_to_messages",
]
