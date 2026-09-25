"""HomeKey Household — Home Assistant custom integration (V2).

Represents HomeKey-ESP32 nodes in Home Assistant over one of two transports:

``mqtt`` (default)
    The documented household *MQTT API* (firmware 0.10.0), reusing Home Assistant's
    core MQTT integration as the transport (no second broker connection). Push-based,
    covers a whole household, needs a broker.

``direct``
    The node's own HTTPS API (``/api/ha/*``), with its self-signed certificate pinned
    to an exact fingerprint. Needs no broker at all, but covers exactly one node and
    has to poll.

Both are *clients of the firmware* only:

* they use documented interfaces and nothing else,
* they do not depend on ESP32 classes, LockManager internals, HomeKey credential
  storage, HomeKit internals, NVS, the firmware filesystem, or legacy MQTT command
  paths,
* they produce identical coordinator state, so entities cannot tell them apart.

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
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from .const import (
    CONF_COMMAND_CONTROL,
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_HOUSEHOLD_ID,
    CONF_HOUSEHOLD_NAME,
    CONF_LEGACY_CLIENT_ID_PREFIX,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TRANSPORT,
    CONF_USERNAME,
    DEFAULT_COMMAND_CONTROL,
    DEFAULT_HTTPS_PORT,
    DEFAULT_LEGACY_CLIENT_ID_PREFIX,
    DOMAIN,
    TRANSPORT_DIRECT,
    TRANSPORT_MQTT,
)
from .coordinator import HomeKeyHouseholdCoordinator
from .credential import CommandKeyStore
from .direct import (
    DirectAuthError,
    DirectFingerprintMismatch,
    DirectNoHouseholdError,
    DirectPoller,
    DirectProtocolError,
    DirectTransportError,
    async_connect_node,
)
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
        transport: Any,
        credential_store: CommandKeyStore,
        transport_kind: str,
    ) -> None:
        self.config_entry = config_entry
        self.coordinator = coordinator
        # Either a ``HomeKeyMqttClient`` or a ``DirectPoller``. Both are started before
        # the platforms are set up and stopped on unload; nothing else in the
        # integration depends on which one it is.
        self.transport = transport
        self.transport_kind = transport_kind
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
    transport_kind: str = entry.data.get(CONF_TRANSPORT, TRANSPORT_MQTT)
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

    # The direct transport authenticates with the device credential, not with a key
    # derived from the household recovery secret, so it must not be handed a command
    # key provider: doing so would make ``command_control_enabled`` depend on a
    # credential that has nothing to do with whether commands can be delivered.
    coordinator = HomeKeyHouseholdCoordinator(
        hass,
        entry,
        household_id=household_id,
        command_key_provider=(
            (lambda: _command_key(credential_store, household_id))
            if command_control and transport_kind != TRANSPORT_DIRECT
            else None
        ),
    )

    if transport_kind == TRANSPORT_DIRECT:
        runtime_transport: Any = await _async_start_direct(hass, entry, coordinator)
    else:
        mqtt_client = HomeKeyMqttClient(
            hass,
            HAMqttTransport(hass),
            coordinator.async_handle_message,
            household_id=household_id,
            legacy_prefix=legacy_prefix,
        )
        coordinator.mqtt = mqtt_client
        runtime_transport = mqtt_client
        await mqtt_client.async_start()
        # Retained household messages are delivered asynchronously on the event loop
        # after subscribing. Without waiting, platform setup would run against an
        # empty node registry and create no entities.
        await coordinator.async_wait_for_initial_nodes()

    runtime = HomeKeyRuntime(
        config_entry=entry,
        coordinator=coordinator,
        transport=runtime_transport,
        credential_store=credential_store,
        transport_kind=transport_kind,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    coordinator.pause_discovery_reload()
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.resume_discovery_reload()

    # Started only once the platforms exist: the direct transport's own first poll
    # happens before this, so entities are created from real data rather than from an
    # empty registry, and the recurring poll is not left running if platform setup
    # fails.
    if isinstance(runtime_transport, DirectPoller):
        await runtime_transport.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _LOGGER.info(
        "HomeKey Household %s ready over %s; nodes discovered so far: %s",
        household_id,
        transport_kind,
        sorted(coordinator.nodes),
    )
    return True


async def _async_start_direct(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HomeKeyHouseholdCoordinator,
) -> DirectPoller:
    """Verify a node and take its first reading before the platforms are set up.

    Every check the config flow performed when the entry was created is repeated here.
    That is the point rather than a duplication: a pinned fingerprint only means
    something if it is re-verified against the node actually answering today, so a
    swapped or factory-reset device is refused at startup instead of after the first
    credential has been sent.
    """
    host: str = entry.data[CONF_HOST]
    port: int = int(entry.data.get(CONF_PORT, DEFAULT_HTTPS_PORT))
    fingerprint: str = entry.data[CONF_FINGERPRINT]
    username: str = entry.data.get(CONF_USERNAME, "")
    password: str = entry.data.get(CONF_PASSWORD, "")

    try:
        probe = await async_connect_node(
            hass.async_add_executor_job,
            _direct_session(hass),
            host=host,
            port=port,
            expected_fingerprint=fingerprint,
            username=username,
            password=password,
        )
    except DirectAuthError as err:
        # Surfaces the reauthentication flow, which is exactly the right response to a
        # credential the node no longer accepts.
        raise ConfigEntryAuthFailed(
            f"The node at {host} rejected the stored credentials: {err}"
        ) from err
    except DirectFingerprintMismatch as err:
        raise ConfigEntryError(
            f"The certificate presented by {host} is not the pinned one. Expected "
            f"{err.expected}, got {err.actual}. If the node was replaced or reset, "
            "remove this integration entry and add it again."
        ) from err
    except DirectNoHouseholdError as err:
        # Not retryable: the node is working correctly and simply has no household,
        # which only the user can create.
        raise ConfigEntryError(str(err)) from err
    except DirectProtocolError as err:
        raise ConfigEntryError(str(err)) from err
    except DirectTransportError as err:
        # Reachability is transient by nature, so this one is retried with backoff.
        raise ConfigEntryNotReady(
            f"Could not reach the HomeKey node at {host}:{port}: {err}"
        ) from err

    if probe.household_id != coordinator.household_id:
        raise ConfigEntryError(
            f"The node now reports household {probe.household_id}, but this entry was "
            f"created for {coordinator.household_id}. Remove and re-add the entry."
        )

    poller = DirectPoller(
        hass, coordinator, probe.client, household_id=coordinator.household_id
    )
    coordinator.direct = poller
    # One reading before the platforms are set up, so entities appear immediately
    # instead of only after the first scheduled poll.
    await poller.async_poll_safely()
    return poller


def _direct_session(hass: HomeAssistant) -> Any:
    """Return Home Assistant's shared aiohttp session (see manifest dependencies)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return async_get_clientsession(hass)


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
        await runtime.transport.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


def entry_title(entry: ConfigEntry) -> str:
    """Human-readable entry title (household name or id)."""
    return entry.data.get(CONF_HOUSEHOLD_NAME) or entry.data.get(
        CONF_HOUSEHOLD_ID, DOMAIN
    )
