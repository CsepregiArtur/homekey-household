"""Config flow for HomeKey Household.

The flow deliberately reuses the Home Assistant core **MQTT integration** rather
than asking for broker details a second time. When MQTT is not configured the
user is offered two explicit paths:

``mqtt_guide``
    A short, read-only walkthrough explaining how to install/configure the
    official MQTT integration, with a retry button that re-checks detection.

``mqtt_express``
    A single broker form (host, port, optional username/password) that creates
    the official MQTT **config entry** directly, then continues automatically.

Neither path changes the MQTT contract, the topics, or the HMAC behaviour — the
integration remains a pure consumer of the firmware's household MQTT API.

Because the firmware's command key is derived from the household *recovery
secret*, the flow asks for that secret once, derives the 32-byte command key
locally, and discards the secret (see :mod:`credential`). The raw secret is never
persisted in the config entry, never logged, and never sent over MQTT.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_COMMAND_CONTROL,
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_HOUSEHOLD_ID,
    CONF_HOUSEHOLD_NAME,
    CONF_LEGACY_CLIENT_ID_PREFIX,
    CONF_MODEL,
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PROTOCOL,
    CONF_MQTT_USERNAME,
    CONF_NODE_ID,
    CONF_NODE_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RECOVERY_SECRET,
    CONF_SALT,
    CONF_TRANSPORT,
    CONF_USERNAME,
    DEFAULT_COMMAND_CONTROL,
    DEFAULT_HTTPS_PORT,
    DEFAULT_LEGACY_CLIENT_ID_PREFIX,
    DEFAULT_MQTT_BROKER,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_PROTOCOL,
    DEFAULT_WEB_USERNAME,
    DOMAIN,
    HA_API_PROTOCOL,
    MQTT_DOMAIN,
    TRANSPORT_DIRECT,
    ZEROCONF_KEY_FINGERPRINT,
    ZEROCONF_KEY_ID,
    ZEROCONF_KEY_MODEL,
    ZEROCONF_KEY_NAME,
    ZEROCONF_KEY_PROTOCOL,
    ZEROCONF_KEY_TLS,
    ZEROCONF_KEY_VERSION,
)
from .credential import CommandKeyStore
from .direct import (
    DirectAuthError,
    DirectFingerprintMismatch,
    DirectNoHouseholdError,
    DirectProbe,
    DirectProtocolError,
    DirectTlsUnavailableError,
    DirectTransportError,
    async_connect_node,
    normalise_fingerprint,
)
from .models import ValidationError, validate_id

_LOGGER = logging.getLogger(__name__)

DOCS_URL = "https://github.com/CsepregiArtur/homekey-household"

# Which form error each direct-transport failure maps to. Keyed by the exception's
# exact type, so the most specific reason wins; anything else is a connection
# problem. DirectFingerprintMismatch and friends are subclasses of
# DirectTransportError, which is why the lookup is by exact type rather than by
# isinstance order.
_DIRECT_ERROR_KEYS: dict[type[BaseException], str] = {
    DirectFingerprintMismatch: "fingerprint_mismatch",
    DirectAuthError: "node_auth_failed",
    DirectTlsUnavailableError: "tls_required",
    DirectNoHouseholdError: "no_household",
    DirectProtocolError: "unsupported_protocol",
    DirectTransportError: "node_unreachable",
}


def _preferred_host(discovery_info: Any) -> str | None:
    """Return the address to reach the node on, preferring a literal one.

    Zeroconf hands over both a resolved address and the service's own name, and the
    name is not usable everywhere: a ``.local`` hostname only resolves if the process
    has mDNS resolution, which a Home Assistant container generally does not. A node
    is equally reachable by address, so the address is always the better choice, and
    this order is the difference between a setup that works and one that reports a
    connection failure against a node sitting on the network in plain sight.
    """
    for candidate in getattr(discovery_info, "ip_addresses", None) or []:
        text = str(candidate)
        if text and ":" not in text:  # IPv4 needs no resolver at all
            return text
    return getattr(discovery_info, "host", None) or getattr(
        discovery_info, "hostname", None
    )


def _txt_properties(discovery_info: Any) -> dict[str, str]:
    """Normalise an mDNS TXT record to plain strings.

    Zeroconf returns the values as bytes, and it omits keys whose value is empty.
    Treating a missing key and an empty one identically keeps the checks below
    readable.
    """
    raw = getattr(discovery_info, "properties", None) or {}
    properties: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        properties[str(key)] = "" if value is None else str(value)
    return properties


async def async_validate_mqtt(hass: HomeAssistant) -> bool:
    """Return True when the HA MQTT integration is configured and available."""
    from homeassistant.components import mqtt

    return bool(await mqtt.async_wait_for_mqtt_client(hass))


async def _async_mqtt_entry_exists(hass: HomeAssistant) -> bool:
    """True when the official MQTT integration already has a config entry."""
    return any(
        entry.domain == MQTT_DOMAIN
        for entry in hass.config_entries.async_entries(MQTT_DOMAIN)
    )


def _is_hassio(hass: HomeAssistant) -> bool:
    """True when Supervisor is available (Home Assistant OS / Supervised).

    Only there can the official Mosquitto broker add-on be installed, so the
    add-on setup option is offered only in that case.
    """
    from homeassistant.helpers.hassio import is_hassio

    return is_hassio(hass)


def _schema_defaults(schema: Any) -> dict[str, Any]:
    """Collect the default values from a voluptuous schema.

    Used to build a valid payload for the official MQTT flow without
    re-declaring its schema (which would drift). Only keys that declare a
    default are included; the caller overrides the ones it knows.
    """
    defaults: dict[str, Any] = {}
    if schema is None:
        return defaults
    raw = getattr(schema, "schema", schema)
    if not isinstance(raw, dict):
        return defaults
    for key, value in raw.items():
        name = getattr(key, "schema", key)
        default = getattr(value, "default", vol.UNDEFINED)
        if default is not vol.UNDEFINED and default is not None:
            defaults[str(name)] = default
        # Collapsed sections need a dict (their sub-defaults are optional).
        elif hasattr(value, "schema"):
            defaults[str(name)] = _schema_defaults(value)
    return defaults


class HomeKeyHouseholdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HomeKey Household.

    A single step collects the household identity and the optional command-key
    material. Nodes are **not** configured manually: they are discovered from
    ``homekey/household/<household_id>/nodes/+/state`` (deterministically, by the
    retained state message) once the entry is set up.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Track the last MQTT-flow error so it can be shown to the user."""
        super().__init__()
        self._mqtt_express_error: str | None = None
        self._mqtt_addon_error: str | None = None
        # Prefilled household values (editable in the form). Populated from the
        # MQTT entry the user just created, so the express/add-on paths arrive
        # pre-configured but the user can still change anything.
        self._household_defaults: dict[str, Any] = {}
        # What mDNS advertised, carried from the discovery step to the credential
        # step. Empty for every flow that did not start from discovery.
        self._discovery: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: ensure MQTT, or offer guided / express / add-on setup."""
        if not await async_validate_mqtt(self.hass):
            # MQTT is missing (or present but not connected). Offer a choice.
            # The add-on option only makes sense where Supervisor can install
            # the official Mosquitto add-on, i.e. Home Assistant OS/Supervised.
            options = ["mqtt_guide", "mqtt_express"]
            if _is_hassio(self.hass):
                options.append("mqtt_addon")
            return self.async_show_menu(step_id="user", menu_options=options)
        return await self.async_step_household()

    # ------------------------------------------------------------------
    # Zeroconf discovery (the broker-less, or "direct", transport)
    # ------------------------------------------------------------------
    async def async_step_zeroconf(self, discovery_info: Any) -> ConfigFlowResult:
        """Handle a node advertising ``_homekey._tcp`` on the local network.

        Discovery leads to the *direct* transport. A node that announces itself on the
        LAN can be talked to without any broker - that is the entire reason it
        announces itself - so making broker setup the first thing a discovered node
        asks for would defeat the point. The MQTT transport remains available from the
        manual "Add integration" path, and both produce identical entities.
        """
        properties = _txt_properties(discovery_info)

        protocol = properties.get(ZEROCONF_KEY_PROTOCOL) or ""
        if protocol != str(HA_API_PROTOCOL):
            return self.async_abort(
                reason="unsupported_protocol",
                description_placeholders={
                    "protocol": protocol or "unknown",
                    "supported": str(HA_API_PROTOCOL),
                },
            )

        # The firmware refuses to serve state or configuration in the clear, so a node
        # discovered without TLS would be discovered and then fail every request. Its
        # own reason, because the fix (enable HTTPS on the node) is specific.
        if properties.get(ZEROCONF_KEY_TLS) != "1":
            return self.async_abort(reason="tls_required")

        fingerprint = properties.get(ZEROCONF_KEY_FINGERPRINT) or ""
        if not fingerprint:
            return self.async_abort(reason="no_fingerprint")

        host = _preferred_host(discovery_info)
        port = getattr(discovery_info, "port", None)
        if not host or not port:
            return self.async_abort(reason="node_address_unknown")

        # The certificate fingerprint is what actually identifies this device: a node
        # id can be reassigned and an address definitely changes, but the pinned
        # certificate is unique to one unit.
        await self.async_set_unique_id(f"{DOMAIN}_{normalise_fingerprint(fingerprint)}")
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: str(host), CONF_PORT: int(port)}
        )

        self._discovery = {
            CONF_HOST: str(host),
            CONF_PORT: int(port),
            CONF_FINGERPRINT: fingerprint,
            CONF_NODE_ID: properties.get(ZEROCONF_KEY_ID) or "",
            CONF_NODE_NAME: properties.get(ZEROCONF_KEY_NAME) or "",
            CONF_MODEL: properties.get(ZEROCONF_KEY_MODEL) or "",
            "version": properties.get(ZEROCONF_KEY_VERSION) or "",
        }
        # Shown on the discovery card, before the user opens the flow.
        self.context["title_placeholders"] = {
            "name": self._discovery[CONF_NODE_NAME]
            or self._discovery[CONF_NODE_ID]
            or "HomeKey node"
        }
        return await self.async_step_direct()

    async def async_step_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the advertised certificate and collect the node's credentials."""
        discovery = dict(self._discovery)
        if not discovery:
            # Only reachable if the flow is resumed without its discovery context.
            return self.async_abort(reason="node_address_unknown")

        errors: dict[str, str] = {}
        presented = ""

        if user_input is not None:
            username = (user_input.get(CONF_USERNAME) or "").strip()
            password = user_input.get(CONF_PASSWORD) or ""
            if not username or not password:
                errors["base"] = "node_auth_failed"
            else:
                try:
                    probe = await self._async_probe_direct(
                        discovery[CONF_HOST],
                        discovery[CONF_PORT],
                        discovery[CONF_FINGERPRINT],
                        username,
                        password,
                    )
                except DirectTransportError as err:
                    errors["base"] = _DIRECT_ERROR_KEYS.get(
                        type(err), "node_unreachable"
                    )
                    # The form error is for the user; this line is for whoever has to
                    # work out why a node that answers in a browser could not be
                    # reached from here. The address that was actually tried is the
                    # part otherwise invisible from the message alone.
                    _LOGGER.warning(
                        "Direct transport: %s:%s failed with %s: %s",
                        discovery[CONF_HOST],
                        discovery[CONF_PORT],
                        type(err).__name__,
                        err,
                    )
                    # Show what the node actually presented. "The certificate differs"
                    # is not actionable; the two values side by side are.
                    presented = getattr(err, "actual", "") or ""
                else:
                    return await self._async_create_direct_entry(
                        probe, username, password
                    )

        return self.async_show_form(
            step_id="direct",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=DEFAULT_WEB_USERNAME
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "name": discovery.get(CONF_NODE_NAME)
                or discovery.get(CONF_NODE_ID)
                or "HomeKey node",
                "model": discovery.get(CONF_MODEL) or "HomeKey-ESP32",
                "version": discovery.get("version") or "unknown",
                "host": discovery[CONF_HOST],
                "port": str(discovery[CONF_PORT]),
                "expected": discovery[CONF_FINGERPRINT],
                "presented": presented or "(not checked yet)",
                "docs": DOCS_URL,
            },
        )

    async def _async_probe_direct(
        self, host: str, port: int, expected: str, username: str, password: str
    ) -> DirectProbe:
        """Pin the certificate, authenticate, and read the node's own identity.

        Delegates to the same implementation entry setup uses, so what is verified when
        a node is added is exactly what is verified on every later start. Raises a
        :class:`DirectTransportError` subclass for every way this can fail, so the
        caller only has to map exception types to form errors.
        """
        return await async_connect_node(
            self.hass.async_add_executor_job,
            async_get_clientsession(self.hass),
            host=host,
            port=port,
            expected_fingerprint=expected,
            username=username,
            password=password,
        )

    async def _async_create_direct_entry(
        self, probe: DirectProbe, username: str, password: str
    ) -> ConfigFlowResult:
        """Create the entry, refusing to duplicate a household's entities."""
        # Entity unique ids are ``<household_id>_<node_id>_<entity>``, so a household
        # that is already configured - over either transport - would produce a second,
        # identical set of entities. Refuse rather than let Home Assistant log
        # duplicates that the user cannot remove.
        for entry in self._async_current_entries():
            if entry.data.get(CONF_HOUSEHOLD_ID) == probe.household_id:
                return self.async_abort(
                    reason="household_already_configured",
                    description_placeholders={
                        "household_id": probe.household_id,
                        "title": entry.title,
                    },
                )

        # The credential is stored in the entry, exactly as the core MQTT integration
        # stores a broker password: the direct transport has to authenticate on every
        # poll, so it cannot be kept only for the lifetime of this flow. The recovery
        # secret is a different matter and is still never persisted (see credential).
        return self.async_create_entry(
            title=probe.node_name or probe.node_id,
            data={
                CONF_TRANSPORT: TRANSPORT_DIRECT,
                CONF_HOUSEHOLD_ID: probe.household_id,
                CONF_HOUSEHOLD_NAME: probe.household_name,
                CONF_NODE_ID: probe.node_id,
                CONF_NODE_NAME: probe.node_name,
                CONF_MODEL: probe.model,
                CONF_HOST: self._discovery[CONF_HOST],
                CONF_PORT: self._discovery[CONF_PORT],
                CONF_FINGERPRINT: probe.fingerprint,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
            },
        )

    # ------------------------------------------------------------------
    # Reauthentication (the node's Web UI credential changed)
    # ------------------------------------------------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the node rejected the stored credential."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the stored credential, but only after proving the new one works."""
        entry = self._get_reauth_entry()
        host: str = entry.data.get(CONF_HOST, "")
        port: int = int(entry.data.get(CONF_PORT, DEFAULT_HTTPS_PORT))
        expected: str = entry.data.get(CONF_FINGERPRINT, "")
        errors: dict[str, str] = {}

        if user_input is not None:
            username = (user_input.get(CONF_USERNAME) or "").strip()
            password = user_input.get(CONF_PASSWORD) or ""
            if not username or not password:
                errors["base"] = "node_auth_failed"
            else:
                try:
                    # The stored fingerprint is still required to match. A node whose
                    # certificate changed is a different trust decision, so it is
                    # deliberately not something an ordinary password reset re-pins;
                    # that is reported and the user removes and re-adds the entry.
                    await self._async_probe_direct(
                        host, port, expected, username, password
                    )
                except DirectTransportError as err:
                    errors["base"] = _DIRECT_ERROR_KEYS.get(
                        type(err), "node_unreachable"
                    )
                    _LOGGER.warning(
                        "Direct transport: %s:%s failed with %s: %s",
                        host,
                        port,
                        type(err).__name__,
                        err,
                    )
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                        },
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME)
                        or DEFAULT_WEB_USERNAME,
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "host": host,
                "port": str(port),
                "expected": expected,
                "docs": DOCS_URL,
            },
        )

    # ------------------------------------------------------------------
    # Guided walkthrough (no automatic configuration)
    # ------------------------------------------------------------------
    async def async_step_mqtt_guide(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show step-by-step instructions and re-check MQTT on submit."""
        if user_input is not None:
            if await async_validate_mqtt(self.hass):
                return await self.async_step_household()
            return self.async_show_form(
                step_id="mqtt_guide",
                data_schema=vol.Schema({}),
                errors={"base": "mqtt_not_configured"},
            )

        return self.async_show_form(
            step_id="mqtt_guide",
            data_schema=vol.Schema({}),
            description_placeholders={"docs": DOCS_URL},
        )

    # ------------------------------------------------------------------
    # Express setup: create the official MQTT config entry, then continue
    # ------------------------------------------------------------------
    async def async_step_mqtt_express(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the official MQTT integration entry from a broker form."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if await _async_mqtt_entry_exists(self.hass):
                # Another entry appeared meanwhile; just re-check connectivity.
                if await async_validate_mqtt(self.hass):
                    return await self.async_step_household()
                errors["base"] = "mqtt_not_configured"

            if not errors:
                data: dict[str, Any] = {
                    CONF_MQTT_BROKER: user_input[CONF_MQTT_BROKER],
                    CONF_MQTT_PORT: user_input[CONF_MQTT_PORT],
                    CONF_MQTT_PROTOCOL: DEFAULT_MQTT_PROTOCOL,
                }
                username = user_input.get(CONF_MQTT_USERNAME)
                password = user_input.get(CONF_MQTT_PASSWORD)
                if username:
                    data[CONF_MQTT_USERNAME] = username
                if password:
                    data[CONF_MQTT_PASSWORD] = password
                # Never retain the password in our own flow state.
                del password

                if await self._async_create_mqtt_entry(data):
                    # Prefill the household step from what the user just chose so
                    # the express path arrives pre-configured; every field stays
                    # editable in the form.
                    self._household_defaults.setdefault(
                        "name", f"HomeKey ({user_input[CONF_MQTT_BROKER]})"
                    )
                    return await self.async_step_household()
                # Surface the MQTT flow's own error (e.g. cannot_connect) instead
                # of a generic message, so the user knows what to fix. The
                # express path requires a REACHABLE broker: 127.0.0.1 is only
                # correct when the broker runs on the same host as Home
                # Assistant.
                errors["base"] = self._mqtt_express_error or "mqtt_setup_failed"

        return self.async_show_form(
            step_id="mqtt_express",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MQTT_BROKER, default=DEFAULT_MQTT_BROKER): str,
                    vol.Required(CONF_MQTT_PORT, default=DEFAULT_MQTT_PORT): vol.Coerce(
                        int
                    ),
                    vol.Optional(CONF_MQTT_USERNAME): str,
                    vol.Optional(CONF_MQTT_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _async_create_mqtt_entry(self, data: dict[str, Any]) -> bool:
        """Create the MQTT config entry via the official flow, then wait.

        The MQTT integration owns broker validation: its ``broker`` step requires
        a *reachable* broker and a fully-populated settings payload, including
        the collapsed ``other_settings`` section. Rather than duplicating that
        schema here (which would silently drift), we ask the MQTT flow for its own
        schema, merge our broker values on top of its defaults, and submit that.
        """
        # Phase 1: obtain the MQTT flow's own broker schema and its defaults.
        init = await self.hass.config_entries.flow.async_init(
            MQTT_DOMAIN, context={"source": "user"}
        )
        flow_id = init.get("flow_id")
        if init.get("type") != "form" or not flow_id:
            _LOGGER.warning(
                "Express MQTT setup: unexpected init result (type=%s)",
                init.get("type"),
            )
            return False

        payload = _schema_defaults(init.get("data_schema"))
        payload.update(data)
        # ``other_settings`` is a required collapsed section whose tri-state
        # selectors have no schema default. The CA-verification mode must be
        # "off" for a plain (non-TLS) broker: "auto" would make the MQTT client
        # attempt TLS and fail with "cannot_connect" against e.g. a local
        # Mosquitto. "custom"/"auto" remain available via the MQTT integration's
        # own options flow when TLS is actually used.
        other = dict(payload.get("other_settings") or {})
        other.setdefault("set_ca_cert", "off")
        other.setdefault("set_client_cert", False)
        payload["other_settings"] = other

        # Phase 2: submit using the official schema's own defaults.
        try:
            result = await self.hass.config_entries.flow.async_configure(
                flow_id, payload
            )
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Express MQTT setup failed to configure the MQTT flow")
            return False

        if result.get("type") != "create_entry":
            errors = result.get("errors") or {}
            # Prefer the specific MQTT error (cannot_connect, invalid_auth, ...)
            # so the user gets an actionable message rather than a generic one.
            self._mqtt_express_error = errors.get("base") or None
            _LOGGER.warning(
                "Express MQTT setup did not create an entry (type=%s, errors=%s)",
                result.get("type"),
                errors,
            )
            return False

        return await async_validate_mqtt(self.hass)

    # ------------------------------------------------------------------
    # Add-on setup (Home Assistant OS / Supervised only)
    # ------------------------------------------------------------------
    async def async_step_mqtt_addon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Install and wire up the official Mosquitto broker add-on.

        Zero input: Supervisor installs/starts the add-on and the MQTT
        integration's own flow derives the entry from the add-on's discovery
        info. Only offered when ``_is_hassio`` is true; guarded again here.
        """
        if not _is_hassio(self.hass):
            return self.async_abort(reason="not_hassio")

        if user_input is not None:
            if await self._async_setup_mqtt_addon():
                return await self.async_step_household()
            # Surface the SPECIFIC add-on failure instead of a generic message:
            #   addon_info_failed       -> Supervisor could not read add-on info
            #   addon_connection_failed -> the add-on did not come up in time
            #   addon_start_failed      -> the add-on started but MQTT never
            #                              connected (most common: the add-on
            #                              is stopped, or it needs its own
            #                              MQTT credentials/logins configured)
            return self.async_show_form(
                step_id="mqtt_addon",
                data_schema=vol.Schema({}),
                errors={"base": self._mqtt_addon_error or "mqtt_addon_failed"},
            )

        return self.async_show_form(
            step_id="mqtt_addon",
            data_schema=vol.Schema({}),
            description_placeholders={"docs": DOCS_URL},
        )

    async def _async_setup_mqtt_addon(self) -> bool:
        """Drive the MQTT flow's add-on branch to completion.

        The MQTT add-on branch is a *progress* flow: it may install the add-on,
        then start it, then create the entry from discovery. We therefore keep
        advancing the flow (answering each progress step) until it either creates
        the entry or fails, with a bounded number of rounds.

        On failure the MQTT flow reports either an ``errors.base`` key or an
        ``abort`` ``reason``; both are recorded in ``_mqtt_addon_error`` so the
        user sees the real cause.
        """
        from homeassistant.data_entry_flow import FlowResultType

        self._mqtt_addon_error = None

        try:
            result = await self.hass.config_entries.flow.async_init(
                MQTT_DOMAIN, context={"source": "user"}
            )
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Add-on MQTT setup failed to start the MQTT flow")
            return False

        # The MQTT user step is a menu on Supervisor: choose the add-on branch.
        if result.get("type") == FlowResultType.MENU and "addon" in (
            result.get("menu_options") or []
        ):
            flow_id = result["flow_id"]
            result = await self.hass.config_entries.flow.async_configure(
                flow_id, {"next_step_id": "addon"}
            )
        else:
            _LOGGER.warning(
                "Add-on MQTT setup: MQTT flow did not offer an add-on branch (type=%s)",
                result.get("type"),
            )
            self._mqtt_addon_error = "mqtt_addon_unavailable"
            return False

        # Advance progress steps (install_addon / start_addon) until settled.
        for _ in range(20):
            rtype = result.get("type")
            if rtype == FlowResultType.CREATE_ENTRY:
                return await async_validate_mqtt(self.hass)
            if rtype != FlowResultType.SHOW_PROGRESS:
                break
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )

        # Preserve the real reason: an error key, an abort reason, or give up.
        error_key = (result.get("errors") or {}).get("base")
        reason = result.get("reason") if rtype == FlowResultType.ABORT else None
        self._mqtt_addon_error = error_key or reason or "mqtt_addon_failed"
        _LOGGER.warning(
            "Add-on MQTT setup did not create an entry (type=%s, error=%s, reason=%s)",
            result.get("type"),
            error_key,
            reason,
        )
        return False

    # ------------------------------------------------------------------
    # Household identity + command credential
    # ------------------------------------------------------------------
    @callback
    def _async_mqtt_household_defaults(self) -> dict[str, Any]:
        """Derive editable household prefills from the configured MQTT entry.

        The household id is the one thing every topic depends on, and the
        firmware publishes it, so we cannot invent it. But we *can* label the
        form meaningfully from the broker the user already set up, which is what
        makes the form feel pre-configured instead of blank. Nothing sensitive
        (username/password) is ever read or shown here.
        """
        from homeassistant.components.mqtt.const import CONF_BROKER

        for entry in self.hass.config_entries.async_entries(MQTT_DOMAIN):
            broker = entry.data.get(CONF_BROKER) or entry.data.get(CONF_MQTT_BROKER)
            if broker:
                return {"name": f"HomeKey ({broker})"}
        return {}

    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect household identity and optional command credentials."""
        errors: dict[str, str] = {}
        # Prefill from whatever is actually known at this point, regardless of
        # how the user got here. MQTT is usually already configured (the common
        # case), so relying on the express step to seed this left the form empty.
        self._household_defaults = {
            **self._async_mqtt_household_defaults(),
            **self._household_defaults,
        }
        if user_input is not None:
            household_id = user_input[CONF_HOUSEHOLD_ID].strip()
            name = (user_input.get(CONF_HOUSEHOLD_NAME) or "").strip() or household_id
            recovery_secret = user_input.get(CONF_RECOVERY_SECRET)
            salt = user_input.get(CONF_SALT)
            command_control = bool(
                user_input.get(CONF_COMMAND_CONTROL, DEFAULT_COMMAND_CONTROL)
            )

            try:
                validate_id(household_id, "household_id")
            except ValidationError:
                errors[CONF_HOUSEHOLD_ID] = "invalid_household_id"

            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}_{household_id}")
                self._abort_if_unique_id_configured()

                # Derive and persist the command key now; the raw secret is not
                # stored in the config entry and is not retained afterwards.
                if recovery_secret:
                    store = CommandKeyStore(self.hass)
                    await store.async_load()
                    try:
                        await store.async_set_from_secret(
                            household_id, recovery_secret, salt or None
                        )
                    except ValidationError:
                        errors[CONF_RECOVERY_SECRET] = "invalid_recovery_secret"
                    finally:
                        del recovery_secret
                elif command_control:
                    # Control enabled but no secret supplied: the integration will
                    # fail closed on commands until a credential is configured.
                    _LOGGER.warning(
                        "No command credential supplied for household %s; "
                        "authenticated lock control will fail closed until one "
                        "is configured via the options flow.",
                        household_id,
                    )

                if not errors:
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_HOUSEHOLD_ID: household_id,
                            CONF_HOUSEHOLD_NAME: name,
                            CONF_COMMAND_CONTROL: command_control,
                        },
                    )

        # Build the schema with only the defaults we actually know: passing
        # ``default=None`` to an optional ``str`` field makes voluptuous reject
        # the form, so an unknown prefill must be omitted rather than set.
        fields: dict[Any, Any] = {}
        id_default = self._household_defaults.get("id")
        if id_default:
            fields[vol.Required(CONF_HOUSEHOLD_ID, default=id_default)] = str
        else:
            fields[vol.Required(CONF_HOUSEHOLD_ID)] = str

        name_default = self._household_defaults.get("name")
        if name_default:
            fields[vol.Optional(CONF_HOUSEHOLD_NAME, default=name_default)] = str
        else:
            fields[vol.Optional(CONF_HOUSEHOLD_NAME)] = str

        # Credentials are never prefilled: the secret is entered once, and a
        # salt is only meaningful when the firmware is configured with one.
        fields[vol.Optional(CONF_RECOVERY_SECRET)] = str
        salt_default = self._household_defaults.get("salt")
        if salt_default:
            fields[vol.Optional(CONF_SALT, default=salt_default)] = str
        else:
            fields[vol.Optional(CONF_SALT)] = str

        fields[vol.Optional(CONF_COMMAND_CONTROL, default=DEFAULT_COMMAND_CONTROL)] = (
            bool
        )

        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"docs": DOCS_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeKeyHouseholdOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Options exist only for the MQTT transport.

        A direct entry has nothing to tune: its one adjustable piece is the stored
        credential, and that changes through reauthentication, which also proves the
        new credential works before replacing a working one. Offering the MQTT options
        (command key, shared-LWT prefix) on a broker-less entry would be two settings
        that silently do nothing.

        Home Assistant asks this before offering the Configure button, so a direct
        entry simply has no options UI rather than an empty one.
        """
        return config_entry.data.get(CONF_TRANSPORT) != TRANSPORT_DIRECT


class HomeKeyHouseholdOptionsFlow(OptionsFlow):
    """Options flow: rotate command credentials and tune the shared LWT prefix."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage command credentials and availability settings."""
        errors: dict[str, str] = {}
        entry = self.config_entry
        household_id: str = entry.data[CONF_HOUSEHOLD_ID]

        current_options = entry.options
        current_password = entry.data.get(CONF_PASSWORD) or current_options.get(
            CONF_PASSWORD, ""
        )

        if user_input is not None:
            recovery_secret = user_input.get(CONF_RECOVERY_SECRET)
            salt = user_input.get(CONF_SALT)
            if recovery_secret:
                store = CommandKeyStore(self.hass)
                await store.async_load()
                try:
                    await store.async_set_from_secret(
                        household_id, recovery_secret, salt or None
                    )
                except ValidationError:
                    errors[CONF_RECOVERY_SECRET] = "invalid_recovery_secret"
                finally:
                    del recovery_secret

            if not errors:
                # ``recovery_secret`` is intentionally not persisted in options.
                new_options = {
                    CONF_LEGACY_CLIENT_ID_PREFIX: user_input.get(
                        CONF_LEGACY_CLIENT_ID_PREFIX,
                        DEFAULT_LEGACY_CLIENT_ID_PREFIX,
                    ),
                    CONF_COMMAND_CONTROL: user_input.get(
                        CONF_COMMAND_CONTROL, DEFAULT_COMMAND_CONTROL
                    ),
                    CONF_HOST: (user_input.get(CONF_HOST) or "").strip(),
                    CONF_PORT: int(user_input.get(CONF_PORT) or DEFAULT_HTTPS_PORT),
                    CONF_USERNAME: user_input.get(CONF_USERNAME) or "",
                    # Blank keeps the stored password: options are shown in the UI, and a
                    # password that round-trips through a form is a password on screen.
                    CONF_PASSWORD: user_input.get(CONF_PASSWORD)
                    or current_password,
                    CONF_FINGERPRINT: (user_input.get(CONF_FINGERPRINT) or "").strip(),
                }
                return self.async_create_entry(data=new_options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_RECOVERY_SECRET): str,
                    vol.Optional(CONF_SALT): str,
                    vol.Optional(
                        CONF_COMMAND_CONTROL,
                        default=current_options.get(
                            CONF_COMMAND_CONTROL, DEFAULT_COMMAND_CONTROL
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_LEGACY_CLIENT_ID_PREFIX,
                        default=current_options.get(
                            CONF_LEGACY_CLIENT_ID_PREFIX,
                            DEFAULT_LEGACY_CLIENT_ID_PREFIX,
                        ),
                    ): str,
                    # The node's own API, which is the only thing a backup or a restore
                    # can travel over. An MQTT entry has no reason to know it otherwise,
                    # so it is optional and only used for those.
                    vol.Optional(
                        CONF_HOST,
                        default=current_options.get(
                            CONF_HOST, entry.data.get(CONF_HOST, "")
                        ),
                    ): str,
                    vol.Optional(
                        CONF_PORT,
                        default=current_options.get(
                            CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_HTTPS_PORT)
                        ),
                    ): cv.port,
                    vol.Optional(
                        CONF_USERNAME,
                        default=current_options.get(
                            CONF_USERNAME, entry.data.get(CONF_USERNAME, DEFAULT_WEB_USERNAME)
                        ),
                    ): str,
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_FINGERPRINT,
                        default=current_options.get(
                            CONF_FINGERPRINT, entry.data.get(CONF_FINGERPRINT, "")
                        ),
                    ): str,
                }
            ),
            errors=errors,
        )
