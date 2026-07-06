"""
Select platform for wemportal component
"""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, _LOGGER, BOOLEAN_OFF_STRINGS, BOOLEAN_ON_STRINGS
from . import get_wemportal_unique_id
from fuzzywuzzy import process


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Select entry setup."""

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities: list[WemPortalSelect] = []
    for device_id, entity_data in coordinator.data.items():
        for unique_id, values in entity_data.items():
            if isinstance(values, int):
                continue
            # .get() instead of direct indexing: one malformed data point
            # should not crash setup for every select entity on this device.
            if values.get("platform") == "select":
                entities.append(
                    WemPortalSelect(
                        coordinator, config_entry, device_id, unique_id, values
                    )
                )

    async_add_entities(entities)


class WemPortalSelect(CoordinatorEntity, SelectEntity):
    """Representation of a WEM Portal Sensor."""

    def _match_boolean_synonym(self, val):
        """Match common German/English on-off synonyms across languages.

        The WEM Portal API can return a live value ("Off"/"On") in a
        different language than the option names discovered from the
        parameter's EnumValues definition ("Aus"/"Ein"), or vice versa -
        this has been observed to happen independently of which language
        is configured for this integration. Plain fuzzy string matching
        (see below) can't bridge that gap, since e.g. "Off" and "Aus"
        share no common letters and score far too low to be considered a
        match. This explicitly recognizes both spellings as the same
        logical state and maps to whichever one is actually present in
        this entity's own option list.

        Returns the matching option name, or None if `val` isn't a
        recognized on/off synonym at all (so the caller can fall through
        to fuzzy matching for genuinely different kinds of mismatches).
        """
        if not isinstance(val, str):
            return None
        val_lower = val.strip().lower()
        if val_lower in BOOLEAN_OFF_STRINGS:
            synonyms = BOOLEAN_OFF_STRINGS
        elif val_lower in BOOLEAN_ON_STRINGS:
            synonyms = BOOLEAN_ON_STRINGS
        else:
            return None
        for option_name in self._options_names:
            if isinstance(option_name, str) and option_name.strip().lower() in synonyms:
                return option_name
        return None

    def _resolve_option(self, val):
        """Resolve a raw coordinator value to one of this select's option names.

        Tries, in order: exact match against option names, exact match
        against option values, integer-coerced match against option
        values, a language-independent on/off synonym match, and finally
        fuzzy string matching as a last resort. Raises ValueError/TypeError
        if none of these succeed, matching the previous per-callsite
        behavior so existing exception handling keeps working unchanged.
        """
        if val in self._options_names:
            return val
        if val in self._options:
            return self._options_names[self._options.index(val)]
        try:
            return self._options_names[self._options.index(int(val))]
        except (ValueError, TypeError):
            pass

        synonym_match = self._match_boolean_synonym(val)
        if synonym_match is not None:
            return synonym_match

        if val is not None and self._options_names:
            best_match, score = process.extractOne(str(val), self._options_names)
            if score >= 75:
                return best_match
        raise ValueError

    def __init__(
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the sensor."""
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
        # unexpected/malformed data point should degrade gracefully
        # (skip this one entity's optional metadata) instead of raising a
        # KeyError that would abort setup for every select entity on this
        # device.
        self._parameter_id = entity_data.get("ParameterID", _unique_id)
        self._data_key = _unique_id
        self._attr_icon = entity_data.get("icon", "mdi:flash")
        self._options = entity_data.get("options", [])
        self._options_names = entity_data.get("optionsNames", [])
        self._module_index = entity_data.get("ModuleIndex")
        self._module_type = entity_data.get("ModuleType")

        try:
            self._attr_current_option = self._resolve_option(entity_data.get("value"))
        except (ValueError, TypeError):
            self._attr_current_option = None
            _LOGGER.warning("Value %s not found in options %s (names: %s) for select %s", entity_data.get("value"), self._options, self._options_names, self._attr_name)
        _LOGGER.debug('Init select: %s: "%s"', self._attr_name, self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        """Call the API to change the parameter value"""
        await self.hass.async_add_executor_job(
            self.coordinator.api.change_value,
            self._device_id,
            self._parameter_id,
            self._module_index,
            self._module_type,
            self._options[self._options_names.index(option)],
        )

        self._attr_current_option = option

        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return {
            "identifiers": {
                (DOMAIN, f"{self._config_entry.entry_id}:{self._device_id}")
            },
            "via_device": (DOMAIN, self._config_entry.entry_id),
            "name": str(self._device_id),
            "manufacturer": "Weishaupt",
        }

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def options(self) -> list[str]:
        """Return list of available options."""
        return self._options_names

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        attr = {}
        if self._last_updated is not None:
            attr["Last Updated"] = self._last_updated
        return attr

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        try:
            val = self.coordinator.data[self._device_id][self._data_key]["value"]
            self._attr_current_option = self._resolve_option(val)
        except KeyError:
            self._attr_current_option = None
            _LOGGER.warning("Can't find %s", self._attr_unique_id)
            _LOGGER.debug("Sensor data %s", self.coordinator.data)
        except (ValueError, TypeError):
            self._attr_current_option = None
            _LOGGER.warning("Value %s not found in options %s (names: %s) for select %s", val, self._options, self._options_names, self._attr_name)

        self.async_write_ha_state()
