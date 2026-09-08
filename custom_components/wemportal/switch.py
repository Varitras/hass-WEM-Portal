"""
Switch platform for wemportal component
"""

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import async_add_readings_as_they_appear, WemPortalEntity
from .utils import fix_value_and_unit

_LOGGER = logging.getLogger(__name__)

# Zero is unlimited, and that is what a coordinator platform already does:
# the readings come from one shared refresh, so no entity fetches on its own.
PARALLEL_UPDATES = 0

# Recognized "on" values, covering both the numeric form (API path) and the
# German/English text forms a value may arrive in (e.g. depending on the
# configured portal language, or whether it came via web scraping vs. the
# API). Previously only `1.0`/`"on"` (lowercase) were recognized, which
# meant a switch reporting "Ein" or "On" (capitalized) would silently and
# incorrectly show as "off" in Home Assistant.
WEM_SWITCH_ON_VALUES = (1, 1.0, "On", "on", "Ein", "ein")


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Switch entry setup."""

    async_add_readings_as_they_appear(
        config_entry, async_add_entities, "switch", WemPortalSwitch
    )


class WemPortalSwitch(WemPortalEntity, SwitchEntity):
    """Representation of a WEM Portal Sensor."""

    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        device_id,
        _unique_id,
        entity_data,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id, _unique_id, entity_data)

        value, unit = fix_value_and_unit(entity_data.value, entity_data.unit)

        self._attr_unit = unit
        # None means "no reading this cycle", which is not the same as
        # off: `None in WEM_SWITCH_ON_VALUES` is False, so a missing value
        # used to look like a real state change to any automation.
        self._attr_is_on = None if value is None else value in WEM_SWITCH_ON_VALUES
        self._attr_device_class = SwitchDeviceClass.SWITCH

        _LOGGER.debug(
            'Init switch: %s: "%s" [%s]',
            self._attr_name,
            self._attr_is_on,
            self._attr_unit,
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self.async_write_parameter(1.0)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.async_write_parameter(0.0)
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        row = self._coordinator_row()
        if row is None:
            self._attr_is_on = None
            self._report_no_reading()
            self.async_write_ha_state()
            return

        # Same distinction as in __init__: a key that is present but carries
        # no reading is "unknown", not "off". Guarding only the constructor
        # covered the very first cycle - the one case where a missing reading
        # is least likely - and left every later one reporting a real
        # switch-off to any automation watching it.
        self._attr_is_on = (
            None if row.value is None else row.value in WEM_SWITCH_ON_VALUES
        )
        _LOGGER.debug(
            'Update switch: %s: "%s" [%s]',
            self._attr_name,
            self._attr_is_on,
            self._attr_unit,
        )
        self.async_write_ha_state()
