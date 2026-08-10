"""
Switch platform for wemportal component
"""

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import WemPortalEntity
from .models import Reading
from .utils import fix_value_and_unit

_LOGGER = logging.getLogger(__name__)

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

    coordinator = config_entry.runtime_data.coordinator
    entities: list[WemPortalSwitch] = []
    for device_id, entity_data in coordinator.data.items():
        for unique_id, values in entity_data.items():
            if isinstance(values, Reading) and values.platform == "switch":
                entities.append(
                    WemPortalSwitch(
                        coordinator, config_entry, device_id, unique_id, values
                    )
                )

    async_add_entities(entities)


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
        try:
            temp_val = self.coordinator.data[self._device_id][self._data_key].value
            # Same distinction as in __init__: a key that is present but
            # carries no reading is "unknown", not "off". Guarding only the
            # constructor covered the very first cycle - the one case where
            # a missing reading is least likely - and left every later one
            # reporting a real switch-off to any automation watching it.
            self._attr_is_on = (
                None if temp_val is None else temp_val in WEM_SWITCH_ON_VALUES
            )

            _LOGGER.debug(
                'Update switch: %s: "%s" [%s]',
                self._attr_name,
                self._attr_is_on,
                self._attr_unit,
            )

        except KeyError:
            self._attr_is_on = None
            _LOGGER.warning("Can't find %s", self._attr_unique_id)
            _LOGGER.debug("Sensor data %s", self.coordinator.data)

        self.async_write_ha_state()
