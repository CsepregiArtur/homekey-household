"""One press to take a backup of a node.

The integration already takes a backup every day and keeps the newest few. This is the
button for the moment somebody wants one *now* - before an update, before moving a node,
before anything that might need putting back - without opening the device's own web
interface and hunting for the download link.

It is an entity on the node's device, so it can sit on a dashboard or be pressed by an
automation. A press that cannot be carried out says why rather than doing nothing.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .backup import BACKUP_STORE_KEY, async_back_up_entry
from .const import DOMAIN, ENTITY_BUTTON_BACKUP, unique_id
from .coordinator import HomeKeyHouseholdCoordinator
from .discovery import async_add_entities_for_nodes
from .entity import HomeKeyBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a backup button for every known node, and for nodes discovered later."""
    coordinator: HomeKeyHouseholdCoordinator = hass.data[entry.domain][
        entry.entry_id
    ].coordinator
    async_add_entities_for_nodes(
        coordinator,
        async_add_entities,
        lambda node_id: [HomeKeyBackupButton(coordinator, node_id)],
    )


class HomeKeyBackupButton(HomeKeyBaseEntity, ButtonEntity):
    """Ask this node for a backup now, and keep it."""

    _attr_translation_key = ENTITY_BUTTON_BACKUP

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(coordinator, node_id)
        self._attr_unique_id = unique_id(
            coordinator.household_id, node_id, ENTITY_BUTTON_BACKUP
        )

    async def async_press(self) -> None:
        """Take one backup of this node.

        It reaches the node over the node's own API rather than over MQTT, which is the
        only thing that can hand a backup over, so a node whose address and Web UI
        credentials are not configured cannot be backed up this way - and that is what the
        press reports instead of silently doing nothing.
        """
        store = self.hass.data.get(DOMAIN, {}).get(BACKUP_STORE_KEY)
        entry = getattr(self.coordinator, "config_entry", None)
        if store is None or entry is None:
            raise HomeAssistantError("Backup storage is not available")

        await async_back_up_entry(self.hass, store, entry.entry_id, explicit=True)
