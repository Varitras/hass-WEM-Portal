"""
Sensor platform for wemportal component
"""

import json
import re

from homeassistant.components.sensor import RestoreSensor
from homeassistant.config_entries import ConfigEntry

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import MAX_LENGTH_STATE_STATE, EntityCategory

from .const import _LOGGER
from .utils import (device_is_reachable, device_model, fix_value_and_uom, uom_to_device_class, uom_to_state_class, build_device_info)
from .entity import WemPortalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sensor entry setup."""

    coordinator = config_entry.runtime_data.coordinator
    entities: list[WemPortalSensor] = []
    for device_id, entity_data in coordinator.data.items():
        for unique_id, values in entity_data.items():
            if isinstance(values, int):
                continue
            # Use .get() rather than values["platform"] here: if a single
            # data point is ever missing this key (e.g. an unexpected API
            # response shape), we want to skip just that one entry instead
            # of raising a KeyError that would abort setup for every
            # sensor on this device.
            if values.get("platform") == "sensor":
                entities.append(
                    WemPortalSensor(
                        coordinator, config_entry, device_id, unique_id, values
                    )
                )
    async_add_entities(entities)


# One of these payloads carries THREE kinds of key, and only the first is a
# time window:
#
#   "MO-1": "00:00-24:00"     the n-th window of a day
#   "MO":   "HLL"             one letter per window, in the same order
#   "zone", "type", "mode", "cmd", "status", "TransferId"
#                             how the programme was transferred - not the
#                             programme
#
# Splitting every key on "-" and keeping the first part put all three in one
# basket: the letters were rendered as a period of Monday, and the transfer
# fields became weekdays of their own. What the letters MEAN is not
# established - only that they line up with the windows by position - so
# they are passed through and not interpreted.
_WINDOW_KEY = re.compile(r"^(?P<day>.+)-(?P<slot>\d+)$")

# How the portal spells a slot that is not in use.
_UNUSED_WINDOW = "00:00-00:00"


def _parse_schedule(raw):
    """A weekly programme as day -> [(window, mark), ...], or None.

    Days come back in the order the portal sent them, which is also the order
    of the week - so nothing here has to know what a week looks like, or
    which language the day abbreviations are in.

    Returns None when the value is not a schedule after all. What is built
    from this is decoration around a reading: getting it wrong must never
    cost the reading itself.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    windows = {}
    marks = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        match = _WINDOW_KEY.match(key)
        if match:
            windows.setdefault(match["day"], {})[int(match["slot"])] = value
        else:
            marks[key] = value

    schedule = {}
    for day, slots in windows.items():
        # Looked up only for days that actually HAVE windows, which is what
        # keeps "zone" and "status" from becoming days of the week.
        letters = marks.get(day, "")
        periods = []
        for slot in sorted(slots):
            period = slots[slot].strip()
            if period in ("", _UNUSED_WINDOW):
                continue
            # By position, and only where there is one to take: a payload
            # whose letters do not line up must not silently borrow the
            # wrong one.
            letter = letters[slot - 1] if 0 < slot <= len(letters) else ""
            periods.append((period, letter))
        if periods:
            schedule[day] = periods
    return schedule or None


def _readable_schedule(raw):
    """The attribute: every day with its windows, marks included."""
    schedule = _parse_schedule(raw)
    if schedule is None:
        return None
    return {
        day: [f"{period} ({mark})" if mark else period for period, mark in periods]
        for day, periods in schedule.items()
    }


def _schedule_summary(raw):
    """The state: the whole week on one line, or None.

    "Programmed" was the entire state of these sensors, which says only that
    the parameter exists - the times were reachable through an attribute and
    nowhere else. Consecutive days with the same windows are collapsed, so
    the common case reads "MO-SO 00:00-24:00" instead of seven repetitions.

    The marks are deliberately left out here. They are one cryptic letter
    whose meaning is not established, and a state has to survive a week of
    three windows a day inside Home Assistant's length limit.
    """
    schedule = _parse_schedule(raw)
    if schedule is None:
        return None

    groups = []
    for day, periods in schedule.items():
        times = ", ".join(period for period, _mark in periods)
        if groups and groups[-1][2] == times:
            groups[-1][1] = day
        else:
            groups.append([day, day, times])
    return "; ".join(
        f"{first} {times}" if first == last else f"{first}-{last} {times}"
        for first, last, times in groups
    )


class WemPortalSensor(WemPortalEntity, RestoreSensor):
    """Representation of a WEM Portal Sensor."""

    def _validated_native_value(self, val, uom):
        """Return a Home Assistant-safe native value."""
        effective_uom = uom
        if effective_uom in (None, ""):
            effective_uom = getattr(self, "_attr_native_unit_of_measurement", None)
        # A sensor is "numeric" if it has a real unit OR if it's tagged
        # with a device_class/state_class that requires a numeric state
        # (Home Assistant enforces this - see the entity's own state
        # property). Checking device_class/state_class too, not just
        # uom, closes a gap where fix_value_and_uom() can legitimately
        # return an empty/None uom for a given reading (e.g. a boolean
        # placeholder string with no unit attached) even though the
        # entity itself is declared as a numeric power/energy/etc.
        # sensor - which would otherwise let a non-numeric string like
        # "Off" slip through uncaught and crash entity setup entirely.
        is_numeric_sensor = (
            effective_uom not in (None, "")
            or getattr(self, "_attr_device_class", None) is not None
            or getattr(self, "_attr_state_class", None) is not None
        )

        # A MISSING reading is expected, not invalid: the portal regularly
        # reports "--" (or an empty string) for a parameter it has no current
        # value for, and that is deliberately mapped to None so the sensor
        # shows as unknown instead of a fabricated 0.0. Logging this at
        # warning level (as an "invalid value") made a normal condition look
        # like a defect and drowned out real problems - so it is debug.
        # Genuinely un-coercible values are still warned about below.
        #
        # "unknown", not "unavailable": those are different states, and the
        # log used to name the wrong one. A None native value is an available
        # entity with no reading; unavailable comes only from `available`
        # below, i.e. a failed cycle or an unreachable device. Saying
        # "unavailable" here sent a reader looking for a fault in the wrong
        # half of the integration.
        if val is None:
            _LOGGER.debug('No value for "%s" this cycle -> unknown', self._attr_name)
            return None

        if isinstance(val, str):
            val = val.strip()
            if val == "":
                _LOGGER.debug('Empty value for "%s" this cycle -> unknown', self._attr_name)
                return None
            if val.startswith("{"):
                summary = _schedule_summary(val)
                # Home Assistant refuses a state longer than this, and a
                # refused state is no reading at all. Seven days that differ
                # from one another, three windows each, can get there. The
                # bare word is worse than the summary and better than
                # nothing - and the Schedule attribute keeps every window
                # either way.
                if summary and len(summary) <= MAX_LENGTH_STATE_STATE:
                    return summary
                return "Programmed"

        if is_numeric_sensor:
            try:
                float(val)
            except (TypeError, ValueError):
                _LOGGER.warning('Invalid numeric sensor value for "%s": %r -> set to None', self._attr_name, val)
                return None

        return val

    def __init__(
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id, _unique_id, entity_data)

        # .get() like the other platforms: one malformed data point must not
        # abort setup for every sensor on this device with a KeyError.
        val, uom = fix_value_and_uom(entity_data.get("value"), entity_data.get("unit"))

        self._attr_native_unit_of_measurement = uom
        # Set device_class/state_class BEFORE validating the native value:
        # _validated_native_value() uses them (in addition to uom) to
        # decide whether a numeric value is required, so they must already
        # be in place the first time it runs, not just on later updates.
        self._attr_device_class = entity_data.get("device_class")
        self._attr_state_class = entity_data.get("state_class")
        self._attr_native_value = self._validated_native_value(val, uom)

        _LOGGER.debug(
            'Init sensor: %s: "%s" [%s]',
            self._attr_name,
            self._attr_native_value,
            self._attr_native_unit_of_measurement
        )

    async def async_added_to_hass(self) -> None:
        """Restore the unit of measurement from the last known state, if needed.

        On a fresh Home Assistant restart, the very first coordinator
        update might briefly report a value without a unit (e.g. "--" from
        the portal). Without this, that would flash the sensor's unit as
        blank/unknown for one cycle. RestoreSensor lets us fall back to
        whatever unit was last recorded, avoiding that.
        """
        await super().async_added_to_hass()
        if self._attr_native_unit_of_measurement in (None, ""):
            last_sensor_data = await self.async_get_last_sensor_data()
            if last_sensor_data is not None and last_sensor_data.native_unit_of_measurement:
                self._attr_native_unit_of_measurement = last_sensor_data.native_unit_of_measurement
                _LOGGER.debug(
                    "Restored unit %s for %s from previous session",
                    self._attr_native_unit_of_measurement,
                    self._attr_name,
                )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        sw_version = None
        if hasattr(self.coordinator.api, "api_version") and self.coordinator.api.api_version:
            sw_version = self.coordinator.api.api_version
        return build_device_info(
            self._config_entry.entry_id, self._device_id, sw_version=sw_version,
            model=device_model(self.coordinator.api, self._device_id),
        )

    @property
    def available(self):
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        # The diagnostic sensors stay available even for an unreachable
        # device: they are what explains WHY everything else went away.
        # Matched on the parameter id rather than as a substring of the
        # unique_id, which also carries the entry id and the device id.
        if self._parameter_id in ("ConnectionStatus", "HasErrors", "ErrorMessages"):
            return True
        return device_is_reachable(self.coordinator.data, self._device_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        try:

            entity_data = self.coordinator.data[self._device_id][self._data_key]
            val, uom = fix_value_and_uom(entity_data.get("value"), entity_data.get("unit"))
            self._attr_native_value = self._validated_native_value(val, uom)

            # set uom if it references a valid non-trivial unit of measurement
            if uom not in (None, ""):
                self._attr_native_unit_of_measurement = uom

            _LOGGER.debug(
                'Update sensor: %s: "%s" [%s]',
                self._attr_name,
                self._attr_native_value,
                self._attr_native_unit_of_measurement
            )

        except KeyError:
            self._attr_native_value = None
            _LOGGER.warning("Can't find %s", self._attr_unique_id)
            _LOGGER.debug("Sensor data %s", self.coordinator.data)

        self.async_write_ha_state()

    @property
    def entity_category(self):
        """Return the entity category."""
        if any(x in self._attr_unique_id for x in ["ConnectionStatus", "HasErrors", "ErrorMessages"]):
            return EntityCategory.DIAGNOSTIC
        return None

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        if self._attr_device_class is not None:
            return self._attr_device_class
        return uom_to_device_class(self._attr_native_unit_of_measurement)

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        if self._attr_state_class is not None:
            return self._attr_state_class
        return uom_to_state_class(self._attr_native_unit_of_measurement)

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        attr = {}
        if self._last_updated is not None:
            attr["Last Updated"] = self._last_updated

        try:
            entity_data = self.coordinator.data[self._device_id][self._data_key]
            if "CircuitTimesDay" in entity_data:
                attr["CircuitTimesDay"] = entity_data["CircuitTimesDay"]
            if "PossibleValues" in entity_data:
                attr["PossibleValues"] = entity_data["PossibleValues"]
            if isinstance(entity_data.get("value"), str) and entity_data["value"].startswith("{"):
                attr["Raw_JSON"] = entity_data["value"]
                schedule = _readable_schedule(entity_data["value"])
                if schedule:
                    attr["Schedule"] = schedule
        except KeyError:
            pass

        return attr
