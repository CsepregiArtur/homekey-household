"""Step 9 - secret-exposure scan of the real firmware serial logs.

This is the hardware counterpart to the integration's step-19 scan. It looks for
ACTUAL secret values that could plausibly appear on the console:

* a 64-hex command MAC or derived key
* the recovery secret or salt, if supplied for cross-checking
* PEM private-key material

Every match is classified by source. The expected product result is
``NO SECRET MATERIAL EXPOSED BY FIRMWARE``.
"""

from __future__ import annotations

import os
import re

import pytest

from tests_hardware.device import SerialConsole
from tests_hardware.helpers import Evidence, find_secret_leaks

pytestmark = pytest.mark.security

_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
_PEM_PRIVATE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_BASE64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")


def _scan(capture_text: str) -> dict[str, list[str]]:
    return {
        "64-hex (key/MAC)": _HEX64.findall(capture_text),
        "PEM private key": _PEM_PRIVATE.findall(capture_text),
        "long base64 blob": _BASE64_BLOB.findall(capture_text),
    }


def test_no_secret_material_on_console(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Boot + run the device and scan the console for secret material."""
    capture = console.capture(seconds=25.0, reset=True)
    evidence.serial(capture.text)

    findings = _scan(capture.text)

    # Report exactly what was found, redacted, so a failure is actionable.
    for kind, hits in findings.items():
        if hits:
            evidence.record("finding", {"kind": kind, "count": len(hits)})

    leaks = findings["64-hex (key/MAC)"]
    assert not leaks, (
        f"{len(leaks)} 64-character hex value(s) appeared on the console. These "
        "are indistinguishable from command keys or MACs and must never be "
        "logged. Values were redacted before storage."
    )

    assert not findings["PEM private key"], (
        "PEM private-key material was written to the console log"
    )


def test_supplied_secrets_never_appear_in_logs(
    console: SerialConsole, evidence: Evidence
) -> None:
    """Cross-check: the actual configured secret must not be echoed.

    Only runs when the secrets are supplied for verification. The values are
    never stored — only the count of occurrences is recorded.
    """
    secret = os.environ.get("HK_RECOVERY_SECRET")
    salt = os.environ.get("HK_SALT")

    if not secret and not salt:
        pytest.skip(
            "BLOCKED: neither HK_RECOVERY_SECRET nor HK_SALT supplied, so the "
            "cross-check against real secret values cannot be performed"
        )

    capture = console.capture(seconds=25.0, reset=True)
    # Do NOT pass the raw text through evidence here: it is the same capture as
    # the test above, and the point is to keep secrets out of evidence entirely.
    evidence.record("scan", {"seconds": capture.seconds})

    offenders = []
    if secret and secret in capture.text:
        offenders.append("recovery secret")
    if salt and salt in capture.text:
        offenders.append("salt")

    assert not offenders, (
        f"the configured {', '.join(offenders)} was written verbatim to the "
        "console. The recovery secret must never be logged or transmitted."
    )


def test_no_secret_leak_helper_finds_nothing_in_boot(console: SerialConsole) -> None:
    """The shared leak detector must report nothing on a clean boot."""
    capture = console.capture(seconds=12.0, reset=False)
    assert find_secret_leaks(capture.text) == [], (
        "the shared 64-hex leak detector found candidate secrets on the console"
    )
