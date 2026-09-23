"""Central coordinator for HomeKey Household.

MQTT is push-based, so the coordinator is driven by ``async_set_updated_data``
from the MQTT callback rather than by polling. It is the single source of truth
for household/node state and the single writer of entity-facing data.

Parsing is defensive: a malformed payload is logged and rejected without
crashing, and the previous valid state is preserved. Missing fields are never
coerced into meaningful zero/false values.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    JSON_SUBTOPICS,
    TOPIC_BACKUP_LAST,
    TOPIC_BACKUP_STATUS,
    TOPIC_HEALTH,
    TOPIC_LAST_AUTH,
    TOPIC_SECURITY,
    TOPIC_STATE,
    TOPIC_STATUS,
    BackupOutcome,
    SecurityState,
)
from .models import (
    BackupRecord,
    LastAuth,
    Node,
    NodeHealth,
    ValidationError,
)
from .mqtt import KIND_LEGACY_STATUS

_LOGGER = logging.getLogger(__name__)

# Debounce for auto-discovery reloads (seconds).
_RELOAD_DEBOUNCE_SECONDS = 3.0

# How long to wait for retained household messages to register nodes before the
# entity platforms are set up. Retained messages are delivered asynchronously
# right after subscribing; without this wait the first platform setup would run
# against an empty node registry and create no entities.
_INITIAL_NODE_WAIT_SECONDS = 5.0

# Documented, reserved-by-firmware security literals. Anything else is rejected.
_VALID_SECURITY = {str(state) for state in SecurityState}
_VALID_BACKUP = {str(outcome) for outcome in BackupOutcome}


@dataclass
class HomeKeyData:
    """Snapshot of the household state exposed to entities."""

    household_id: str
    nodes: dict[str, Node] = field(default_factory=dict)


class HomeKeyHouseholdCoordinator(DataUpdateCoordinator[HomeKeyData]):
    """Maintains household and node state from MQTT push messages."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        *,
        household_id: str,
        command_key_provider: Callable[[], bytes | None] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{household_id}",
            update_interval=None,
            always_update=True,
        )
        self.household_id = household_id
        self._entry_id = config_entry.entry_id
        self._data = HomeKeyData(household_id=household_id)
        # Reattach any node state discovered before a discovery-triggered reload.
        # Non-retained topics (notably ``B/health``) are not replayed by the
        # broker, so without this the reloaded coordinator would lose them and
        # entities created after the reload would have no data.
        self._restore_pooled_state(hass)
        self._command_key_provider = command_key_provider
        self._reload_handle: Any = None
        self._pending_reload = False
        self._reload_paused = False
        # Injected by async_setup_entry once the MQTT client exists.
        self.mqtt: Any = None

    # ------------------------------------------------------------------
    # State pooling across config-entry reloads
    # ------------------------------------------------------------------
    @property
    def _pool_key(self) -> str:
        return f"{DOMAIN}_state_{self.household_id}"

    def _restore_pooled_state(self, hass: HomeAssistant) -> None:
        pooled: dict[str, Node] | None = hass.data.get(self._pool_key)
        if pooled:
            self._data.nodes.update(pooled)
            _LOGGER.debug(
                "Restored %d pooled node(s) for household %s after reload",
                len(pooled),
                self.household_id,
            )
            # The pooled nodes already existed, so no reload is needed for them.
            self._known_node_ids = set(pooled)
        else:
            self._known_node_ids = set()

    def _pool_state(self) -> None:
        """Publish the current node state so a reload can reattach it."""
        if not hasattr(self, "hass") or self.hass is None:  # pragma: no cover
            return
        self.hass.data[self._pool_key] = dict(self._data.nodes)

    def async_drop_pooled_state(self) -> None:
        """Remove pooled state (called on final unload)."""
        if self.hass is not None:
            self.hass.data.pop(self._pool_key, None)

    # ------------------------------------------------------------------
    # DataUpdateCoordinator contract
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> HomeKeyData:
        """Return the current in-memory snapshot (push-based, no polling)."""
        return self._data

    # ------------------------------------------------------------------
    # Public helpers for entities
    # ------------------------------------------------------------------
    def get_node(self, node_id: str) -> Node | None:
        return self._data.nodes.get(node_id)

    @property
    def nodes(self) -> dict[str, Node]:
        return self._data.nodes

    # ------------------------------------------------------------------
    # MQTT message entry point
    # ------------------------------------------------------------------
    async def async_handle_message(self, message: Any) -> None:
        """Process a parsed MQTT message (see :class:`mqtt.HomeKeyMessage`)."""
        if message.household_id != self.household_id:
            _LOGGER.debug(
                "Ignoring message for other household: %s", message.household_id
            )
            return
        try:
            await self._dispatch(message)
        except ValidationError as exc:
            _LOGGER.warning(
                "Rejected malformed MQTT payload on %s (node=%s): %s",
                message.subtopic,
                message.node_id,
                exc,
            )
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception(
                "Unexpected error handling MQTT message subtopic=%s",
                message.subtopic,
            )

    async def _dispatch(self, message: Any) -> None:
        subtopic = message.subtopic
        node_id = message.node_id
        updated = False

        # Decode JSON exactly once for subtopics whose contract is JSON.
        payload: Any = message.payload
        if subtopic in JSON_SUBTOPICS:
            payload = self._decode_json(message.payload)

        if subtopic == KIND_LEGACY_STATUS and node_id:
            self._handle_legacy_status(node_id, message.payload)
            updated = True
        elif subtopic == TOPIC_STATUS and node_id:
            self._handle_node_status(node_id, message.payload)
            updated = True
        elif subtopic == TOPIC_STATE and node_id:
            self._merge_node(Node.from_state(self.household_id, node_id, payload))
            updated = True
        elif subtopic == TOPIC_HEALTH and node_id:
            self._ensure_node(node_id).health = NodeHealth.from_dict(
                payload, self.household_id, node_id
            )
            updated = True
        elif subtopic == TOPIC_SECURITY and node_id:
            self._handle_security(node_id, message.payload)
            updated = True
        elif subtopic == TOPIC_BACKUP_STATUS and node_id:
            self._handle_backup_status(node_id, message.payload)
            updated = True
        elif subtopic == TOPIC_BACKUP_LAST and node_id:
            self._ensure_node(node_id).backup = BackupRecord.from_dict(
                payload, self.household_id, node_id
            )
            updated = True
        elif subtopic == TOPIC_LAST_AUTH and node_id:
            self._ensure_node(node_id).last_auth = LastAuth.from_dict(
                payload, self.household_id, node_id
            )
            updated = True
        else:
            _LOGGER.debug("Unhandled subtopic: %s", subtopic)

        if updated:
            # Keep the reload pool in sync so a discovery reload can reattach the
            # state (including non-retained telemetry such as ``B/health``).
            self._pool_state()
            self.async_set_updated_data(self._data)

    @staticmethod
    def _decode_json(payload: Any) -> Any:
        """Decode a JSON payload string, rejecting invalid JSON."""
        if isinstance(payload, dict):
            return payload
        if not isinstance(payload, str):
            raise ValidationError("payload: invalid type")
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise ValidationError("payload: invalid JSON") from exc

    # ------------------------------------------------------------------
    # Node lifecycle
    # ------------------------------------------------------------------
    def _ensure_node(self, node_id: str) -> Node:
        node = self._data.nodes.get(node_id)
        if node is None:
            node = Node(node_id=node_id, household_id=self.household_id)
            self._data.nodes[node_id] = node
            _LOGGER.info(
                "Discovered household node %s/%s",
                self.household_id,
                node_id,
            )
            self._maybe_schedule_reload(node_id)
        return node

    def _merge_node(self, incoming: Node) -> None:
        """Merge a state payload into the existing node, preserving other fields."""
        existing = self._data.nodes.get(incoming.node_id)
        if existing is None:
            self._data.nodes[incoming.node_id] = incoming
            _LOGGER.info(
                "Discovered household node %s/%s", self.household_id, incoming.node_id
            )
            self._maybe_schedule_reload(incoming.node_id)
            return
        existing.node_name = incoming.node_name
        existing.node_role = incoming.node_role
        existing.node_state = incoming.node_state
        if incoming.generation is not None:
            existing.generation = incoming.generation
        if incoming.firmware is not None:
            existing.firmware = incoming.firmware

    def _maybe_schedule_reload(self, node_id: str) -> None:
        """Record a newly discovered node.

        Entity platforms register a coordinator listener and add entities for new
        nodes incrementally (see ``discovery.async_add_entities_for_nodes``), so a
        full config-entry reload is no longer required. Reloading was both slow
        and lossy: it rebuilt the coordinator and discarded non-retained state
        such as ``B/health``, which the broker never replays.
        """
        known: set[str] = getattr(self, "_known_node_ids", set())
        known.add(node_id)
        self._known_node_ids = known
        _LOGGER.debug(
            "New node %s/%s registered incrementally (no reload)",
            self.household_id,
            node_id,
        )

    # ------------------------------------------------------------------
    # Per-topic handlers
    # ------------------------------------------------------------------
    def _handle_node_status(self, node_id: str, payload: Any) -> None:
        """Handle ``B/status`` (retained, raw ``online``)."""
        value = payload.strip().lower() if isinstance(payload, str) else ""
        if value not in ("online", "offline"):
            raise ValidationError(f"status: unexpected payload {payload!r}")
        node = self._ensure_node(node_id)
        node.online = value == "online"
        node.last_seen = self._now_iso()

    def _handle_legacy_status(self, node_id: str, payload: Any) -> None:
        """Handle the shared broker LWT availability topic.

        This is the only broker-driven offline signal. It is deliberately tracked
        separately from ``B/status`` so the documented clean-disconnect
        limitation is respected rather than masked with a timer.
        """
        value = payload.strip().lower() if isinstance(payload, str) else ""
        if value not in ("online", "offline"):
            raise ValidationError(f"legacy status: unexpected payload {payload!r}")
        node = self._data.nodes.get(node_id)
        if node is None:
            # The LWT topic is MAC-derived and must not create household entities
            # on its own: it carries no household identity.
            return
        node.lwt_online = value == "online"
        if node.lwt_online:
            node.last_seen = self._now_iso()

    def _handle_security(self, node_id: str, payload: Any) -> None:
        """Handle ``B/security`` (raw ``OK`` / ``WARNING``, never numeric)."""
        value = payload.strip() if isinstance(payload, str) else ""
        if value not in _VALID_SECURITY:
            raise ValidationError(f"security: unknown value {value!r}")
        self._ensure_node(node_id).security = value

    def _handle_backup_status(self, node_id: str, payload: Any) -> None:
        """Handle ``B/backup/status`` (raw ``completed`` / ``failed``)."""
        value = payload.strip().lower() if isinstance(payload, str) else ""
        if value not in _VALID_BACKUP:
            raise ValidationError(f"backup/status: unknown value {value!r}")
        self._ensure_node(node_id).backup_status = value

    # ------------------------------------------------------------------
    # Command helpers (fail closed when credentials are unavailable)
    # ------------------------------------------------------------------
    def command_key(self) -> bytes | None:
        """Return the command key, or ``None`` when unavailable."""
        if self._command_key_provider is None:
            return None
        key = self._command_key_provider()
        return key or None

    @property
    def command_control_enabled(self) -> bool:
        """True when HMAC-authenticated commands can be published."""
        return bool(self.command_key())

    async def async_send_lock_command(self, node_id: str, action: str) -> Any:
        """Publish an authenticated lock/unlock command for a node.

        Fails closed (raises :class:`ValidationError`) when no key material is
        configured, so an unauthenticated command is never published.
        """
        key = self.command_key()
        if not key:
            raise ValidationError(
                "command credential unavailable; refusing to publish an "
                "unauthenticated command"
            )
        if self.mqtt is None:
            raise ValidationError("MQTT client unavailable")
        if action == "lock":
            return await self.mqtt.async_lock(self.household_id, node_id, key)
        return await self.mqtt.async_unlock(self.household_id, node_id, key)

    async def async_lock_node(self, node_id: str) -> Any:
        return await self.async_send_lock_command(node_id, "lock")

    async def async_unlock_node(self, node_id: str) -> Any:
        return await self.async_send_lock_command(node_id, "unlock")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # Auto-discovery reload (debounced)
    # ------------------------------------------------------------------
    def pause_discovery_reload(self) -> None:
        """Suppress reloads while entities are being added for the first time."""
        self._reload_paused = True

    def resume_discovery_reload(self) -> None:
        self._reload_paused = False

    async def async_wait_for_initial_nodes(self, timeout: float | None = None) -> bool:
        """Wait for retained household messages to register the first nodes.

        MQTT retained messages are delivered asynchronously after subscribing, so
        platform setup would otherwise run against an empty node registry and
        create no entities. Returns True if at least one node was discovered
        within the timeout (a household with no nodes is also a valid outcome and
        simply yields no entities until the next message arrives).
        """
        wait_seconds = (
            _INITIAL_NODE_WAIT_SECONDS if timeout is None else timeout
        )
        if self._data.nodes:
            return True
        if wait_seconds <= 0:
            return False

        discovered = asyncio.Event()
        unsubscribe = self.async_add_listener(
            lambda: discovered.set() if self._data.nodes else None
        )
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait_seconds):
                    await discovered.wait()
        finally:
            unsubscribe()
        if self._data.nodes:
            _LOGGER.debug(
                "Discovered %d node(s) from retained messages before platform setup: %s",
                len(self._data.nodes),
                sorted(self._data.nodes),
            )
        return bool(self._data.nodes)

    @callback
    def _schedule_reload(self) -> None:
        """Debounce a config-entry reload so new nodes appear without a restart."""
        if self._reload_paused or self._pending_reload:
            return
        self._pending_reload = True

        def _reload() -> None:
            self._reload_handle = None
            self._pending_reload = False
            if self.hass.is_stopping:
                return
            _LOGGER.debug("Reloading config entry after discovering new nodes")
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self._entry_id)
            )

        self._reload_handle = self.hass.loop.call_later(
            _RELOAD_DEBOUNCE_SECONDS, _reload
        )

    @callback
    def async_cancel_pending_reload(self) -> None:
        """Cancel a pending debounced reload.

        Must be called on config-entry unload: otherwise the timer survives
        teardown and leaks (and could reload an entry that is being removed).
        """
        self._pending_reload = False
        if self._reload_handle is not None:
            with contextlib.suppress(Exception):
                self._reload_handle.cancel()
            self._reload_handle = None


__all__ = ["HomeKeyData", "HomeKeyHouseholdCoordinator"]
