"""Data mapper for mapping API values to Home Assistant platforms."""

import re
from .translations import friendly_name_mapper, translate
from .const import WemDataType, _LOGGER
from .utils import looks_like_schedule, sanitize_value, unit_to_icon


def get_min_max(
    parameter_id: str, data_type: int, min_value, max_value
) -> tuple[float, float]:
    try:
        if min_value is not None and max_value is not None:
            return float(min_value), float(max_value)
    except (ValueError, TypeError):
        pass

    if data_type == WemDataType.SWITCH:
        return 0.0, 1.0

    parameter_lower = parameter_id.lower()
    if "ww" in parameter_lower or "warmwasser" in parameter_lower:
        return 30.0, 65.0
    if any(
        keyword in parameter_lower
        for keyword in ["raum", "komfort", "absenk", "normal"]
    ):
        return 5.0, 35.0

    return 0.0, 100.0


def _tokenize(text):
    """The words of a name, punctuation removed, for comparing two names."""
    return set(re.sub(r"[^a-zA-Z0-9äöüß]", " ", text.lower()).split())


def _friendly_name(language: str, parameter_id: str, module_name: str) -> str:
    """The display name, without repeating the module name when the parameter
    name already carries it."""
    translated_name = translate(
        language,
        friendly_name_mapper(parameter_id),
    )
    translated_module_name = translate(language, module_name.strip())

    module_words = set(translated_module_name.lower().split())
    entity_words = set(translated_name.lower().split())

    if module_words.issubset(entity_words):
        return translated_name
    return f"{translated_module_name} {translated_name}"


def _describe_value(
    parameter_id, module, device_module, parameter, value, language
) -> tuple[str, dict]:
    """Flatten one portal value into the description the rest of the mapper
    works with. Raises on malformed portal data just like the inline code it
    replaces - the caller's guard turns that into a skipped value."""
    name = f"{device_module['Name']}-{parameter['ParameterID']}"

    numeric_value = value.get("NumericValue")
    string_value = value.get("StringValue", "")

    final_value = numeric_value if numeric_value is not None else string_value

    data_type = parameter.get("DataType")

    if parameter.get("EnumValues"):
        if data_type == WemDataType.SWITCH:
            # Only normalize true booleans (on/off) here. Other
            # enum-valued parameters - e.g. a SELECT dropdown
            # like a 0-240 minute push duration, where one
            # option happens to be "Aus"/"Off" - must keep
            # their exact original string, so they still match
            # the literal option names built from this same
            # parameter's EnumValues in _writeable_entity
            # (select.py matches the raw value against those
            # names verbatim). Rewriting "Aus" to "Off"/0.0
            # here would silently break that match.
            final_value = sanitize_value(string_value, value.get("Unit"), name)
        else:
            final_value = string_value
    else:
        if isinstance(final_value, str):
            final_value = sanitize_value(final_value, value.get("Unit"), name)

    return name, {
        "friendlyName": _friendly_name(language, parameter_id, device_module["Name"]),
        "ParameterID": parameter_id,
        "unit": value.get("Unit"),
        "value": final_value,
        "IsWriteable": parameter.get("IsWriteable", False),
        "DataType": data_type,
        "ModuleIndex": module["ModuleIndex"],
        "ModuleType": module["ModuleType"],
    }


def _declares_bounds(parameter: dict) -> bool:
    """Whether the portal gave this parameter BOTH bounds.

    Asked of the raw parameter, never of get_min_max(): that function fills in
    a range when there is none, which is useful for building a number entity
    and actively wrong for deciding what kind of parameter this is.
    """
    for key in ("MinValue", "MaxValue"):
        raw = parameter.get(key)
        if raw is None or raw == "":
            return False
        try:
            float(raw)
        except (TypeError, ValueError):
            return False
    return True


def _is_time_or_programme(parameter: dict) -> bool:
    """Whether this DataType 2 parameter is not a switch at all.

    The type is overloaded, and TWO signals say a parameter is genuinely
    binary. Bounds are one: a real switch declares MinValue 0 and MaxValue 1.
    Named states are the other: a switch may instead ship EnumValues holding
    its off/on wording, which _describe_value already relies on to normalise
    "Ein"/"Aus" - so an enumerated parameter is a switch even with no bounds.

    Neither present means a time or programme parameter. That is what the
    portal's own parameter list shows: a weekly heating schedule and holiday
    begin/end all arrive as DataType 2, no bounds, EnumValues null.
    """
    if parameter.get("EnumValues"):
        return False
    return not _declares_bounds(parameter)


def _time_or_programme_entity(
    common_attributes: dict, sent_a_number: bool
) -> dict | None:
    """The writeable platform for a time or programme parameter, if any.

    Two forms have been observed. A schedule comes as a JSON string and is
    caught by the caller before this point. A date comes as a plain number:
    Unix epoch seconds, always landing on midnight UTC, which is why it maps
    to a date rather than a datetime.

    Decided on the RAW NumericValue, not on the mapped one. sanitize_value()
    turns "Off"/"Aus" into 0.0, so a parameter that answered with a word would
    otherwise have looked like a number and become the 1st of January 1970 -
    caught by the golden matrix the moment it gained an EnumValues axis.

    Anything else stays a plain sensor. Guessing a writeable platform is the
    mistake this function exists to undo - the guess turned two dates into
    on/off switches, and switching one would have written epoch 0 or 1, i.e.
    1970, to a heating system.
    """
    if sent_a_number:
        return {**common_attributes, "platform": "date"}
    return None


def _writeable_entity(sensor: dict, parameter: dict, value: dict) -> dict | None:
    """The platform entity this parameter becomes, or None for a plain sensor.

    Three ways to get None, and the caller does not have to tell them apart:
    the portal does not allow writing this parameter, its data type has no
    writeable platform, or building one was refused (a dropdown with no
    options). All three mean the same thing here - the value stays the plain
    sensor already recorded.

    The IsWriteable test used to sit at the one call site, which split a
    single question across two places and put the whole thing one level
    deeper for no gain.
    """
    if not sensor["IsWriteable"]:
        return None

    data_type = sensor["DataType"]
    final_value = sensor["value"]

    common_attributes = {
        "friendlyName": sensor["friendlyName"],
        "ParameterID": sensor["ParameterID"],
        "unit": sensor["unit"],
        "icon": unit_to_icon(sensor["unit"]),
        "value": final_value,
        "DataType": data_type,
        "ModuleIndex": sensor["ModuleIndex"],
        "ModuleType": sensor["ModuleType"],
    }

    min_value, max_value = get_min_max(
        sensor["ParameterID"],
        data_type,
        parameter.get("MinValue"),
        parameter.get("MaxValue"),
    )

    if data_type in (WemDataType.NUMBER_STEP_HALF, WemDataType.NUMBER_STEP_ONE):
        return {
            **common_attributes,
            "platform": "number",
            "min_value": min_value,
            "max_value": max_value,
            "step": 0.5 if data_type == WemDataType.NUMBER_STEP_HALF else 1,
        }
    if data_type == WemDataType.SELECT:
        # `or []`, not .get()'s default: the portal sends the key with an
        # explicit null rather than omitting it, so the default never applied
        # and the comprehensions below iterated None. That raised inside the
        # caller's broad except, which logged an "unexpected error" nobody
        # could act on, once per value per cycle, and fell through to a plain
        # sensor.
        #
        # Returning None reaches the same plain sensor deliberately: a
        # dropdown with no options is not a better outcome than showing the
        # value, it is a broken control. Same behaviour as before, minus the
        # exception and the noise.
        enum_values = parameter.get("EnumValues") or []
        if not enum_values:
            return None
        return {
            **common_attributes,
            "platform": "select",
            "options": [enum_value["Value"] for enum_value in enum_values],
            "optionsNames": [enum_value["Name"] for enum_value in enum_values],
        }
    if data_type == WemDataType.SWITCH:
        if isinstance(final_value, str) and final_value.startswith("{"):
            return None  # It's a JSON schedule, fallback to sensor
        # Before the 0/1 test, because get_min_max() answers 0/1 for a
        # parameter that declared no bounds at all - so the test below cannot
        # tell "the portal says this is binary" from "the portal said
        # nothing". That is the whole defect: every unbounded time parameter
        # passed it and became a switch.
        if _is_time_or_programme(parameter):
            return _time_or_programme_entity(
                common_attributes, value.get("NumericValue") is not None
            )
        if int(min_value) == 0 and int(max_value) == 1:
            return {
                **common_attributes,
                "platform": "switch",
            }
        return {
            **common_attributes,
            "platform": "number",
            "min_value": min_value,
            "max_value": max_value,
            "step": 1,
        }
    return None


def _described_module(device_id, module, modules_dict):
    """The stored description of one answered module, or None to skip it.

    Two ways to have nothing: the answer is not shaped like a module at all,
    or it is one this integration never discovered.
    """
    try:
        module_tuple = (module["ModuleIndex"], module["ModuleType"])
    except (KeyError, TypeError) as exc:
        _LOGGER.warning("Skipping malformed module entry in API response: %s", exc)
        return None
    return modules_dict[device_id].get(module_tuple)


def _described_parameter(value, device_module):
    """The id and stored description of one answered value, or None to skip
    it. Same two ways to have nothing as above."""
    try:
        parameter_id = value["ParameterID"]
    except (KeyError, TypeError) as exc:
        _LOGGER.warning("Skipping malformed value entry in API response: %s", exc)
        return None
    if parameter_id not in device_module["parameters"]:
        return None
    return parameter_id, device_module["parameters"][parameter_id]


def _read_modules(device_id, values_json, modules_dict, language, api_data) -> dict:
    """Every value the portal returned, flattened - and every writeable one
    already placed on the platform its data type calls for."""
    parsed_sensors = {}

    for module in values_json.get("Modules", []):
        device_module = _described_module(device_id, module, modules_dict)
        if device_module is None:
            continue

        for value in module.get("Values", []):
            described = _described_parameter(value, device_module)
            if described is None:
                continue
            parameter_id, parameter = described

            try:
                name, sensor = _describe_value(
                    parameter_id, module, device_module, parameter, value, language
                )
                # Recorded before the platform decision below, which can
                # raise: a value that has no writeable platform - because
                # its type is unknown or because building it failed - must
                # still reach the second pass as a plain sensor.
                parsed_sensors[name] = sensor

                entity = _writeable_entity(sensor, parameter, value)
                if entity is not None:
                    api_data[device_id][name] = entity
            except Exception as exc:  # pylint: disable=broad-except
                # A single malformed/unexpected data point should never
                # cost us the rest of this device's update - log and
                # move on to the next value instead of letting the
                # exception abort processing for everything after it.
                _LOGGER.warning(
                    "Skipping value for parameter %s due to unexpected error: %s",
                    value.get("ParameterID", "?") if isinstance(value, dict) else "?",
                    exc,
                )
                continue

    return parsed_sensors


def _merge_into_scraped(
    device_id, key, sensor, language, scraping_mapper, api_data
) -> None:
    """Feed an API reading into the scraped entity that shows the same value,
    so both sources keep one entity instead of two that drift apart."""
    parameter_id = sensor["ParameterID"]
    if parameter_id not in scraping_mapper:
        for scraped_data in api_data[device_id].values():
            if not isinstance(scraped_data, dict):
                continue
            scraped_entity_id = scraped_data.get("ParameterID", "")
            try:
                scraped_part = scraped_entity_id.split("-")[1]
                translated_scraped = translate(
                    language, friendly_name_mapper(scraped_part)
                )

                sensor_words = _tokenize(sensor["friendlyName"])
                scraped_words = _tokenize(translated_scraped)

                if scraped_words and scraped_words.issubset(sensor_words):
                    scraping_mapper.setdefault(parameter_id, []).append(
                        scraped_entity_id
                    )
            except IndexError:
                pass

        if parameter_id not in scraping_mapper:
            scraping_mapper[parameter_id] = [key]

    for scraped_entity in scraping_mapper[parameter_id]:
        # An API read that came back empty must not erase a
        # web value that was scraped successfully in the same
        # cycle. Both paths feed this one entity, and writing
        # None over a good reading turned a partial API
        # failure into an unknown sensor.
        api_value = sensor.get("value")
        previous = api_data[device_id].get(scraped_entity, {})
        sensor_dict = {
            "value": (previous.get("value") if api_value is None else api_value),
            "name": previous.get("name"),
            "unit": previous.get("unit", sensor.get("unit")),
            "icon": previous.get("icon", unit_to_icon(sensor.get("unit"))),
            "friendlyName": previous.get("friendlyName", sensor.get("friendlyName")),
            "ParameterID": scraped_entity,
            "platform": "sensor",
        }
        if scraped_entity in api_data[device_id]:
            api_data[device_id][scraped_entity].update(sensor_dict)
        else:
            api_data[device_id][scraped_entity] = sensor_dict


def _emit_plain_sensor(device_id, key, sensor, api_data) -> None:
    """Write the reading as a read-only sensor, keeping the unit it already
    carried when this update brought none."""
    new_unit = sensor.get("unit")
    old_unit = api_data[device_id].get(key, {}).get("unit")
    final_unit = new_unit if new_unit not in (None, "") else old_unit

    api_data[device_id][key] = {
        "value": sensor["value"],
        "ParameterID": sensor["ParameterID"],
        "unit": final_unit,
        "icon": unit_to_icon(final_unit),
        "friendlyName": sensor["friendlyName"],
        "platform": "sensor",
    }


def _clear_unanswered(
    device_id, values_json, modules_dict, parsed_sensors, api_data
) -> None:
    """Stop presenting a reading the portal did not send this cycle.

    api_data is only rebuilt by get_devices(), which runs once per session,
    and every cycle writes only what came back. A parameter the portal leaves
    out therefore keeps its previous entry, and the entity goes on publishing
    it as current - for as long as the session lasts, with no log line saying
    anything is missing. Measured on the web path as a setpoint reading 50.5
    degrees for three hours; this is the same gap on the API path.

    Three deliberate limits on what gets cleared:

      * only inside modules the portal ANSWERED for. A module missing
        altogether is more likely a partial answer than a claim that none of
        its parameters has a value.
      * never a heating schedule (DataType 6). Those are fetched on their own
        path with their own hourly throttle, so their freshness is somebody
        else's job and clearing them here would throw away what that path
        maintains.
      * only the `value`. Unit, name and icon stay, so the entity keeps its
        identity and Home Assistant is not told a unit changed.

    Holiday begin and end fall in here too, and that is intended: the portal
    stops sending them once no holiday is set, and last August's date
    standing as current is the same mistake in a less obvious place.
    """
    device_data = api_data.get(device_id)
    if not device_data:
        return

    for module in values_json.get("Modules", []):
        try:
            module_key = (module["ModuleIndex"], module["ModuleType"])
        except (KeyError, TypeError):
            continue
        device_module = modules_dict.get(device_id, {}).get(module_key)
        if not device_module or not device_module.get("parameters"):
            continue

        cleared = []
        for parameter_id, parameter in device_module["parameters"].items():
            name = f"{device_module['Name']}-{parameter_id}"
            if name in parsed_sensors:
                continue
            entry = device_data.get(name)
            # A weekly programme is exempt, and asking the DECLARED type alone
            # got the wrong installations: a 3.1.3.0 portal types every
            # programme as 2 (an ordinary switch) with the schedule as JSON in
            # the value, so the exemption applied to nobody who has one. Same
            # mistake, same fix as the schedule fetch itself.
            if parameter.get("DataType") == WemDataType.PROGRAM or looks_like_schedule(
                (entry or {}).get("value")
            ):
                continue
            if isinstance(entry, dict) and entry.get("value") is not None:
                entry["value"] = None
                cleared.append(parameter_id)

        if cleared:
            _LOGGER.debug(
                "Device %s module %s/%s: the portal sent no value for %s; "
                "their last reading is not current any more.",
                device_id,
                module_key[0],
                module_key[1],
                ", ".join(sorted(cleared)),
            )


class WemPortalDataMapper:
    """Handles mapping of raw API and Scraped data into Home Assistant platforms."""

    @staticmethod
    def process_api_values(
        device_id: str,
        values_json: dict,
        modules_dict: dict,
        language: str,
        scraping_mapper: dict,
        mode: str,
        api_data: dict,
        scraper_device_id: str | None,
    ):
        """Processes the read values JSON and maps it to api_data."""

        parsed_sensors = _read_modules(
            device_id, values_json, modules_dict, language, api_data
        )

        # Process read-only sensors and fallback for unknown writeable datatypes
        for key, sensor in parsed_sensors.items():
            if sensor["IsWriteable"] and key in api_data.get(device_id, {}):
                continue

            # Merge an API sensor into its scraped counterpart only for
            # the device the scraper actually writes into - there is
            # exactly one (see resolve_scraper_device_id), and only its
            # dict can contain scraped rows to match against.
            #
            # This used to ask "is there fewer than one other device?"
            # instead, which happens to be the same thing on a
            # single-device installation but disabled the merge entirely
            # as soon as a second device existed. The API sensor was then
            # written under its own key next to the scraped row for the
            # same reading: two entities, two names, two values that
            # drift apart because they refresh on different schedules.
            if mode == "both" and device_id == scraper_device_id:
                _merge_into_scraped(
                    device_id, key, sensor, language, scraping_mapper, api_data
                )
            else:
                _emit_plain_sensor(device_id, key, sensor, api_data)

        # Last, so it sees everything this cycle actually wrote.
        _clear_unanswered(
            device_id, values_json, modules_dict, parsed_sensors, api_data
        )
