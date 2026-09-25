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
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEVICE_INITIATED_SOURCES,
    DOMAIN,
    ENTITY_LOCK,
    JSON_SUBTOPICS,
    LOCK_SOURCE_LABELS,
    LOCK_STATE_MAP,
    TOPIC_BACKUP_LAST,
    TOPIC_BACKUP_STATUS,
    TOPIC_HEALTH,
    TOPIC_LAST_AUTH,
    TOPIC_LOCK_LAST,
    TOPIC_SECURITY,
    TOPIC_STATE,
    TOPIC_STATUS,
    BackupOutcome,
    LockState,
    SecurityState,
    unique_id,
)
from .models import (
    BackupRecord,
    LastAuth,
    LockChange,
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
        # A cause established during the update currently being dispatched, handed to the
        # entities so the state change they write carries it. Cleared once the update has
        # been delivered: keeping it would attach the same cause to the next change too.
        self._pending_contexts: dict[str, Context] = {}
        self._reload_paused = False
        # Injected by async_setup_entry once the MQTT client exists. Exactly one of
        # ``mqtt`` / ``direct`` is set, per the transport the entry was configured for.
        self.mqtt: Any = None
        # The direct poller, when the entry uses the broker-less transport. It feeds
        # ``async_handle_message`` with the same messages the MQTT client would have
        # produced, so nothing below this line needs to know which transport is live.
        self.direct: Any = None

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
            node = self._ensure_node(node_id)
            previous_current = node.health.lock_current if node.health else None
            health = NodeHealth.from_dict(payload, self.household_id, node_id)
            node.health = health
            # Attribution is a *description* of this update, not part of it. If it fails,
            # the state still has to be published: dropping a real reading because a
            # logbook entry could not be written would be a far worse failure than a
            # change going unexplained.
            try:
                self._attribute_lock_change(node, previous_current, health)
            except Exception:  # noqa: BLE001 - a description must not break ingestion
                _LOGGER.exception(
                    "Could not attribute a lock change for %s/%s",
                    self.household_id,
                    node_id,
                )
            updated = True
        elif subtopic == TOPIC_LOCK_LAST and node_id:
            self._ensure_node(node_id).lock_change = LockChange.from_dict(
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
            # The entities have now written their state, carrying any cause attached
            # during this update. Dropping it keeps it from being attached again to a
            # change it does not describe.
            self._pending_contexts.clear()

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
    # Attributing a lock change to its cause
    # ------------------------------------------------------------------
    def _attribute_lock_change(
        self, node: Node, previous_current: int | None, health: NodeHealth
    ) -> None:
        """Give a lock change a cause, so the activity log can name it.

        Home Assistant attributes a state change to whatever ``Context`` was attached when
        the state was written. A change Home Assistant asked for already has one: the
        service call's context is pending on the entity and is consumed by the very write
        the change causes - which is why those read "Action used: Lock lock" under the
        user's name. A change made at the door has nobody to attribute to, because the node
        reports a number and nothing that identifies the person, so the node's own account
        of what asked for the change is turned into a context here.
        """
        current = health.lock_current
        if previous_current is None or current is None or current == previous_current:
            # Not a change: either the first reading, with nothing to compare against, or a
            # repeat. Attributing either would credit something that did not happen.
            return

        change = node.lock_change
        if change is None or change.current != current:
            # No cause on record for *this* change. Using the previous one would blame
            # whatever happened last for what happened now.
            return
        if change.source not in DEVICE_INITIATED_SOURCES:
            # Home Assistant already knows who asked, and overriding that would replace a
            # real person with a vaguer description.
            return

        context = Context()
        self._pending_contexts[node.node_id] = context
        self._async_log_lock_cause(node, current, change.source, context)

    @callback
    def _async_log_lock_cause(
        self, node: Node, current: int, source: str, context: Context
    ) -> None:
        """Record what changed the lock, sharing the context of the state change.

        Fired with the same context the entities are about to write with, so the activity
        view has something to name rather than reporting that no cause was recorded.
        """
        # Imported where it is used: the logbook is a companion, not a dependency, and this
        # must not stop the integration working when it is not set up.
        from homeassistant.components.logbook import async_log_entry

        state = LOCK_STATE_MAP.get(current, LockState.UNKNOWN)
        async_log_entry(
            self.hass,
            name="HomeKey",
            message=(
                f"{node.node_name} {state} by "
                f"{LOCK_SOURCE_LABELS.get(source, source)}"
            ),
            domain=DOMAIN,
            entity_id=self._lock_entity_id(node.node_id),
            context=context,
        )

    def _lock_entity_id(self, node_id: str) -> str | None:
        """Entity id of a node's lock, or ``None`` when it cannot be resolved.

        Defensive on purpose: this only decides how a logbook entry is scoped, and the
        entity registry may not even be loaded in every context this runs in.
        """
        from homeassistant.helpers import entity_registry as er

        try:
            registry = er.async_get(self.hass)
            return registry.async_get_entity_id(
                "lock", DOMAIN, unique_id(self.household_id, node_id, ENTITY_LOCK)
            )
        except Exception:  # noqa: BLE001 - scoping is cosmetic
            _LOGGER.debug(
                "Could not resolve the lock entity id for %s/%s",
                self.household_id,
                node_id,
            )
            return None

    def pending_context(self, node_id: str) -> Context | None:
        """Cause attached to the update being dispatched, for an entity to write with."""
        return self._pending_contexts.get(node_id)

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
        """True when a lock command can actually be delivered.

        The MQTT transport authenticates each command with a key derived from the
        household recovery secret, so it needs that key. The direct transport
        authenticates with the device credential over a pinned TLS connection, so it
        needs no key and must not be reported as unable to control the lock.
        """
        if self.direct is not None:
            return True
        return bool(self.command_key())

    async def async_send_lock_command(self, node_id: str, action: str) -> Any:
        """Deliver a lock/unlock command over whichever transport is configured.

        The MQTT path fails closed (raises :class:`ValidationError`) when no key
        material is configured, so an unauthenticated command is never published.
        """
        if self.direct is not None:
            return await self._async_send_direct_command(node_id, action)

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

    async def _async_send_direct_command(self, node_id: str, action: str) -> Any:
        """Send a lock command to the node's own API, then re-read its state.

        The command is the node's own operation, so the state it reports afterwards is
        the truth; the follow-up poll is what makes the entity reflect a jam or a
        failed mechanism instead of the request. A failed refresh is logged and
        swallowed: the command was delivered, and the next scheduled poll will catch
        up regardless.
        """
        poller = self.direct
        if action not in ("lock", "unlock"):
            raise ValidationError(f"unsupported action: {action!r}")

        result = await poller.client.async_lock(action)
        try:
            await poller.async_poll_once()
        except Exception as err:  # noqa: BLE001 - a refresh failure must not hide success
            _LOGGER.debug(
                "Could not refresh %s/%s after a %s command: %s",
                self.household_id,
                node_id,
                action,
                err,
            )
        return result

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
