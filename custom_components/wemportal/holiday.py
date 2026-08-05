"""The service that writes a holiday as one range.

Home Assistant sets one entity at a time, and a holiday is not one value.
Begin and end are two parameters of the same module, and the portal takes
them only together - measured on a live installation, see date.py: a write
carrying one of them alone comes back Status -1 with no job id, while the
pair is accepted and answered with one.

Worse for a sequence of `date.set_value` calls, the pair also has to describe
a range that makes sense. A write whose begin falls after its end came back
Status 0, job id and all, and changed nothing. So whichever of the two goes
first describes half a range the portal is entitled to discard, and which
half that is depends on the order the user happens to click in. No sequence
of single writes can do this reliably.

This service takes the whole range in one call and puts it on the wire in one
request, which is the only shape that can express what the user means.

The two entities are named explicitly rather than derived from one another.
Nothing in the portal's data says which of a module's dates is the beginning
and which the end - the ids are the portal's own, and reading intent into
them would be a guess dressed up as a feature.
"""

from functools import partial
from typing import NamedTuple

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry
from homeassistant.helpers.service import async_register_admin_service

from .const import _LOGGER, DOMAIN, SERVICE_SET_HOLIDAY
from .date import date_to_epoch
from .models import raise_if_not_writable


class DateTarget(NamedTuple):
    """Everything a write needs about one date entity."""

    entity_id: str
    entry: object
    data: object
    device_id: str
    data_key: str
    row: dict

    @property
    def address(self):
        """The module this parameter belongs to, as the portal addresses it."""
        return (self.row.get("ModuleIndex"), self.row.get("ModuleType"))


def resolve_date_target(hass: HomeAssistant, entity_id: str) -> DateTarget:
    """The account, device and coordinator row behind a date entity.

    Raises HomeAssistantError with the reason, because every one of these is
    something the user can act on: the wrong entity, an account that is not
    loaded, a reading that has not arrived yet.
    """
    if entity_id.split(".")[0] != "date":
        raise HomeAssistantError(
            f"{entity_id} is not a date entity, so it cannot carry a holiday date."
        )
    registry_entry = entity_registry.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.platform != DOMAIN:
        raise HomeAssistantError(f"{entity_id} is not a WEM Portal entity.")

    # The format get_wemportal_unique_id builds: "<entry id>:<device>:<row>".
    # maxsplit=2 because only the first two parts are known not to contain a
    # colon; the row key is whatever the portal named the parameter.
    parts = (registry_entry.unique_id or "").split(":", 2)
    if len(parts) != 3:
        raise HomeAssistantError(
            f"{entity_id} does not carry a WEM Portal identity this version "
            "understands. Reload the integration."
        )
    _entry_id, device_id, data_key = parts

    entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
    if entry is None:
        raise HomeAssistantError(f"The account behind {entity_id} no longer exists.")
    # Also the unload gate: a write must not start into an entry that is on
    # its way out, and this is the one place that knows how to say so.
    data = raise_if_not_writable(entry, f"Setting the holiday of {entity_id}")

    row = (data.coordinator.data or {}).get(device_id, {}).get(data_key)
    if not isinstance(row, dict):
        raise HomeAssistantError(
            f"{entity_id} has no reading yet, so there is nothing to write "
            "against. Wait for the next update."
        )
    return DateTarget(entity_id, entry, data, device_id, data_key, row)


def _check_pair(begin: DateTarget, end: DateTarget) -> None:
    """Refuse a pair the portal cannot be asked to store as one range."""
    if begin.entity_id == end.entity_id:
        raise HomeAssistantError(
            f"Begin and end name the same entity ({begin.entity_id}). "
            "A holiday needs two."
        )
    if begin.entry.entry_id != end.entry.entry_id:
        raise HomeAssistantError(
            f"{begin.entity_id} and {end.entity_id} belong to different "
            "accounts, so they cannot be written in one request."
        )
    if begin.device_id != end.device_id or begin.address != end.address:
        raise HomeAssistantError(
            f"{begin.entity_id} and {end.entity_id} belong to different "
            "modules. The portal addresses parameters per module, so a "
            "holiday has to be two dates of the same one."
        )


async def _write_holiday(hass: HomeAssistant, call) -> None:
    """Set both dates of one holiday in a single request."""
    begin_day = call.data["begin"]
    end_day = call.data["end"]
    if begin_day > end_day:
        # Not a guess about the portal: a holiday that ends before it starts
        # is not a holiday. The portal answers such a pair with Status 0 and
        # stores nothing, so letting it through would report success for a
        # setting that never happened.
        raise HomeAssistantError(
            f"A holiday from {begin_day} to {end_day} would end before it "
            "starts. Nothing was written."
        )

    begin = resolve_date_target(hass, call.data["begin_entity"])
    end = resolve_date_target(hass, call.data["end_entity"])
    _check_pair(begin, end)

    begin_epoch = date_to_epoch(begin_day)
    end_epoch = date_to_epoch(end_day)
    module_index, module_type = begin.address

    await hass.async_add_executor_job(
        partial(
            begin.data.api.change_value,
            begin.device_id,
            begin.row.get("ParameterID", begin.data_key),
            module_index,
            module_type,
            begin_epoch,
            together_with={end.row.get("ParameterID", end.data_key): end_epoch},
        )
    )

    # Only now, and for BOTH rows: change_value raises when the portal
    # refuses, so nothing below runs on a write that did not happen. Leaving
    # the rows behind would make the next write send the old dates back as
    # companions - the defect this service exists alongside.
    begin.row["value"] = begin_epoch
    end.row["value"] = end_epoch
    begin.data.coordinator.async_update_listeners()

    _LOGGER.info(
        "Holiday set from %s to %s on device %s.", begin_day, end_day, begin.device_id
    )


def async_register_holiday_service(hass: HomeAssistant) -> None:
    """Register wemportal.set_holiday (idempotent).

    An ADMIN service, like the expert one and for the same reason: it changes
    a real setting on a heating system, and a service call is not covered by
    the per-user entity permissions Home Assistant applies to the entities.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SET_HOLIDAY):
        return

    async def _handle(call):
        await _write_holiday(hass, call)

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_HOLIDAY,
        _handle,
        schema=vol.Schema(
            {
                vol.Required("begin_entity"): cv.entity_id,
                vol.Required("begin"): cv.date,
                vol.Required("end_entity"): cv.entity_id,
                vol.Required("end"): cv.date,
            }
        ),
    )


def async_release_holiday_service(hass: HomeAssistant, config_entry) -> None:
    """Drop the service once no loaded entry is left to serve it.

    One domain-wide registration shared by every account, so unloading one
    entry must not take it away from the others.
    """
    if not hass.services.has_service(DOMAIN, SERVICE_SET_HOLIDAY):
        return
    still_loaded = any(
        other.entry_id != config_entry.entry_id
        and getattr(other, "runtime_data", None) is not None
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not still_loaded:
        hass.services.async_remove(DOMAIN, SERVICE_SET_HOLIDAY)
