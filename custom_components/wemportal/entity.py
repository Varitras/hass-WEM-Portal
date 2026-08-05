"""Shared base class for the WEM Portal entity platforms."""

from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import get_wemportal_unique_id
from .models import raise_if_not_writable
from .utils import build_device_info, device_is_reachable, device_model


class WemPortalEntity(CoordinatorEntity):
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
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the shared entity state."""
        super().__init__(coordinator)
        self._last_updated = None
        self._config_entry = config_entry
        self._device_id = device_id
        self._attr_has_entity_name = True
        self._attr_name = entity_data.get("friendlyName", _unique_id)
        self._attr_unique_id = get_wemportal_unique_id(
            self._config_entry.entry_id, str(self._device_id), str(_unique_id)
        )
        # .get() with sensible fallbacks rather than direct indexing: an
        # unexpected/malformed data point should degrade gracefully (skip
        # this one entity's optional metadata) instead of raising a KeyError
        # that would abort setup for every entity on this device.
        self._parameter_id = entity_data.get("ParameterID", _unique_id)
        self._data_key = _unique_id
        # Only when the data carries one: an explicit icon overrides the one
        # Home Assistant derives from the device class.
        icon = entity_data.get("icon")
        if icon:
            self._attr_icon = icon
        # Only the writeable platforms use these, but the write path below is
        # shared, so the address of the parameter is too.
        self._module_index = entity_data.get("ModuleIndex")
        self._module_type = entity_data.get("ModuleType")

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
        raise_if_not_writable(self._config_entry, self._attr_name)
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

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return build_device_info(
            self._config_entry.entry_id, self._device_id,
            model=device_model(self.coordinator.api, self._device_id),
        )

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success and device_is_reachable(
            self.coordinator.data, self._device_id
        )
