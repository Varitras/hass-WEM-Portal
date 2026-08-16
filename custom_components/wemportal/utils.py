"""Utility functions for WEM Portal."""

from typing import Final
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)

from .models import ModuleRef, Reading
from .const import (
    BOOLEAN_OFF_STRINGS,
    BOOLEAN_ON_STRINGS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_DEVICE_MODEL: Final = "WEM Portal"

# DeviceType as reported by Device/Read. Only used for the device model
# shown in Home Assistant; an unknown value falls back to the generic name.
DEVICE_TYPE_NAMES: Final = {
    1: "Combi boiler",
    2: "Heat pump",
}

# Scraper Constants
MISSING_DATA_STRINGS: Final = ["--", "label ist null", "label ist null "]

# The portal announces planned downtime by rendering this container on the
# login page. Matched on the CSS class, NOT on its text: the wording changes
# per announcement and is localised, while the class is purpose-built and
# language-independent. Note the login form stays fully present and
# submittable during maintenance - only the backend behind it is down - so
# "is there a form?" cannot tell the two apart.
WEB_MAINTENANCE_MARKER: Final = "offlinecontent"


def clamped_scan_interval(options, key, default, minimum):
    """Read a stored scan interval and hold it to its floor.

    The floors were only ever enforced by the options-flow schema, which
    validates what the user types NOW. Options stored by an older release -
    when the API floor was ten seconds - were read back verbatim on every
    start, so an installation configured once at one second kept hammering
    the portal at one second no matter what the current limits say. Nothing
    in the UI reveals that either: the form shows the stored value as if it
    were legal.

    Also survives a non-numeric or missing value, which storage can contain
    after a hand-edited .storage file or a failed migration.
    """
    value = options.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Ignoring the unusable stored value %r for %s; using %s seconds.",
            value,
            key,
            default,
        )
        return default
    if value < minimum:
        _LOGGER.warning(
            "The stored %s of %s s is below the %s s minimum and has been "
            "raised to it. Re-save the options to make this permanent.",
            key,
            value,
            minimum,
        )
        return minimum
    return value


def device_identifier(entry_id, device_id):
    """Return the device-registry identifier for a WEM Portal sub-device.

    Single source of truth so the entity platforms (via build_device_info)
    and the coordinator's disabled-device lookup use the SAME tuple.
    Previously the entities registered "<entry>:<device>" while the
    coordinator looked up "<device>" alone, so the lookup never matched and
    disabled devices were still polled.
    """
    return (DOMAIN, f"{entry_id}:{device_id}")


def short_device_id(device_id) -> str:
    """A device id shortened for text the user is invited to publish.

    The failure it appears in ends with "open an issue at <tracker>", so the
    whole string gets pasted into a public tracker as it stands - and a device
    id belongs to one installation. The TAIL rather than the head, because the
    ids of one account share their leading digits and the tail is what tells
    two of them apart, which is all the message needs it for.
    """
    text = str(device_id or "")
    return f"…{text[-2:]}" if len(text) > 2 else text


def close_api_sessions(api) -> None:
    """Close a WemPortalApi's HTTP sessions, under its own lock.

    Called after an entry is unloaded or reloaded, after a failed setup, and
    after config-flow validation, so neither the API `requests` session nor
    the persistent scraper's curl_cffi session lingers with an open
    connection.

    A plain call, deliberately. This used to reach in with
    `getattr(api, "session", None)` and `getattr(api, "_reset_scraper", None)`,
    which reads as defensive and is the opposite: the defaults meant that
    renaming or moving either one turned the whole function into a silent
    no-op, closing nothing, raising nothing, and failing no test - while the
    docstring went on promising the sessions were closed. Naming the method
    makes that failure an AttributeError instead of a leak.

    The api owns the teardown because the api owns the lock: an operation can
    be inside make_api_call right now, and closing its session underneath it
    is the thing this must not do.
    """
    api.close_transport()


def build_device_info(entry_id, device_id, sw_version=None, model=None):
    """Build the DeviceInfo dict for a WEM Portal sub-device.

    Every entity platform (date, number, select, sensor, switch) exposes the same
    per-device identity - a child device keyed by "<entry_id>:<device_id>"
    that links back to the integration hub via via_device. Centralizing it
    here keeps them in sync; previously each duplicated the
    same dict. `sw_version` is optional (only the sensor platform has an
    API version to report).
    """
    info = {
        "identifiers": {device_identifier(entry_id, device_id)},
        "via_device": (DOMAIN, entry_id),
        "name": str(device_id),
        "manufacturer": "Weishaupt",
        "model": model or DEFAULT_DEVICE_MODEL,
    }
    if sw_version:
        info["sw_version"] = sw_version
    return info


def parse_portal_number(value):
    """The number a portal value carries, or None if it carries none.

    The portal spells decimals with a dot in dialog labels and API strings
    and with a comma in scraped cells and values people type ("21,5",
    "0,55"). Every reader shares this one parser - a second one is how the
    edit dialog came to accept "0,55" while the service refused it.

    None rather than a raise: each caller has its own answer to "no number
    here" (skip the option, keep the raw string, name the accepted words),
    and an exception would turn every one of them into a try block.
    """
    if value is None:
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def sanitize_value(value_str):
    """Sanitize typical German/English WEM Portal strings into numeric values.

    The single implementation for both readers - the API mapper (mapper.py)
    and the web scraper (scraper.py) - so that "Ein"/"On" cannot be
    recognised on one path and not the other.

    Args:
        value_str: The raw string value coming from the portal (or already
            a non-string value, in which case it is returned unchanged).

    Returns:
        A float for numeric/boolean values; None for empty or "missing
        data" values (the sensor then shows as unavailable rather than
        reporting a fabricated 0); or the original string if it can't be
        interpreted as a number or known boolean/placeholder.

    Note on boolean handling: "Ein"/"On"/"Aus"/"Off" are ALWAYS mapped to
    1.0/0.0 here, never to text, regardless of `unit`. An earlier version
    of this function returned text ("On"/"Off") when no unit was present,
    to make plain status sensors read more naturally - but the same raw
    value/name can also belong to a genuinely numeric sensor (e.g. a
    "power" sensor reading "Aus" while idle, with device_class="power"
    and a real unit like "kW" that simply isn't attached to *this*
    particular string). Home Assistant requires a numeric state whenever
    state_class/device_class/unit are set, so returning text there
    crashes entity setup entirely. Always-numeric matches this
    component's original, crash-free behavior and is what switch.py's
    WEM_SWITCH_ON_VALUES already accepts alongside the text forms.
    """
    if not isinstance(value_str, str):
        return value_str

    value_lower = value_str.lower().strip()

    # An empty or whitespace-only string is missing data, not a real value.
    # The portal occasionally sends "" for a parameter (e.g. a value that
    # didn't serialize, or a momentarily absent reading). Left as-is it
    # reaches a numeric sensor and crashes entity setup with
    # "could not convert string to float: ''". Return None (HA shows the
    # sensor as "unavailable") rather than a fabricated 0.0 - a room sensor
    # briefly without a reading should not report 0 degrees. This is the
    # same honesty the energy/power branch below already applies.
    if value_lower == "":
        return None

    if value_lower in [missing.strip() for missing in MISSING_DATA_STRINGS]:
        # Missing data is missing for EVERY sensor, not just energy/power:
        # return None (HA shows the sensor "unavailable") instead of a
        # fabricated 0.0. Previously non-energy/power sensors fell through to
        # 0.0, so a momentarily missing temperature read as 0 C and could
        # fire automations. This matches the empty-string branch above and
        # the project's "None over fabrication" principle (energy/power
        # already returned None; now the same honesty applies to all).
        return None

    if value_lower in BOOLEAN_OFF_STRINGS:
        return 0.0
    if value_lower in BOOLEAN_ON_STRINGS:
        return 1.0

    number = parse_portal_number(value_str)
    if number is not None:
        return number
    return value_str


def serialize_modules(modules: dict) -> dict:
    """Convert the in-memory `modules` dict into a JSON-serializable dict.

    `modules` is keyed as `{device_id: {ModuleRef: {...}}}`. Tuple keys are
    not valid JSON object keys, so ModuleRef's own storage spelling is used.
    Built via `ModuleRef(*key)` on purpose: during the typed-model migration
    the same dict can briefly hold bare-tuple keys from older code paths and
    tests, and both must serialize identically.
    """
    if not modules:
        return {}
    serialized = {}
    for device_id, device_modules in modules.items():
        serialized[device_id] = {
            ModuleRef(*module_key).as_storage_key(): dict(module_data)
            for module_key, module_data in device_modules.items()
        }
    return serialized


def deserialize_modules(data: dict) -> dict:
    """Convert a persisted modules dict back into the in-memory keyed format.

    Inverse of `serialize_modules`; the keys come back as ModuleRef. Returns
    an empty dict (not None) if `data` is empty/None, so callers can safely
    treat the result as "no cached data" without extra None-checks.
    """
    if not data:
        return {}
    modules: dict = {}
    for device_id, device_modules in data.items():
        modules[device_id] = {}
        for key, module_data in device_modules.items():
            modules[device_id][ModuleRef.from_storage_key(key)] = module_data
    return modules


def fix_value_and_unit(value, unit):
    """
    Translate WEM specific values and units of measurement to Home Assistant.

    This function returns:
      * a valid Home Assistant unit if it can be mapped
        (see: https://github.com/home-assistant/core/blob/dev/homeassistant/const.py)
      * an empty string as unit if the value is a number without any indication
        of its unit of measurement (e.g., a counter)
      * None as unit if the value is a string without any indication
        of its unit of measurement (e.g., a status text)
    """

    # special case: volume flow rate, the one unit that is read out of the
    # value instead of the unit field. Through the shared parser like every
    # other number here: a scraped cell spells its decimals with a comma, and
    # a bare float() raised on it - out of a platform mid-update, so it cost
    # more than the reading it could not parse. A value carrying the unit but
    # no number (the portal's placeholder) falls through to the handling
    # below, which keeps it as the text it is.
    if isinstance(value, str) and value.endswith("m3/h"):
        flow_rate = parse_portal_number(value.replace("m3/h", ""))
        if flow_rate is not None:
            return flow_rate, UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR

    # special case: no unit of measurement
    if unit is None:
        return value, None

    # special case: empty string for unit of measurement for a number
    if unit == "":
        try:
            return float(value), ""
        except (ValueError, TypeError):
            return value, None

    unit = {
        "": None,
        "w": UnitOfPower.WATT,
        "kw (w)": UnitOfPower.WATT,
        "kw": UnitOfPower.KILO_WATT,
        "kwh": UnitOfEnergy.KILO_WATT_HOUR,
        "kw (w)h": UnitOfEnergy.WATT_HOUR,
        "h": UnitOfTime.HOURS,
        "hz": UnitOfFrequency.HERTZ,
        # The portal writes "BAR"; Home Assistant only accepts "bar" for the
        # pressure device class and logs a warning for anything else. The
        # device-class lookup is case-insensitive, but the UNIT that reaches
        # the entity has to be the canonical spelling too.
        "bar": UnitOfPressure.BAR,
        "m3/h": UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
    }.get(unit.lower(), unit)
    return value, unit


def _unit_lookup_key(unit) -> str:
    """How a unit is spelled when a table is asked about it.

    BOTH sides need this, and that is the whole point: the keys of those
    tables are Home Assistant constants in their own casing ("kW", "°C",
    "K"), so lowering only the value would miss every one of them. The
    portal delivers "BAR" where the constant is "bar", and a stray space is
    just as easy to get.

    Deliberately not folding None into "": a reading with no unit and a
    reading whose unit is the empty string are different questions, and the
    second one has a state class.
    """
    return str(unit).strip().lower()


def unit_to_device_class(unit):
    """Return the device_class of this unit of measurement, if any."""

    # see: <https://developers.home-assistant.io/docs/core/entity/sensor/#available-device-classes>
    #
    # NOTE: "%" deliberately has NO device class. It used to be mapped to
    # POWER_FACTOR, but the percent sensors here are things like power limit,
    # heating/cooling output, pump speed and power demand - none of which is a
    # power factor (cos phi, the real/apparent power ratio). Home Assistant
    # accepted the combination, so nothing broke, but the label was simply
    # wrong. Percent sensors keep their "%" unit and MEASUREMENT state class
    # (see unit_to_state_class), so history and long-term statistics are
    # unaffected; only the icon and any device_class-based filtering change.
    # Matched case-insensitively: the portal delivers e.g. "BAR" where the
    # Home Assistant constant is "bar", which silently missed and left the
    # sensor without a device class (and thus without a proper icon).
    if unit is None:
        return None
    mapping = {
        UnitOfPressure.BAR: SensorDeviceClass.PRESSURE,
        UnitOfTemperature.CELSIUS: SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.KELVIN: SensorDeviceClass.TEMPERATURE,
        UnitOfEnergy.KILO_WATT_HOUR: SensorDeviceClass.ENERGY,
        UnitOfEnergy.WATT_HOUR: SensorDeviceClass.ENERGY,
        UnitOfPower.KILO_WATT: SensorDeviceClass.POWER,
        UnitOfPower.WATT: SensorDeviceClass.POWER,
        UnitOfTime.HOURS: SensorDeviceClass.DURATION,
        UnitOfFrequency.HERTZ: SensorDeviceClass.FREQUENCY,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR: SensorDeviceClass.VOLUME_FLOW_RATE,
    }
    return {_unit_lookup_key(key): value for key, value in mapping.items()}.get(
        _unit_lookup_key(unit)
    )


def unit_to_icon(unit):
    """Icon for a unit - or None to let Home Assistant decide.

    Returning None is the important part: an explicitly set icon ALWAYS
    beats the one Home Assistant derives from the device class, so the old
    blanket "mdi:flash" default overrode correct, consistent icons on every
    sensor that had a device class (duration, power, energy, flow, ...).
    An icon is only supplied where HA has nothing to go on.
    """
    if unit_to_device_class(unit) is not None:
        return None
    return {
        "%": "mdi:percent",
        "rpm": "mdi:fan",
    }.get(str(unit).strip().lower() if unit else "", "mdi:flash")


def unit_to_state_class(unit):
    """Return the state class of this unit of measurement, if any."""

    # see: <https://developers.home-assistant.io/docs/core/entity/sensor/#available-state-classes>
    #
    # Spelled the same way as the device class one function above. They
    # answer two halves of one question, and where they disagreed the result
    # was a sensor with a device class and no state class - which Home
    # Assistant accepts and the Energy Dashboard refuses.
    if unit is None:
        return None
    mapping = {
        "": SensorStateClass.MEASUREMENT,
        "%": SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS: SensorStateClass.MEASUREMENT,
        UnitOfTemperature.KELVIN: SensorStateClass.MEASUREMENT,
        UnitOfEnergy.KILO_WATT_HOUR: SensorStateClass.TOTAL_INCREASING,
        UnitOfEnergy.WATT_HOUR: SensorStateClass.TOTAL_INCREASING,
        UnitOfPower.KILO_WATT: SensorStateClass.MEASUREMENT,
        UnitOfPower.WATT: SensorStateClass.MEASUREMENT,
        UnitOfTime.HOURS: SensorStateClass.TOTAL_INCREASING,
        UnitOfFrequency.HERTZ: SensorStateClass.MEASUREMENT,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR: SensorStateClass.MEASUREMENT,
    }
    # None if this unit has no state class.
    return {_unit_lookup_key(key): value for key, value in mapping.items()}.get(
        _unit_lookup_key(unit)
    )


def report_unexpected_maintenance_marker(notice, what, reported) -> None:
    """Note a maintenance marker on a response that is not treated as downtime.

    The marker check is currently enabled only where a real maintenance page
    was observed. Whether it is safe everywhere depends on one question that
    cannot be answered by reading the code: can the marker also appear on a
    HEALTHY portal page? Enabling it everywhere on the assumption that it
    cannot would trade a known gap for an unknown false positive - one that
    would report the portal as down while it is serving fine.

    So the question is measured instead. This fires only if the marker turns
    up somewhere it is not acted on, which under the current assumption should
    be never. Silence over a few days is the evidence that the check can be
    applied to every request; a hit names the exact request that would have
    produced a false alarm.

    Warning level, because the user has to see it without enabling debug
    logging - and once per request label, so a marker that IS on every page
    cannot flood the log.
    """
    if what in reported:
        _LOGGER.debug("Maintenance marker seen again on the %s.", what)
        return
    reported.add(what)
    _LOGGER.warning(
        "The WEM Portal maintenance marker appeared in the response to the "
        "%s, which is NOT treated as downtime. If the portal was working "
        "normally, please report this - it decides whether the maintenance "
        "check can be applied to every request. Notice text: %s",
        what,
        notice,
    )


def maintenance_notice(html_text):
    """Return the portal's maintenance notice, or None if there is none.

    Detected via the dedicated `offlinecontent` container (see
    WEB_MAINTENANCE_MARKER), not via keywords: the announcement text is
    localised and changes every time, the class does not. The text is only
    read out afterwards, to put the actual window into the log.
    """
    if not html_text or WEB_MAINTENANCE_MARKER not in html_text:
        return None
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html_text)
        for div in tree.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '),"
            " ' " + WEB_MAINTENANCE_MARKER + " ')]"
        ):
            text = " ".join(div.text_content().split())
            if text:
                return text
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not read the maintenance notice: %s", exc)
    # Marker present but unreadable - still a maintenance page.
    return "The portal reports scheduled maintenance."


def device_model(api, device_id):
    """Human-readable model for a device, from its reported DeviceType.

    Returns None when the type is unknown or was never reported, so
    build_device_info falls back to the generic name.
    """
    types = getattr(api, "device_types", None) or {}
    return DEVICE_TYPE_NAMES.get(types.get(str(device_id)))


def latest_statistics_entry(values):
    """Pick the newest statistics entry by its Date, not by list position.

    The API returns one entry per day and the newest happens to be last, so
    the code used values[-1] and never looked at Date. That is an assumption
    about ordering, not a check: a differently sorted response, or a trailing
    placeholder, would silently yield the wrong day's reading. Sorting by the
    Date the entry carries removes the assumption; entries without a usable
    Date fall back to the previous positional behaviour.
    """
    if not values:
        return None
    dated = [entry for entry in values if isinstance(entry, dict) and entry.get("Date")]
    if not dated:
        return values[-1]
    # ISO-8601 ("2026-04-27T00:00:00") sorts correctly as text, so no date
    # parsing - and thus no locale or format surprises - is needed.
    return max(dated, key=lambda entry: str(entry["Date"]))


_MORE = " (+{} more)"


def error_state_and_detail(errors) -> tuple[str, list]:
    """The fault sensor's state, and the full list for its attribute.

    Home Assistant refuses a state longer than it allows, so this has to fit.
    It used to fit by slicing the joined text at 255 characters and saying
    nothing - which is how a second active fault disappears without trace,
    while the first one still reads like the whole story.

    So the complete list always travels in the attribute, and when the state
    cannot hold everything it says how many are missing. Whole messages are
    kept where possible; a single message too long on its own is cut and
    marked, because a cut that announces itself is still worth more than no
    state at all.
    """
    messages = [text for text in (str(error).strip() for error in errors or []) if text]
    if not messages:
        return "None", []

    joined = ", ".join(messages)
    if len(joined) <= MAX_LENGTH_STATE_STATE:
        return joined, messages

    kept: list[str] = []
    for message in messages:
        dropped = len(messages) - len(kept) - 1
        candidate = ", ".join([*kept, message])
        if dropped:
            candidate += _MORE.format(dropped)
        if len(candidate) > MAX_LENGTH_STATE_STATE:
            break
        kept.append(message)

    if kept:
        return ", ".join(kept) + _MORE.format(len(messages) - len(kept)), messages

    marker = _MORE.format(len(messages) - 1)
    room = MAX_LENGTH_STATE_STATE - len(marker) - len("...")
    return messages[0][:room] + "..." + marker, messages


def looks_like_schedule(value) -> bool:
    """Whether a reading is one of the portal's weekly programmes.

    The portal types these two ways. Some installations declare DataType 6,
    which is what the heating-schedule fetch was written for. Others declare
    DataType 2 - the same type as an ordinary switch - and put a JSON object
    in the value instead. Measured on a 3.1.3.0 portal, where every one of
    them arrives as 2, so keying on the declared type alone meant the fetch
    never ran there at all.

    Asking what the value IS catches both, and it is the same question the
    sensor platform already asks to decide a reading is a programme rather
    than a number.
    """
    return isinstance(value, str) and value.strip().startswith("{")


# Connection states that mean the device is definitively not reachable, as
# opposed to momentarily busy. Kept deliberately narrow: `busy` (8) is
# transient and `unknown` covers a status we failed to read, and treating
# either as unavailable would make entities flicker on a healthy system.
UNREACHABLE_CONNECTION_STATES = ("offline", "wrong_secret")


def device_is_reachable(coordinator_data, device_id) -> bool:
    """Whether this device answered, as far as the portal knows.

    The coordinator's `last_update_success` covers the CYCLE, not the single
    device: with several devices, one that has been offline for days still
    counted as available and kept presenting its last reading as current.

    Anything other than a definitively-dead state counts as reachable, and so
    does a device with no status at all - notably the scraper's pseudo device,
    which never gets one. Being strict there would mark every scraped sensor
    unavailable forever and take out `web` mode entirely.
    """
    device_data = (coordinator_data or {}).get(device_id)
    if not isinstance(device_data, dict):
        return True
    status = device_data.get(f"{device_id}-ConnectionStatus")
    if not isinstance(status, Reading):
        return True
    return status.value not in UNREACHABLE_CONNECTION_STATES
