"""End-to-end validation of the broker-less (``direct``) transport.

Path under test::

    mDNS discovery -> config flow -> config entry -> TLS client -> coordinator
                                                            -> entities -> lock

A real HTTPS server stands in for a node's ``/api/ha`` surface, with a real
self-signed certificate, reached over a real socket. That matters: certificate
pinning is the entire trust anchor of this transport, and a mocked client would
verify none of it. The server is the *only* fake here — Home Assistant, the flow
engine, the coordinator and every entity are genuine.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import socket
import ssl
import subprocess
from datetime import timedelta
from ipaddress import ip_address
from pathlib import Path

import pytest
from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.homekey_household.const import (
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_HOUSEHOLD_ID,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TRANSPORT,
    CONF_USERNAME,
    DIRECT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    TRANSPORT_DIRECT,
    ZEROCONF_KEY_FINGERPRINT,
    ZEROCONF_KEY_ID,
    ZEROCONF_KEY_MODEL,
    ZEROCONF_KEY_NAME,
    ZEROCONF_KEY_PROTOCOL,
    ZEROCONF_KEY_TLS,
    ZEROCONF_KEY_VERSION,
)
from custom_components.homekey_household.direct import certificate_fingerprint

HOUSEHOLD = "HOME-DIRECT"
NODE = "GATE-DIRECT-001"
NODE_NAME = "Gate"
FIRMWARE = "0.11.0"

USERNAME = "admin"
PASSWORD = "test-only-web-password"

LOCK_UNLOCKED = 0
LOCK_LOCKED = 1

SERVICE_TYPE = "_homekey._tcp.local."


# ---------------------------------------------------------------------------
# A node's HTTPS face
# ---------------------------------------------------------------------------
class FakeNodeServer:
    """A node's ``/api/ha`` endpoints, served over real TLS on localhost."""

    def __init__(self, *, certfile: Path, keyfile: Path, fingerprint: str) -> None:
        self.certfile = certfile
        self.keyfile = keyfile
        self.fingerprint = fingerprint
        self.port: int | None = None
        self.lock_current = LOCK_LOCKED
        self.lock_target = LOCK_LOCKED
        # What asked for the most recent change, mirroring LockManager::sourceName().
        self.lock_source = "device"
        self.lock_requests: list[str] = []
        self.state_requests = 0
        self._runner: web.AppRunner | None = None

    # -- authentication -------------------------------------------------
    def _authorised(self, request: web.Request) -> bool:
        expected = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        return request.headers.get("Authorization", "") == f"Basic {expected}"

    def _unauthorised(self) -> web.Response:
        return web.json_response(
            {"error": "unauthorized"},
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Polaris"'},
        )

    # -- handlers -------------------------------------------------------
    async def _info(self, request: web.Request) -> web.Response:
        # Deliberately reachable without credentials, exactly like the firmware: a
        # client must be able to read the fingerprint before it has anything to
        # authenticate with.
        return web.json_response(
            {
                "protocol": 1,
                "transport": "tls",
                "secure": True,
                "port": self.port,
                "fingerprint": self.fingerprint,
                "setup_completed": True,
                "device": {
                    "name": "HomeKey",
                    "model": "HomeKey-ESP32",
                    "firmware": FIRMWARE,
                    "mac": "aa:bb:cc:dd:ee:ff",
                    "node_id": NODE,
                    "node_name": NODE_NAME,
                },
                "capabilities": {
                    "read_state": True,
                    "write_config": True,
                    "lock_control": True,
                },
            }
        )

    async def _state(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return self._unauthorised()
        self.state_requests += 1
        return web.json_response(
            {
                "protocol": 1,
                "firmware": FIRMWARE,
                "household_id": HOUSEHOLD,
                "household_name": "Direct Household",
                "household_state": "ACTIVE",
                "config_version": 1,
                "node_id": NODE,
                "node_name": NODE_NAME,
                "node_role": "gate",
                "node_state": "ACTIVE",
                "generation": 4,
                "wifi": {"connected": True, "rssi": -52},
                # The documented health document, verbatim.
                "health": {
                    "network": "UNKNOWN",
                    "mqtt": "ERROR",
                    "mqtt_error": 3,
                    "nfc": "OK",
                    "lock_current": self.lock_current,
                    "lock_target": self.lock_target,
                    "backup": "ok",
                    "certificate": "unknown",
                    "firmware_version": FIRMWARE,
                    "uptime": 600,
                    "free_heap": 123456,
                    "reset_reason": "1",
                    "security": {"all_ok": True, "warnings": ""},
                },
                "security": "OK",
                "backup_status": "completed",
                "lock_last": {
                    "current": self.lock_current,
                    "target": self.lock_target,
                    "source": self.lock_source,
                },
                "last_auth": {
                    "type": "HomeKey",
                    "result": "SUCCESS",
                    "timestamp": 1700000000,
                },
            }
        )

    async def _lock(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return self._unauthorised()
        body = await request.json()
        action = body.get("action")
        if action not in ("lock", "unlock"):
            return web.json_response({"error": "bad action"}, status=400)
        self.lock_requests.append(action)
        self.lock_current = LOCK_LOCKED if action == "lock" else LOCK_UNLOCKED
        self.lock_target = self.lock_current
        # Commands arrive through the device's own API, which is how the firmware reports
        # them: a change Home Assistant asked for, not one made at the door.
        self.lock_source = "api"
        return web.json_response(
            {
                "action": action,
                "state": "locked" if action == "lock" else "unlocked",
                "current": self.lock_current,
                "target": self.lock_target,
            }
        )

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/ha/info", self._info)
        app.router.add_get("/api/ha/state", self._state)
        app.router.add_post("/api/ha/lock", self._lock)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        # Bind explicitly so the OS picks a free port and the test can learn it.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        sock.setblocking(False)
        self.port = sock.getsockname()[1]

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.certfile), str(self.keyfile))
        site = web.SockSite(self._runner, sock, ssl_context=context)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def service_info(self, **txt_overrides) -> ZeroconfServiceInfo:
        """The mDNS record the firmware would advertise for this node."""
        properties: dict[str, object] = {
            ZEROCONF_KEY_ID: NODE.encode(),
            ZEROCONF_KEY_NAME: NODE_NAME.encode(),
            ZEROCONF_KEY_MODEL: b"HomeKey",
            ZEROCONF_KEY_VERSION: FIRMWARE.encode(),
            ZEROCONF_KEY_PROTOCOL: b"1",
            ZEROCONF_KEY_FINGERPRINT: self.fingerprint.encode(),
            "cfg": b"rw",
            ZEROCONF_KEY_TLS: b"1",
        }
        properties.update(txt_overrides)
        properties = {k: v for k, v in properties.items() if v is not None}
        return ZeroconfServiceInfo(
            ip_address=ip_address("127.0.0.1"),
            ip_addresses=[ip_address("127.0.0.1")],
            port=self.port,
            hostname="HK-TEST.local.",
            type=SERVICE_TYPE,
            name=f"HK-TEST.{SERVICE_TYPE}",
            properties=properties,
        )


@pytest.fixture(scope="session")
def certificate(tmp_path_factory) -> tuple[Path, Path, str]:
    """Generate the node's self-signed certificate once, with openssl.

    A real certificate rather than a fixture string: the point of these tests is
    that the client pins the certificate actually presented by the peer.
    """
    return _make_certificate(tmp_path_factory, "homekey-tls", "HomeKey-Test")


def _make_certificate(
    tmp_path_factory, directory_name: str, common_name: str
) -> tuple[Path, Path, str]:
    directory = tmp_path_factory.mktemp(directory_name)
    certfile = directory / "cert.pem"
    keyfile = directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(keyfile),
            "-out",
            str(certfile),
            "-days",
            "3650",
            "-subj",
            f"/CN={common_name}",
        ],
        check=True,
        capture_output=True,
    )
    fingerprint = certificate_fingerprint(
        ssl.PEM_cert_to_DER_cert(certfile.read_text())
    )
    return certfile, keyfile, fingerprint


@pytest.fixture(scope="session")
def foreign_certificate(tmp_path_factory) -> bytes:
    """A perfectly valid certificate belonging to a completely different device."""
    certfile, _, _ = _make_certificate(
        tmp_path_factory, "foreign-tls", "Some-Other-Device"
    )
    return ssl.PEM_cert_to_DER_cert(certfile.read_text())


@pytest.fixture
async def node(certificate) -> FakeNodeServer:
    certfile, keyfile, fingerprint = certificate
    server = FakeNodeServer(
        certfile=certfile, keyfile=keyfile, fingerprint=fingerprint
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@contextlib.contextmanager
def _ignoring_aborted_handshakes():
    """Swallow the loop error a server logs when a client aborts its handshake.

    Rejecting a certificate is the behaviour under test, and the peer's reaction to
    an abruptly closed connection is a consequence of that, not a fault in the
    integration. Everything else still reaches the original handler.
    """
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()

    def handler(loop_, context):
        if "transport creation for incoming connection" in str(context.get("message")):
            return
        if original is not None:
            original(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        loop.set_exception_handler(original)


def direct_entry(node: FakeNodeServer, **overrides) -> MockConfigEntry:
    data = {
        CONF_TRANSPORT: TRANSPORT_DIRECT,
        CONF_HOUSEHOLD_ID: HOUSEHOLD,
        CONF_HOST: "127.0.0.1",
        CONF_PORT: node.port,
        CONF_FINGERPRINT: node.fingerprint,
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, title=NODE_NAME)


async def start_flow(hass, service_info):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=service_info
    )
    await hass.async_block_till_done()
    return result


async def poll_once(hass) -> None:
    """Advance past the poll interval and let the background poll finish.

    Two intervals rather than one, so the test does not depend on how long setup itself
    took. Only one extra poll can happen: the helper iterates the timers that existed when
    it was called, so the rescheduled one is not in that snapshot.
    """
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DIRECT_POLL_INTERVAL_SECONDS * 2)
    )
    await hass.async_block_till_done(wait_background_tasks=True)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
async def test_zeroconf_reaches_the_credential_step(hass, node):
    """A discovered node is not asked to set up a broker first."""
    result = await start_flow(hass, node.service_info())

    assert result["type"] == "form"
    assert result["step_id"] == "direct"
    placeholders = result["description_placeholders"]
    # The fingerprint is shown so the user can compare it with the node's Web UI:
    # that comparison is the only reason pinning means anything.
    assert placeholders["expected"] == node.fingerprint

    # Aborting is a plain callback in this Home Assistant version, not a coroutine.
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_zeroconf_refuses_a_node_without_tls(hass, node):
    """The firmware will not serve state in the clear, so this cannot work."""
    result = await start_flow(hass, node.service_info(**{ZEROCONF_KEY_TLS: b"0"}))
    assert result["type"] == "abort"
    assert result["reason"] == "tls_required"


async def test_zeroconf_refuses_an_unknown_protocol(hass, node):
    result = await start_flow(hass, node.service_info(**{ZEROCONF_KEY_PROTOCOL: b"9"}))
    assert result["type"] == "abort"
    assert result["reason"] == "unsupported_protocol"


async def test_zeroconf_refuses_a_node_without_a_fingerprint(hass, node):
    """Nothing to pin means nothing to trust, so this is refused rather than
    accepted with verification silently disabled."""
    result = await start_flow(
        hass, node.service_info(**{ZEROCONF_KEY_FINGERPRINT: None})
    )
    assert result["type"] == "abort"
    assert result["reason"] == "no_fingerprint"


async def test_discovery_creates_a_working_broker_less_entry(hass, node):
    """The whole path: discovery, flow, entry, entities — with no broker anywhere."""
    result = await start_flow(hass, node.service_info())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": USERNAME, "password": PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_DIRECT
    assert entry.data[CONF_FINGERPRINT] == node.fingerprint
    assert entry.data[CONF_HOUSEHOLD_ID] == HOUSEHOLD
    assert entry.state is ConfigEntryState.LOADED
    # No MQTT entry was created or required.
    assert not hass.config_entries.async_entries("mqtt")


async def test_discovery_rejects_a_mismatched_certificate(hass, node):
    """A node advertising one fingerprint and presenting another is refused, and
    refused *before* any credential is sent."""
    before = node.state_requests
    result = await start_flow(
        hass, node.service_info(**{ZEROCONF_KEY_FINGERPRINT: b"AA:BB:CC:DD"})
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": USERNAME, "password": PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] == "form"
    assert result["errors"]["base"] == "fingerprint_mismatch"
    # The certificate is verified before the credentials are used at all, so the
    # password was never offered to whatever is answering.
    assert node.state_requests == before
    assert not hass.config_entries.async_entries(DOMAIN)

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_discovery_rejects_wrong_credentials(hass, node):
    result = await start_flow(hass, node.service_info())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": USERNAME, "password": "wrong"}
    )
    await hass.async_block_till_done()

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_discovery_aborts_when_the_household_is_already_configured(
    hass, node
):
    """Entities are keyed on the household, so a second entry would duplicate them."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOUSEHOLD_ID: HOUSEHOLD, CONF_TRANSPORT: "mqtt"},
        title="Existing",
    )
    existing.add_to_hass(hass)

    result = await start_flow(hass, node.service_info())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": USERNAME, "password": PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "household_already_configured"


# ---------------------------------------------------------------------------
# Entry setup
# ---------------------------------------------------------------------------
async def test_the_pin_is_enforced_by_tls_not_by_comparison(
    hass, node, foreign_certificate
):
    """A client pinning a different certificate cannot reach the node at all.

    This is the check that makes the whole transport mean something. Comparing
    fingerprints after a request has already been sent would be too late: the
    credential would already be in the hands of whoever answered. Here the TLS
    layer itself refuses, so nothing is ever sent.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.homekey_household.direct import (
        DirectClient,
        DirectFingerprintMismatch,
        pinned_ssl_context,
    )

    before = node.state_requests
    foreign = DirectClient(
        async_get_clientsession(hass),
        "127.0.0.1",
        node.port,
        pinned_ssl_context(foreign_certificate),
        USERNAME,
        PASSWORD,
    )
    # Aborting a handshake mid-flight makes the *server* log its own surprise, and it
    # does so asynchronously - so the drain below happens while the suppression is
    # still in place, because by teardown the harness would attribute that error to
    # the test as an unhandled loop error.
    with _ignoring_aborted_handshakes():
        # noqa: SIM117 - the drain has to sit inside the suppression, which is why
        # these cannot be collapsed into one statement.
        with pytest.raises(DirectFingerprintMismatch):  # noqa: SIM117
            await foreign.async_get_state()
        for _ in range(20):
            await asyncio.sleep(0.05)
    assert node.state_requests == before, "the request reached the node anyway"

    # And the node's own certificate is accepted, so the refusal above is the pin
    # working rather than the connection simply being broken.
    certificate_der = ssl.PEM_cert_to_DER_cert(node.certfile.read_text())
    good = DirectClient(
        async_get_clientsession(hass),
        "127.0.0.1",
        node.port,
        pinned_ssl_context(certificate_der),
        USERNAME,
        PASSWORD,
    )
    assert (await good.async_get_state())["node_id"] == NODE


async def test_entry_creates_the_documented_entities(hass, node):
    entry = direct_entry(node)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    lock_ids = hass.states.async_entity_ids("lock")
    assert len(lock_ids) == 1, f"expected one lock entity, got {lock_ids}"
    assert hass.states.get(lock_ids[0]).state == "locked"

    sensors = hass.states.async_entity_ids("sensor")
    assert len(sensors) == 5, f"expected the five documented sensors, got {sensors}"

    # The device reports its own firmware, and its health/security/last-auth
    # telemetry came through the shared ingestion path.
    from homeassistant.helpers import device_registry as dr

    devices = [
        d
        for d in dr.async_get(hass).devices
        if any(ident[0] == DOMAIN for ident in d.identifiers)
    ]
    assert len(devices) == 1
    assert devices[0].sw_version == FIRMWARE


async def test_lock_can_be_controlled_with_no_broker(hass, node):
    entry = direct_entry(node)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    lock_id = hass.states.async_entity_ids("lock")[0]
    assert node.lock_current == LOCK_LOCKED

    await hass.services.async_call(
        "lock", "unlock", {"entity_id": lock_id}, blocking=True
    )
    await hass.async_block_till_done()

    assert node.lock_requests == ["unlock"]
    # The entity reflects what the node reported afterwards, not what was requested.
    assert hass.states.get(lock_id).state == "unlocked"

    await hass.services.async_call(
        "lock", "lock", {"entity_id": lock_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert node.lock_requests == ["unlock", "lock"]
    assert hass.states.get(lock_id).state == "locked"


async def test_the_node_is_polled_again_on_its_interval(hass, node):
    entry = direct_entry(node)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock_id = hass.states.async_entity_ids("lock")[0]
    baseline = node.state_requests

    # The node's lock is operated outside Home Assistant, as it would be by a key.
    node.lock_current = LOCK_UNLOCKED
    node.lock_target = LOCK_UNLOCKED

    # Two intervals, not one: the helper advances the clock and fires every timer
    # that is now due, and a margin keeps the test independent of how long setup
    # itself took. Only one extra poll can happen either way - the helper iterates
    # the timers that existed when it was called, so the rescheduled one is not in
    # that snapshot.
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=DIRECT_POLL_INTERVAL_SECONDS * 2),
    )
    # The interval listener dispatches the poll as a background task - Home
    # Assistant's own tracking helper does that, so a slow poll cannot hold up the
    # loop - and ``async_block_till_done`` ignores background tasks unless asked.
    await hass.async_block_till_done(wait_background_tasks=True)

    assert node.state_requests > baseline
    assert hass.states.get(lock_id).state == "unlocked"


async def test_entry_refuses_a_certificate_that_does_not_match(hass, node):
    """Re-verified at every start, not assumed from the day it was configured."""
    entry = direct_entry(node, **{CONF_FINGERPRINT: "AA:BB:CC:DD"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_entry_asks_for_reauth_when_the_password_changed(hass, node):
    entry = direct_entry(node, **{CONF_PASSWORD: "stale"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == "reauth" for flow in flows), flows


async def test_reauth_replaces_the_credential(hass, node):
    entry = direct_entry(node, **{CONF_PASSWORD: "stale"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flow = [
        f
        for f in hass.config_entries.flow.async_progress()
        if f["context"].get("source") == "reauth"
    ][0]
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"username": USERNAME, "password": PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_PASSWORD] == PASSWORD
    assert entry.state is ConfigEntryState.LOADED


async def test_unlock_at_the_door_is_attributed_in_the_activity_log(hass, node):
    """The whole point of the lock-cause work, with a real Home Assistant.

    The node knows what changed the lock, reports it, and the integration turns that into a
    cause the activity view can name - sharing the context of the state change so the two
    are joined rather than merely adjacent.
    """
    from homeassistant.const import EVENT_LOGBOOK_ENTRY

    entry = direct_entry(node)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    lock_id = hass.states.async_entity_ids("lock")[0]
    assert hass.states.get(lock_id).state == "locked"

    entries: list = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, entries.append)

    # Someone unlocks it at the door. The node reports the new state *and* what caused it.
    node.lock_current = LOCK_UNLOCKED
    node.lock_target = LOCK_UNLOCKED
    node.lock_source = "homekit"
    await poll_once(hass)

    assert hass.states.get(lock_id).state == "unlocked"
    assert len(entries) == 1, entries
    assert entries[0].data["entity_id"] == lock_id
    assert "unlocked" in entries[0].data["message"]
    assert "HomeKit" in entries[0].data["message"]
    # The state change must carry the same context: that is what the activity view joins
    # the cause to. Without it the entry is just an unrelated line in the log.
    assert hass.states.get(lock_id).context == entries[0].context

    # A repeat reading is not a change, so it is not attributed twice.
    await poll_once(hass)
    assert len(entries) == 1


async def test_a_change_home_assistant_made_keeps_home_assistants_attribution(
    hass, node
):
    """A service call's own context must survive, or the user's name would vanish."""
    from homeassistant.const import EVENT_LOGBOOK_ENTRY
    from homeassistant.core import Context

    entry = direct_entry(node)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock_id = hass.states.async_entity_ids("lock")[0]

    entries: list = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, entries.append)

    context = Context()
    await hass.services.async_call(
        "lock", "unlock", {"entity_id": lock_id}, blocking=True, context=context
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert node.lock_requests == ["unlock"]
    assert hass.states.get(lock_id).state == "unlocked"
    # Nothing of ours was added, and the change still carries the service call's context.
    assert entries == []
    assert hass.states.get(lock_id).context == context


async def test_unload_stops_polling(hass, node):
    entry = direct_entry(node)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("lock")

    poller = hass.data[DOMAIN][entry.entry_id].transport
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert poller.scheduled is False, "the poll timer outlived the entry"

    before = node.state_requests
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert node.state_requests == before, "polling continued after unload"

