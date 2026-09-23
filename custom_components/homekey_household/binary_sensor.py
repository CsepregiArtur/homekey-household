"""Binary sensor platform for HomeKey Household.

Implements the single documented binary sensor from the firmware discovery
contract: **Node online** (``homeassistant/binary_sensor/<hid>_<nid>/config``).

The firmware's shared broker LWT (``<CLIENT_ID>/status``) is the documented
``availability_topic`` for that entity. Because this integration owns the node
directly, the LWT is consumed as an availability signal in the coordinator rather
than by an MQTT discovery payload, so no second MQTT will is created and the
documented clean-disconnect limitation is respected.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ENTITY_SENSOR_ONLINE, unique_id
from .coordinator import HomeKeyHouseholdCoordinator
from .discovery import async_add_entities_for_nodes
from .entity import HomeKeyBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the node-online binary sensor for known and future nodes."""
    coordinator: HomeKeyHouseholdCoordinator = hass.data[entry.domain][
        entry.entry_id
    ].coordinator
    async_add_entities_for_nodes(
        coordinator,
        async_add_entities,
        lambda node_id: [HomeKeyNodeOnline(coordinator, node_id)],
    )


class HomeKeyNodeOnline(HomeKeyBaseEntity, BinarySensorEntity):
    """Whether the node is currently online.

    ``B/status`` carries ``online`` (retained). A *clean* MQTT disconnect can
    leave the retained ``online`` state until another connection/will event; this
    limitation is documented and deliberately not masked with a timer.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = ENTITY_SENSOR_ONLINE

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(coordinator, node_id)
        self._attr_unique_id = unique_id(
            coordinator.household_id, node_id, ENTITY_SENSOR_ONLINE
        )

    @property
    def is_on(self) -> bool | None:
        """``online`` per the retained node status topic."""
        node = self._node()
        return node.online if node else None

    @property
    def available(self) -> bool:
        """The online sensor itself is always readable when the node is known."""
        return self._node() is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes = super().extra_state_attributes
        node = self._node()
        if node is not None:
            attributes["lwt_online"] = node.lwt_online
        return attributes
