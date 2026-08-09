"""Shared base class for the WEM Portal entity platforms."""

from typing import Final
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import get_wemportal_unique_id
from .coordinator import WemPortalDataUpdateCoordinator
from .models import Reading, raise_if_not_writable
from .utils import build_device_info, device_is_reachable, device_model

# The same thought on the API side, which had none: one failed cycle used to
# take every entity of the account unavailable at once. The portal answers a
# cycle with "Unbekannter Fehler" now and then and the next one succeeds, so a
# single failure says nothing - but at the default interval it costs half an
# hour of every graph and sends automations a state change on the way out and
# back.
#
# One rather than the scrape's three, because an API cycle is the expensive
# one: at the default interval three failures is an hour and a half of
# readings presented as current. The counter is the coordinator's own, reset
# by any successful cycle.
API_FAILURES_TOLERATED: Final = 1


class WemPortalEntity(CoordinatorEntity[WemPortalDataUpdateCoordinator]):
    """What the date, number, select, sensor and switch platforms share.

    Every entity is one row of the coordinator's data, addressed by device id
    and key. Identity, the device it belongs to and the availability rule are
    the same for every platform; only what each does with the row's value
    differs. Keeping them here means a fix lands once instead of four times -
    the diagnostic-availability rule below was fixed in three of four places
    once already.

    `should_poll` is deliberately not set: CoordinatorEntity answers it with a
    property, so the `_attr_should_poll = False` three of the platforms used
    to carry was never read.
    """

    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        device_id,
        _unique_id,
        entity_data: Reading,
    ) -> None:
        """Initialize the shared entity state."""
        super().__init__(coordinator)
        self._last_updated = None
        self._config_entry = config_entry
        self._device_id = device_id
        self._attr_has_entity_name = True
        self._attr_name = (
            entity_data.friendly_name
            if entity_data.friendly_name is not None
            else _unique_id
        )
        self._attr_unique_id = get_wemportal_unique_id(
            self._config_entry.entry_id, str(self._device_id), str(_unique_id)
        )
        self._parameter_id = (
            entity_data.parameter_id
            if entity_data.parameter_id is not None
            else _unique_id
        )
        self._data_key = _unique_id
        # Only when the data carries one: an explicit icon overrides the one
        # Home Assistant derives from the device class.
        if entity_data.icon:
            self._attr_icon = entity_data.icon
        # Only the writeable platforms use these, but the write path below is
        # shared, so the address of the parameter is too.
        self._module_index = entity_data.module_index
        self._module_type = entity_data.module_type

    async def async_write_parameter(self, value, together_with=None) -> None:
        """The one way an entity changes a value on the portal.

        Number, Select and Switch each had their own copy of this call, and
        none of them asked whether the entry was still there - so a click
        that landed while the entry was unloading started a write into a
        session that was about to be closed.

        `together_with` names further parameters of the same module to send in
        the same request. Only the date platform uses it, for a parameter the
        portal will not accept on its own - see WemPortalApi._change_value.
        """
        raise_if_not_writable(
            self._config_entry, self._attr_name or str(self._attr_unique_id)
        )
        await self.hass.async_add_executor_job(
            partial(
                self.coordinator.api.change_value,
                self._device_id,
                self._parameter_id,
                self._module_index,
                self._module_type,
                value,
                together_with=together_with,
            )
        )
        self._record_written_value(value)

    def _record_written_value(self, value) -> None:
        """Bring the coordinator's copy up to date with what was just written.

        Each platform already updates its OWN displayed value after a write.
        The coordinator's row keeps the value from the last poll, which is
        minutes old, and something reads it in between: the date platform
        sends the module's other dates along with a write and takes them from
        here. Measured on a live installation - two writes four seconds
        apart, and the second carried a begin date the first had already
        replaced, asking the portal to undo it.

        Only after the write returned. change_value raises when the portal
        refuses, so a rejected value never lands here - recording it would
        make the integration certain of something the heating system never
        accepted.
        """
        row = self._coordinator_row()
        if row is not None:
            row.value = value

    def _forget_written_value(self) -> None:
        """Take back a value nobody could confirm was kept.

        The counterpart to the above, for the case the read-back exists to
        catch and then cannot: the portal ACCEPTED the write, so the value
        was recorded, but the read that checks whether it was actually stored
        did not come back. Leaving the written value in the row would publish
        it as verified, which is the one claim the read-back is there to
        avoid - and the row is what the entity reads on every update, so it
        would return at the next poll even if the entity blanked itself.

        Only the value goes, as on every other path that does this: unit,
        name and icon stay, so the entity keeps its identity.
        """
        row = self._coordinator_row()
        if row is not None:
            row.value = None

    def _coordinator_row(self) -> Reading | None:
        """This parameter's row in the coordinator's data, or None.

        Three callers ask the same two-step question - the device, then the
        parameter - and each guards against the answer not being a reading,
        because a malformed or half-built update must not raise into a write
        path.
        """
        device = (self.coordinator.data or {}).get(self._device_id)
        row = device.get(self._data_key) if isinstance(device, dict) else None
        return row if isinstance(row, Reading) else None

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        info: DeviceInfo = build_device_info(
            self._config_entry.entry_id,
            self._device_id,
            model=device_model(self.coordinator.api, self._device_id),
        )
        return info

    @property
    def available(self):
        """Return if entity is available.

        One failed cycle is not an outage. The portal answers with
        "Unbekannter Fehler" now and then and the next cycle succeeds, and
        taking every entity of the account unavailable for that costs half an
        hour of every graph at the default interval - plus a state change out
        and back for anything automating on it.

        The readings themselves are unaffected: a device that stops answering
        still has its values aged out, and the count resets on any successful
        cycle, so a real outage still shows within two.
        """
        return self._cycle_is_worth_showing() and device_is_reachable(
            self.coordinator.data, self._device_id
        )

    def _cycle_is_worth_showing(self) -> bool:
        """Whether the last cycle's outcome allows showing anything at all.

        Its own method because `available` is overridden where a platform has
        a second rule of its own - the sensor platform keeps its three
        diagnostic entities alive for an unreachable device - and an override
        that reimplements this half is how the tolerance came to apply to
        numbers but not to sensors.
        """
        return (
            self.coordinator.last_update_success
            or self.coordinator.num_failed <= API_FAILURES_TOLERATED
        )
