"""Verify real Mosquitto semantics required by the firmware contract (step 5).

Runs against the REAL mosquitto broker (spawned from ``mosquitto.test.conf``),
over real TCP on loopback. Checks:

1. MQTT connection works
2. QoS 0 and QoS 1 publish/subscribe work
3. retained messages are delivered to a *late* subscriber
4. retained state survives subscriber reconnect
5. LWT (will) fires on an unclean disconnect
6. **retained delivery respects the subscription filter** — this is the check
   that amqtt failed (it replayed unrelated retained topics).

Check 6 is essential: every important firmware household topic is retained, so
without correct filtering no downstream result would be trustworthy.

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiomqtt

HOST = "127.0.0.1"
PORT = 18830
CONF = Path(__file__).with_name("mosquitto.test.conf")
TIMEOUT = 5.0

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")


def _mosquitto_binary() -> str | None:
    from shutil import which

    for candidate in (
        "mosquitto",
        "/opt/homebrew/opt/mosquitto/bin/mosquitto",
        "/opt/homebrew/sbin/mosquitto",
        "/usr/local/opt/mosquitto/bin/mosquitto",
        "/usr/sbin/mosquitto",
    ):
        if Path(candidate).is_absolute():
            if Path(candidate).exists():
                return candidate
        else:
            found = which(candidate)
            if found:
                return found
    return None


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with (
            contextlib.suppress(OSError),
            socket.create_connection((host, port), timeout=1),
        ):
            return True
        time.sleep(0.15)
    return False


async def _read_all(sub: aiomqtt.Client, timeout: float = 1.2) -> list[tuple[str, str]]:
    """Collect every message that arrives within ``timeout`` seconds."""
    out: list[tuple[str, str]] = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                msg = await sub.messages.__anext__()
                out.append((msg.topic.value, bytes(msg.payload).decode()))
    except (TimeoutError, StopAsyncIteration):
        pass
    return out


def _unique(prefix: str) -> str:
    return f"validate/{prefix}/{uuid.uuid4().hex[:10]}"


async def check_connection_and_qos() -> None:
    try:
        async with aiomqtt.Client(HOST, PORT):
            pass
        record("connection over TCP", True, f"{HOST}:{PORT}")
    except Exception as exc:  # noqa: BLE001
        record("connection over TCP", False, repr(exc))
        return

    topic = _unique("qos")
    async with aiomqtt.Client(HOST, PORT) as sub:
        await sub.subscribe(topic, qos=1)
        async with aiomqtt.Client(HOST, PORT) as pub:
            await pub.publish(topic, "q0", qos=0)
            await pub.publish(topic, "q1", qos=1)
        got = {payload for _t, payload in await _read_all(sub)}
    record("QoS 0 + QoS 1 publish/subscribe", {"q0", "q1"} <= got, f"got={sorted(got)}")


async def check_retained_late_subscriber() -> None:
    topic = _unique("retained")
    async with aiomqtt.Client(HOST, PORT) as pub:
        await pub.publish(topic, "retained-value", qos=1, retain=True)
    # Subscriber connects AFTER the publish: only true retained delivery can work.
    async with aiomqtt.Client(HOST, PORT) as sub:
        await sub.subscribe(topic, qos=1)
        msgs = await _read_all(sub, 2.0)
    record(
        "retained delivered to late subscriber",
        msgs == [(topic, "retained-value")],
        f"got={msgs}",
    )


async def check_retained_survives_reconnect() -> None:
    topic = _unique("persist")
    async with aiomqtt.Client(HOST, PORT) as pub:
        await pub.publish(topic, "persisted", qos=1, retain=True)

    async with aiomqtt.Client(HOST, PORT) as sub1:
        await sub1.subscribe(topic, qos=1)
        first = await _read_all(sub1, 2.0)

    async with aiomqtt.Client(HOST, PORT) as sub2:
        await sub2.subscribe(topic, qos=1)
        second = await _read_all(sub2, 2.0)

    record(
        "retained survives subscriber reconnect",
        first == [(topic, "persisted")] and second == [(topic, "persisted")],
        f"first={first} second={second}",
    )


async def check_retained_filter_isolation() -> None:
    """Retained delivery must respect the subscription filter (amqtt failed this)."""
    t1 = _unique("iso-a")
    t2 = _unique("iso-b")
    t3 = _unique("iso-c")

    async with aiomqtt.Client(HOST, PORT) as pub:
        await pub.publish(t1, "PAYLOAD-A", qos=1, retain=True)
        await pub.publish(t2, "PAYLOAD-B", qos=1, retain=True)
        # t3 has NO retained message at all.

    # Subscribe to t2 only -> must receive exactly PAYLOAD-B, never PAYLOAD-A.
    async with aiomqtt.Client(HOST, PORT) as sub_b:
        await sub_b.subscribe(t2, qos=1)
        got_b = await _read_all(sub_b, 2.0)

    # Subscribe to t1 only -> must receive exactly PAYLOAD-A.
    async with aiomqtt.Client(HOST, PORT) as sub_a:
        await sub_a.subscribe(t1, qos=1)
        got_a = await _read_all(sub_a, 2.0)

    # Subscribe to an empty topic -> must receive NOTHING.
    async with aiomqtt.Client(HOST, PORT) as sub_c:
        await sub_c.subscribe(t3, qos=1)
        got_c = await _read_all(sub_c, 2.0)

    ok = (
        got_b == [(t2, "PAYLOAD-B")]
        and got_a == [(t1, "PAYLOAD-A")]
        and got_c == []
    )
    record(
        "retained delivery respects subscription filter",
        ok,
        f"t2->{got_b} t1->{got_a} empty->{got_c}",
    )


async def check_retained_wildcard_matches() -> None:
    """A ``+`` wildcard must match retained topics on that level only."""
    tag = uuid.uuid4().hex[:10]
    a = f"validate/wild/{tag}/GATE-001/state"
    b = f"validate/wild/{tag}/HOUSE-001/state"
    async with aiomqtt.Client(HOST, PORT) as pub:
        await pub.publish(a, "A", qos=1, retain=True)
        await pub.publish(b, "B", qos=1, retain=True)

    # The integration subscribes to the household tree; verify wildcard retained.
    async with aiomqtt.Client(HOST, PORT) as sub:
        await sub.subscribe(f"validate/wild/{tag}/+/state", qos=1)
        got = await _read_all(sub, 2.0)

    record(
        "retained respects '+' wildcard",
        sorted(got) == sorted([(a, "A"), (b, "B")]),
        f"got={got}",
    )


def _drop_socket_abruptly(client: aiomqtt.Client) -> None:
    """Close the TCP socket with no MQTT DISCONNECT so the will must fire."""
    sock = client._client._sock  # noqa: SLF001 - deliberate low-level drop
    with contextlib.suppress(OSError):
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
        )
    sock.close()


async def check_lwt() -> None:
    topic = _unique("lwt")
    will_payload = None
    try:
        async with aiomqtt.Client(HOST, PORT) as watcher:
            await watcher.subscribe(topic, qos=1)
            # Allow the subscription to be established before the will fires.
            await asyncio.sleep(0.3)

            victim = aiomqtt.Client(
                HOST,
                PORT,
                will=aiomqtt.Will(topic=topic, payload="offline", qos=1, retain=True),
            )
            await victim.__aenter__()
            try:
                await victim.publish(topic, "online", qos=1, retain=False)
                _drop_socket_abruptly(victim)
                msgs = await _read_all(watcher, 5.0)
                payloads = [p for _t, p in msgs]
                will_payload = "offline" if "offline" in payloads else payloads
            finally:
                with contextlib.suppress(Exception):
                    await victim.__aexit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        record("LWT fires on unclean disconnect", False, repr(exc))
        return

    record(
        "LWT fires on unclean disconnect",
        will_payload == "offline",
        f"will={will_payload!r}",
    )


async def check_retained_cleared_by_empty_payload() -> None:
    """An empty retained payload must delete the retained message."""
    topic = _unique("clear")
    async with aiomqtt.Client(HOST, PORT) as pub:
        await pub.publish(topic, "temp", qos=1, retain=True)
    async with aiomqtt.Client(HOST, PORT) as pub2:
        await pub2.publish(topic, "", qos=1, retain=True)
    async with aiomqtt.Client(HOST, PORT) as sub:
        await sub.subscribe(topic, qos=1)
        got = await _read_all(sub, 1.5)
    record("empty retained payload clears the topic", got == [], f"got={got}")


async def main() -> int:
    binary = _mosquitto_binary()
    if binary is None:
        print("FAIL: mosquitto not installed")
        return 2

    proc = subprocess.Popen(  # noqa: S603 - fixed binary and conf
        [binary, "-c", str(CONF)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not _wait_for_port(HOST, PORT):
        proc.terminate()
        print("FAIL: mosquitto did not start")
        return 2
    print(f"mosquitto {binary} listening on {HOST}:{PORT} (loopback only)\n")

    try:
        await check_connection_and_qos()
        await check_retained_late_subscriber()
        await check_retained_survives_reconnect()
        await check_retained_filter_isolation()
        await check_retained_wildcard_matches()
        await check_retained_cleared_by_empty_payload()
        await check_lwt()
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} broker checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
