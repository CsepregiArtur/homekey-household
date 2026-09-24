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
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_COMMAND_CONTROL,
    CONF_HOUSEHOLD_ID,
    CONF_HOUSEHOLD_NAME,
    CONF_LEGACY_CLIENT_ID_PREFIX,
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PROTOCOL,
    CONF_MQTT_USERNAME,
    CONF_RECOVERY_SECRET,
    CONF_SALT,
    DEFAULT_COMMAND_CONTROL,
    DEFAULT_LEGACY_CLIENT_ID_PREFIX,
    DEFAULT_MQTT_BROKER,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_PROTOCOL,
    DOMAIN,
    MQTT_DOMAIN,
)
from .credential import CommandKeyStore
from .models import ValidationError, validate_id

_LOGGER = logging.getLogger(__name__)

DOCS_URL = "https://github.com/example/homekey-household"


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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: ensure MQTT, or offer guided / express setup."""
        if not await async_validate_mqtt(self.hass):
            # MQTT is missing (or present but not connected). Offer a choice.
            return self.async_show_menu(
                step_id="user",
                menu_options=["mqtt_guide", "mqtt_express"],
            )
        return await self.async_step_household()

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
                    return await self.async_step_household()
                errors["base"] = "mqtt_setup_failed"

        return self.async_show_form(
            step_id="mqtt_express",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MQTT_BROKER, default=DEFAULT_MQTT_BROKER
                    ): str,
                    vol.Required(
                        CONF_MQTT_PORT, default=DEFAULT_MQTT_PORT
                    ): vol.Coerce(int),
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
            _LOGGER.warning(
                "Express MQTT setup did not create an entry (type=%s, errors=%s)",
                result.get("type"),
                result.get("errors"),
            )
            return False

        return await async_validate_mqtt(self.hass)

    # ------------------------------------------------------------------
    # Household identity + command credential
    # ------------------------------------------------------------------
    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect household identity and optional command credentials."""
        errors: dict[str, str] = {}
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

        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOUSEHOLD_ID): str,
                    vol.Optional(CONF_HOUSEHOLD_NAME): str,
                    vol.Optional(CONF_RECOVERY_SECRET): str,
                    vol.Optional(CONF_SALT): str,
                    vol.Optional(
                        CONF_COMMAND_CONTROL, default=DEFAULT_COMMAND_CONTROL
                    ): bool,
                }
            ),
            errors=errors,
            description_placeholders={"docs": DOCS_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        return HomeKeyHouseholdOptionsFlow()


class HomeKeyHouseholdOptionsFlow(OptionsFlow):
    """Options flow: rotate command credentials and tune the shared LWT prefix."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage command credentials and availability settings."""
        errors: dict[str, str] = {}
        entry = self.config_entry
        household_id: str = entry.data[CONF_HOUSEHOLD_ID]

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
                }
                return self.async_create_entry(data=new_options)

        current_options = entry.options
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
                }
            ),
            errors=errors,
        )
