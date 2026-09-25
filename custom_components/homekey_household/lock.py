"""Lock entity for HomeKey Household nodes.

The Lock entity is logically associated with the node and controls it through the
**authoritative** HMAC-authenticated household command topics:

* ``homekey/household/<household_id>/nodes/<node_id>/command/lock``
* ``homekey/household/<household_id>/nodes/<node_id>/command/unlock``

It deliberately does **not** use the legacy/internal compatibility topics
(``<CLIENT_ID>/homekit/set_state``, ``set_target_state``, ``set_current_state``),
which are unauthenticated numeric commands.

Lock state is derived from the node health snapshot (``lock_current``), which is
the documented household state source. If no health snapshot has been received
yet the state is unknown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    COMMAND_CONFIRM_POLL_SECONDS,
    COMMAND_CONFIRM_TIMEOUT_SECONDS,
    ENTITY_LOCK,
    LockState,
    unique_id,
)
from .coordinator import HomeKeyHouseholdCoordinator
from .discovery import async_add_entities_for_nodes
from .entity import HomeKeyBaseEntity
from .models import ValidationError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lock entities for known nodes and for nodes discovered later."""
    coordinator: HomeKeyHouseholdCoordinator = hass.data[entry.domain][
        entry.entry_id
    ].coordinator
    async_add_entities_for_nodes(
        coordinator, async_add_entities, lambda node_id: [HomeKeyLock(coordinator, node_id)]
    )


class HomeKeyLock(HomeKeyBaseEntity, LockEntity):
    """A lock controlled through the HMAC-authenticated household MQTT API."""

    _attr_translation_key = ENTITY_LOCK

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(coordinator, node_id)
        self._attr_unique_id = unique_id(
            coordinator.household_id, node_id, ENTITY_LOCK
        )

    @property
    def is_locked(self) -> bool | None:
        node = self._node()
        if node is None:
            return None
        state = node.lock_state
        if state == LockState.UNKNOWN:
            return None
        # A jammed lock is not properly locked.
        return state == LockState.LOCKED

    @property
    def is_jammed(self) -> bool | None:
        node = self._node()
        return node.lock_state == LockState.JAMMED if node else None

    @property
    def is_locking(self) -> bool | None:
        node = self._node()
        return node.lock_state == LockState.LOCKING if node else None

    @property
    def is_unlocking(self) -> bool | None:
        node = self._node()
        return node.lock_state == LockState.UNLOCKING if node else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = super().extra_state_attributes
        node = self._node()
        if node is None:
            return attributes
        health = node.health
        attributes["lock_state"] = node.lock_state
        if health is not None:
            attributes["lock_current"] = health.lock_current
            attributes["lock_target"] = health.lock_target
        # The command key is never exposed; only whether control is possible.
        attributes["authenticated_control"] = self.coordinator.command_control_enabled
        return attributes

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_send("lock")

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._async_send("unlock")

    async def _async_wait_for_state(self, target: str) -> bool:
        """Wait for the node to report the state the command asked for.

        Home Assistant writes the requested state the moment the call returns, so a command
        the node rejected is indistinguishable from one it carried out - until the next
        report arrives and the entity snaps back, which is what "it worked, then jumped
        back" means. Waiting for that report is what turns it into an answer.
        """
        deadline = time.monotonic() + COMMAND_CONFIRM_TIMEOUT_SECONDS
        while True:
            node = self._node()
            if node is not None and node.lock_state == target:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(COMMAND_CONFIRM_POLL_SECONDS)

    async def _async_send(self, action: str) -> None:
        """Publish an authenticated command, and report whether the node carried it out.

        Fails closed: with no credential the command is not published at all.
        """
        target = LockState.LOCKED if action == "lock" else LockState.UNLOCKED
        try:
            await self.coordinator.async_send_lock_command(self._node_id, action)
        except ValidationError as exc:
            raise HomeAssistantError(
                f"Cannot {action} {self.coordinator.household_id}/{self._node_id}: {exc}"
            ) from exc
        except Exception as exc:  # pragma: no cover - transport failure
            _LOGGER.warning(
                "Failed to publish %s command for %s/%s: %s",
                action,
                self.coordinator.household_id,
                self._node_id,
                exc,
            )
            raise HomeAssistantError(
                f"Failed to publish {action} command for "
                f"{self.coordinator.household_id}/{self._node_id}"
            ) from exc

        if await self._async_wait_for_state(target):
            return

        # Published, and nothing came back. Saying so is the whole point: the alternative
        # is a call that reports success for a door that never moved.
        raise HomeAssistantError(
            f"The node did not report {target} within "
            f"{COMMAND_CONFIRM_TIMEOUT_SECONDS:.0f} s of the {action} command. The command "
            f"was published, so this usually means the node rejected it - its audit log "
            f"records why."
        )
