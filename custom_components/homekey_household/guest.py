"""Guest NFC tag actions, exposed as Home Assistant services.

Guest tags let an ordinary card (an NTAG sticker, a key fob) unlock a node the same
way a HomeKey tap does, with an optional validity window. Two things about them
cannot be an ordinary entity:

* **Teaching takes a physical step.** The node arms a write; the card must then be
  presented to *that node's* reader within about a minute, and the outcome arrives
  afterwards. A button can start that, but the interesting result is not the press.
* **Revoking names one card.** A tag id is data, and a button cannot carry any.

So both are services, callable from an automation as well as from the UI, with the
result visible on the node's guest entities.

Managing guest tags needs the **direct (TLS) transport**. Enabling guest access could
be done over MQTT - the node has plain ``guest/set_*`` topics - but teaching and
revoking cannot: they are not single signed actions, and the household command
namespace deliberately carries only lock/unlock. Rather than implement two thirds of
the feature on one transport and let the rest fail obscurely, the writes need the API
and say so. Guest *state* still arrives over both transports.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import (
    DOMAIN,
    SERVICE_GUEST_CANCEL,
    SERVICE_GUEST_REVOKE,
    SERVICE_GUEST_TEACH,
)

_LOGGER = logging.getLogger(__name__)


def _runtime_for(hass: HomeAssistant, data: dict[str, Any]) -> Any:
    """Resolve the entry a guest service call targets.

    A guest card is taught to one node's reader, so a call that does not name an entry
    is only unambiguous when exactly one is configured. Guessing between two doors
    would be worse than refusing.
    """
    runtimes = {
        entry_id: runtime
        for entry_id, runtime in hass.data.get(DOMAIN, {}).items()
        if hasattr(runtime, "coordinator")
    }
    if not runtimes:
        raise ServiceValidationError("No HomeKey Household entry is set up")

    requested = data.get("config_entry_id")
    if requested:
        runtime = runtimes.get(requested)
        if runtime is None:
            raise ServiceValidationError(f"Unknown HomeKey config entry: {requested}")
        return runtime

    if len(runtimes) > 1:
        raise ServiceValidationError(
            "Several HomeKey entries are set up; choose one with config_entry_id"
        )
    return next(iter(runtimes.values()))


def _node_id_for(runtime: Any) -> str:
    """The one node this direct entry talks to.

    Guest writes need the direct transport, which is one node per entry, so the
    address of the node comes from the poller rather than from a parameter.
    """
    poller = getattr(runtime.coordinator, "direct", None)
    if poller is None:
        raise ServiceValidationError(
            "Managing guest tags needs the direct (TLS) transport; this entry uses MQTT"
        )
    node_id = poller.node_id
    if not node_id:
        raise ServiceValidationError(
            "The node has not answered a poll yet, so there is no reader to teach"
        )
    return node_id


async def async_setup_guest_services(hass: HomeAssistant) -> None:
    """Register the guest tag services."""

    async def _teach(call: Any) -> None:
        runtime = _runtime_for(hass, call.data)
        node_id = _node_id_for(runtime)

        label = str(call.data.get("label") or "").strip() or None
        raw_days = call.data.get("valid_days")
        valid_days = None if raw_days is None else float(raw_days)

        try:
            result = await runtime.coordinator.async_teach_guest_tag(
                node_id, label=label, valid_days=valid_days
            )
        except Exception as err:  # noqa: BLE001 - surfaced to the caller as a message
            raise ServiceValidationError(
                f"Could not arm the card write: {err}"
            ) from err

        # Say plainly that this is not finished: the card still has to be tapped on the
        # node. Reporting success here would be reporting the arming, not the teaching.
        _LOGGER.info(
            "Guest card write armed on %s for tag %s; present the card to the node now",
            node_id,
            result.get("tag_id") if isinstance(result, dict) else "?",
        )

    async def _revoke(call: Any) -> None:
        runtime = _runtime_for(hass, call.data)
        node_id = _node_id_for(runtime)

        tag_id = str(call.data.get("tag_id") or "").strip()
        if not tag_id:
            raise ServiceValidationError("A tag_id is required to revoke a guest tag")

        try:
            await runtime.coordinator.async_revoke_guest_tag(node_id, tag_id)
        except Exception as err:  # noqa: BLE001 - surfaced to the caller as a message
            raise ServiceValidationError(f"Could not revoke {tag_id}: {err}") from err

    async def _cancel(call: Any) -> None:
        runtime = _runtime_for(hass, call.data)
        node_id = _node_id_for(runtime)
        try:
            await runtime.coordinator.async_cancel_guest_write(node_id)
        except Exception as err:  # noqa: BLE001 - surfaced to the caller as a message
            raise ServiceValidationError(
                f"Could not cancel the card write: {err}"
            ) from err

    hass.services.async_register(DOMAIN, SERVICE_GUEST_TEACH, _teach)
    hass.services.async_register(DOMAIN, SERVICE_GUEST_REVOKE, _revoke)
    hass.services.async_register(DOMAIN, SERVICE_GUEST_CANCEL, _cancel)


__all__ = ["async_setup_guest_services"]
