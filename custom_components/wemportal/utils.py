"""Utility functions for WEM Portal."""

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfPower,
    UnitOfVolumeFlowRate,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfFrequency,
)

from .const import (
    _LOGGER,
    MISSING_DATA_STRINGS,
    BOOLEAN_OFF_STRINGS,
    BOOLEAN_ON_STRINGS,
    DOMAIN,
)


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


def build_device_info(entry_id, device_id, sw_version=None):
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
        "model": "WEM Portal",
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
    return {
        UnitOfTemperature.CELSIUS:                  SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.KELVIN:                   SensorDeviceClass.TEMPERATURE,
        UnitOfEnergy.KILO_WATT_HOUR:                SensorDeviceClass.ENERGY,
        UnitOfEnergy.WATT_HOUR:                     SensorDeviceClass.ENERGY,
        UnitOfPower.KILO_WATT:                      SensorDeviceClass.POWER,
        UnitOfPower.WATT:                           SensorDeviceClass.POWER,
        UnitOfTime.HOURS:                           SensorDeviceClass.DURATION,
        UnitOfFrequency.HERTZ:                      SensorDeviceClass.FREQUENCY,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR: SensorDeviceClass.VOLUME_FLOW_RATE,
    }.get(uom) # return None if no device class is available

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
