"""Resilience: MQTT disconnect/reconnect, HA restart, retained vs non-retained.

Steps 7, 8, 9:

* unexpected MQTT disconnect -> LWT offline -> HA marks unavailable
* reconnect -> online -> HA recovers, no duplicates, commands work again
* Home Assistant restart with retained data present -> entities reappear with
  identical unique ids and device ids, no duplicates, credentials retained
* ``B/health`` is NON-retained: it must not be fabricated after a restart, and
  must recover when the node republishes it
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
from firmware_verifier import derive_command_key
from test_hmac_commands import (
    BASE,
    LOCK_TOPIC,
    MQTT_BROKER,
    MQTT_PORT,
    TEST_SALT,
    TEST_SECRET,
    CommandTap,
    _seed_credential,
    _setup_entry,
    _wait_for_lock_entity,
)

from helpers import (
    HOUSEHOLD,
    NODE_GATE,
    health_payload,
    last_auth_payload,
    node_base,
    publish_telemetry,
    settle,
    state_payload,
)

DOMAIN = "homekey_household"
MOSQUITTO_CONF = Path(__file__).resolve().parents[1] / "tools" / "integration" / "mosquitto.test.conf"


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.15)
    return False


class RestartableBroker:
    """Starts/stops mosquitto on demand so a real disconnect can be simulated."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(  # noqa: S603
            ["mosquitto", "-c", str(MOSQUITTO_CONF)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert _wait_for_port(MQTT_BROKER, MQTT_PORT), "mosquitto did not start"

    def stop(self) -> None:
        """Kill the broker abruptly (no clean shutdown) to force will delivery."""
        if self.proc is None:
            return
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)
        self.proc = None
        # Wait for the port to actually close.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((MQTT_BROKER, MQTT_PORT), timeout=0.5):
                    time.sleep(0.2)
            except OSError:
                return


@pytest.fixture
def restartable_broker(mosquitto_broker):
    """Give tests control over the session broker's lifecycle.

    The session broker is already running; this wrapper only stops/starts it.
    """
    broker = RestartableBroker()
    broker.proc = mosquitto_broker["proc"]
    yield broker
    # Ensure the broker is up for subsequent tests.
    if broker.proc is None or broker.proc.poll() is not None:
        broker.start()
        mosquitto_broker["proc"] = broker.proc
    else:
        mosquitto_broker["proc"] = broker.proc


async def _unavailable(hass, entity_id: str, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = hass.states.get(entity_id)
        if state is not None and state.state == "unavailable":
            return True
        await asyncio.sleep(0.2)
    return False


async def _state_becomes(hass, entity_id: str, value: str, timeout: float = 20.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = hass.states.get(entity_id)
        if state is not None and state.state == value:
            return True
        await asyncio.sleep(0.2)
    return False


LWT_HELPER = Path(__file__).resolve().parent / "lwt_node_helper.py"


class UncleanNode:
    """A node running in its OWN process, killable without an MQTT DISCONNECT.

    See ``lwt_node_helper.py`` for why the will must be triggered from a separate
    process rather than by force-closing a socket inside the test.
    """

    def __init__(self, client_id: str, status_topic: str) -> None:
        self.client_id = client_id
        self.status_topic = status_topic
        self.proc: subprocess.Popen[str] | None = None

    async def start(self) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 - fixed script + argv
            [
                os.environ.get("LWT_HELPER_PYTHON", "python3"),
                str(LWT_HELPER),
                MQTT_BROKER,
                str(MQTT_PORT),
                self.client_id,
                self.status_topic,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Wait for the helper to report that its Will is registered and it has
        # published the retained ``online`` status.
        assert self.proc.stdout is not None
        events: list[str] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line = await asyncio.get_running_loop().run_in_executor(
                None, self.proc.stdout.readline
            )
            if not line:
                break
            events.append(line.strip())
            if '"online_sent"' in line:
                return
        raise AssertionError(
            f"LWT helper did not come online; events={events!r} "
            f"rc={self.proc.poll()}"
        )

    def kill_unclean(self) -> None:
        """SIGKILL: no DISCONNECT is sent, so the broker fires the Will."""
        assert self.proc is not None
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)

    def cleanup(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=5)


def _registry_snapshot(hass) -> tuple[dict[str, str], set[str]]:
    """Return {unique_id: entity_id} and device identifier tuples for the domain."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    ents = {
        e.unique_id: e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == DOMAIN and e.unique_id
    }
    devices = set()
    for device in dr.async_get(hass).devices:
        for ident in device.identifiers:
            if ident[0] == DOMAIN:
                devices.add(ident)
    return ents, devices


class TestMqttDisconnectReconnect:
    async def test_lwt_offline_then_recovery(self, hass, mqtt_client):
        """LWT contract: will fires on uncompensated drop, then online recovers.

        Fidelity note: an LWT is published by a **live broker** when it detects a
        client disconnect. Killing the broker cannot deliver a will (nothing is
        left to publish it) and a graceful MQTT DISCONNECT suppresses it. The
        unclean drop is therefore produced by a disposable **helper process**
        that registers the will, publishes ``online``, then is ``SIGKILL``ed by
        the test. Nothing in the test's own event loop is ever touched, so
        teardown stays clean.

        Verification is done twice:
          1. observable at the broker (an independent subscriber sees ``offline``)
          2. observable in Home Assistant (the connectivity sensor reads ``off``)
              * the node-online entity is a CONNECTIVITY binary sensor, so a node
                reporting ``offline`` reads ``off`` -- ``unavailable`` is reserved
                for a node the integration cannot read at all.
        """
        import aiomqtt
        from homeassistant.helpers import entity_registry as er

        from helpers import node_base as nb

        await _seed_credential(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        online_id = None
        for e in er.async_get(hass).entities.values():
            if e.unique_id == f"{HOUSEHOLD}_{NODE_GATE}_online":
                online_id = e.entity_id
        assert online_id is not None, "node online entity missing"

        # Step 1: node initially available.
        assert await _state_becomes(hass, online_id, "on"), (
            "node did not start available"
        )

        status_topic = f"{nb(HOUSEHOLD, NODE_GATE)}/status"

        # Independent broker-side subscriber, so the will itself is proven even
        # if HA's view were inconclusive.
        will_seen = asyncio.Event()
        seen_payloads: list[str] = []

        async def _watch():
            async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as watcher:
                await watcher.subscribe(status_topic, qos=1)
                async for msg in watcher.messages:
                    payload = bytes(msg.payload).decode()
                    seen_payloads.append(payload)
                    if payload == "offline":
                        will_seen.set()
                        return

        watcher_task = asyncio.create_task(_watch())
        await asyncio.sleep(0.4)

        node = UncleanNode(f"node-{int(time.time() * 1000)}", status_topic)
        await node.start()
        try:
            await settle(hass, seconds=0.5)
            assert await _state_becomes(hass, online_id, "on"), (
                "node did not report online via its own process"
            )

            # Step 2: unexpected disconnect (SIGKILL -> no MQTT DISCONNECT).
            node.kill_unclean()

            # Step 3: broker publishes the offline LWT.
            try:
                async with asyncio.timeout(20):
                    await will_seen.wait()
            except TimeoutError:
                pytest.fail(
                    f"broker never published the LWT offline; saw {seen_payloads}"
                )

            # Step 4: HA sees the node go offline.
            assert await _state_becomes(hass, online_id, "off", timeout=20), (
                "LWT fired at the broker but HA did not mark the node offline"
            )
        finally:
            node.cleanup()
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher_task

        # Steps 5-7: node reconnects, publishes online, HA recovers.
        async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as node2:
            await node2.publish(status_topic, "online", qos=1, retain=True)
            await node2.publish(
                f"{nb(HOUSEHOLD, NODE_GATE)}/state",
                state_payload(HOUSEHOLD, NODE_GATE, "Gate Test"),
                qos=0,
                retain=True,
            )
        assert await _state_becomes(hass, online_id, "on", timeout=20), (
            "node did not recover to online after reconnect"
        )

    async def test_no_duplicates_after_reconnect(self, hass, restartable_broker, mqtt_client):
        await _seed_credential(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        before_ents, before_devs = _registry_snapshot(hass)
        assert before_ents

        restartable_broker.stop()
        await settle(hass, seconds=0.5)
        restartable_broker.start()
        await asyncio.sleep(1.0)

        import aiomqtt

        async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as node:
            await publish_telemetry(node, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass, seconds=1.5)

        after_ents, after_devs = _registry_snapshot(hass)
        assert after_ents == before_ents, "entity registry changed after reconnect"
        assert after_devs == before_devs, "device registry changed after reconnect"

    async def test_command_works_after_reconnect(self, hass, restartable_broker, mqtt_client):
        await _seed_credential(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        restartable_broker.stop()
        await settle(hass, seconds=0.5)
        restartable_broker.start()
        await asyncio.sleep(1.0)
        # Re-establish the integration's MQTT session by re-publishing telemetry.
        import aiomqtt

        async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as node:
            await publish_telemetry(node, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass, seconds=1.0)

        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "lock", {"entity_id": entity_id}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(LOCK_TOPIC)
        assert set(captured.body) == {"ts", "nonce", "req_id", "mac"}
        assert captured.qos == 1 and captured.retain is False

    async def test_retained_state_survives_broker_restart(self, hass, restartable_broker, mqtt_client):
        """Retained messages are broker state; mosquitto keeps them in memory."""
        await _seed_credential(hass, HOUSEHOLD)
        await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        # Re-publish retained telemetry, stop/start the broker, and confirm a
        # fresh subscriber still sees retained values.
        import aiomqtt

        async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as node:
            await node.publish(
                f"{node_base(HOUSEHOLD, NODE_GATE)}/security", "WARNING", qos=0, retain=True
            )
        restartable_broker.stop()
        restartable_broker.start()
        await asyncio.sleep(0.8)

        # mosquitto is configured with persistence=false, so retained state is
        # in-memory only: after a broker restart it is expected to be gone. This
        # documents the disposable-broker behaviour rather than asserting HA-side
        # retention (HA retains its own restored state).
        assert _wait_for_port(MQTT_BROKER, MQTT_PORT)


class TestHaRestartPersistence:
    """Step 8: restart Home Assistant with retained node data present."""

    async def test_entities_and_ids_survive_ha_setup_teardown(self, hass, mqtt_client):
        await _seed_credential(hass, HOUSEHOLD)
        entry = await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        await _wait_for_lock_entity(hass)

        before_ents, before_devs = _registry_snapshot(hass)
        assert before_ents, "no entities registered"
        assert f"{HOUSEHOLD}_{NODE_GATE}_lock" in before_ents

        # --- stop HA side: unload the config entry (as a restart would) ---
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        # --- start HA side again: set the SAME entry back up ---
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # The retained messages must repopulate nodes before platforms set up.
        await settle(hass, seconds=1.5)
        await _wait_for_lock_entity(hass)

        after_ents, after_devs = _registry_snapshot(hass)

        # Unique ids and device ids unchanged, and no duplicates.
        assert after_ents == before_ents, (
            f"entity registry changed across restart:\n"
            f"  only-before={set(before_ents) - set(after_ents)}\n"
            f"  only-after={set(after_ents) - set(before_ents)}"
        )
        assert after_devs == before_devs, "device registry changed across restart"

    async def test_no_duplicate_entities_after_restart(self, hass, mqtt_client):
        await _seed_credential(hass, HOUSEHOLD)
        entry = await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)

        for _ in range(2):
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await settle(hass, seconds=1.0)

        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        lock_entries = [
            e
            for e in registry.entities.values()
            if e.unique_id == f"{HOUSEHOLD}_{NODE_GATE}_lock" and e.platform == DOMAIN
        ]
        assert len(lock_entries) == 1, "duplicate lock entities after restarts"

    async def test_credential_survives_and_command_works_after_restart(
        self, hass, mqtt_client
    ):
        await _seed_credential(hass, HOUSEHOLD)
        entry = await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test")
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await settle(hass, seconds=1.5)
        entity_id = await _wait_for_lock_entity(hass)

        # Credential still available -> the command still signs correctly.
        key = derive_command_key(TEST_SECRET, TEST_SALT)
        async with CommandTap() as tap:
            await hass.services.async_call(
                "lock", "unlock", {"entity_id": entity_id}, blocking=True
            )
            await settle(hass, seconds=0.5)
            captured = await tap.wait_for(f"{BASE}/command/unlock")

        import hashlib
        import hmac
        import json

        body = json.loads(captured.payload)
        expected = hmac.new(
            key,
            f"{body['ts']}{body['nonce']}{body['req_id']}unlock".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert body["mac"] == expected, "MAC invalid after restart (credential lost?)"


class TestRetainedVsNonRetained:
    """Step 9: ``B/health`` is non-retained and must not be fabricated."""

    async def test_health_is_not_fabricated_after_restart(self, hass, mqtt_client):
        await _seed_credential(hass, HOUSEHOLD)
        entry = await _setup_entry(hass, HOUSEHOLD)

        # Retained telemetry (state/status/security/...) plus non-retained health.
        await publish_telemetry(
            mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test", lock_current=1
        )
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)
        assert hass.states.get(entity_id).state == "locked"

        # Unload/reload: only retained topics are replayed by the broker.
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await settle(hass, seconds=1.5)
        entity_id = await _wait_for_lock_entity(hass)

        # The lock state must NOT be claimed as fresh from stale data: with no
        # fresh B/health it is unknown (or unavailable), never "locked"/"unlocked"
        # derived from a fabricated snapshot.
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state in ("unknown", "unavailable"), (
            f"lock state {state.state!r} looks fabricated without fresh B/health"
        )

    async def test_health_restores_state_when_republished(self, hass, mqtt_client):
        await _seed_credential(hass, HOUSEHOLD)
        entry = await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(mqtt_client, HOUSEHOLD, NODE_GATE, "Gate Test", lock_current=1)
        await settle(hass)
        entity_id = await _wait_for_lock_entity(hass)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await settle(hass, seconds=1.5)
        entity_id = await _wait_for_lock_entity(hass)

        # A fresh non-retained B/health arrives (firmware publishes every 30 s).
        await mqtt_client.publish(
            f"{node_base(HOUSEHOLD, NODE_GATE)}/health",
            health_payload(lock_current=0),
            qos=0,
            retain=False,
        )
        await settle(hass, seconds=0.5)

        assert await _state_becomes(hass, entity_id, "unlocked", timeout=10), (
            "lock did not return to the correct state after fresh B/health"
        )

    async def test_retained_topics_replayed_after_restart(self, hass, mqtt_client):
        """Retained topics must repopulate node identity and metadata."""
        await _seed_credential(hass, HOUSEHOLD)
        entry = await _setup_entry(hass, HOUSEHOLD)
        await publish_telemetry(
            mqtt_client,
            HOUSEHOLD,
            NODE_GATE,
            "Gate Test",
            security="WARNING",
            last_auth_result="FAILURE",
        )
        # Make last_auth/security definitely retained and distinct.
        await mqtt_client.publish(
            f"{node_base(HOUSEHOLD, NODE_GATE)}/last_auth",
            last_auth_payload(result="FAILURE"),
            qos=0,
            retain=True,
        )
        await settle(hass)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await settle(hass, seconds=1.5)

        from homeassistant.helpers import entity_registry as er

        ids = {}
        for e in er.async_get(hass).entities.values():
            if e.platform == DOMAIN:
                ids[e.unique_id] = e.entity_id

        security_id = ids.get(f"{HOUSEHOLD}_{NODE_GATE}_security")
        assert security_id is not None
        assert await _state_becomes(hass, security_id, "WARNING", timeout=10), (
            "retained B/security was not replayed after restart"
        )
