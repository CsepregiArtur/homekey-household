"""Node actions that take one press.

Three of them, all commands rather than settings:

* **Back up now** - the integration already takes a backup every day and keeps the
  newest few. This is the button for the moment somebody wants one *now*, without
  opening the device's own web interface and hunting for the download link.
* **Teach guest card** - arms a write on the node. The card then has to be presented
  to *that node's* reader within about a minute, so a press is the start of the
  action, not the end of it. Teaching with a specific label or window, and revoking a
  card, are services because they carry data a button cannot.
* **Cancel guest card write** - abandons an armed write, so a forgotten teach request
  does not sit waiting to be applied to the next card somebody taps.

They are entities on the node's device, so any of them can sit on a dashboard or be
pressed by an automation. A press that cannot be carried out says why rather than
doing nothing.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .backup import BACKUP_STORE_KEY, async_back_up_entry
from .const import (
    DOMAIN,
    ENTITY_BUTTON_BACKUP,
    ENTITY_BUTTON_GUEST_CANCEL,
    ENTITY_BUTTON_GUEST_TEACH,
    unique_id,
)
from .coordinator import HomeKeyHouseholdCoordinator
from .discovery import async_add_entities_for_nodes
from .entity import HomeKeyBaseEntity

_LOGGER = logging.getLogger(__name__)


def _buttons_for_node(
    coordinator: HomeKeyHouseholdCoordinator, node_id: str
) -> list[HomeKeyBaseEntity]:
    """Build the per-node buttons."""
    return [
        HomeKeyBackupButton(coordinator, node_id),
        HomeKeyGuestTeachButton(coordinator, node_id),
        HomeKeyGuestCancelButton(coordinator, node_id),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buttons for every known node, and for nodes discovered later."""
    coordinator: HomeKeyHouseholdCoordinator = hass.data[entry.domain][
        entry.entry_id
    ].coordinator
    async_add_entities_for_nodes(
        coordinator,
        async_add_entities,
        lambda node_id: _buttons_for_node(coordinator, node_id),
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


class HomeKeyGuestTeachButton(HomeKeyBaseEntity, ButtonEntity):
    """Arm a write for a new guest card and wait for the card to be tapped.

    The card is written with the node's configured default validity (set on the
    *Guest default validity* number) and no label, so one press is enough for the
    common case. Anything else - a name, an explicit window - goes through the
    ``homekey_household.guest_teach`` service.
    """

    _attr_translation_key = ENTITY_BUTTON_GUEST_TEACH

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(coordinator, node_id)
        self._attr_unique_id = unique_id(
            coordinator.household_id, node_id, ENTITY_BUTTON_GUEST_TEACH
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.guest_manageable

    async def async_press(self) -> None:
        try:
            result = await self.coordinator.async_teach_guest_tag(self._node_id)
        except Exception as err:  # noqa: BLE001 - reported to the user
            raise HomeAssistantError(f"Could not arm the card write: {err}") from err

        # Not "done": the node is now waiting for a card. The message says what to do
        # next, because a press that silently arms and expires would look broken.
        tag_id = result.get("tag_id") if isinstance(result, dict) else None
        _LOGGER.info(
            "Guest card write armed on %s for tag %s; present the card to the node now",
            self._node_id,
            tag_id or "?",
        )


class HomeKeyGuestCancelButton(HomeKeyBaseEntity, ButtonEntity):
    """Abandon an armed guest card write.

    An armed write that nobody completes would otherwise be applied to the next card
    tapped on that node - including a HomeKey tap - so there has to be a way to call it
    off. The node also times the arm out; this is for doing it deliberately.
    """

    _attr_translation_key = ENTITY_BUTTON_GUEST_CANCEL

    def __init__(self, coordinator: HomeKeyHouseholdCoordinator, node_id: str) -> None:
        super().__init__(coordinator, node_id)
        self._attr_unique_id = unique_id(
            coordinator.household_id, node_id, ENTITY_BUTTON_GUEST_CANCEL
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.guest_manageable

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_cancel_guest_write(self._node_id)
        except Exception as err:  # noqa: BLE001 - reported to the user
            raise HomeAssistantError(f"Could not cancel the card write: {err}") from err
