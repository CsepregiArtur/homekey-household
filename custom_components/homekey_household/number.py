"""Guest access: the default validity window, in days.

When a guest card is taught without an explicit expiry, the node applies this
window. Days rather than seconds because that is the unit a person reasons in for a
guest - "a week", "a month" - and the firmware keeps the seconds.

Setting it to 0 means "no expiry", which is the honest way to offer a permanent
guest card: it is a deliberate choice rather than something you get by leaving a
field empty.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ENTITY_NUMBER_GUEST_VALIDITY,
    GUEST_VALIDITY_DAYS_MAX,
    GUEST_VALIDITY_DAYS_MIN,
    GUEST_VALIDITY_DAYS_STEP,
    unique_id,
)
from .coordinator import HomeKeyHouseholdCoordinator
from .discovery import async_add_entities_for_nodes
from .entity import HomeKeyBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the default-validity number for every known node, and later ones."""
    coordinator: HomeKeyHouseholdCoordinator = hass.data[entry.domain][
        entry.entry_id
    ].coordinator
    async_add_entities_for_nodes(
        coordinator,
        async_add_entities,
        lambda node_id: [HomeKeyGuestValidityNumber(coordinator, node_id)],
    )


class HomeKeyGuestValidityNumber(HomeKeyBaseEntity, NumberEntity):
    """Default guest validity, in days. ``0`` means no expiry."""

    _attr_translation_key = ENTITY_NUMBER_GUEST_VALIDITY
    _attr_native_min_value = GUEST_VALIDITY_DAYS_MIN
    _attr_native_max_value = GUEST_VALIDITY_DAYS_MAX
    _attr_native_step = GUEST_VALIDITY_DAYS_STEP
    _attr_native_unit_of_measurement = "d"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(coordinator, node_id)
        self._attr_unique_id = unique_id(
            coordinator.household_id, node_id, ENTITY_NUMBER_GUEST_VALIDITY
        )

    @property
    def available(self) -> bool:
        # Hidden when the node has no guest feature, or the transport cannot manage it.
        return (
            super().available
            and self.coordinator.guest_manageable
            and self.guest_reported
        )

    @property
    def native_value(self) -> float | None:
        node = self._node()
        if node is None or node.guest is None:
            return None
        return node.guest.default_validity_days

    async def async_set_native_value(self, value: float) -> None:
        """Store the new default on the node.

        The value is written as days and converted once, in the coordinator, so the
        firmware's unit (seconds) never leaks into the interface.
        """
        try:
            await self.coordinator.async_set_guest_access(
                self._node_id, default_validity_days=value
            )
        except Exception as err:  # noqa: BLE001 - reported to the user
            raise HomeAssistantError(
                f"Could not set the default guest validity: {err}"
            ) from err
