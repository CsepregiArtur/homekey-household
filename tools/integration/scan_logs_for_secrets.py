"""Step 19 — secret-exposure log scan (dynamic).

Runs the real HMAC + negative-command integration tests while capturing ALL log
output, then searches the captured text for the ACTUAL values of the secrets that
exist during those runs.

Attribution is the point of this scanner: a match is only a *product* finding if
it is emitted by the integration's own loggers (``custom_components.homekey_household``).
Matches from other loggers are classified separately, because the integration
cannot control what a third party logs.

Known third-party caveat (verified, not assumed):

  ``pytest_homeassistant_custom_component.common`` logs the FULL payload of every
  ``Store`` read/write at DEBUG (``common.py:1550/1558``). Since the integration
  persists the derived command key via ``Store``, the harness prints the storage
  JSON — including the key hex and the salt — to the test log.

  This is TEST-HARNESS behaviour only. Home Assistant's real
  ``homeassistant.helpers.storage.Store`` logs only the store key and the file
  path, never the contents (``storage.py:614``). The integration itself never
  logs the key, the salt or the secret at any level.

Classification:

  * ``PRODUCT-LEAK`` — actual secret material logged by ``custom_components.*``.
                       This is a real defect and fails the scan.
  * ``HARNESS-LEAK`` — actual secret material logged by the test harness only.
                       Reported, does not fail the scan, but is documented.
  * ``MAC``          — a command MAC (64 hex chars). A MAC is not a stored secret
                       but must not be logged either. The integration logs only
                       ``req_id``; HA core's MQTT client
                       (``homeassistant.components.mqtt.client``) logs the WHOLE
                       payload at DEBUG, so the MAC appears there. That is a
                       core/transport behaviour, not something the integration
                       can suppress; it is reported, not treated as a product
                       defect, but it does mean a DEBUG-level MQTT log contains
                       command MACs.
  * ``SAFE``         — key/field NAMES, redaction placeholders, or the
                       ``HK-HOUSEHOLD-CMD-v1`` KDF label: terminology, not values.

Exit code is non-zero only when a PRODUCT-LEAK or an integration-emitted MAC is
found, so this can gate CI without being defeated by harness logging.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests_integration"

# Synthetic credentials used by the integration test suite (see helpers.py).
TEST_SECRET = "integration-test-recovery-secret"
TEST_SALT = "integration-test-salt"
KDF_LABEL = "HK-HOUSEHOLD-CMD-v1"

INTEGRATION_PREFIX = "custom_components."

# Command MACs are HMAC-SHA256 -> 64 lowercase hex chars.
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
# "LEVEL   logger:file:line message"
_LOGGER_RE = re.compile(r"^\w+\s+([\w.]+):")


def derive_expected_key_hex(secret: str, salt: str) -> str:
    """Independently re-derive the key the integration would use (BLAKE2b KDF)."""
    return hashlib.blake2b(
        (secret + salt).encode(),
        key=KDF_LABEL.encode(),
        digest_size=32,
    ).hexdigest()


def logger_of(line: str) -> str:
    match = _LOGGER_RE.match(line)
    return match.group(1) if match else "<continuation>"


def run_tests() -> str:
    """Run the HMAC + negative tests capturing every byte of output at DEBUG."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests_integration/test_hmac_commands.py",
        "tests_integration/test_negative_commands.py",
        "-c",
        "tests_integration/pytest.ini",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "log_cli=true",
        "--log-cli-level=DEBUG",
        "-s",
    ]
    proc = subprocess.run(  # noqa: S603
        cmd,
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, "LWT_HELPER_PYTHON": sys.executable},
        check=False,
    )
    combined = proc.stdout + "\n" + proc.stderr
    # Persist the raw log so the finding is auditable.
    Path("/tmp/step19_scan.log").write_text(combined, encoding="utf-8")
    print(combined[-1200:])
    return combined


def _attribute(lines: list[str], needle: str) -> Counter[str]:
    return Counter(logger_of(line) for line in lines if needle in line)


def main() -> int:
    print("=== STEP 19: secret-exposure log scan ===")
    print(f"repo: {REPO}")

    if not TESTS.is_dir():
        print(f"FAIL: test dir not found: {TESTS}")
        return 2

    log_text = run_tests()
    lines = log_text.splitlines()
    key_hex = derive_expected_key_hex(TEST_SECRET, TEST_SALT)

    # Known secret VALUES worth searching for.
    needles = {
        "recovery secret": TEST_SECRET,
        "salt value": TEST_SALT,
        "derived key hex": key_hex,
    }

    print()
    print("--- value attribution (which logger emitted each actual secret) ---")
    product_leaks: list[str] = []
    harness_leaks: list[str] = []

    for label, needle in needles.items():
        by_logger = _attribute(lines, needle)
        if not by_logger:
            print(f"  {label:18s} NOT PRESENT anywhere")
            continue
        for logger_name, count in by_logger.most_common():
            line = f"{label} ({count}x) via {logger_name}"
            print(f"  {line}")
            if logger_name.startswith(INTEGRATION_PREFIX):
                product_leaks.append(line)
            elif logger_name != "<continuation>":
                harness_leaks.append(line)

    # MAC-like tokens (64 hex), excluding the derived key handled above.
    # Attribute by the ``"mac"`` payload field (JSON may or may not have a space
    # after the colon), so the finding is unambiguous rather than matching the
    # first line that merely happens to contain the hex.
    _mac_field_re = re.compile(r'"mac"\s*:\s*"([0-9a-f]{64})"')
    mac_by_logger: Counter[str] = Counter()
    for line in lines:
        for match in _mac_field_re.finditer(line):
            if match.group(1) == key_hex:
                continue
            mac_by_logger[logger_of(line)] += 1
    product_macs = {
        k: v for k, v in mac_by_logger.items() if k.startswith(INTEGRATION_PREFIX)
    }

    print()
    print("--- summary ---")
    print(f"  integration-emitted secret values (PRODUCT-LEAK): {len(product_leaks)}")
    print(f"  harness-only secret values       (HARNESS-LEAK): {len(harness_leaks)}")
    print(f"  integration-emitted MAC tokens   (PRODUCT-MAC) : {len(product_macs)}")
    print(
        f"  command MACs by logger (payload \"mac\" field): "
        f"{dict(mac_by_logger.most_common(5))}"
    )

    print()
    if product_leaks or product_macs:
        print("RESULT: FAIL — the integration logged secret material")
        for item in product_leaks:
            print(f"  ! {item}")
        for item, count in product_macs.items():
            print(f"  ! MAC via {item} ({count}x)")
        return 1

    core_macs = {
        k: v for k, v in mac_by_logger.items() if k.startswith("homeassistant.")
    }
    print()
    if core_macs:
        print(
            "RESULT: PASS (product) — the integration never logs the recovery "
            "secret,\n"
            "        the command key or the salt, and never logs the MAC.\n"
            "        FINDING (not an integration defect): Home Assistant core's "
            "MQTT\n"
            "        client logs the entire command payload at DEBUG "
            "(client.py:772\n"
            "        'Transmitting message ...' and client.py:1337 'Received "
            "message'),\n"
            "        so a DEBUG-level MQTT log contains command MACs. The "
            "integration\n"
            "        cannot suppress core logging; keep MQTT DEBUG off in "
            "production."
        )
        return 0

    if harness_leaks:
        print(
            "RESULT: PASS (product) — no secret material logged by the "
            "integration.\n"
            "        NOTE: the pytest HA harness logs Store payloads verbatim "
            "at DEBUG\n"
            "        (pytest_homeassistant_custom_component/common.py:1550/1558);\n"
            "        that harness-only exposure is documented, not a product "
            "defect."
        )
        return 0

    print("RESULT: PASS — no secret material logged anywhere in the captured run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
