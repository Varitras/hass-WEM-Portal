"""Tests for the API data mapper.

process_api_values() decides which Home Assistant platform every portal
parameter becomes and what value it carries. A mistake here is silent: the
integration still starts, it just exposes the wrong entity type or a wrong
value - so the branch-by-branch mapping is worth pinning down.
"""

import pytest

from custom_components.wemportal.const import WemDataType
from custom_components.wemportal.mapper import WemPortalDataMapper, get_min_max

DEVICE = "1234"
MODULE_KEY = (0, 1)


def _parameter(param_id, **overrides):
    parameter = {
        "ParameterID": param_id,
        "IsWriteable": False,
        "DataType": None,
        "MinValue": None,
        "MaxValue": None,
    }
    parameter.update(overrides)
    return parameter


def _modules(*parameters):
    return {
        DEVICE: {
            MODULE_KEY: {
                "Name": "Heat pump",
                "parameters": {p["ParameterID"]: p for p in parameters},
            }
        }
    }


def _values(*values):
    return {
        "Modules": [
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": list(values),
            }
        ]
    }


def _value(param_id, numeric=None, string="", unit=None):
    return {
        "ParameterID": param_id,
        "NumericValue": numeric,
        "StringValue": string,
        "Unit": unit,
    }


def _process(modules, values, mode="api", language="en", existing=None,
             scraping_mapper=None):
    api_data = {DEVICE: dict(existing or {})}
    WemPortalDataMapper.process_api_values(
        DEVICE, values, modules, language,
        scraping_mapper if scraping_mapper is not None else {},
        mode, api_data,
    )
    return api_data[DEVICE]


# --- get_min_max ------------------------------------------------------


def test_explicit_bounds_win():
    assert get_min_max("anything", WemDataType.NUMBER_STEP_ONE, "5", "40") == (5.0, 40.0)


def test_switch_defaults_to_zero_one():
    assert get_min_max("anything", WemDataType.SWITCH, None, None) == (0.0, 1.0)


@pytest.mark.parametrize(
    ("param_id", "expected"),
    [
        ("WW_Solltemperatur", (30.0, 65.0)),
        ("Warmwasser", (30.0, 65.0)),
        ("Raumtemperatur", (5.0, 35.0)),
        ("Komfort", (5.0, 35.0)),
        ("Absenk", (5.0, 35.0)),
        ("Unbekannt", (0.0, 100.0)),
    ],
)
def test_bounds_are_guessed_from_the_parameter_name(param_id, expected):
    """Without bounds from the portal, the name decides the plausible range -
    a hot-water setpoint must not offer 0-100 °C."""
    assert get_min_max(param_id, WemDataType.NUMBER_STEP_ONE, None, None) == expected


def test_unparsable_bounds_fall_back_instead_of_raising():
    assert get_min_max("x", WemDataType.NUMBER_STEP_ONE, "n/a", "n/a") == (0.0, 100.0)


# --- platform mapping -------------------------------------------------


def test_read_only_parameter_becomes_a_sensor():
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
    )

    sensor = data["Heat pump-Outside"]
    assert sensor["platform"] == "sensor"
    assert sensor["value"] == 12.5
    assert sensor["unit"] == "°C"
    assert sensor["icon"] == "mdi:thermometer"


@pytest.mark.parametrize(
    ("data_type", "expected_step"),
    [(WemDataType.NUMBER_STEP_HALF, 0.5), (WemDataType.NUMBER_STEP_ONE, 1)],
)
def test_writeable_number_carries_its_step_and_bounds(data_type, expected_step):
    data = _process(
        _modules(
            _parameter("Setpoint", IsWriteable=True, DataType=data_type,
                       MinValue=10, MaxValue=30)
        ),
        _values(_value("Setpoint", numeric=21, unit="°C")),
    )

    entity = data["Heat pump-Setpoint"]
    assert entity["platform"] == "number"
    assert entity["step"] == expected_step
    assert (entity["min_value"], entity["max_value"]) == (10.0, 30.0)


def test_writeable_enum_becomes_a_select_with_both_option_lists():
    """select.py matches the raw portal value against `options`, while
    `optionsNames` is what the user sees - both must survive."""
    data = _process(
        _modules(
            _parameter(
                "Mode",
                IsWriteable=True,
                DataType=WemDataType.SELECT,
                EnumValues=[
                    {"Value": "0", "Name": "Auto"},
                    {"Value": "1", "Name": "Manual"},
                ],
            )
        ),
        _values(_value("Mode", string="Auto")),
    )

    entity = data["Heat pump-Mode"]
    assert entity["platform"] == "select"
    assert entity["options"] == ["0", "1"]
    assert entity["optionsNames"] == ["Auto", "Manual"]
    assert entity["value"] == "Auto"


def test_binary_switch_becomes_a_switch():
    data = _process(
        _modules(
            _parameter("Pump", IsWriteable=True, DataType=WemDataType.SWITCH,
                       EnumValues=[{"Value": "0", "Name": "Aus"},
                                   {"Value": "1", "Name": "Ein"}])
        ),
        _values(_value("Pump", string="Ein")),
    )

    entity = data["Heat pump-Pump"]
    assert entity["platform"] == "switch"
    # sanitize_value() normalises the German on/off wording to a number.
    assert entity["value"] == 1.0


def test_switch_with_a_wider_range_becomes_a_number():
    """A SWITCH data type whose bounds are not 0/1 is really a stepped value
    (e.g. a 0-240 minute duration) and must not become a toggle."""
    data = _process(
        _modules(
            _parameter("Push", IsWriteable=True, DataType=WemDataType.SWITCH,
                       MinValue=0, MaxValue=240)
        ),
        _values(_value("Push", numeric=60)),
    )

    entity = data["Heat pump-Push"]
    assert entity["platform"] == "number"
    assert (entity["min_value"], entity["max_value"]) == (0.0, 240.0)


def test_json_schedule_falls_back_to_a_sensor():
    """A time program arrives as a JSON blob - it is neither a switch nor a
    number, so it stays a plain sensor."""
    data = _process(
        _modules(
            _parameter("Program", IsWriteable=True, DataType=WemDataType.SWITCH)
        ),
        _values(_value("Program", string='{"Mon":[]}')),
    )

    assert data["Heat pump-Program"]["platform"] == "sensor"


def test_unknown_writeable_data_type_falls_back_to_a_sensor():
    data = _process(
        _modules(_parameter("Odd", IsWriteable=True, DataType=WemDataType.PROGRAM)),
        _values(_value("Odd", numeric=1)),
    )

    assert data["Heat pump-Odd"]["platform"] == "sensor"


# --- robustness -------------------------------------------------------


def test_unknown_module_and_parameter_are_ignored():
    """Only what discovery already knows may become an entity."""
    modules = _modules(_parameter("Known"))
    values = {
        "Modules": [
            {"ModuleIndex": 9, "ModuleType": 9, "Values": [_value("Known", numeric=1)]},
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [_value("Unknown", numeric=1)],
            },
        ]
    }

    assert _process(modules, values) == {}


def test_malformed_entries_do_not_cost_the_remaining_values():
    """One broken module/value must not abort the whole device update."""
    modules = _modules(_parameter("Good"))
    values = {
        "Modules": [
            {"Values": []},  # no ModuleIndex/ModuleType
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [
                    {"NumericValue": 1},  # no ParameterID
                    _value("Good", numeric=42),
                ],
            },
        ]
    }

    data = _process(modules, values)

    assert data["Heat pump-Good"]["value"] == 42


def test_missing_value_becomes_none_rather_than_zero():
    """"--" is missing data. Reported as 0 it would look like a real
    reading and could trigger automations."""
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", string="--", unit="°C")),
    )

    assert data["Heat pump-Outside"]["value"] is None


def test_friendly_name_does_not_repeat_the_module_name():
    """The module name is only prefixed when it is not already contained in
    the parameter name - otherwise entities read "Heat pump Heat pump ...".

    Note both names pass through translate(), which also normalises casing
    ("Heat pump" -> "Heat Pump").
    """
    data = _process(
        _modules(_parameter("Outside"), _parameter("Heat pump status")),
        _values(
            _value("Outside", numeric=1),
            _value("Heat pump status", numeric=1),
        ),
    )

    assert data["Heat pump-Outside"]["friendlyName"] == "Heat Pump Outside"
    assert data["Heat pump-Heat pump status"]["friendlyName"] == "Heat Pump Status"


# --- mode "both": API values merged onto scraped sensors --------------


def _scraped(param_id, friendly_name, value=None, unit="°C"):
    """One entry as the web scraper leaves it in api_data."""
    return {
        param_id: {
            "value": value,
            "name": param_id,
            "unit": unit,
            "icon": "mdi:thermometer",
            "friendlyName": friendly_name,
            "ParameterID": param_id,
            "platform": "sensor",
        }
    }


def test_both_mode_writes_the_api_value_onto_the_matching_scraped_sensor():
    """In mode "both" the same reading arrives twice (scraped + API). The
    API value is merged into the EXISTING scraped entity instead of adding
    a second one, so there are no duplicate sensors for one reading."""
    scraped = _scraped("heat_pump-outside", "Heat pump - Outside", value=11.0)

    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=scraped,
    )

    assert data["heat_pump-outside"]["value"] == 12.5
    assert "Heat pump-Outside" not in data, "API value must not create a second sensor"


def test_both_mode_remembers_the_match_in_the_scraping_mapper():
    """The correlation is cached per parameter, so later cycles reuse it
    instead of re-tokenising every scraped name."""
    scraping_mapper = {}

    _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside"),
        scraping_mapper=scraping_mapper,
    )

    assert scraping_mapper["Outside"] == ["heat_pump-outside"]


def test_both_mode_keeps_an_unmatched_api_value_under_its_own_key():
    """No scraped counterpart: the reading must still surface, under the
    API's own key, rather than being dropped."""
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
    )

    assert data["Heat pump-Outside"]["value"] == 12.5
