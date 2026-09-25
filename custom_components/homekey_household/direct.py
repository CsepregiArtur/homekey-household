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

import asyncio
import hashlib
import logging
import socket
import ssl
from typing import Any

import aiohttp

from .const import (
    HEALTH_CERTIFICATE_UNKNOWN,
    HEALTH_ERROR,
    HEALTH_NETWORK_UNKNOWN,
    HEALTH_OK,
)

_LOGGER = logging.getLogger(__name__)

# The node is an ESP32 also running HomeKit plus an HTTPS server, and its TLS
# handshake is not fast. This is deliberately generous.
REQUEST_TIMEOUT = 20
CERT_FETCH_TIMEOUT = 10


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


class DirectFingerprintMismatch(DirectTransportError):
    """The node presented a certificate other than the pinned one."""


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

    with socket.create_connection((host, port), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
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
        except asyncio.TimeoutError as err:
            raise DirectTransportError("Timed out talking to the node") from err

    async def async_get_info(self) -> dict[str, Any]:
        """Identity and capabilities. Reachable without credentials by design."""
        return await self._request("GET", "/api/ha/info")

    async def async_get_state(self) -> dict[str, Any]:
        return await self._request("GET", "/api/ha/state")

    async def async_check_protocol(self, supported: int) -> dict[str, Any]:
        """Fetch info and refuse a node this integration cannot interpret."""
        info = await self.async_get_info()
        reported = info.get("protocol")
        if reported != supported:
            raise DirectProtocolError(
                f"The node speaks API protocol {reported}, this integration "
                f"understands {supported}"
            )
        return info
