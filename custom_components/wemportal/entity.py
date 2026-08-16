"""Shared base class for the WEM Portal entity platforms."""

from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import get_wemportal_unique_id
from .coordinator import API_FAILURES_TOLERATED, WemPortalDataUpdateCoordinator
from .models import Reading, raise_if_not_writable
from .utils import build_device_info, device_is_reachable, device_model


def _readings_of(data):
    """Every (device id, key, reading) the coordinator holds.

    Not every entry is a reading - the raw ConnectionStatus travels in the
    same dict as a plain int - so the isinstance test is the filter, not a
    precaution.

    Unfiltered by platform on purpose: the caller has to see the rows that
    are NOT its own, because a row leaving this platform is what tells it to
    forget the row.
    """
    for device_id, rows in (data or {}).items():
        for key, reading in rows.items():
            if isinstance(reading, Reading):
                yield device_id, key, reading


@callback
def async_add_readings_as_they_appear(
    config_entry: ConfigEntry, async_add_entities, platform: str, build
) -> None:
    """Give every reading of one platform an entity - now and on later cycles.

    Each platform used to walk the coordinator's data once, during setup, and
    never look again. Four ordinary situations produce a reading only on a
    LATER cycle: a device that was unreachable at startup (get_parameters
    skips it), the parameter re-discovery that deliberately waits for the
    second cycle, the hourly statistics whose first attempt failed - those are
    the Energy Dashboard rows - and the scrape half of `both` mode. Every one
    of them ended as coordinator data no entity ever rendered, until somebody
    reloaded the entry by hand. There is no update listener that would have
    done it for them; the flows reload explicitly and nothing else does.

    Adding only. A reading that goes away leaves its entity showing unknown,
    which is a state a user can read and act on - removing the entity would
    take its history with it for what is often one bad cycle.
    """
    coordinator = config_entry.runtime_data.coordinator
    # What this platform currently HAS an entity for - not what it has ever
    # seen. The difference decides whether a control survives a flicker: a
    # parameter's platform is read from the value of that cycle, so one odd
    # answer moves a row to `sensor` and the next one moves it back, and the
    # migration takes the abandoned registry entry down in between. An
    # add-only memo then never rebuilt it, and the control stayed gone until
    # the entry was reloaded.
    known: set[tuple[str, str]] = set()

    @callback
    def _add_the_ones_without_an_entity() -> None:
        fresh = []
        for device_id, key, reading in _readings_of(coordinator.data):
            if reading.platform != platform:
                # Deliberately NOT the same as "the row is gone". A row that
                # is merely absent keeps its place, because its entity is
                # still registered and showing unknown; only a row that has
                # gone to another platform loses one, because that is the
                # case where the entity is taken away.
                known.discard((device_id, key))
                continue
            if (device_id, key) in known:
                continue
            known.add((device_id, key))
            fresh.append(build(coordinator, config_entry, device_id, key, reading))
        if fresh:
            async_add_entities(fresh)

    _add_the_ones_without_an_entity()
    # Removed on unload with everything else: a listener that outlives its
    # entry keeps building entities for a coordinator nobody reads.
    config_entry.async_on_unload(
        coordinator.async_add_listener(_add_the_ones_without_an_entity)
    )


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
        # What this entity was built as. The re-discovery can reclassify a
        # parameter, and the listener then builds an entity of the new
        # platform for the same row - so "the row is still there" stopped
        # being the same question as "the row is still mine".
        self._platform = entity_data.platform

    async def async_write_parameter(self, value, together_with=None) -> None:
        """The one way an entity changes a value on the portal.

        Number, Select and Switch each had their own copy of this call, and
        none of them asked whether the entry was still there - so a click
        that landed while the entry was unloading started a write into a
        session that was about to be closed.

        `together_with` names further parameters of the same module to send in
        the same request. Only the date platform uses it, for a parameter the
        portal will not accept on its own - see WemPortalApi._change_value.

        The second gate is the reading itself. An entity outlives the row it
        was built from: the module address it writes to was taken at
        construction, and a parameter the portal stops describing has its
        reading removed while the entity stays until the next reload. Writing
        then addressed something that is not there any more - which only
        became reachable when dropping those readings did, and would have
        been the same silent wrong write either way.
        """
        what = self._attr_name or str(self._attr_unique_id)
        raise_if_not_writable(self._config_entry, what)
        if self._coordinator_row() is None:
            raise HomeAssistantError(
                f"{what}: the portal no longer answers for this parameter, so "
                "the address this entity would write to may not mean anything "
                "any more. Nothing was written; the entity starts working "
                "again by itself once the parameter is back."
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

        Every caller asks the same two-step question - the device, then the
        parameter - and guards against the answer not being a reading,
        because a malformed or half-built update must not raise into a write
        path.

        The platform check is what makes "the row is there" and "the row is
        mine" two different questions, and it belongs to the DISPLAY path as
        much as to the write path: after a reclassification both entities are
        loaded and both find the row, so an update handler doing its own
        lookup rendered someone else's value as its own type.
        """
        device = (self.coordinator.data or {}).get(self._device_id)
        row = device.get(self._data_key) if isinstance(device, dict) else None
        if not isinstance(row, Reading) or row.platform != self._platform:
            return None
        return row

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

        The failure count alone, and not `last_update_success` beside it: any
        successful cycle sets the count back to zero, so the flag added
        nothing - except that it read as success while the cycle that just
        failed was still on its way out of the coordinator, which is exactly
        when the crossing of the tolerance is published.
        """
        return self.coordinator.num_failed <= API_FAILURES_TOLERATED
