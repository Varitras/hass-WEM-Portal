"""Date platform for wemportal component.

Holiday begin and end are dates, and the portal sends them as Unix epoch
seconds. They used to arrive here as switches, because the portal types them
the same way it types a real on/off parameter (DataType 2) and the only thing
telling the two apart - the declared bounds - was being filled in with a
guess. Toggling one of those switches would have written epoch 0 or 1 to the
heating system, i.e. a holiday starting in 1970.

The encoding was measured on a live installation, not assumed: begin and end
came back as 1785715200.0 and 1785801600.0, exactly 86400 apart and both
exactly on midnight UTC. Whole days, no time component - which is why this is
a date platform and not a datetime one.
"""

from datetime import UTC, date, datetime

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import _LOGGER
from .entity import WemPortalEntity


def epoch_to_date(value) -> date | None:
    """The portal's encoding as a date, or None when there is not one.

    None rather than a fallback date on purpose: "no holiday set" and "1st of
    January 1970" must not look the same in Home Assistant, and a fabricated
    date is one an automation would act on.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        # Out of range for the platform's clock - report nothing rather than
        # a wrong date.
        return None


def date_to_epoch(value: date) -> float:
    """The inverse, in the same encoding the portal sent: midnight UTC.

    Deliberately UTC and not local time. The measured values were exact
    multiples of 86400, so the portal means the calendar day itself; using the
    local midnight would shift every write by the UTC offset and, east of
    Greenwich, land the previous day.
    """
    return datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Date entry setup."""

    coordinator = config_entry.runtime_data.coordinator
    entities: list[WemPortalDate] = []
    for device_id, entity_data in coordinator.data.items():
        for unique_id, values in entity_data.items():
            if isinstance(values, int):
                continue
            # .get() instead of direct indexing: one malformed data point
            # should not crash setup for every date entity on this device.
            if values.get("platform") == "date":
                entities.append(
                    WemPortalDate(
                        coordinator, config_entry, device_id, unique_id, values
                    )
                )

    async_add_entities(entities)


class WemPortalDate(WemPortalEntity, DateEntity):
    """A portal parameter that carries a calendar day."""

    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        device_id,
        _unique_id,
        entity_data,
    ) -> None:
        """Initialize the date entity."""
        super().__init__(coordinator, config_entry, device_id, _unique_id, entity_data)

        self._attr_native_value = epoch_to_date(entity_data.get("value"))

        _LOGGER.debug("Init date: %s: %s", self._attr_name, self._attr_native_value)

    def _companion_dates(self) -> dict:
        """The other date parameters of this module, at their current value.

        A holiday is a range, and the portal appears to want the whole of it:
        begin and end are marked writeable and read back fine, but written one
        at a time each answer is Status -1 with no JobID, while an ordinary
        setpoint on the same account and the same endpoint is accepted. So the
        write carries the module's other dates along, unchanged.

        Found through the coordinator rather than by naming the two parameters
        literally: their ids are the portal's, and a rule that reads "the date
        parameters of this module" does not have to be revisited when an
        installation calls them something else.
        """
        companions = {}
        device = self.coordinator.data.get(self._device_id, {})
        for key, row in device.items():
            # Some rows are plain counters, not parameters - skip anything
            # that is not one, rather than assuming the shape.
            if not isinstance(row, dict) or key == self._data_key:
                continue
            if row.get("platform") != "date":
                continue
            if (row.get("ModuleIndex"), row.get("ModuleType")) != (
                self._module_index,
                self._module_type,
            ):
                continue
            try:
                companions[row.get("ParameterID", key)] = float(row.get("value"))
            except (TypeError, ValueError):
                # No readable value to repeat. Sending a guess would set a
                # date on the heating system that nobody asked for.
                continue
        return companions

    async def async_set_value(self, value: date) -> None:
        """Write a new day to the portal, then ask what it kept.

        A write that returns without raising means the portal ACCEPTED the
        request, not that it stored the value. Measured on a live
        installation: a holiday range that ends before it starts is answered
        with Status 0 and quietly discarded. Publishing the written date on
        the strength of that answer showed a holiday nobody had - until the
        next poll took it away again, minutes later and with no explanation.

        The service that writes both dates at once refuses such a pair up
        front. A single date cannot: which of the module's dates is the begin
        and which the end is only known from parameter ids that differ per
        installation, and guessing them is how this platform's predecessor
        turned two dates into switches. So instead of deciding what the
        portal will accept, this asks it afterwards.

        Costs one refresh-and-read of this device. Dates are changed by hand
        a few times a year, not once a cycle, and the alternative is
        reporting a setting that did not happen.
        """
        await self.async_write_parameter(
            date_to_epoch(value), together_with=self._companion_dates()
        )

        failure = await self.hass.async_add_executor_job(
            self.coordinator.api.reread_device_values, self._device_id
        )
        if failure is not None:
            # The write itself went through, so this must not be raised as a
            # failed service call. What is unknown is whether it was KEPT -
            # and an unknown answer is not the written day. Forgetting the
            # recorded value leaves the entity showing nothing until the next
            # poll answers the question properly.
            _LOGGER.warning(
                'Wrote %s to "%s" but could not read the value back (%s). '
                "It is shown as unknown until the next update says what the "
                "portal actually stored.",
                value,
                self._attr_name,
                failure,
            )
            self._forget_written_value()
        self._attr_native_value = epoch_to_date(self._current_value())
        self.coordinator.async_update_listeners()

    def _current_value(self):
        """This parameter's value as the coordinator now holds it."""
        row = self._coordinator_row()
        return row.get("value") if row is not None else None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        try:
            self._attr_native_value = epoch_to_date(
                self.coordinator.data[self._device_id][self._data_key]["value"]
            )
            _LOGGER.debug(
                "Update date: %s: %s", self._attr_name, self._attr_native_value
            )
        except KeyError:
            self._attr_native_value = None
            _LOGGER.warning("Can't find %s", self._attr_unique_id)
            _LOGGER.debug("Sensor data %s", self.coordinator.data)

        self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        attributes = {}
        if self._last_updated is not None:
            attributes["Last Updated"] = self._last_updated
        return attributes
