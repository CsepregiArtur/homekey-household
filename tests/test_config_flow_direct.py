"""The direct transport explains its own failures, not MQTT's.

Written after a node that could not be reached was reported as "Home Assistant could
not reach the MQTT broker" - a transport this path does not use, and a broker that was
not part of the problem. The two transports shared error keys, so the first test here
is the one that would have caught it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.homekey_household import config_flow
from custom_components.homekey_household.const import (
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_NODE_ID,
    CONF_NODE_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from custom_components.homekey_household.direct import (
    DirectAuthError,
    DirectFingerprintMismatch,
    DirectNoHouseholdError,
    DirectProtocolError,
    DirectTlsUnavailableError,
    DirectTransportError,
)

STRINGS = json.loads(
    (Path(config_flow.__file__).parent / "strings.json").read_text(encoding="utf-8")
)

DISCOVERY: dict[str, Any] = {
    CONF_HOST: "192.168.1.141",
    CONF_PORT: 443,
    CONF_FINGERPRINT: (
        "1D:72:77:46:E3:BC:94:33:03:4D:8F:76:B0:61:86:B9:01:6F:"
        "3E:1B:3B:CD:46:24:84:7F:33:37:7C:10:FF:16"
    ),
    CONF_NODE_ID: "NODE-001",
    CONF_NODE_NAME: "HK",
}


class _Address:
    """Stand-in for the address objects zeroconf hands over."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _Discovery:
    """Stand-in for a ZeroconfServiceInfo, with the fields the flow reads."""

    def __init__(self, *, host: str, addresses: list[str]) -> None:
        self.host = host
        self.hostname = f"{host}."
        self.port = 443
        self.ip_addresses = [_Address(address) for address in addresses]


def _flow(hass: Any) -> config_flow.HomeKeyHouseholdConfigFlow:
    flow = config_flow.HomeKeyHouseholdConfigFlow()
    flow.hass = hass
    flow._discovery = dict(DISCOVERY)
    return flow


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DirectTransportError("no route to host"), "node_unreachable"),
        (DirectAuthError("wrong password"), "node_auth_failed"),
        (DirectTlsUnavailableError("plain http"), "tls_required"),
        (DirectNoHouseholdError("no household"), "no_household"),
        (DirectProtocolError("protocol 2"), "unsupported_protocol"),
    ],
    ids=["unreachable", "auth", "tls", "household", "protocol"],
)
async def test_each_failure_gets_its_own_message(
    hass: Any, error: Exception, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow(hass)

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(flow, "_async_probe_direct", fail)

    result = await flow.async_step_direct(
        {CONF_USERNAME: "admin", CONF_PASSWORD: "password"}
    )

    assert result["errors"]["base"] == expected
    assert result["step_id"] == "direct"


async def test_empty_credentials_do_not_blame_the_node(hass: Any) -> None:
    """An empty field means the form is incomplete; the node was never asked."""
    result = await _flow(hass).async_step_direct(
        {CONF_USERNAME: "", CONF_PASSWORD: ""}
    )

    assert result["errors"]["base"] == "node_auth_failed"


async def test_a_mismatch_shows_what_the_node_presented(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow(hass)

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise DirectFingerprintMismatch(expected="AA:BB", actual="CC:DD")

    monkeypatch.setattr(flow, "_async_probe_direct", fail)

    result = await flow.async_step_direct(
        {CONF_USERNAME: "admin", CONF_PASSWORD: "password"}
    )

    assert result["errors"]["base"] == "fingerprint_mismatch"
    assert result["description_placeholders"]["presented"] == "CC:DD"


def test_no_direct_error_is_described_in_mqtt_terms() -> None:
    """The original bug: a direct failure rendered the MQTT broker explanation."""
    for key in config_flow._DIRECT_ERROR_KEYS.values():
        text = STRINGS["config"]["error"][key]
        lowered = text.lower()
        for word in ("broker", "mqtt", "mosquitto"):
            assert word not in lowered, f"{key} is explained in MQTT terms: {text}"


def test_every_direct_error_key_has_a_message() -> None:
    for key in config_flow._DIRECT_ERROR_KEYS.values():
        assert key in STRINGS["config"]["error"], key


def test_a_numeric_address_is_preferred_over_a_hostname() -> None:
    """A container usually cannot resolve .local, and the address needs no resolver."""
    named = _Discovery(host="HK-9E492F46.local", addresses=["192.168.1.141"])
    assert config_flow._preferred_host(named) == "192.168.1.141"

    both_versions = _Discovery(host="fe80::1", addresses=["fe80::1", "192.168.1.141"])
    assert config_flow._preferred_host(both_versions) == "192.168.1.141"

    unresolved = _Discovery(host="HK-9E492F46.local", addresses=[])
    assert config_flow._preferred_host(unresolved) == "HK-9E492F46.local"
