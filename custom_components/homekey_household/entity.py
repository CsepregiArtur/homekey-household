"""Shared entity base for HomeKey Household platforms."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import MANUFACTURER, MODEL, TARGET_FIRMWARE_VERSION, device_identifiers
from .coordinator import HomeKeyHouseholdCoordinator
from .models import Node


class HomeKeyBaseEntity(CoordinatorEntity[HomeKeyHouseholdCoordinator]):
    """Base entity attached to a stable household-node device.

    Device identity is ``household_id + node_id`` — never the MAC address or the
    HomeKit ``deviceID``. A replacement node (``GATE-001`` -> ``GATE-002``)
    therefore produces a distinct device.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HomeKeyHouseholdCoordinator, node_id: str
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write this entity's state, carrying any cause the coordinator established.

        The context is what Home Assistant attributes a state change to. For a change made
        at the door there is no user and no service call, so without this the activity log
        can only say that no cause was recorded. ``async_set_context`` is the same
        mechanism a service call uses, and the write below consumes it.
        """
        context = self.coordinator.pending_context(self._node_id)
        if context is not None:
            self.async_set_context(context)
        super()._handle_coordinator_update()

    def _node(self) -> Node | None:
        return self.coordinator.get_node(self._node_id)

    @property
    def available(self) -> bool:
        """Available only when the node reports online (``B/status`` + shared LWT)."""
        node = self._node()
        return node is not None and node.available

    @property
    def device_info(self) -> DeviceInfo:
        node = self._node()
        name = node.node_name if node else self._node_id
        identifiers: set[tuple[str, str, str]] = device_identifiers(
            self.coordinator.household_id, self._node_id
        )
        return DeviceInfo(
            identifiers=identifiers,  # type: ignore[typeddict-item]
            name=f"HomeKey Household Node: {name}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            # Software version = the node's reported firmware. Until the node
            # reports it, fall back to the firmware contract this integration
            # implements, so the device card never shows a blank version.
            sw_version=(
                node.firmware
                if node and node.firmware
                else TARGET_FIRMWARE_VERSION
            ),
            # ``hw_version`` is deliberately NOT set: the firmware version is
            # software, not hardware. It was previously mislabelled there, which
            # made the device card show the firmware version twice.
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Common, non-secret attributes safe for state and diagnostics.

        Always returns a dict (never ``None``) so subclasses can safely
        ``update()`` it and Home Assistant never drops attributes for a node that
        exists but has no snapshot yet.
        """
        node = self._node()
        if node is None:
            return {}
        return {
            "household_id": node.household_id,
            "node_id": node.node_id,
            "node_role": node.node_role,
            "node_state": node.node_state,
            "last_seen": node.last_seen,
        }
