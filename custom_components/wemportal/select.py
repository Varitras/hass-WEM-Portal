"""
Select platform for wemportal component
"""

import logging

import difflib
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BOOLEAN_OFF_STRINGS, BOOLEAN_ON_STRINGS
from .entity import async_add_readings_as_they_appear, WemPortalEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Select entry setup."""

    async_add_readings_as_they_appear(
        config_entry, async_add_entities, "select", WemPortalSelect
    )


class WemPortalSelect(WemPortalEntity, SelectEntity):
    """Representation of a WEM Portal Sensor."""

    def _match_boolean_synonym(self, value):
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

        Returns the matching option name, or None if `value` isn't a
        recognized on/off synonym at all (so the caller can fall through
        to fuzzy matching for genuinely different kinds of mismatches).
        """
        if not isinstance(value, str):
            return None
        value_lower = value.strip().lower()
        if value_lower in BOOLEAN_OFF_STRINGS:
            synonyms = BOOLEAN_OFF_STRINGS
        elif value_lower in BOOLEAN_ON_STRINGS:
            synonyms = BOOLEAN_ON_STRINGS
        else:
            return None
        for option_name in self._options_names:
            if isinstance(option_name, str) and option_name.strip().lower() in synonyms:
                return option_name
        return None

    def _resolve_option(self, value):
        """Resolve a raw coordinator value to one of this select's option names.

        Tries, in order: exact match against option names, exact match
        against option values, integer-coerced match against option
        values, a language-independent on/off synonym match, and finally
        fuzzy string matching as a last resort. Raises ValueError/TypeError
        if none of these succeed, matching the previous per-callsite
        behavior so existing exception handling keeps working unchanged.

        A MISSING reading is the exception: it returns None rather than
        raising, because it is not a value this can fail to resolve. See the
        guard below.
        """
        # No reading this cycle is expected, not invalid. The portal
        # regularly answers nothing for a parameter, and _clear_unanswered
        # deliberately blanks one the portal left out so a stale reading is
        # not published as current - so the warning this used to raise
        # reported the integration's own bookkeeping as a fault, with all 49
        # option names attached to say "no value". Same rule sensor.py states
        # for its own readings. Genuinely unusable values still raise below.
        if value is None:
            _LOGGER.debug('No value for "%s" this cycle -> unknown', self._attr_name)
            return None

        if value in self._options_names:
            return value
        if value in self._options:
            return self._options_names[self._options.index(value)]
        try:
            return self._options_names[self._options.index(int(value))]
        except ValueError, TypeError:
            pass

        synonym_match = self._match_boolean_synonym(value)
        if synonym_match is not None:
            return synonym_match

        if value is not None and self._options_names:
            # Last-resort fuzzy match against the option names. Uses stdlib
            # difflib (no external dependency); its 0.0-1.0 ratio cutoff of
            # 0.75 mirrors the previous fuzzywuzzy score threshold of 75.
            # Only string option names can be compared, so non-str options
            # are filtered out first.
            str_names = [
                option_name
                for option_name in self._options_names
                if isinstance(option_name, str)
            ]
            matches = difflib.get_close_matches(str(value), str_names, n=1, cutoff=0.75)
            if matches:
                return matches[0]
        raise ValueError

    def __init__(
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id, _unique_id, entity_data)
        self._options: list[Any] = entity_data.options or []
        self._options_names: list[Any] = entity_data.options_names or []

        try:
            self._attr_current_option = self._resolve_option(entity_data.value)
        except ValueError, TypeError:
            self._attr_current_option = None
            _LOGGER.warning(
                "Value %s not found in options %s (names: %s) for select %s",
                entity_data.value,
                self._options,
                self._options_names,
                self._attr_name,
            )
        _LOGGER.debug(
            'Init select: %s: "%s"', self._attr_name, self._attr_current_option
        )

    def _portal_value_for(self, option: str) -> Any:
        """The portal value the chosen name stands for.

        Found by PAIRING the two lists, not by the position of the first
        matching name. The portal decides what an EnumValues list looks
        like, and two entries sharing a display name made that position a
        coin toss: choosing the second one wrote the first one's value into
        the heating system, while the entity went on showing the name that
        was clicked - nothing in the log, nothing in the state.

        A set, so two entries that agree on both name and value are still
        one answer; it is only a name meaning two different values that
        cannot be resolved.

        Refusing is the honest answer for both of the ways this fails: which
        of two values the user meant is not knowable from what the portal
        sent, and a name with no value paired to it (the two lists are
        refreshed under separate guards) must not fall back to whatever
        happens to sit at that index.
        """
        candidates = {
            value
            for value, name in zip(self._options, self._options_names, strict=False)
            if name == option
        }
        if len(candidates) != 1:
            raise HomeAssistantError(
                f'Cannot set "{self._attr_name}" to "{option}": the portal '
                f"offers {len(candidates)} values under that name, so which "
                "one was meant is not decidable. Nothing was written."
            )
        return candidates.pop()

    async def async_select_option(self, option: str) -> None:
        """Call the API to change the parameter value"""
        await self.async_write_parameter(self._portal_value_for(option))

        self._attr_current_option = option

        self.async_write_ha_state()

    @property
    def options(self) -> list[str]:
        """Return list of available options."""
        return self._options_names

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        row = self._coordinator_row()
        if row is None:
            self._attr_current_option = None
            self._report_no_reading()
            self.async_write_ha_state()
            return

        # Options BEFORE the value, for the same reason number refreshes its
        # bounds: rediscovery can add an option, and a device already ON it
        # read as unknown against the construction-time list -
        # indistinguishable from a failed read.
        if row.options is not None:
            self._options = row.options
        if row.options_names is not None:
            self._options_names = row.options_names
        value = row.value
        try:
            self._attr_current_option = self._resolve_option(value)
        except ValueError, TypeError:
            self._attr_current_option = None
            _LOGGER.warning(
                "Value %s not found in options %s (names: %s) for select %s",
                value,
                self._options,
                self._options_names,
                self._attr_name,
            )

        self.async_write_ha_state()
