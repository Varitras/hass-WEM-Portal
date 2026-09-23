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

import logging

from functools import partial
from typing import Final, NamedTuple

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    DOMAIN,
)
from .date import date_to_epoch
from .exceptions import WemPortalError
from .models import Reading, WemPortalData, is_still_serving, raise_if_not_writable

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_HOLIDAY: Final = "set_holiday"


class DateTarget(NamedTuple):
    """Everything a write needs about one date entity."""

    entity_id: str
    entry: ConfigEntry
    data: WemPortalData
    device_id: str
    data_key: str
    row: Reading

    @property
    def address(self) -> tuple[int | None, int | None]:
        """The module this parameter belongs to, as the portal addresses it."""
        return (self.row.module_index, self.row.module_type)


def resolve_date_target(hass: HomeAssistant, entity_id: str) -> DateTarget:
    """The account, device and coordinator row behind a date entity.

    Raises HomeAssistantError with the reason, because every one of these is
    something the user can act on: the wrong entity, an account that is not
    loaded, a reading that has not arrived yet.
    """
    if entity_id.split(".")[0] != "date":
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="holiday_not_a_date_entity",
            translation_placeholders={"entity_id": entity_id},
        )
    registry_entry = entity_registry.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.platform != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="holiday_not_a_wemportal_entity",
            translation_placeholders={"entity_id": entity_id},
        )

    # The format get_wemportal_unique_id builds: "<entry id>:<device>:<row>".
    # maxsplit=2 because only the first two parts are known not to contain a
    # colon; the row key is whatever the portal named the parameter.
    parts = (registry_entry.unique_id or "").split(":", 2)
    if len(parts) != 3:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="holiday_identity_not_understood",
            translation_placeholders={"entity_id": entity_id},
        )
    _entry_id, device_id, data_key = parts

    entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id or "")
    if entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="holiday_account_gone",
            translation_placeholders={"entity_id": entity_id},
        )
    # Also the unload gate: a write must not start into an entry that is on
    # its way out, and this is the one place that knows how to say so.
    data = raise_if_not_writable(entry, entity_id)

    row = (data.coordinator.data or {}).get(device_id, {}).get(data_key)
    if not isinstance(row, Reading):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="holiday_no_reading_yet",
            translation_placeholders={"entity_id": entity_id},
        )
    # The entity_id above says how Home Assistant files the entity; the row
    # says what the portal currently calls the parameter, and the daily
    # re-discovery can change that underneath a loaded entity. The entities'
    # own write path asks the same question - without it here, the service
    # could still send an epoch to a parameter that has become a switch.
    if row.platform != "date":
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="holiday_reclassified",
            translation_placeholders={
                "entity_id": entity_id,
                "platform": str(row.platform),
            },
        )
    return DateTarget(entity_id, entry, data, device_id, data_key, row)


def _check_pair(begin: DateTarget, end: DateTarget) -> None:
    """Refuse a pair the portal cannot be asked to store as one range."""
    if begin.entity_id == end.entity_id:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="holiday_same_entity",
            translation_placeholders={"entity_id": begin.entity_id},
        )
    if begin.entry.entry_id != end.entry.entry_id:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="holiday_different_accounts",
            translation_placeholders={"begin": begin.entity_id, "end": end.entity_id},
        )
    if begin.device_id != end.device_id or begin.address != end.address:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="holiday_different_modules",
            translation_placeholders={"begin": begin.entity_id, "end": end.entity_id},
        )


async def _write_holiday(hass: HomeAssistant, call: ServiceCall) -> None:
    """Set both dates of one holiday in a single request."""
    begin_day = call.data["begin"]
    end_day = call.data["end"]
    if begin_day > end_day:
        # Not a guess about the portal: a holiday that ends before it starts
        # is not a holiday. The portal answers such a pair with Status 0 and
        # stores nothing, so letting it through would report success for a
        # setting that never happened.
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="holiday_ends_before_it_starts",
            translation_placeholders={"begin": str(begin_day), "end": str(end_day)},
        )

    begin = resolve_date_target(hass, call.data["begin_entity"])
    end = resolve_date_target(hass, call.data["end_entity"])
    _check_pair(begin, end)

    begin_epoch = date_to_epoch(begin_day)
    end_epoch = date_to_epoch(end_day)
    module_index, module_type = begin.address

    try:
        await hass.async_add_executor_job(
            partial(
                begin.data.api.change_value,
                begin.device_id,
                begin.row.parameter_id or begin.data_key,
                module_index,
                module_type,
                begin_epoch,
                together_with={(end.row.parameter_id or end.data_key): end_epoch},
            )
        )
    except WemPortalError as exc:
        # The portal's refusal is detail for the message, not the message:
        # raised as it was, it reached the user in English only.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="holiday_write_failed",
            translation_placeholders={"error": str(exc)},
        ) from exc

    # Ask what was kept rather than publishing what was asked for. Returning
    # without raising means the portal ACCEPTED the request: measured on this
    # endpoint, a range ending before it starts comes back as Status 0 and is
    # discarded. That one pair is refused above, but the check covers only
    # the rejection somebody measured. One read for the whole device, at a
    # service used a few times a year.
    failure = await hass.async_add_executor_job(
        begin.data.api.reread_device_values, begin.device_id
    )
    if failure is not None:
        # The write itself went through, so this is not a failed service
        # call - what is unknown is whether it was kept, and an unknown
        # answer is not the written day. Both rows, because both were sent:
        # leaving either behind would make the next write send a date the
        # portal may never have taken back as a companion.
        _LOGGER.warning(
            "Wrote the holiday from %s to %s but could not read it back (%s). "
            "Both dates are shown as unknown until the next update says what "
            "the portal actually stored.",
            begin_day,
            end_day,
            failure,
        )
        begin.row.value = None
        end.row.value = None
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

    async def _handle(call: ServiceCall) -> None:
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


def async_release_holiday_service(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Drop the service once no loaded entry is left to serve it.

    One domain-wide registration shared by every account, so unloading one
    entry must not take it away from the others.
    """
    if not hass.services.has_service(DOMAIN, SERVICE_SET_HOLIDAY):
        return
    still_loaded = any(
        other.entry_id != config_entry.entry_id and is_still_serving(other)
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not still_loaded:
        hass.services.async_remove(DOMAIN, SERVICE_SET_HOLIDAY)
