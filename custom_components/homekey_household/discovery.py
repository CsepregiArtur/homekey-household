"""Helpers for incremental node-entity registration.

A config-entry reload is a heavyweight way to discover new nodes: it tears the
whole entry down and rebuilds the coordinator, which discards non-retained state
(``B/health`` is never replayed by the broker) and takes seconds.

Instead, each platform registers a listener on the coordinator and adds entities
for nodes as they are discovered. This is faster, keeps state, and avoids
duplicate entities because each node id is only ever tracked once.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HomeKeyHouseholdCoordinator


def async_add_entities_for_nodes(
    coordinator: HomeKeyHouseholdCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[str], list],
) -> None:
    """Add entities for every known node and for nodes discovered later.

    ``factory`` receives a ``node_id`` and returns the list of entities to add
    for that node. It is called exactly once per node id.
    """
    seen: set[str] = set()

    def _add_new_nodes() -> None:
        new_entities = []
        for node_id in coordinator.nodes:
            if node_id in seen:
                continue
            seen.add(node_id)
            new_entities.extend(factory(node_id))
        if new_entities:
            async_add_entities(new_entities)

    # Initial pass: nodes discovered before/at platform setup.
    _add_new_nodes()
    # Later passes: nodes discovered while running.
    coordinator.async_add_listener(_add_new_nodes)


__all__ = ["async_add_entities_for_nodes"]
