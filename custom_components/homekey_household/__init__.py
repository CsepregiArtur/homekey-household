"""HomeKey Household — Home Assistant custom integration (V2).

Represents an entire household of HomeKey-ESP32 nodes over the documented
household MQTT API (firmware 0.10.0). The integration is a *client* of that API:

* it communicates only through the documented household topics,
* it does not depend on ESP32 classes, LockManager internals, HomeKey credential
  storage, HomeKit internals, NVS, the firmware filesystem, or legacy MQTT
  command paths,
* it reuses Home Assistant's core MQTT integration as its transport (no second
  broker connection).

Local HomeKey unlock itself remains entirely on the ESP32 and does not depend on
Home Assistant, MQTT, or internet connectivity. Home Assistant is the management
and control plane, not a dependency for local access.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_COMMAND_CONTROL,
    CONF_HOUSEHOLD_ID,
    CONF_HOUSEHOLD_NAME,
    CONF_LEGACY_CLIENT_ID_PREFIX,
    DEFAULT_COMMAND_CONTROL,
    DEFAULT_LEGACY_CLIENT_ID_PREFIX,
    DOMAIN,
)
from .coordinator import HomeKeyHouseholdCoordinator
from .credential import CommandKeyStore
from .mqtt import HAMqttTransport, HomeKeyMqttClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

type HomeKeyConfigEntry = ConfigEntry[HomeKeyRuntime]


class HomeKeyRuntime:
    """Per-entry runtime object shared across the integration."""

    def __init__(
        self,
        *,
        config_entry: ConfigEntry,
        coordinator: HomeKeyHouseholdCoordinator,
        mqtt: HomeKeyMqttClient,
        credential_store: CommandKeyStore,
    ) -> None:
        self.config_entry = config_entry
        self.coordinator = coordinator
        self.mqtt = mqtt
        self.credential_store = credential_store

    def command_key(self) -> bytes | None:
        """Return the household command key, or ``None`` when unavailable."""
        credential = self.credential_store.get(self.coordinator.household_id)
        return credential.key if credential else None


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration from configuration.yaml (no services are defined)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomeKeyConfigEntry) -> bool:
    """Set up HomeKey Household from a config entry."""
    household_id: str = entry.data[CONF_HOUSEHOLD_ID]
    options = entry.options
    legacy_prefix: str | None = options.get(
        CONF_LEGACY_CLIENT_ID_PREFIX, DEFAULT_LEGACY_CLIENT_ID_PREFIX
    )
    command_control: bool = options.get(
        CONF_COMMAND_CONTROL,
        entry.data.get(CONF_COMMAND_CONTROL, DEFAULT_COMMAND_CONTROL),
    )

    credential_store = CommandKeyStore(hass)
    await credential_store.async_load()

    coordinator = HomeKeyHouseholdCoordinator(
        hass,
        entry,
        household_id=household_id,
        command_key_provider=(
            (lambda: _command_key(credential_store, household_id))
            if command_control
            else None
        ),
    )

    transport = HAMqttTransport(hass)
    mqtt_client = HomeKeyMqttClient(
        hass,
        transport,
        coordinator.async_handle_message,
        household_id=household_id,
        legacy_prefix=legacy_prefix,
    )
    coordinator.mqtt = mqtt_client

    runtime = HomeKeyRuntime(
        config_entry=entry,
        coordinator=coordinator,
        mqtt=mqtt_client,
        credential_store=credential_store,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await mqtt_client.async_start()

    # Retained household messages are delivered asynchronously on the event loop
    # after subscribing. Without waiting, platform setup would run against an
    # empty node registry and create no entities.
    await coordinator.async_wait_for_initial_nodes()

    coordinator.pause_discovery_reload()
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.resume_discovery_reload()

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _LOGGER.info(
        "HomeKey Household %s ready (%s); nodes discovered so far: %s",
        household_id,
        "authenticated control enabled"
        if command_control and _command_key(credential_store, household_id)
        else "authenticated control DISABLED (no credential)",
        sorted(coordinator.nodes),
    )
    return True


def _command_key(store: CommandKeyStore, household_id: str) -> bytes | None:
    credential = store.get(household_id)
    return credential.key if credential else None


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HomeKeyConfigEntry) -> bool:
    """Unload a config entry."""
    runtime: HomeKeyRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None:
        # Cancel any pending debounced discovery reload so its timer does not
        # outlive the entry (and cannot reload an entry being removed), and drop
        # the pooled node state for this household.
        runtime.coordinator.async_cancel_pending_reload()
        runtime.coordinator.async_drop_pooled_state()
        await runtime.mqtt.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


def entry_title(entry: ConfigEntry) -> str:
    """Human-readable entry title (household name or id)."""
    return entry.data.get(CONF_HOUSEHOLD_NAME) or entry.data.get(
        CONF_HOUSEHOLD_ID, DOMAIN
    )
