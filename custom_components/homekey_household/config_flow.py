"""Config flow for HomeKey Household.

The flow deliberately reuses the Home Assistant core **MQTT integration** rather
than asking for broker details a second time. MQTT connection/reference is
therefore the already-configured HA MQTT integration; the flow aborts when it is
not available.

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
    CONF_RECOVERY_SECRET,
    CONF_SALT,
    DEFAULT_COMMAND_CONTROL,
    DEFAULT_LEGACY_CLIENT_ID_PREFIX,
    DOMAIN,
)
from .credential import CommandKeyStore
from .models import ValidationError, validate_id

_LOGGER = logging.getLogger(__name__)


async def async_validate_mqtt(hass: HomeAssistant) -> bool:
    """Return True when the HA MQTT integration is configured and available."""
    from homeassistant.components import mqtt

    return bool(await mqtt.async_wait_for_mqtt_client(hass))


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
        """Collect household identity and optional command credentials."""
        if not await async_validate_mqtt(self.hass):
            return self.async_abort(reason="mqtt_not_configured")

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
            step_id="user",
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
            description_placeholders={
                "docs": "https://github.com/example/homekey-household",
            },
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
