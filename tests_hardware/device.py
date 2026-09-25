"""Hardware access layer for the HomeKey validation suite.

Provides a serial console reader and an MQTT observer. Both are strictly
read-mostly: the console is only read (plus an optional normal reset), and MQTT
is used to subscribe to the firmware's own topics and to publish contract-legal
command payloads during the positive command tests.

Credentials are never hardcoded. They are read from the environment so no secret
can land in the repository or in evidence files:

    HK_MQTT_HOST      broker host (default 127.0.0.1)
    HK_MQTT_PORT      broker port (default 1883)
    HK_MQTT_USERNAME  broker username (required to observe topics)
    HK_MQTT_PASSWORD  broker password
    HK_SERIAL_PORT    serial device (default: auto-detect)
    HK_FIRMWARE_VERSION  expected version (default 0.10.0)
"""

from __future__ import annotations

import contextlib
import glob
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from .helpers import HardwareBlocked, redact_text

# The device's USB-serial bridge on macOS is a CP2102 on this setup; the glob
# covers the usual Espressif/CP210x/CH34x names so the suite is portable.
_SERIAL_GLOBS = (
    "/dev/cu.usbserial-*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.wchusbserial*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
)

BAUD = 115200
DEFAULT_FIRMWARE_VERSION = "0.10.0"


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def expected_firmware_version() -> str:
    return env("HK_FIRMWARE_VERSION", DEFAULT_FIRMWARE_VERSION) or (
        DEFAULT_FIRMWARE_VERSION
    )


def broker_host() -> str:
    return env("HK_MQTT_HOST", "127.0.0.1") or "127.0.0.1"


def broker_port() -> int:
    return int(env("HK_MQTT_PORT", "1883") or "1883")


def mqtt_credentials() -> tuple[str, str] | None:
    """Return (username, password) or None when not configured.

    The broker on this system requires authentication, so observation is
    impossible without credentials; callers must treat None as BLOCKED.
    """
    username = env("HK_MQTT_USERNAME")
    if not username:
        return None
    return username, env("HK_MQTT_PASSWORD", "") or ""


# --------------------------------------------------------------------------
# Serial console
# --------------------------------------------------------------------------


def find_serial_port() -> str | None:
    """Locate the ESP32 USB-serial device, or None when nothing is attached."""
    override = env("HK_SERIAL_PORT")
    if override:
        return override
    for pattern in _SERIAL_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


@dataclass
class SerialCapture:
    """A block of serial console output plus the facts extracted from it."""

    text: str
    seconds: float

    def find(self, patterns: dict[str, str]) -> dict[str, list[str]]:
        """Return unique regex matches per named pattern."""
        found: dict[str, list[str]] = {}
        for label, pattern in patterns.items():
            hits = sorted(set(re.findall(pattern, self.text, re.IGNORECASE)))
            if hits:
                found[label] = hits
        return found

    def has_error(self, pattern: str) -> bool:
        return bool(re.search(pattern, self.text, re.IGNORECASE))

    @property
    def redacted(self) -> str:
        return redact_text(self.text)


class SerialConsole:
    """Reads the ESP32 console. Opens/closes per capture so nothing is held."""

    def __init__(self, port: str | None = None, baud: int = BAUD) -> None:
        self.port = port or find_serial_port()
        self.baud = baud

    def require(self) -> str:
        if not self.port:
            raise HardwareBlocked(
                "No ESP32 serial device found. Attach the board (expected "
                "/dev/cu.usbserial-*) or set HK_SERIAL_PORT."
            )
        return self.port

    def capture(self, seconds: float = 15.0, reset: bool = True) -> SerialCapture:
        """Capture console output, optionally after a normal boot reset."""
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise HardwareBlocked(
                "pyserial is not installed in this environment."
            ) from exc

        port = self.require()
        try:
            ser = serial.Serial(port, self.baud, timeout=0.25)
        except Exception as exc:
            raise HardwareBlocked(f"Cannot open serial port {port}: {exc}") from exc

        with ser:
            if reset:
                # Normal boot: GPIO0 high (DTR False), pulse EN low via RTS.
                ser.dtr = False
                ser.rts = True
                time.sleep(0.15)
                ser.rts = False
                ser.reset_input_buffer()

            deadline = time.time() + seconds
            chunks: list[bytes] = []
            while time.time() < deadline:
                data = ser.read(4096)
                if data:
                    chunks.append(data)

        return SerialCapture(
            b"".join(chunks).decode("utf-8", errors="replace"), seconds
        )


# --------------------------------------------------------------------------
# MQTT observation
# --------------------------------------------------------------------------

# Topics the contract defines (firmware 0.10.0).
TOPIC_SUFFIXES = ("state", "status", "health", "security")


def household_base(household_id: str, node_id: str) -> str:
    return f"homekey/household/{household_id}/nodes/{node_id}"


def observed_topic_filter(household_id: str) -> str:
    """Wildcard capture of every household node topic."""
    return f"homekey/household/{household_id}/nodes/+/#"


class MqttObserver:
    """Subscribes to the household topics and records retained/live messages."""

    def __init__(
        self,
        household_id: str,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.household_id = household_id
        self.host = host or broker_host()
        self.port = port or broker_port()
        self.messages: list[dict[str, Any]] = []

    def require(self) -> tuple[str, str]:
        creds = mqtt_credentials()
        if not creds:
            raise HardwareBlocked(
                "Broker credentials not provided. The broker requires "
                "authentication, so topics cannot be observed. Set "
                "HK_MQTT_USERNAME/HK_MQTT_PASSWORD."
            )
        return creds

    def collect(self, seconds: float = 10.0) -> list[dict[str, Any]]:
        """Collect messages on the household topic tree for ``seconds``."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise HardwareBlocked("paho-mqtt is not installed.") from exc

        username, password = self.require()
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="hk-hw-validator"
        )
        client.username_pw_set(username, password)

        def on_message(_c: Any, _u: Any, msg: Any) -> None:
            # Retained flag matters: state/status/security are retained, health
            # is deliberately NOT retained.
            self.messages.append(
                {
                    "topic": msg.topic,
                    "payload": msg.payload.decode("utf-8", errors="replace"),
                    "retain": bool(msg.retain),
                    "qos": msg.qos,
                }
            )

        client.on_message = on_message
        try:
            client.connect(self.host, self.port, keepalive=15)
        except Exception as exc:
            raise HardwareBlocked(
                f"Cannot reach broker at {self.host}:{self.port}: {exc}"
            ) from exc

        client.loop_start()
        client.subscribe(observed_topic_filter(self.household_id), qos=1)
        time.sleep(seconds)
        client.loop_stop()
        with contextlib.suppress(Exception):
            client.disconnect()
        return self.messages

    def publish(self, topic: str, payload: str, qos: int = 1) -> None:
        """Publish a command payload. Retained is always False per contract."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise HardwareBlocked("paho-mqtt is not installed.") from exc

        username, password = self.require()
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="hk-hw-commander"
        )
        client.username_pw_set(username, password)
        try:
            client.connect(self.host, self.port, keepalive=15)
        except Exception as exc:
            raise HardwareBlocked(
                f"Cannot reach broker at {self.host}:{self.port}: {exc}"
            ) from exc
        client.loop_start()
        client.publish(topic, payload, qos=qos, retain=False)
        time.sleep(0.5)
        client.loop_stop()
        with contextlib.suppress(Exception):
            client.disconnect()


# --------------------------------------------------------------------------
# Contract literals used for assertions
# --------------------------------------------------------------------------

# Command topics are NOT retained and are QoS 1.
COMMAND_TOPICS = ("command/lock", "command/unlock")

# Topics that must be retained, and the one that must not be.
RETAINED_SUFFIXES = {"state", "status", "security"}
NON_RETAINED_SUFFIXES = {"health"}


def topic_suffix(topic: str) -> str:
    return topic.rsplit("/", 1)[-1]
