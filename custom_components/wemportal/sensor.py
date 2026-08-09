"""
Sensor platform for wemportal component
"""

import logging

import json
import re
from typing import Any

from homeassistant.components.sensor import RestoreSensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, MAX_LENGTH_STATE_STATE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import GITHUB_PROJECT_URL
from .entity import WemPortalEntity
from .models import Reading, account_state
from .wemportalapi import DEVICE_STATUS_ROWS
from .utils import (
    build_device_info,
    device_is_reachable,
    device_model,
    fix_value_and_unit,
    unit_to_device_class,
    unit_to_state_class,
)

_LOGGER = logging.getLogger(__name__)


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
            if isinstance(values, Reading) and values.platform == "sensor":
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

# The portal numbers the days 1..6 for Monday..Saturday and 0 for Sunday, and
# the JSON lists them in exactly that order. Taking the day NAMES from there
# means no weekday table lives here and nothing has to know which language the
# portal speaks.
_DAY_ORDER = (1, 2, 3, 4, 5, 6, 0)


def _report_unreadable_value(name, value, reported) -> None:
    """Say once that a reading could not be made into a number.

    The portal sometimes sends a word this integration does not know - a pump
    speed reading "Stop" is the one in upstream issue #146, on an
    installation whose portal writes "Aus" and "off" everywhere else. That is
    a legitimate answer, not a fault, and it arrives on every single cycle
    for as long as the condition lasts. Warning each time filled the log with
    a line that never changes and never resolves, which is how a real problem
    goes unnoticed.

    Once per sensor and value instead - and the one line is worth reading:
    it names the word and asks for it, so adding it to the vocabulary later
    rests on a report rather than on a guess about somebody else's heat pump.

    The value is keyed by its repr, because what arrives here is whatever the
    portal sent and need not be hashable.
    """
    key = (name, repr(value))
    if key in reported:
        return
    reported.add(key)
    _LOGGER.warning(
        'Cannot read %r as a number for "%s", so it shows as unknown. If the '
        "WEM Portal shows something meaningful there, please report that word "
        "at %s - it is probably a state this integration does not know yet.",
        value,
        name,
        GITHUB_PROJECT_URL,
    )


def _parse_schedule(raw):
    """A weekly programme as day -> [(window, letter), ...], or None.

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

    windows: dict[str, dict[int, str]] = {}
    marks: dict[str, str] = {}
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


def _day_labels(raw):
    """Day number -> the name the portal gives that day, or None.

    Built from the window keys in the order they arrive, which is the order
    of the week. Refuses anything that is not exactly seven days: a wrong
    label would file a whole day's programme under the wrong heading, and
    falling back to the JSON view is the better failure.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    days = []
    for key in parsed:
        if not isinstance(key, str):
            continue
        match = _WINDOW_KEY.match(key)
        if match and match["day"] not in days:
            days.append(match["day"])
    if len(days) != len(_DAY_ORDER):
        return None
    # strict, although the length was just checked: the guarantee then lives
    # on this line rather than two above it, where a later edit can move it
    # out from under this one. Pairing seven day numbers with six names would
    # file a whole day's programme under the wrong heading.
    return dict(zip(_DAY_ORDER, days, strict=True))


def _level_names(possible_values) -> dict[Any, str]:
    """Level number -> the portal's own word for it.

    The portal ships this alongside the programme, which is what makes the
    names right rather than guessed - and right in the portal's language
    rather than in one this file would have had to pick.
    """
    names = {}
    for entry in possible_values or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("Text", "")).strip()
        if text:
            names[entry.get("Value")] = text
    return names


def _clock(minutes: int) -> str:
    """Minutes since midnight as the portal writes a time. 1440 is 24:00."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _stretches(circuit_times):
    """A day as (start, end, level), reading each entry as an END.

    Measured against the portal's own view of the same programme:
    360/610/810/1440 carrying 3/2/1/3 is exactly the four cycles it lists -
    00:00-06:00 comfort, 06:00-10:10 normal, 10:10-13:30 reduced, then
    comfort again. The reduced stretch has no window in the JSON at all,
    which is the whole reason this source is worth preferring.
    """
    stretches = []
    start = 0
    for entry in circuit_times or []:
        if not isinstance(entry, dict):
            continue
        end = entry.get("MinutesSinceMidnight")
        if not isinstance(end, (int, float)) or isinstance(end, bool) or end <= start:
            continue
        stretches.append((start, int(end), entry.get("Value")))
        start = int(end)
    return stretches


def _stretch_text(start, end, level, names) -> str:
    """One stretch of a day, named where the portal named its level."""
    span = f"{_clock(start)}-{_clock(end)}"
    level_name = names.get(level)
    if level_name:
        return f"{span} {level_name}"
    return span


def _window_text(period: str, letter) -> str:
    """One programmed window, with the letter the portal put on it."""
    if letter:
        return f"{period} ({letter})"
    return period


def _schedule_from_circuit_times(row: Reading):
    """The whole week from what the DEVICE reported, or None.

    None whenever anything is missing or does not line up - the JSON view is
    a complete answer in its own right, so degrading to it costs detail and
    nothing else.
    """
    days = row.circuit_times_day
    if not isinstance(days, list) or not days:
        return None
    labels = _day_labels(row.value)
    if labels is None:
        return None

    names = _level_names(row.possible_values)
    schedule = {}
    for day in days:
        if not isinstance(day, dict):
            continue
        label = labels.get(day.get("Day"))
        if label is None:
            continue
        entries = [
            _stretch_text(start, end, level, names)
            for start, end, level in _stretches(day.get("CircuitTimes"))
        ]
        if entries:
            schedule[label] = entries
    return schedule or None


def _schedule_from_json(raw):
    """The week as the value read delivers it: windows only, bare letters.

    Incomplete by construction - the base level is a GAP here, not an entry,
    so a day that is reduced from ten past ten until half one shows nothing
    for those three hours. What the letters mean is not readable from this
    side either. It is the fallback for the hour after a restart, before the
    schedule fetch has run.
    """
    schedule = _parse_schedule(raw)
    if schedule is None:
        return None
    return {
        day: [_window_text(period, letter) for period, letter in periods]
        for day, periods in schedule.items()
    }


def _readable_schedule(row: Reading | None):
    """Every day with its stretches, from the best source the row carries."""
    if row is None:
        return None
    return _schedule_from_circuit_times(row) or _schedule_from_json(row.value)


def _schedule_summary(row):
    """The state: the whole week on one line, or None.

    "Programmed" was the entire state of these sensors, which says only that
    the parameter exists. Consecutive days that read the same are collapsed,
    so a week that is programmed alike throughout reads once instead of seven
    times.
    """
    schedule = _readable_schedule(row)
    if not schedule:
        return None

    # Lists, not tuples: a day that repeats the previous text extends the
    # group in place.
    groups: list[list[str]] = []
    for day, entries in schedule.items():
        text = ", ".join(entries)
        if groups and groups[-1][2] == text:
            groups[-1][1] = day
        else:
            groups.append([day, day, text])
    return "; ".join(
        f"{first} {text}" if first == last else f"{first}-{last} {text}"
        for first, last, text in groups
    )


class WemPortalSensor(WemPortalEntity, RestoreSensor):
    """Representation of a WEM Portal Sensor."""

    def _current_row(self) -> Reading | None:
        """The coordinator row behind this entity, or None.

        A weekly programme is read from more than its value - the schedule
        fetch adds the device's own view of it to the same row - so the
        value alone is no longer enough to build the state from.
        """
        try:
            row = self.coordinator.data[self._device_id][self._data_key]
        except (KeyError, TypeError):
            return None
        return row if isinstance(row, Reading) else None

    def _validated_native_value(self, value, unit):
        """Return a Home Assistant-safe native value."""
        effective_unit = unit
        if effective_unit in (None, ""):
            effective_unit = getattr(self, "_attr_native_unit_of_measurement", None)
        # A sensor is "numeric" if it has a real unit OR if it's tagged
        # with a device_class/state_class that requires a numeric state
        # (Home Assistant enforces this - see the entity's own state
        # property). Checking device_class/state_class too, not just
        # unit, closes a gap where fix_value_and_unit() can legitimately
        # return an empty/None unit for a given reading (e.g. a boolean
        # placeholder string with no unit attached) even though the
        # entity itself is declared as a numeric power/energy/etc.
        # sensor - which would otherwise let a non-numeric string like
        # "Off" slip through uncaught and crash entity setup entirely.
        is_numeric_sensor = (
            effective_unit not in (None, "")
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
        if value is None:
            _LOGGER.debug('No value for "%s" this cycle -> unknown', self._attr_name)
            return None

        if isinstance(value, str):
            value = value.strip()
            if value == "":
                _LOGGER.debug(
                    'Empty value for "%s" this cycle -> unknown', self._attr_name
                )
                return None
            if value.startswith("{"):
                summary = _schedule_summary(self._current_row())
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
                float(value)
            except (TypeError, ValueError):
                _report_unreadable_value(
                    self._attr_name,
                    value,
                    account_state(
                        self._config_entry.data.get(CONF_USERNAME)
                    ).unreadable_values_reported,
                )
                return None

        return value

    def __init__(
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id, _unique_id, entity_data)

        value, unit = fix_value_and_unit(entity_data.value, entity_data.unit)

        self._attr_native_unit_of_measurement = unit
        # Set device_class/state_class BEFORE validating the native value:
        # _validated_native_value() uses them (in addition to unit) to
        # decide whether a numeric value is required, so they must already
        # be in place the first time it runs, not just on later updates.
        self._attr_device_class = entity_data.device_class
        self._attr_state_class = entity_data.state_class
        self._attr_native_value = self._validated_native_value(value, unit)

        _LOGGER.debug(
            'Init sensor: %s: "%s" [%s]',
            self._attr_name,
            self._attr_native_value,
            self._attr_native_unit_of_measurement,
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
            if (
                last_sensor_data is not None
                and last_sensor_data.native_unit_of_measurement
            ):
                self._attr_native_unit_of_measurement = (
                    last_sensor_data.native_unit_of_measurement
                )
                _LOGGER.debug(
                    "Restored unit %s for %s from previous session",
                    self._attr_native_unit_of_measurement,
                    self._attr_name,
                )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        sw_version = None
        if (
            hasattr(self.coordinator.api, "api_version")
            and self.coordinator.api.api_version
        ):
            sw_version = self.coordinator.api.api_version
        info: DeviceInfo = build_device_info(
            self._config_entry.entry_id,
            self._device_id,
            sw_version=sw_version,
            model=device_model(self.coordinator.api, self._device_id),
        )
        return info

    @property
    def available(self):
        """Return if entity is available.

        Overrides the base for the second rule below, and asks the base for
        the first: the tolerance of a single failed cycle lives there, and
        spelling it out again here is how it came to apply to every platform
        except this one - which is most of the entities on an installation.
        """
        if not self._cycle_is_worth_showing():
            return False
        # The diagnostic sensors stay available even for an unreachable
        # device: they are what explains WHY everything else went away.
        # Matched on the parameter id rather than as a substring of the
        # unique_id, which also carries the entry id and the device id.
        if self._parameter_id in DEVICE_STATUS_ROWS:
            return True
        return device_is_reachable(self.coordinator.data, self._device_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        try:
            entity_data = self.coordinator.data[self._device_id][self._data_key]
            value, unit = fix_value_and_unit(entity_data.value, entity_data.unit)
            self._attr_native_value = self._validated_native_value(value, unit)

            # set unit if it references a valid non-trivial unit of measurement
            if unit not in (None, ""):
                self._attr_native_unit_of_measurement = unit

            _LOGGER.debug(
                'Update sensor: %s: "%s" [%s]',
                self._attr_name,
                self._attr_native_value,
                self._attr_native_unit_of_measurement,
            )

        except KeyError:
            self._attr_native_value = None
            _LOGGER.warning("Can't find %s", self._attr_unique_id)
            _LOGGER.debug("Sensor data %s", self.coordinator.data)

        self.async_write_ha_state()

    @property
    def entity_category(self):
        """Return the entity category.

        Decided on the parameter id, like `available` above. The former
        substring match on the unique_id also carried the entry id and the
        device id, so any device whose portal name happened to contain one
        of these words would have turned every one of its sensors into a
        diagnostic entity.
        """
        if self._parameter_id in DEVICE_STATUS_ROWS:
            return EntityCategory.DIAGNOSTIC
        return None

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        if self._attr_device_class is not None:
            return self._attr_device_class
        return unit_to_device_class(self._attr_native_unit_of_measurement)

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        if self._attr_state_class is not None:
            return self._attr_state_class
        return unit_to_state_class(self._attr_native_unit_of_measurement)

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        attributes: dict[str, Any] = {}
        if self._last_updated is not None:
            attributes["Last Updated"] = self._last_updated

        entity_data = self._current_row()
        if entity_data is None:
            return attributes
        if entity_data.circuit_times_day is not None:
            attributes["CircuitTimesDay"] = entity_data.circuit_times_day
        if entity_data.possible_values is not None:
            attributes["PossibleValues"] = entity_data.possible_values
        # Every active fault, whatever the state had room for. The state
        # is capped by Home Assistant and says how many it dropped; this
        # is where the dropped ones are.
        if entity_data.errors is not None:
            attributes["Errors"] = entity_data.errors
        if isinstance(entity_data.value, str) and entity_data.value.startswith("{"):
            attributes["Raw_JSON"] = entity_data.value
            schedule = _readable_schedule(entity_data)
            if schedule:
                attributes["Schedule"] = schedule

        return attributes
