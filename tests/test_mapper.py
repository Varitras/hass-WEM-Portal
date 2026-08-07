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


def _parameter(parameter_id, **overrides):
    parameter = {
        "ParameterID": parameter_id,
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


def _value(parameter_id, numeric=None, string="", unit=None):
    return {
        "ParameterID": parameter_id,
        "NumericValue": numeric,
        "StringValue": string,
        "Unit": unit,
    }


def _process(
    modules,
    values,
    mode="api",
    language="en",
    existing=None,
    scraping_mapper=None,
    scraper_device_id=DEVICE,
):
    api_data = {DEVICE: dict(existing or {})}
    WemPortalDataMapper.process_api_values(
        DEVICE,
        values,
        modules,
        language,
        scraping_mapper if scraping_mapper is not None else {},
        mode,
        api_data,
        scraper_device_id,
    )
    return api_data[DEVICE]


# --- get_min_max ------------------------------------------------------


def test_explicit_bounds_win():
    assert get_min_max("anything", WemDataType.NUMBER_STEP_ONE, "5", "40") == (
        5.0,
        40.0,
    )


def test_switch_defaults_to_zero_one():
    assert get_min_max("anything", WemDataType.SWITCH, None, None) == (0.0, 1.0)


@pytest.mark.parametrize(
    ("parameter_id", "expected"),
    [
        ("WW_Solltemperatur", (30.0, 65.0)),
        ("Warmwasser", (30.0, 65.0)),
        ("Raumtemperatur", (5.0, 35.0)),
        ("Komfort", (5.0, 35.0)),
        ("Absenk", (5.0, 35.0)),
        ("Unbekannt", (0.0, 100.0)),
    ],
)
def test_bounds_are_guessed_from_the_parameter_name(parameter_id, expected):
    """Without bounds from the portal, the name decides the plausible range -
    a hot-water setpoint must not offer 0-100 °C."""
    assert (
        get_min_max(parameter_id, WemDataType.NUMBER_STEP_ONE, None, None) == expected
    )


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
    # No icon on purpose: °C carries a temperature device class, and an
    # explicit icon would override the one Home Assistant derives from it.
    assert sensor["icon"] is None


@pytest.mark.parametrize(
    ("data_type", "expected_step"),
    [(WemDataType.NUMBER_STEP_HALF, 0.5), (WemDataType.NUMBER_STEP_ONE, 1)],
)
def test_writeable_number_carries_its_step_and_bounds(data_type, expected_step):
    data = _process(
        _modules(
            _parameter(
                "Setpoint",
                IsWriteable=True,
                DataType=data_type,
                MinValue=10,
                MaxValue=30,
            )
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
            _parameter(
                "Pump",
                IsWriteable=True,
                DataType=WemDataType.SWITCH,
                EnumValues=[
                    {"Value": "0", "Name": "Aus"},
                    {"Value": "1", "Name": "Ein"},
                ],
            )
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
            _parameter(
                "Push",
                IsWriteable=True,
                DataType=WemDataType.SWITCH,
                MinValue=0,
                MaxValue=240,
            )
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
        _modules(_parameter("Program", IsWriteable=True, DataType=WemDataType.SWITCH)),
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
    """ "--" is missing data. Reported as 0 it would look like a real
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


def _scraped(parameter_id, friendly_name, value=None, unit="°C"):
    """One entry as the web scraper leaves it in api_data."""
    return {
        parameter_id: {
            "value": value,
            "name": parameter_id,
            "unit": unit,
            "icon": "mdi:thermometer",
            "friendlyName": friendly_name,
            "ParameterID": parameter_id,
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


def test_both_mode_still_merges_when_a_second_device_exists():
    """The merge belongs to the device the scraper writes into, not to
    installations that happen to have exactly one device.

    The condition used to be "fewer than two devices known", which is the
    same thing on a single-device system but switched the merge off entirely
    as soon as a second device appeared - leaving the API reading beside the
    scraped one as a second entity for the same measurement, with values
    drifting apart because the two paths refresh on different schedules.
    """
    api_data = {
        DEVICE: dict(_scraped("heat_pump-outside", "Heat pump - Outside", value=11.0)),
        "5678": {},
    }

    WemPortalDataMapper.process_api_values(
        DEVICE,
        _values(_value("Outside", numeric=12.5, unit="°C")),
        _modules(_parameter("Outside")),
        "en",
        {},
        "both",
        api_data,
        DEVICE,
    )

    assert api_data[DEVICE]["heat_pump-outside"]["value"] == 12.5
    assert "Heat pump-Outside" not in api_data[DEVICE], (
        "a second entity for one reading"
    )


def test_a_device_the_scraper_does_not_write_into_keeps_its_own_key():
    """The other side of the same coin: only the scraper's device may merge.

    Matching another device's readings against rows the scraper never put
    there would attribute one device's measurement to another.
    """
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside", value=11.0),
        scraper_device_id="5678",
    )

    assert data["Heat pump-Outside"]["value"] == 12.5
    assert data["heat_pump-outside"]["value"] == 11.0, (
        "another device's row was rewritten"
    )


def test_a_malformed_parameter_does_not_cost_the_others(caplog):
    """One bad data point must not abort the device's whole update.

    The broad handler in the per-value loop exists for this, and it is the
    most fragile thing in the function: the read-only bookkeeping entry is
    written BEFORE the writeable entity is built, so a failure halfway
    through leaves a half-processed parameter behind on purpose. Any
    restructuring that builds both and assigns them together at the end
    would silently drop the surviving half.
    """
    import logging

    modules = _modules(
        # EnumValues of non-dicts: the SELECT branch subscripts them and
        # raises, which no malformed VALUE could trigger.
        _parameter(
            "Broken",
            IsWriteable=True,
            DataType=WemDataType.SELECT,
            EnumValues=["not-a-dict"],
        ),
        _parameter("Healthy"),
    )
    values = _values(
        _value("Broken", string="Auto"),
        _value("Healthy", numeric=12.5, unit="°C"),
    )

    with caplog.at_level(logging.WARNING):
        data = _process(modules, values)

    assert "Broken" in caplog.text, "the skipped parameter went unreported"
    # The parameter after the failure still has to be processed.
    assert data["Heat pump-Healthy"]["value"] == 12.5
    # And the failed one still surfaces, as a plain sensor, from the entry
    # written before the exception.
    assert data["Heat pump-Broken"]["platform"] == "sensor"


def test_an_empty_api_value_does_not_erase_the_scraped_one():
    """Both paths feed the same entity in `both` mode.

    The API value was written over the scraped one unconditionally, so a
    parameter the API happened to return empty wiped a reading the web scrape
    had just collected successfully - turning a partial API failure into an
    unknown sensor.
    """
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=None, string="")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside", value=11.0),
    )

    assert data["heat_pump-outside"]["value"] == 11.0


def test_a_real_api_value_still_wins_over_the_scraped_one():
    """The API reading is the fresher of the two - only an EMPTY one is
    ignored, otherwise the merge would stop updating at all."""
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside", value=11.0),
    )

    assert data["heat_pump-outside"]["value"] == 12.5


# --- DataType 2 is overloaded: switch, schedule, or date ----------------
#
# The portal types holiday begin/end, a weekly heating schedule and a real
# on/off parameter identically. What separates them is what the parameter
# DECLARES, and the whole family used to collapse into "switch" because
# get_min_max() answers 0/1 for a parameter that declared no bounds at all.
#
# The shapes below are taken from a live installation's parameter list, not
# invented: holiday begin/end arrive as DataType 2 with MinValue and MaxValue
# null, EnumValues null, and a NumericValue holding Unix epoch seconds on an
# exact midnight UTC boundary.

HOLIDAY_BEGIN_EPOCH = 1785715200.0  # 2026-08-03 00:00:00 UTC


def test_an_unbounded_time_parameter_becomes_a_holiday_date():
    data = _process(
        _modules(
            _parameter(
                "U_Beginn",
                IsWriteable=True,
                DataType=WemDataType.SWITCH,
                MinValue=None,
                MaxValue=None,
                EnumValues=None,
            )
        ),
        _values(_value("U_Beginn", numeric=HOLIDAY_BEGIN_EPOCH)),
    )

    entity = data["Heat pump-U_Beginn"]
    assert entity["platform"] == "date", (
        "an unbounded time parameter became a writeable toggle again - "
        "switching it writes epoch 0/1, i.e. 1970, to the heating system"
    )
    assert entity["value"] == HOLIDAY_BEGIN_EPOCH


def test_an_unbounded_parameter_that_answered_with_a_word_is_not_a_date():
    """sanitize_value() turns "Off" into 0.0, which looks numeric.

    Deciding on the mapped value rather than the raw one would publish the
    1st of January 1970 as a date the user can act on.
    """
    data = _process(
        _modules(
            _parameter(
                "Something",
                IsWriteable=True,
                DataType=WemDataType.SWITCH,
                MinValue=None,
                MaxValue=None,
                EnumValues=None,
            )
        ),
        _values(_value("Something", string="Off")),
    )

    assert data["Heat pump-Something"]["platform"] == "sensor"


def test_an_unbounded_schedule_stays_a_sensor():
    """A weekly programme has the same type and bounds as a holiday date and
    differs only in carrying JSON."""
    data = _process(
        _modules(
            _parameter(
                "Heizprogramm1",
                IsWriteable=True,
                DataType=WemDataType.SWITCH,
                MinValue=None,
                MaxValue=None,
                EnumValues=None,
            )
        ),
        _values(_value("Heizprogramm1", string='{"MO-1":"00:00-24:00"}')),
    )

    assert data["Heat pump-Heizprogramm1"]["platform"] == "sensor"


def test_an_optionless_dropdown_stays_a_sensor():
    """The portal sends EnumValues as an explicit null, so .get()'s default
    never applied and the option comprehension iterated None. That raised into
    the caller's broad except once per value per cycle. The value already fell
    back to a plain sensor - this says so without the exception."""
    data = _process(
        _modules(
            _parameter(
                "Mystery",
                IsWriteable=True,
                DataType=WemDataType.SELECT,
                EnumValues=None,
            )
        ),
        _values(_value("Mystery", string="whatever")),
    )

    assert data["Heat pump-Mystery"]["platform"] == "sensor"


# --- a parameter the portal did not send is not current any more --------


def _two_parameters(**overrides):
    return _modules(
        _parameter("AktRaumSoll", **overrides),
        _parameter("Vorlaufsoll", **overrides),
    )


def test_a_parameter_the_portal_left_out_stops_being_current():
    """api_data is only rebuilt once per session, and each cycle writes only
    what came back - so a parameter the portal leaves out kept its previous
    entry and went on being published as current."""
    modules = _two_parameters()
    existing = {
        "Heat pump-AktRaumSoll": {
            "value": 21.0,
            "ParameterID": "AktRaumSoll",
            "unit": "°C",
            "platform": "sensor",
        },
        "Heat pump-Vorlaufsoll": {
            "value": 50.5,
            "ParameterID": "Vorlaufsoll",
            "unit": "°C",
            "platform": "sensor",
        },
    }

    data = _process(
        modules,
        _values(_value("AktRaumSoll", numeric=22.0)),
        existing=existing,
    )

    assert data["Heat pump-AktRaumSoll"]["value"] == 22.0
    assert data["Heat pump-Vorlaufsoll"]["value"] is None, (
        "a reading the portal did not send was still reported as current"
    )
    assert data["Heat pump-Vorlaufsoll"]["unit"] == "°C", "the unit was thrown away"


def test_a_module_the_portal_did_not_answer_for_is_left_alone():
    """A whole module missing is more likely a partial answer than a claim
    that none of its parameters has a value."""
    modules = _two_parameters()
    existing = {
        "Heat pump-AktRaumSoll": {
            "value": 21.0,
            "ParameterID": "AktRaumSoll",
            "unit": "°C",
            "platform": "sensor",
        },
    }

    data = _process(modules, {"Modules": []}, existing=existing)

    assert data["Heat pump-AktRaumSoll"]["value"] == 21.0


def test_a_heating_schedule_is_not_cleared_by_the_value_read():
    """Schedules are fetched on their own path with their own hourly
    throttle, so clearing them here throws away what that path maintains."""
    modules = _modules(
        _parameter("AktRaumSoll"),
        _parameter("Heizprogramm1", DataType=WemDataType.PROGRAM),
    )
    existing = {
        "Heat pump-Heizprogramm1": {
            "value": "Active",
            "ParameterID": "Heizprogramm1",
            "unit": None,
            "platform": "sensor",
            "CircuitTimesDay": [{"day": "MO"}],
        },
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-Heizprogramm1"]["value"] == "Active"
    assert data["Heat pump-Heizprogramm1"]["CircuitTimesDay"] == [{"day": "MO"}]


def test_a_schedule_typed_as_a_switch_is_not_cleared_either():
    """The same exemption, for the installations that actually have one.

    A 3.1.3.0 portal types every weekly programme as DataType 2 - an ordinary
    switch - and puts the schedule as JSON in the value. Keyed on the declared
    type alone, the exemption applied to nobody with such a portal, so the
    programme the schedule fetch maintains was blanked by the value read.
    """
    modules = _modules(
        _parameter("AktRaumSoll"),
        _parameter("Heizprogramm1", DataType=WemDataType.SWITCH),
    )
    existing = {
        "Heat pump-Heizprogramm1": {
            "value": '{"MO-1": "06:00-22:00"}',
            "ParameterID": "Heizprogramm1",
            "unit": None,
            "platform": "sensor",
            "CircuitTimesDay": [{"day": "MO"}],
        },
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-Heizprogramm1"]["value"] == '{"MO-1": "06:00-22:00"}'


def test_an_ordinary_switch_the_portal_dropped_is_still_cleared():
    """The counter-test: exempting every DataType 2 parameter would take the
    freshness rule off real switches, which is most of them."""
    modules = _modules(
        _parameter("AktRaumSoll"),
        _parameter("Pumpe", DataType=WemDataType.SWITCH),
    )
    existing = {
        "Heat pump-Pumpe": {
            "value": "on",
            "ParameterID": "Pumpe",
            "unit": None,
            "platform": "switch",
        },
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-Pumpe"]["value"] is None


def test_a_holiday_date_stops_being_current_once_the_portal_drops_it():
    """Intended, and the reason this is not an exception: the portal stops
    sending holiday begin once no holiday is set, and last August's date
    standing as current is the same mistake in a less obvious place."""
    modules = _modules(
        _parameter("AktRaumSoll"),
        _parameter(
            "U_Beginn",
            IsWriteable=True,
            DataType=WemDataType.SWITCH,
            MinValue=None,
            MaxValue=None,
            EnumValues=None,
        ),
    )
    existing = {
        "Heat pump-U_Beginn": {
            "value": 1785715200.0,
            "ParameterID": "U_Beginn",
            "unit": None,
            "platform": "date",
        },
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-U_Beginn"]["value"] is None
