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
    MISSING_DATA_STRINGS,
    BOOLEAN_OFF_STRINGS,
    BOOLEAN_ON_STRINGS,
    ENERGY_POWER_KEYWORDS,
)


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
        A float for numeric/boolean values, or the original string if it
        can't be interpreted as a number or known boolean placeholder.

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

    if val_lower in [x.strip() for x in MISSING_DATA_STRINGS]:
        name_lower = name.lower()
        if any(x in name_lower for x in ENERGY_POWER_KEYWORDS):
            # Energy/Power sensors MUST be None (not 0.0) to avoid Energy
            # Dashboard spikes/false readings when data is temporarily missing.
            return None
        return 0.0

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
    return {
        "%": SensorDeviceClass.POWER_FACTOR,
        UnitOfTemperature.CELSIUS:                  SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.KELVIN:                   SensorDeviceClass.TEMPERATURE,
        UnitOfEnergy.KILO_WATT_HOUR:                SensorDeviceClass.ENERGY,
        UnitOfEnergy.WATT_HOUR:                     SensorDeviceClass.ENERGY,
        UnitOfPower.KILO_WATT:                      SensorDeviceClass.POWER,
        UnitOfPower.WATT:                           SensorDeviceClass.POWER,
        UnitOfTime.HOURS:                           SensorDeviceClass.DURATION,
        UnitOfFrequency.HERTZ:                      SensorDeviceClass.FREQUENCY,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR: SensorDeviceClass.VOLUME_FLOW_RATE,
    }.get(uom, None) # return None if no device class is available

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
    }.get(uom, None) # return None if no state class is available
