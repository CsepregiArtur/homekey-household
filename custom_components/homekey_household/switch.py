"""Guest access: one switch per node.

Guest tags let an ordinary NFC card unlock a node the same way a HomeKey tap does,
with an optional validity window. This switch is the master enable: while it is off,
no guest card is accepted, whatever its dates.

Two honest details:

* The state is **unknown, not off, until the node says so.** ``is_on`` returns
  ``None`` rather than ``False`` when no status has arrived - "we have not asked yet"
  and "guest access is off" are different claims about a door.
* The switch is unavailable when the transport cannot manage guest tags at all, so it
  does not look merely offline.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ENTITY_SWITCH_GUEST_ACCESS, unique_id
from .coordinator import HomeKeyHouseholdCoordinator
from .discovery import async_add_entities_for_nodes
from .entity import HomeKeyBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a guest access switch for every known node, and later ones."""
    coordinator: HomeKeyHouseholdCoordinator = hass.data[entry.domain][
        entry.entry_id
    ].coordinator
    async_add_entities_for_nodes(
        coordinator,
        async_add_entities,
        lambda node_id: [HomeKeyGuestAccessSwitch(coordinator, node_id)],
    )


class HomeKeyGuestAccessSwitch(HomeKeyBaseEntity, SwitchEntity):
    """Master switch for guest card access on this node."""

    _attr_translation_key = ENTITY_SWITCH_GUEST_ACCESS

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(coordinator, node_id)
        self._attr_unique_id = unique_id(
            coordinator.household_id, node_id, ENTITY_SWITCH_GUEST_ACCESS
        )

    @property
    def available(self) -> bool:
        # Also hidden when the node has no guest feature at all, or when the
        # transport cannot manage it: a switch that can only fail is worse than no
        # switch.
        return (
            super().available
            and self.coordinator.guest_manageable
            and self.guest_reported
        )

    @property
    def is_on(self) -> bool | None:
        """Whether guest access is on, or ``None`` while the node has not said."""
        node = self._node()
        if node is None or node.guest is None:
            return None
        return node.guest.enabled

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        try:
            await self.coordinator.async_set_guest_access(
                self._node_id, enabled=enabled
            )
        except Exception as err:  # noqa: BLE001 - reported to the user
            raise HomeAssistantError(
                f"Could not {'enable' if enabled else 'disable'} guest access: {err}"
            ) from err

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Everything a person needs to judge the switch at a glance."""
        attrs = dict(super().extra_state_attributes)
        node = self._node()
        guest = node.guest if node else None
        if guest is not None:
            attrs.update(
                {
                    "tags": guest.count,
                    "capacity": guest.capacity,
                    "default_validity_days": round(guest.default_validity_days, 2),
                    # A time-bounded tag cannot be verified without a clock, so this
                    # is what explains a card that "should" work and is refused.
                    "node_has_wall_clock": guest.has_wall_clock,
                }
            )
        return attrs
