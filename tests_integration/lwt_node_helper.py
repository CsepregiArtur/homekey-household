"""Disposable MQTT "node" process for LWT (Last Will and Testament) testing.

This is TEST-ONLY infrastructure. It is deliberately a *separate process* so an
unclean disconnect can be simulated without ever touching the sockets or the
event loop of the test that spawns it.

Why a process (and not an in-test client):
    A Last Will and Testament is published by a **live broker** when the broker
    detects that a client vanished *without* sending an MQTT ``DISCONNECT``.

    Simulating that reliably is impossible from inside the test process:
      * a graceful ``disconnect()`` suppresses the will;
      * force-closing an ``aiomqtt``/``paho`` socket corrupts that client's
        state, because paho still owns the socket and will later call
        ``loop.add_writer(fd, ...)`` with ``fd == -1`` -> ``ValueError`` during
        teardown.

    Instead this helper connects, registers the will, publishes ``online`` and
    then parks forever. The *test* kills the process with ``SIGKILL`` (the MQTT
    equivalent of a cable pull): no ``DISCONNECT`` is sent, so the broker
    publishes the configured will.

Protocol (stdout, one JSON object per line):
    {"event": "connected"}    -- after CONNACK and Will registration
    {"event": "online_sent"}  -- after the retained ``online`` status publish

The process never exits on its own; it is meant to be killed.
"""

from __future__ import annotations

import json
import sys
import time

import paho.mqtt.client as mqtt  # type: ignore[import-untyped]


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: lwt_node_helper.py <host> <port> <client_id> <status_topic>",
            file=sys.stderr,
        )
        return 2

    host = sys.argv[1]
    port = int(sys.argv[2])
    client_id = sys.argv[3]
    status_topic = sys.argv[4]

    def _say(event: str) -> None:
        print(json.dumps({"event": event}), flush=True)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
    )

    # Register the will: if this process dies without a DISCONNECT, the broker
    # publishes ``offline`` (retained) on the node status topic.
    client.will_set(status_topic, payload="offline", qos=1, retain=True)

    connected = {"done": False}

    def _on_connect(
        _client: mqtt.Client,
        _userdata: object,
        _flags: object,
        reason_code: object,
        _properties: object = None,
    ) -> None:
        connected["done"] = True

    client.on_connect = _on_connect

    client.connect(host, port, keepalive=30)
    client.loop_start()

    # Wait for CONNACK (will now registered broker-side).
    deadline = time.monotonic() + 10
    while not connected["done"] and time.monotonic() < deadline:
        time.sleep(0.05)
    if not connected["done"]:
        print(json.dumps({"event": "error", "detail": "no CONNACK"}), flush=True)
        return 1
    _say("connected")

    # Publish the retained online status, then park forever. The test will
    # SIGKILL this process to trigger the will.
    info = client.publish(status_topic, payload="online", qos=1, retain=True)
    info.wait_for_publish(timeout=10)
    _say("online_sent")

    while True:
        time.sleep(3600)

    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
