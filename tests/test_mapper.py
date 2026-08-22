"""Tests for the API data mapper.

process_api_values() decides which Home Assistant platform every portal
parameter becomes and what value it carries. A mistake here is silent: the
integration still starts, it just exposes the wrong entity type or a wrong
value - so the branch-by-branch mapping is worth pinning down.
"""

import pytest

from custom_components.wemportal.const import WemDataType
from custom_components.wemportal.models import ModuleRef, Reading
from custom_components.wemportal.mapper import WemPortalDataMapper, get_min_max

DEVICE = "1234"
MODULE_KEY = (0, 1)
# A second module of the SAME type: two heating circuits share one parameter
# catalogue, so the same ParameterID legitimately appears twice on a device.
SECOND_MODULE_KEY = (1, 1)


# A week the device really reported a programme for: the switching times
# are what makes it one, and both the read and the ageing exemption ask
# for them now.
A_FED_WEEK = [{"Day": 1, "CircuitTimes": [{"MinutesSinceMidnight": 360, "Value": 3}]}]


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
                "parameters": {
                    parameter["ParameterID"]: parameter for parameter in parameters
                },
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
    scrape_still_feeds=None,
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
        scrape_still_feeds,
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


def test_a_parameter_with_no_id_falls_back_instead_of_raising():
    """The name is what the guess is made from - and there may not be one.

    `Reading.parameter_id` is optional, and the mapper hands exactly that
    field in. With no bounds from the portal and a data type that is not a
    switch, the guess reached `.lower()` on None and took the whole device's
    read down with an AttributeError. Nothing to guess from is not an error;
    it is the case the widest default exists for.

    Found by widening the mypy scope to mapper.py, which is the reason the
    scope is worth widening.
    """
    assert get_min_max(None, WemDataType.NUMBER_STEP_ONE, None, None) == (0.0, 100.0)


def test_a_parameter_with_no_data_type_still_answers():
    """Same shape one argument over: the type is read from the portal's
    answer and can be absent."""
    assert get_min_max("Komfort", None, None, None) == (5.0, 35.0)


# --- platform mapping -------------------------------------------------


def test_read_only_parameter_becomes_a_sensor():
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
    )

    sensor = data["Heat pump-Outside"]
    assert sensor.platform == "sensor"
    assert sensor.value == 12.5
    assert sensor.unit == "°C"
    # No icon on purpose: °C carries a temperature device class, and an
    # explicit icon would override the one Home Assistant derives from it.
    assert sensor.icon is None


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
    assert entity.platform == "number"
    assert entity.step == expected_step
    assert (entity.min_value, entity.max_value) == (10.0, 30.0)


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
    assert entity.platform == "select"
    assert entity.options == ["0", "1"]
    assert entity.options_names == ["Auto", "Manual"]
    assert entity.value == "Auto"


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
    assert entity.platform == "switch"
    # sanitize_value() normalises the German on/off wording to a number.
    assert entity.value == 1.0


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
    assert entity.platform == "number"
    assert (entity.min_value, entity.max_value) == (0.0, 240.0)


def test_json_schedule_falls_back_to_a_sensor():
    """A time program arrives as a JSON blob - it is neither a switch nor a
    number, so it stays a plain sensor."""
    data = _process(
        _modules(_parameter("Program", IsWriteable=True, DataType=WemDataType.SWITCH)),
        _values(_value("Program", string='{"Mon":[]}')),
    )

    assert data["Heat pump-Program"].platform == "sensor"


def test_unknown_writeable_data_type_falls_back_to_a_sensor():
    data = _process(
        _modules(_parameter("Odd", IsWriteable=True, DataType=WemDataType.PROGRAM)),
        _values(_value("Odd", numeric=1)),
    )

    assert data["Heat pump-Odd"].platform == "sensor"


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

    assert data["Heat pump-Good"].value == 42


def test_missing_value_becomes_none_rather_than_zero():
    """ "--" is missing data. Reported as 0 it would look like a real
    reading and could trigger automations."""
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", string="--", unit="°C")),
    )

    assert data["Heat pump-Outside"].value is None


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

    assert data["Heat pump-Outside"].friendly_name == "Heat Pump Outside"
    assert data["Heat pump-Heat pump status"].friendly_name == "Heat Pump Status"


# --- mode "both": API values merged onto scraped sensors --------------


def _scraped(parameter_id, friendly_name, value=None, unit="°C"):
    """One entry as the web scraper leaves it in api_data."""
    return {
        parameter_id: Reading(
            value=value,
            unit=unit,
            icon="mdi:thermometer",
            friendly_name=friendly_name,
            parameter_id=parameter_id,
            platform="sensor",
        )
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

    assert data["heat_pump-outside"].value == 12.5
    assert "Heat pump-Outside" not in data, "API value must not create a second sensor"


def test_both_mode_remembers_the_match_in_the_scraping_mapper():
    """The correlation is cached per parameter OF ITS MODULE, so later cycles
    reuse it instead of re-tokenising every scraped name - and a second module
    with the same parameter id gets an entry of its own."""
    scraping_mapper = {}

    _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside"),
        scraping_mapper=scraping_mapper,
    )

    assert scraping_mapper[(DEVICE, ModuleRef(*MODULE_KEY), "Outside")] == [
        "heat_pump-outside"
    ]


def test_a_scraper_arriving_later_takes_the_api_row_it_replaces():
    """The API can run alone for a while - `both` mode with the web half
    failing, or simply a scrape that has not succeeded yet.

    Every cycle in that state writes a row under the parameter's own key.
    When the scrape finally works, the value belongs in the SCRAPED row - but
    the old one was accepted as a merge target of its own, because the test
    for "is this a scraped row" was the shape of the name and an API row
    written under its own key has that same shape. So the reading went to
    both, and entities are built from whatever rows exist: two entities for
    one measurement.
    """
    api_only = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
    )
    assert "Heat pump-Outside" in api_only, "the api-only cycle wrote no row at all"

    merged = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=13.5, unit="°C")),
        mode="both",
        existing={
            **api_only,
            **_scraped("heat_pump-outside", "Heat pump - Outside"),
        },
    )

    assert merged["heat_pump-outside"].value == 13.5
    assert "Heat pump-Outside" not in merged, (
        "the row from before the scrape existed is still there, and the "
        "reading is now published twice"
    )


def test_a_merged_parameter_left_out_of_the_answer_is_cleared_where_it_lives():
    """The ageing pass looked under the API key, and the value is not there.

    A parameter merged into a scraped row has no row of its own any more, so
    `f"{module}-{parameter}"` finds nothing and the pass moves on - while the
    row that actually carries the reading keeps showing what the last answer
    said, with nothing left to age it.
    """
    scraping_mapper = {}
    merged = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside"),
        scraping_mapper=scraping_mapper,
    )
    assert merged["heat_pump-outside"].value == 12.5

    # The next answer leaves the parameter out entirely.
    aged = _process(
        _modules(_parameter("Outside")),
        _values(),
        mode="both",
        existing=merged,
        scraping_mapper=scraping_mapper,
    )

    assert aged["heat_pump-outside"].value is None, (
        "a merged reading nothing refreshed is still shown as current"
    )


def test_a_scrape_fresh_merged_row_survives_an_api_omission():
    """Both sources feed one row in `both` mode, on different schedules.

    The API leaving a parameter out is evidence about the API only. Blanking a
    row the scrape delivered THIS cycle throws away a value seconds old -
    _forget_scraped_values ages it on the scrape's own terms instead. The
    merge target is a scraped key, and whether the scrape still feeds it is a
    question only the api instance can answer, so the caller injects it.
    """
    scraping_mapper = {}
    merged = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside"),
        scraping_mapper=scraping_mapper,
    )
    assert merged["heat_pump-outside"].value == 12.5

    aged = _process(
        _modules(_parameter("Outside")),
        _values(),
        mode="both",
        existing=merged,
        scraping_mapper=scraping_mapper,
        scrape_still_feeds=lambda key: key == "heat_pump-outside",
    )

    assert aged["heat_pump-outside"].value == 12.5, (
        "a merged row the scrape delivered this cycle was blanked because the "
        "API left its counterpart out"
    )


def test_a_merged_row_the_scrape_stopped_feeding_is_cleared_on_omission():
    """The counter-test: the exemption is a condition, not a blanket.

    Once the scrape has stopped delivering the row, the API leaving its
    parameter out is the only evidence there is that the reading ended - and
    exempting it then would leave a value neither source refreshes standing as
    current, which is the very mistake the freshness rule exists against.
    """
    scraping_mapper = {}
    merged = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside"),
        scraping_mapper=scraping_mapper,
    )

    aged = _process(
        _modules(_parameter("Outside")),
        _values(),
        mode="both",
        existing=merged,
        scraping_mapper=scraping_mapper,
        scrape_still_feeds=lambda key: False,
    )

    assert aged["heat_pump-outside"].value is None, (
        "a merged row neither source refreshes is still shown as current"
    )


def test_a_module_id_the_portal_sent_as_a_list_costs_only_that_module():
    """The guard wraps building the key, not looking it up - and the lookup
    is where it breaks.

    `ModuleIndex: []` builds a ModuleRef fine and then raises TypeError at
    the dict lookup, one line below the except that was meant to catch it.
    Nothing above catches it either, so one unusable row from the portal
    took every LATER module of the same device with it - the readings the
    portal answered correctly included.
    """
    modules = {
        DEVICE: {
            MODULE_KEY: {
                "Name": "Heat pump",
                "parameters": {"P1": _parameter("P1")},
            }
        }
    }
    values = {
        "Modules": [
            {"ModuleIndex": [], "ModuleType": 1, "Values": []},
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [_value("P1", numeric=21.0)],
            },
        ]
    }

    data = _process(modules, values)

    assert data["Heat pump-P1"].value == 21.0, (
        "the good module was never read - one malformed row aborted the whole device"
    )


def test_a_parameter_id_the_portal_sent_as_a_dict_costs_only_that_value():
    """Same shape one level down: the membership test hashes the id, and it
    sits outside the guard that exists for exactly this."""
    values = {
        "Modules": [
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [
                    {"ParameterID": {}, "NumericValue": 1, "StringValue": ""},
                    _value("P1", numeric=21.0),
                ],
            }
        ]
    }

    data = _process(_modules(_parameter("P1")), values)

    assert data["Heat pump-P1"].value == 21.0, (
        "one unusable value aborted the rest of the module"
    )


def test_a_module_whose_value_list_is_null_costs_only_that_module():
    """Third shape of the same failure, and the one the portal itself
    produces: a module with nothing to report comes back as `Values: null`.

    `.get("Values", [])` returns the default only for an ABSENT key - a
    present null comes back as None, and iterating that raises past every
    guard here. One quiet module then costs the whole device its readings,
    every cycle, for as long as the portal keeps sending it that way.
    """
    # Both modules KNOWN: an unknown one is skipped before its values are
    # ever read, so a test using one would pass without reaching the line
    # under test.
    modules = {
        DEVICE: {
            SECOND_MODULE_KEY: {"Name": "Circuit", "parameters": {}},
            MODULE_KEY: {
                "Name": "Heat pump",
                "parameters": {"P1": _parameter("P1")},
            },
        }
    }
    values = {
        "Modules": [
            {
                "ModuleIndex": SECOND_MODULE_KEY[0],
                "ModuleType": SECOND_MODULE_KEY[1],
                "Values": None,
            },
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [_value("P1", numeric=21.0)],
            },
        ]
    }

    data = _process(modules, values)

    assert data["Heat pump-P1"].value == 21.0, (
        "a module with nothing to say took the whole device's readings with it"
    )


def test_a_device_whose_module_list_is_null_is_not_a_crash():
    """The same null one level up. Nothing to read is not an error - the
    device simply reported nothing this cycle."""
    assert _process(_modules(_parameter("P1")), {"Modules": None}) == {}


def _programme_left_out_of_the_answer(circuit_times_day):
    """The module answers, but without its programme parameter."""
    return _process(
        _modules(_parameter("Programme", DataType=WemDataType.PROGRAM)),
        _values(),
        existing={
            "Heat pump-Programme": Reading(
                parameter_id="Programme",
                value="MoDiMi",
                data_type=WemDataType.PROGRAM,
                circuit_times_day=circuit_times_day,
            )
        },
    )


def test_a_programme_the_schedule_fetch_still_feeds_survives_a_missing_value():
    """Programmes are exempt from this ageing because the schedule fetch owns
    their staleness - and the detail it attached is the evidence that it is
    still delivering."""
    data = _programme_left_out_of_the_answer(circuit_times_day=A_FED_WEEK)

    assert data["Heat pump-Programme"].value == "MoDiMi"


def test_a_programme_nobody_refreshes_any_more_ages_out_here_too():
    """The sibling of the module-level ageing, and the half left behind.

    That one learned to treat the exemption as a CONDITION: it holds while
    the schedule fetch is still delivering, and that fetch drops its own
    detail when a due refresh fails. Here the exemption stayed a category, so
    a programme neither source refreshes went on showing its pre-outage plan
    - the very case the other repair was about.
    """
    data = _programme_left_out_of_the_answer(circuit_times_day=None)

    assert data["Heat pump-Programme"].value is None, (
        "a programme neither source has refreshed is still shown as current"
    )


def test_a_value_read_keeps_the_schedule_detail_the_hourly_fetch_attached():
    """The programme fetch runs once an hour, the value read every cycle.

    _emit_plain_sensor builds a fresh Reading from a fixed set of fields, so
    the week the portal reported was dropped again on the next ordinary
    cycle and the sensor fell back to its raw JSON view - eleven cycles out
    of twelve, then back for one. The sibling path keeps it on purpose; the
    comment there names a schedule detail as the thing that must survive.

    Clearing it is the schedule path's own job: a refresh that fails sets
    both fields to None, so nothing stale can hide behind this.
    """
    existing = {
        "Heat pump-Programm": Reading(
            value='{"Mon":[]}',
            parameter_id="Programm",
            platform="sensor",
            circuit_times_day=[{"Mon": ["06:00"]}],
            possible_values=["Normal"],
        )
    }

    data = _process(
        _modules(_parameter("Programm", DataType=WemDataType.PROGRAM)),
        _values(_value("Programm", string='{"Mon":[]}')),
        existing=existing,
    )

    row = data["Heat pump-Programm"]
    assert row.circuit_times_day == [{"Mon": ["06:00"]}], (
        "the week the hourly fetch reported was thrown away by an ordinary value read"
    )
    assert row.possible_values == ["Normal"]
    assert row.data_type == WemDataType.PROGRAM, (
        "the declared type went missing, and it is what tells a programme "
        "from an ordinary sensor further down"
    )


def _two_circuits(first_value, second_value):
    """Two modules of one type, both describing the same ParameterID."""
    modules = {
        DEVICE: {
            MODULE_KEY: {
                "Name": "Heating circuit 1",
                "parameters": {"P1": _parameter("P1")},
            },
            SECOND_MODULE_KEY: {
                "Name": "Heating circuit 2",
                "parameters": {"P1": _parameter("P1")},
            },
        }
    }
    values = {
        "Modules": [
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [_value("P1", numeric=first_value, unit="°C")],
            },
            {
                "ModuleIndex": SECOND_MODULE_KEY[0],
                "ModuleType": SECOND_MODULE_KEY[1],
                "Values": [_value("P1", numeric=second_value, unit="°C")],
            },
        ]
    }
    return modules, values


def test_two_modules_sharing_a_parameter_id_keep_their_own_readings():
    """A parameter id identifies a parameter WITHIN its module, not on the
    device - and the merge cache was keyed on the bare id.

    Two heating circuits are two modules of one type with one parameter
    catalogue, so the same id on one device is the ordinary case, not an
    exotic one. The second circuit therefore found the first circuit's entry
    already in the cache, wrote ITS value into the first circuit's reading
    and stamped its own module address on it - so circuit 1 published circuit
    2's temperature, circuit 2 got no reading at all and thus no entity, and
    the per-module ageing pass was watching the wrong module. Nothing logged.
    """
    modules, values = _two_circuits(21.0, 45.0)

    data = _process(modules, values, mode="both")

    first = data.get("Heating circuit 1-P1")
    second = data.get("Heating circuit 2-P1")
    assert first is not None and second is not None, (
        f"a circuit lost its reading entirely, so it can never get an "
        f"entity: {sorted(data)}"
    )
    assert (first.value, second.value) == (21.0, 45.0), (
        "one circuit is publishing the other's value as its own"
    )
    assert (first.module_index, first.module_type) == MODULE_KEY
    assert (second.module_index, second.module_type) == SECOND_MODULE_KEY


def test_only_one_module_may_claim_the_same_scraped_row():
    """Both circuits' names contain the scraped row's words, so both would
    merge into it - and the second would overwrite the first's value in it.

    A scraped row shows one value, so at most one API reading can BE that
    row. The one that gets there first keeps it; the other stays under its
    own key, which is the outcome that costs nothing: an extra entity beats
    two circuits sharing one.
    """
    modules, values = _two_circuits(21.0, 45.0)
    scraping_mapper = {}

    data = _process(
        modules,
        values,
        mode="both",
        existing=_scraped("heat_pump-p1", "Heating circuit - P1"),
        scraping_mapper=scraping_mapper,
    )

    targets = [target for targets in scraping_mapper.values() for target in targets]
    assert len(targets) == len(set(targets)), (
        f"two modules were pointed at the same target row: {scraping_mapper}"
    )
    assert {21.0, 45.0} == {
        row.value for row in data.values() if isinstance(row, Reading)
    }, f"a circuit's value was overwritten by the other's: {data}"


def test_both_mode_keeps_an_unmatched_api_value_under_its_own_key():
    """No scraped counterpart: the reading must still surface, under the
    API's own key, rather than being dropped."""
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
    )

    assert data["Heat pump-Outside"].value == 12.5


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

    assert api_data[DEVICE]["heat_pump-outside"].value == 12.5
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

    assert data["Heat pump-Outside"].value == 12.5
    assert data["heat_pump-outside"].value == 11.0, "another device's row was rewritten"


def test_two_devices_sharing_a_module_and_parameter_age_independently():
    """The merge cache was keyed on (module, parameter) with no device.

    Only the scraper device writes into the cache, but the ageing pass reads
    it for EVERY device. A second device carrying the same module address and
    parameter id as a scraper-device merge therefore looked up the scraper
    device's scraped target, found it absent from its own dict, and left its
    own stale reading standing - so a parameter that second device stopped
    answering kept its last value for the rest of the session.
    """
    scraping_mapper = {}
    other_device = "5678"
    modules = {
        DEVICE: {
            MODULE_KEY: {
                "Name": "Heat pump",
                "parameters": {"Outside": _parameter("Outside")},
            }
        },
        other_device: {
            MODULE_KEY: {
                "Name": "Circuit",
                "parameters": {"Outside": _parameter("Outside")},
            }
        },
    }
    api_data = {
        DEVICE: dict(_scraped("heat_pump-outside", "Heat pump - Outside")),
        other_device: {
            "Circuit-Outside": Reading(
                value=45.0,
                parameter_id="Outside",
                unit="°C",
                platform="sensor",
                module_index=MODULE_KEY[0],
                module_type=MODULE_KEY[1],
            )
        },
    }

    # The scraper device answers and merges Outside into its scraped row,
    # filling the cache under (module, parameter).
    WemPortalDataMapper.process_api_values(
        DEVICE,
        _values(_value("Outside", numeric=21.0, unit="°C")),
        modules,
        "en",
        scraping_mapper,
        "both",
        api_data,
        DEVICE,
    )

    # The other device answers for the module but leaves Outside out entirely.
    WemPortalDataMapper.process_api_values(
        other_device,
        _values(),
        modules,
        "en",
        scraping_mapper,
        "both",
        api_data,
        DEVICE,
    )

    assert api_data[other_device]["Circuit-Outside"].value is None, (
        "the second device's dropped parameter kept its value because the "
        "merge cache the ageing pass consulted had no device in its key"
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
    assert data["Heat pump-Healthy"].value == 12.5
    # And the failed one still surfaces, as a plain sensor, from the entry
    # written before the exception.
    assert data["Heat pump-Broken"].platform == "sensor"


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

    assert data["heat_pump-outside"].value == 11.0


def test_a_real_api_value_still_wins_over_the_scraped_one():
    """The API reading is the fresher of the two - only an EMPTY one is
    ignored, otherwise the merge would stop updating at all."""
    data = _process(
        _modules(_parameter("Outside")),
        _values(_value("Outside", numeric=12.5, unit="°C")),
        mode="both",
        existing=_scraped("heat_pump-outside", "Heat pump - Outside", value=11.0),
    )

    assert data["heat_pump-outside"].value == 12.5


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
    assert entity.platform == "date", (
        "an unbounded time parameter became a writeable toggle again - "
        "switching it writes epoch 0/1, i.e. 1970, to the heating system"
    )
    assert entity.value == HOLIDAY_BEGIN_EPOCH


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

    assert data["Heat pump-Something"].platform == "sensor"


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

    assert data["Heat pump-Heizprogramm1"].platform == "sensor"


def _schedule_modules():
    return _modules(
        _parameter(
            "Heizprogramm1",
            IsWriteable=True,
            DataType=WemDataType.SWITCH,
            MinValue=None,
            MaxValue=None,
            EnumValues=None,
        )
    )


def test_a_writeable_value_that_fell_back_to_a_sensor_still_updates():
    """The second poll must show the second reading.

    api_data is built once per session and every cycle writes into it, so by
    the second poll the fallback sensor from the first is already there. The
    skip asked "is this writeable AND already present", which is true of that
    sensor - and cannot tell it from a control this cycle just created. The
    value then never changed again for as long as the session lasted.

    Weekly programmes are the ones that hit this: they are writeable, and
    they fall back to a sensor because their value is JSON.
    """
    modules = _schedule_modules()
    first = _process(
        modules, _values(_value("Heizprogramm1", string='{"MO-1":"00:00-24:00"}'))
    )
    assert first["Heat pump-Heizprogramm1"].value == '{"MO-1":"00:00-24:00"}', (
        "the setup did not read"
    )

    second = _process(
        modules,
        _values(_value("Heizprogramm1", string='{"MO-1":"06:00-22:00"}')),
        existing=first,
    )

    assert second["Heat pump-Heizprogramm1"].value == '{"MO-1":"06:00-22:00"}', (
        "the programme froze on the value it had in the first poll"
    )


def test_a_real_control_is_still_left_alone_on_the_second_poll():
    """The counter-test, and the reason the skip exists at all.

    A parameter that DOES become a control is written by the first pass; the
    second pass must not overwrite it with a plain sensor, or the entity
    loses its platform and its bounds on every cycle.
    """
    modules = _modules(
        _parameter(
            "Raumsolltemperatur",
            IsWriteable=True,
            DataType=WemDataType.NUMBER_STEP_HALF,
            MinValue=5,
            MaxValue=30,
        )
    )
    first = _process(modules, _values(_value("Raumsolltemperatur", numeric=21.0)))
    assert first["Heat pump-Raumsolltemperatur"].platform == "number"

    second = _process(
        modules, _values(_value("Raumsolltemperatur", numeric=22.0)), existing=first
    )

    assert second["Heat pump-Raumsolltemperatur"].platform == "number", (
        "the control was demoted to a plain sensor on the second poll"
    )
    assert second["Heat pump-Raumsolltemperatur"].value == 22.0


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

    assert data["Heat pump-Mystery"].platform == "sensor"


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
        "Heat pump-AktRaumSoll": Reading(
            value=21.0, parameter_id="AktRaumSoll", unit="°C", platform="sensor"
        ),
        "Heat pump-Vorlaufsoll": Reading(
            value=50.5, parameter_id="Vorlaufsoll", unit="°C", platform="sensor"
        ),
    }

    data = _process(
        modules,
        _values(_value("AktRaumSoll", numeric=22.0)),
        existing=existing,
    )

    assert data["Heat pump-AktRaumSoll"].value == 22.0
    assert data["Heat pump-Vorlaufsoll"].value is None, (
        "a reading the portal did not send was still reported as current"
    )
    assert data["Heat pump-Vorlaufsoll"].unit == "°C", "the unit was thrown away"


def test_a_module_the_portal_did_not_answer_for_is_left_alone():
    """A whole module missing is more likely a partial answer than a claim
    that none of its parameters has a value."""
    modules = _two_parameters()
    existing = {
        "Heat pump-AktRaumSoll": Reading(
            value=21.0, parameter_id="AktRaumSoll", unit="°C", platform="sensor"
        ),
    }

    data = _process(modules, {"Modules": []}, existing=existing)

    assert data["Heat pump-AktRaumSoll"].value == 21.0


def test_a_heating_schedule_is_not_cleared_by_the_value_read():
    """Schedules are fetched on their own path with their own hourly
    throttle, so clearing them here throws away what that path maintains."""
    modules = _modules(
        _parameter("AktRaumSoll"),
        _parameter("Heizprogramm1", DataType=WemDataType.PROGRAM),
    )
    existing = {
        "Heat pump-Heizprogramm1": Reading(
            value="Active",
            parameter_id="Heizprogramm1",
            unit=None,
            platform="sensor",
            circuit_times_day=A_FED_WEEK,
        ),
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-Heizprogramm1"].value == "Active"
    assert data["Heat pump-Heizprogramm1"].circuit_times_day == A_FED_WEEK


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
        "Heat pump-Heizprogramm1": Reading(
            value='{"MO-1": "06:00-22:00"}',
            parameter_id="Heizprogramm1",
            unit=None,
            platform="sensor",
            circuit_times_day=A_FED_WEEK,
        ),
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-Heizprogramm1"].value == '{"MO-1": "06:00-22:00"}'


def test_an_ordinary_switch_the_portal_dropped_is_still_cleared():
    """The counter-test: exempting every DataType 2 parameter would take the
    freshness rule off real switches, which is most of them."""
    modules = _modules(
        _parameter("AktRaumSoll"),
        _parameter("Pumpe", DataType=WemDataType.SWITCH),
    )
    existing = {
        "Heat pump-Pumpe": Reading(
            value="on", parameter_id="Pumpe", unit=None, platform="switch"
        ),
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-Pumpe"].value is None


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
        "Heat pump-U_Beginn": Reading(
            value=1785715200.0, parameter_id="U_Beginn", unit=None, platform="date"
        ),
    }

    data = _process(
        modules, _values(_value("AktRaumSoll", numeric=22.0)), existing=existing
    )

    assert data["Heat pump-U_Beginn"].value is None
