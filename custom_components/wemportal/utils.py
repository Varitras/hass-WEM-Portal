"""Utility functions for WEM Portal."""

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfPressure,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfVolumeFlowRate,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfFrequency,
)

from .const import (
    DEFAULT_DEVICE_MODEL,
    DEVICE_TYPE_NAMES,
    WEB_MAINTENANCE_MARKER,
    _LOGGER,
    MISSING_DATA_STRINGS,
    BOOLEAN_OFF_STRINGS,
    BOOLEAN_ON_STRINGS,
    DOMAIN,
)


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
            value, key, default,
        )
        return default
    if value < minimum:
        _LOGGER.warning(
            "The stored %s of %s s is below the %s s minimum and has been "
            "raised to it. Re-save the options to make this permanent.",
            key, value, minimum,
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


def close_api_sessions(api) -> None:
    """Best-effort close of a WemPortalApi's HTTP sessions.

    Closes the API `requests` session and the persistent scraper (its own
    curl_cffi session) so they don't linger with an open connection after an
    entry is unloaded/reloaded or after config-flow validation. Never raises -
    the objects are being discarded anyway.
    """
    session = getattr(api, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Ignoring error closing API session: %s", exc)
    reset_scraper = getattr(api, "_reset_scraper", None)
    if callable(reset_scraper):
        try:
            reset_scraper()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Ignoring error closing scraper session: %s", exc)


def build_device_info(entry_id, device_id, sw_version=None, model=None):
    """Build the DeviceInfo dict for a WEM Portal sub-device.

    Every entity platform (number, select, sensor, switch) exposes the same
    per-device identity - a child device keyed by "<entry_id>:<device_id>"
    that links back to the integration hub via via_device. Centralizing it
    here keeps the four platforms in sync; previously each duplicated the
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


def sanitize_value(value_str, unit=None, name=""):
    """Sanitize typical German/English WEM Portal strings into numeric values.

    This is the single, shared implementation used by both the API mapper
    (mapper.py) and the web scraper (scraper.py). Previously each of those
    modules had its own slightly different copy of this logic, which meant
    a value like "Ein" (German) or "On" (English) could be recognized in
    one code path but not the other. Consolidating it here fixes that
    inconsistency once, for both paths.

    Args:
        value_str: The raw string value coming from the portal (or already
            a non-string value, in which case it is returned unchanged).
        unit: Currently unused for the boolean branches below (see note),
            kept for call-site/signature stability.
        name: The (internal) sensor name, used to detect energy/power
            sensors so that a "missing data" placeholder becomes None
            instead of a misleading 0.0 (which would show up as a false
            reading/spike on the Home Assistant Energy Dashboard).

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

    val_lower = value_str.lower().strip()

    # An empty or whitespace-only string is missing data, not a real value.
    # The portal occasionally sends "" for a parameter (e.g. a value that
    # didn't serialize, or a momentarily absent reading). Left as-is it
    # reaches a numeric sensor and crashes entity setup with
    # "could not convert string to float: ''". Return None (HA shows the
    # sensor as "unavailable") rather than a fabricated 0.0 - a room sensor
    # briefly without a reading should not report 0 degrees. This is the
    # same honesty the energy/power branch below already applies.
    if val_lower == "":
        return None

    if val_lower in [x.strip() for x in MISSING_DATA_STRINGS]:
        # Missing data is missing for EVERY sensor, not just energy/power:
        # return None (HA shows the sensor "unavailable") instead of a
        # fabricated 0.0. Previously non-energy/power sensors fell through to
        # 0.0, so a momentarily missing temperature read as 0 C and could
        # fire automations. This matches the empty-string branch above and
        # the project's "None over fabrication" principle (energy/power
        # already returned None; now the same honesty applies to all).
        return None

    if val_lower in BOOLEAN_OFF_STRINGS:
        return 0.0
    if val_lower in BOOLEAN_ON_STRINGS:
        return 1.0

    try:
        return float(value_str)
    except ValueError:
        return value_str


def serialize_modules(modules: dict) -> dict:
    """Convert the in-memory `modules` dict into a JSON-serializable dict.

    `modules` is keyed as `{device_id: {(module_index, module_type): {...}}}`.
    Tuple keys are not valid JSON object keys, so they are flattened into
    "index:type" strings here. Used to persist discovered module/parameter
    metadata across Home Assistant restarts (see `deserialize_modules` for
    the inverse operation).
    """
    if not modules:
        return {}
    serialized = {}
    for device_id, device_modules in modules.items():
        serialized[device_id] = {
            f"{module_index}:{module_type}": module_data
            for (module_index, module_type), module_data in device_modules.items()
        }
    return serialized


def deserialize_modules(data: dict) -> dict:
    """Convert a persisted modules dict back into the in-memory tuple-keyed format.

    Inverse of `serialize_modules`. Returns an empty dict (not None) if
    `data` is empty/None, so callers can safely treat the result as
    "no cached data" without extra None-checks.
    """
    if not data:
        return {}
    modules = {}
    for device_id, device_modules in data.items():
        modules[device_id] = {}
        for key, module_data in device_modules.items():
            index_str, type_str = key.split(":", 1)
            modules[device_id][(int(index_str), int(type_str))] = module_data
    return modules


def fix_value_and_uom(val, uom):
    """
    Translate WEM specific values and units of measurement to Home Assistant.

    This function returns:
      * a valid Home Assistant UoM if it can be mapped
        (see: https://github.com/home-assistant/core/blob/dev/homeassistant/const.py)
      * an empty string as UoM if the value is a number without any indication
        of its unit of measurement (e.g., a counter)
      * None as UoM if the value is a string without any indication
        of its unit of measurement (e.g., a status text)
    """

    # special case: volume flow rate
    if isinstance(val, str) and val.endswith("m3/h"):
        return float(val.replace("m3/h", "")), UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR

    # special case: no unit of measurement
    if uom is None:
        return val, None

    # special case: empty string for unit of measurement for a number
    if uom == "":
        try:
            return float(val), ""
        except (ValueError, TypeError):
            return val, None

    uom = {
        "":         None,
        "w":        UnitOfPower.WATT,
        "kw (w)":   UnitOfPower.WATT,
        "kw":       UnitOfPower.KILO_WATT,
        "kwh":      UnitOfEnergy.KILO_WATT_HOUR,
        "kw (w)h":  UnitOfEnergy.WATT_HOUR,
        "h":        UnitOfTime.HOURS,
        "hz":       UnitOfFrequency.HERTZ,
        # The portal writes "BAR"; Home Assistant only accepts "bar" for the
        # pressure device class and logs a warning for anything else. The
        # device-class lookup is case-insensitive, but the UNIT that reaches
        # the entity has to be the canonical spelling too.
        "bar":      UnitOfPressure.BAR,
        "m3/h":     UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR
    }.get(uom.lower(), uom)
    return val, uom

def uom_to_device_class(uom):
    """Return the device_class of this unit of measurement, if any."""

    # see: <https://developers.home-assistant.io/docs/core/entity/sensor/#available-device-classes>
    #
    # NOTE: "%" deliberately has NO device class. It used to be mapped to
    # POWER_FACTOR, but the percent sensors here are things like power limit,
    # heating/cooling output, pump speed and power demand - none of which is a
    # power factor (cos phi, the real/apparent power ratio). Home Assistant
    # accepted the combination, so nothing broke, but the label was simply
    # wrong. Percent sensors keep their "%" unit and MEASUREMENT state class
    # (see uom_to_state_class), so history and long-term statistics are
    # unaffected; only the icon and any device_class-based filtering change.
    # Matched case-insensitively: the portal delivers e.g. "BAR" where the
    # Home Assistant constant is "bar", which silently missed and left the
    # sensor without a device class (and thus without a proper icon).
    if uom is None:
        return None
    mapping = {
        UnitOfPressure.BAR:                         SensorDeviceClass.PRESSURE,
        UnitOfTemperature.CELSIUS:                  SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.KELVIN:                   SensorDeviceClass.TEMPERATURE,
        UnitOfEnergy.KILO_WATT_HOUR:                SensorDeviceClass.ENERGY,
        UnitOfEnergy.WATT_HOUR:                     SensorDeviceClass.ENERGY,
        UnitOfPower.KILO_WATT:                      SensorDeviceClass.POWER,
        UnitOfPower.WATT:                           SensorDeviceClass.POWER,
        UnitOfTime.HOURS:                           SensorDeviceClass.DURATION,
        UnitOfFrequency.HERTZ:                      SensorDeviceClass.FREQUENCY,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR: SensorDeviceClass.VOLUME_FLOW_RATE,
    }
    # Both sides normalised - the KEYS above are Home Assistant constants
    # in their own casing ("kW", "°C", "K"), so lowering only the lookup
    # value would miss every one of them.
    return {str(k).strip().lower(): v for k, v in mapping.items()}.get(
        str(uom).strip().lower()
    )


def uom_to_icon(uom):
    """Icon for a unit - or None to let Home Assistant decide.

    Returning None is the important part: an explicitly set icon ALWAYS
    beats the one Home Assistant derives from the device class, so the old
    blanket "mdi:flash" default overrode correct, consistent icons on every
    sensor that had a device class (duration, power, energy, flow, ...).
    An icon is only supplied where HA has nothing to go on.
    """
    if uom_to_device_class(uom) is not None:
        return None
    return {
        "%": "mdi:percent",
        "rpm": "mdi:fan",
    }.get(str(uom).strip().lower() if uom else "", "mdi:flash")

def uom_to_state_class(uom):
    """Return the state class of this unit of measurement, if any."""

    # see: <https://developers.home-assistant.io/docs/core/entity/sensor/#available-state-classes>
    return {
        "":                                         SensorStateClass.MEASUREMENT,
        "%":                                        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS:                  SensorStateClass.MEASUREMENT,
        UnitOfTemperature.KELVIN:                   SensorStateClass.MEASUREMENT,
        UnitOfEnergy.KILO_WATT_HOUR:                SensorStateClass.TOTAL_INCREASING,
        UnitOfEnergy.WATT_HOUR:                     SensorStateClass.TOTAL_INCREASING,
        UnitOfPower.KILO_WATT:                      SensorStateClass.MEASUREMENT,
        UnitOfPower.WATT:                           SensorStateClass.MEASUREMENT,
        UnitOfTime.HOURS:                           SensorStateClass.TOTAL_INCREASING,
        UnitOfFrequency.HERTZ:                      SensorStateClass.MEASUREMENT,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR: SensorStateClass.MEASUREMENT,
    }.get(uom) # return None if no state class is available


# Request labels for which an unexpected maintenance marker has already been
# reported. Bounded by the number of request sites, so this can never grow
# without limit - and one report per site is all the evidence needed.
_MARKER_REPORTED: set[str] = set()


def report_unexpected_maintenance_marker(notice, what) -> None:
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
    if what in _MARKER_REPORTED:
        _LOGGER.debug("Maintenance marker seen again on the %s.", what)
        return
    _MARKER_REPORTED.add(what)
    _LOGGER.warning(
        "The WEM Portal maintenance marker appeared in the response to the "
        "%s, which is NOT treated as downtime. If the portal was working "
        "normally, please report this - it decides whether the maintenance "
        "check can be applied to every request. Notice text: %s",
        what, notice,
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
    except Exception as exc:  # pylint: disable=broad-except
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
    dated = [v for v in values if isinstance(v, dict) and v.get("Date")]
    if not dated:
        return values[-1]
    # ISO-8601 ("2026-04-27T00:00:00") sorts correctly as text, so no date
    # parsing - and thus no locale or format surprises - is needed.
    return max(dated, key=lambda v: str(v["Date"]))


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
    if not isinstance(status, dict):
        return True
    return status.get("value") not in UNREACHABLE_CONNECTION_STATES


def portal_status_is_success(status) -> bool:
    """Whether the portal's `Status` field means "this worked".

    Only the integer 0 does. `Status: false` is NOT success, and Python
    treats it as equal to 0 - so the obvious `status != 0` waved it through
    at all three places that check it, including the one that decides whether
    a write to a heating parameter actually happened.

    `type(status) is int` rather than isinstance: bool IS a subclass of int,
    which is the whole problem.
    """
    return type(status) is int and status == 0
