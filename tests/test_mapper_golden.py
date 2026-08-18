"""A frozen record of what the mapper produces, for every input shape.

process_api_values and its helpers decide which Home Assistant entity every
portal value becomes and what it carries. A mistake in them is SILENT: the
integration still starts, it just exposes the wrong entity type, the wrong
unit, or a value that quietly stops updating. One of this project's audit
findings lived in exactly that code for that reason.

The branch tests next door say "these cases still behave"; they cannot say
"nothing else changed", which is the question a refactor asks. So this walks a
deterministic matrix over the input space - data type, bounds, writeability,
value shape, unit, language, mode, and whether the device is the one the
scraper writes into - and compares the ENTIRE resulting structure against a
recorded snapshot.

The matrix holds two things fixed that production does not: it always passes
ONE parameter and an EMPTY scraping_mapper. On its own it therefore reaches
86% of the module, and the states it misses are not exotic - see the second
half of this file, which covers them as additive cases. Together with the
branch tests next door the module is fully covered, arcs included; that was
measured, not assumed:

    python -m coverage run --branch \\
        --source=custom_components.wemportal.mapper \\
        -m pytest tests/test_mapper_golden.py tests/test_mapper.py -q -m ""
    python -m coverage report -m

Regenerate only when a change to the output is intended, and read the diff
line by line before you do:

    python -m pytest tests/test_mapper_golden.py --update-golden

A diff that nobody can explain is the finding, not the inconvenience.
"""

import json
from pathlib import Path

import pytest

from custom_components.wemportal.const import WemDataType
from custom_components.wemportal.mapper import WemPortalDataMapper
from custom_components.wemportal.models import ModuleRef, Reading

GOLDEN = Path(__file__).parent / "fixtures" / "mapper_golden.json"

DEVICE = "1234"
MODULE_KEY = (0, 1)

# Deliberately includes the shapes that have caused trouble: a missing value,
# an empty string, the German decimal comma, a JSON blob (a time program), and
# the on/off wordings the portal uses in both languages.
VALUES = [
    ("numeric", 21.5, "", "°C"),
    ("numeric_int", 30.0, "", "%"),
    ("missing", None, "--", "°C"),
    ("empty", None, "", "°C"),
    ("text", None, "Reduziert Betrieb", None),
    ("german_on", None, "Ein", None),
    ("english_off", None, "Off", None),
    ("program", None, '{"Mon":[]}', None),
    ("energy_missing", None, "--", "kWh"),
]

DATA_TYPES = [
    ("none", None),
    ("half", WemDataType.NUMBER_STEP_HALF),
    ("one", WemDataType.NUMBER_STEP_ONE),
    ("select", WemDataType.SELECT),
    ("switch", WemDataType.SWITCH),
    ("program", WemDataType.PROGRAM),
]

ENUMS = [{"Value": "0", "Name": "Aus"}, {"Value": "1", "Name": "Ein"}]

# Whether the portal named the parameter's states. This is an axis and not a
# constant because it decides two different things, and it was pinned to
# "always present" until a real installation showed what that hid: a DataType
# 2 parameter with no bounds AND no EnumValues is not a switch at all but a
# date, and the whole date platform was therefore unreachable from this
# matrix. It also changes the VALUE for every type - _describe_value only
# runs a value through sanitize_value() when there are no EnumValues.
ENUM_SETS = [
    ("enum", ENUMS),
    ("noenum", None),
]

# The bounds decide more than the range: a SWITCH with 0/1 becomes a toggle
# while the same type with a wider range is a stepped number, and absent
# bounds fall back to a guess from the parameter name. Fixing them at one
# value left the entire switch platform out of the recording - noticed only
# because the platform distribution was printed rather than the case count.
BOUNDS = [
    ("wide", 10, 30),
    ("binary", 0, 1),
    ("unbounded", None, None),
]


def _case(
    label,
    data_type,
    writeable,
    value_label,
    numeric,
    string,
    unit,
    language,
    mode,
    is_scraper_device,
    min_value,
    max_value,
    enum_values,
):
    """One fully specified call, named so a diff points at the exact input."""
    parameter = {
        "ParameterID": "P1",
        "IsWriteable": writeable,
        "DataType": data_type,
        "MinValue": min_value,
        "MaxValue": max_value,
        "EnumValues": enum_values,
    }
    modules = {
        DEVICE: {MODULE_KEY: {"Name": "Heat pump", "parameters": {"P1": parameter}}}
    }
    values = {
        "Modules": [
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [
                    {
                        "ParameterID": "P1",
                        "NumericValue": numeric,
                        "StringValue": string,
                        "Unit": unit,
                    }
                ],
            }
        ]
    }
    # A scraped row already present, so the `both`-mode merge is exercised
    # rather than skipped.
    api_data = {
        DEVICE: {
            "heat_pump-p1": Reading(
                value=11.0,
                unit="°C",
                icon="mdi:thermometer",
                friendly_name="Heat pump - P1",
                parameter_id="heat_pump-p1",
                platform="sensor",
            )
        }
    }
    WemPortalDataMapper.process_api_values(
        DEVICE,
        values,
        modules,
        language,
        {},
        mode,
        api_data,
        DEVICE if is_scraper_device else "9999",
    )
    # Serialised through the same compact form diagnostics uses, so the
    # snapshot pins what a reading actually carries, not object identities.
    return label, {key: row.as_dict() for key, row in api_data[DEVICE].items()}


def build_snapshot():
    """The full matrix, in a stable order so diffs stay readable."""
    snapshot = {}
    for type_label, data_type in DATA_TYPES:
        for bounds_label, min_value, max_value in BOUNDS:
            for enum_label, enum_values in ENUM_SETS:
                for writeable in (False, True):
                    for value_label, numeric, string, unit in VALUES:
                        for language in ("en", "de"):
                            for mode in ("api", "both"):
                                for is_scraper_device in (True, False):
                                    if mode == "api" and not is_scraper_device:
                                        # The scraper device is irrelevant outside
                                        # `both`; skipping keeps the matrix honest
                                        # rather than padded with duplicates.
                                        continue
                                    label = "|".join(
                                        [
                                            type_label,
                                            bounds_label,
                                            enum_label,
                                            "rw" if writeable else "ro",
                                            value_label,
                                            language,
                                            mode,
                                            "scraperdev"
                                            if is_scraper_device
                                            else "otherdev",
                                        ]
                                    )
                                    key, result = _case(
                                        label,
                                        data_type,
                                        writeable,
                                        value_label,
                                        numeric,
                                        string,
                                        unit,
                                        language,
                                        mode,
                                        is_scraper_device,
                                        min_value,
                                        max_value,
                                        enum_values,
                                    )
                                    snapshot[key] = result
    return snapshot


def _normalise(obj):
    """JSON round-trip, so tuple/float/enum shapes compare as they serialise."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


def _as_text(result):
    """One result as the stable string the packing indexes it by."""
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


def _packed(snapshot):
    """The snapshot with each distinct result stored once.

    The matrix walks thousands of input combinations that land on a few
    hundred distinct results, so written out in full the file repeated every
    result dozens of times - 73,000 lines carrying 7,000 lines of
    information.

    What this recording freezes is the MAPPING from a combination to its
    result, and that survives the packing exactly: every case still names its
    own result, it just names it by index. Keeping only one case per distinct
    result would freeze something weaker - that these results exist - and a
    change moving a combination from one result to another, both still
    present, is precisely the regression a mapper produces.

    The results are sorted so the file is deterministic: an unordered dump
    would reshuffle indices on every regeneration and make the diff
    unreadable, which is the one thing this file has to stay good at.
    """
    results = sorted({_as_text(result) for result in snapshot.values()})
    index = {text: number for number, text in enumerate(results)}
    return {
        "results": [json.loads(text) for text in results],
        "cases": {case: index[_as_text(result)] for case, result in snapshot.items()},
    }


def _unpacked(recorded):
    """The recording, back in the shape the comparison below works on."""
    results = recorded["results"]
    return {case: results[number] for case, number in recorded["cases"].items()}


def test_the_matrix_covers_a_meaningful_number_of_shapes():
    """Guards the guard: a matrix that collapsed to a handful of cases would
    still pass its own comparison while covering almost nothing."""
    snapshot = build_snapshot()
    platforms = {
        entity.get("platform")
        for result in snapshot.values()
        for entity in result.values()
    }

    assert len(snapshot) >= 3000
    # Every platform the mapper can produce has to appear, or a refactor could
    # break one of them without the recording noticing. The switch platform
    # was missing from the first version of this matrix for exactly that
    # reason: its bounds were never 0/1.
    assert platforms == {"sensor", "number", "select", "switch", "date"}, platforms


def test_packing_the_recording_loses_nothing():
    """Guards the storage, the way the test above guards the mapper.

    Storing each distinct result once is only safe while unpacking gives back
    exactly what was packed. A packing that quietly dropped or merged cases
    would shrink the file and the coverage together, and the comparison above
    could not notice: it only ever sees the unpacked form.
    """
    snapshot = _normalise(build_snapshot())

    assert _unpacked(_packed(snapshot)) == snapshot


def test_the_recording_still_names_every_case_separately():
    """The property the packing must not trade away.

    What is frozen is which combination produces which result. Keeping one
    case per distinct result would freeze something weaker - that these
    results exist - and a change moving a combination from one result to
    another, both still present, is exactly what a broken mapper does.
    """
    recorded = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert len(recorded["cases"]) >= 3000, "cases were collapsed, not deduplicated"
    assert len(recorded["results"]) < len(recorded["cases"]), (
        "nothing was deduplicated - the file is the expanded form again"
    )


def test_the_mapper_output_is_unchanged(request):
    """Every input shape must still produce exactly what it produced before."""
    current = _normalise(build_snapshot())

    if request.config.getoption("--update-golden"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(
            json.dumps(_packed(current), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        pytest.skip("golden snapshot rewritten - review the diff before committing")

    assert GOLDEN.exists(), (
        "no snapshot recorded yet - run: pytest tests/test_mapper_golden.py "
        "--update-golden"
    )
    expected = _unpacked(json.loads(GOLDEN.read_text(encoding="utf-8")))

    # Compared key by key: a whole-dict assertion prints thousands of lines
    # and hides which input actually moved.
    assert sorted(current) == sorted(expected), "the set of covered inputs changed"
    for case in sorted(expected):
        assert current[case] == expected[case], f"output changed for: {case}"


# --- the states the matrix above cannot reach --------------------------
#
# The matrix varies one parameter in one module and always starts with an
# empty scraping_mapper. Measured, it covers 78% of mapper.py on its own -
# and the gap is not incidental. scraping_mapper is a long-lived dict on the
# api object, so from the SECOND poll cycle onwards production always takes
# the cached path in _merge_into_scraped. The hottest path in the field was
# the one with no recording at all.
#
# Deliberately a SEPARATE fixture rather than new axes on the matrix above:
# adding an axis to the case key renames all 1944 existing keys, which turns
# the diff into pure churn and destroys the one property everything else
# rests on - that the previously recorded outputs did not move. Additive
# cases keep `git diff tests/fixtures/mapper_golden.json` empty, and that is
# checkable.

EXTRA_GOLDEN = Path(__file__).parent / "fixtures" / "mapper_golden_extra.json"


def _scraped_row(parameter_id, **overrides):
    fields = {
        "value": 11.0,
        "unit": "°C",
        "icon": "mdi:thermometer",
        "friendly_name": "Heat pump - Outside",
        "parameter_id": parameter_id,
        "platform": "sensor",
    }
    fields.update(overrides)
    return Reading(**fields)


def _param(parameter_id, **overrides):
    parameter = {
        "ParameterID": parameter_id,
        "IsWriteable": False,
        "DataType": None,
        "MinValue": 10,
        "MaxValue": 30,
        "EnumValues": ENUMS,
    }
    parameter.update(overrides)
    return parameter


def _extra_case(
    parameters,
    existing,
    scraping_mapper,
    mode="both",
    language="en",
    is_scraper_device=True,
):
    """One call with full control over what the matrix holds fixed."""
    modules = {
        DEVICE: {
            MODULE_KEY: {
                "Name": "Heat pump",
                "parameters": {
                    parameter["ParameterID"]: parameter for parameter in parameters
                },
            }
        }
    }
    values = {
        "Modules": [
            {
                "ModuleIndex": MODULE_KEY[0],
                "ModuleType": MODULE_KEY[1],
                "Values": [
                    {
                        "ParameterID": parameter["ParameterID"],
                        "NumericValue": 21.5,
                        "StringValue": "",
                        "Unit": "°C",
                    }
                    for parameter in parameters
                ],
            }
        ]
    }
    api_data = {DEVICE: dict(existing)}
    mapper_state = {key: list(value) for key, value in scraping_mapper.items()}
    WemPortalDataMapper.process_api_values(
        DEVICE,
        values,
        modules,
        language,
        mapper_state,
        mode,
        api_data,
        DEVICE if is_scraper_device else "9999",
    )
    # Both are recorded. scraping_mapper is mutated in place and carried
    # across poll cycles, so leaving it out leaves its two write sites frozen
    # nowhere - the matrix above discards it entirely.
    return {
        "api_data": {
            key: row.as_dict() if isinstance(row, Reading) else row
            for key, row in api_data[DEVICE].items()
        },
        "scraping_mapper": {
            _cache_key_name(key): targets for key, targets in mapper_state.items()
        },
    }


def _cache_key_name(key) -> str:
    """The merge cache key as one readable string.

    It is a (device id, ModuleRef, ParameterID) triple - the device because
    only the scraper device writes the cache while every device reads it, the
    module because two heating circuits share one parameter catalogue - and
    JSON has no tuple keys. Spelled out rather than str()'d so a diff in this
    fixture stays readable.
    """
    device_id, module, parameter_id = key
    return f"{device_id}:{ModuleRef(*module).as_storage_key()}/{parameter_id}"


def build_extra_snapshot():
    snapshot = {}

    # The cached path: _merge_into_scraped skips rebuilding the mapping and
    # goes straight to the write loop, which production does on every cycle
    # after the first and the matrix never does.
    snapshot["cached_mapping"] = _extra_case(
        [_param("Outside")],
        {"heat_pump-outside": _scraped_row("heat_pump-outside")},
        {(DEVICE, MODULE_KEY, "Outside"): ["heat_pump-outside"]},
    )
    # Cached, but pointing at a row that no longer exists: the mapper has to
    # create it rather than fail (the else branch of the write loop).
    snapshot["cached_mapping_missing_row"] = _extra_case(
        [_param("Outside")],
        {},
        {(DEVICE, MODULE_KEY, "Outside"): ["heat_pump-gone"]},
    )
    # A cached mapping onto SEVERAL rows, so the write loop runs twice.
    snapshot["cached_mapping_two_targets"] = _extra_case(
        [_param("Outside")],
        {
            "heat_pump-outside": _scraped_row("heat_pump-outside"),
            "heat_pump-outside2": _scraped_row("heat_pump-outside2", value=12.0),
        },
        {(DEVICE, MODULE_KEY, "Outside"): ["heat_pump-outside", "heat_pump-outside2"]},
    )

    # A row whose ParameterID carries no "-": split("-")[1] raises, and the
    # IndexError guard is load-bearing rather than defensive noise - the
    # writes phase 1 makes itself look exactly like this.
    snapshot["row_without_a_dash"] = _extra_case(
        [_param("Outside")],
        {"Outside": _scraped_row("Outside")},
        {},
    )
    # A non-dict entry in the device dict, reaching the isinstance skip.
    snapshot["non_dict_entry"] = _extra_case(
        [_param("Outside")],
        {"ConnectionStatus": 0},
        {},
    )
    # Rows that match nothing, so the scan runs to the end and the fallback
    # assigns the key itself.
    snapshot["no_row_matches"] = _extra_case(
        [_param("Outside")],
        {
            "heat_pump-unrelated": _scraped_row(
                "heat_pump-unrelated", friendly_name="Heat pump - Pressure"
            )
        },
        {},
    )

    # Two parameters in one module: phase 1 writes the writeable one, whose
    # entry then becomes a scan candidate for the read-only one.
    snapshot["two_parameters"] = _extra_case(
        [
            _param("Setpoint", IsWriteable=True, DataType=WemDataType.NUMBER_STEP_ONE),
            _param("Outside"),
        ],
        {"heat_pump-outside": _scraped_row("heat_pump-outside")},
        {},
    )

    # No EnumValues at all - the only way a writeable value reaches the plain
    # sanitize path in _describe_value.
    for label, data_type in (
        ("switch", WemDataType.SWITCH),
        ("select", WemDataType.SELECT),
        ("number", WemDataType.NUMBER_STEP_ONE),
    ):
        snapshot[f"no_enum_values_{label}"] = _extra_case(
            [_param("P", IsWriteable=True, DataType=data_type, EnumValues=[])],
            {},
            {},
            mode="api",
        )

    # A scraped row carrying explicit None fields: the difference between
    # `.get(key, default)` and `or default` in the write loop is invisible unless
    # the stored value is falsy but present.
    snapshot["falsy_scraped_fields"] = _extra_case(
        [_param("Outside")],
        {
            "heat_pump-outside": _scraped_row(
                "heat_pump-outside",
                value=None,
                unit=None,
                icon=None,
                friendly_name=None,
            )
        },
        {(DEVICE, MODULE_KEY, "Outside"): ["heat_pump-outside"]},
    )
    return snapshot


def test_the_extra_cases_reach_what_the_matrix_cannot():
    """Guards the guard: each case exists for a specific uncovered branch, so
    one that stopped reaching its branch has to be noticed rather than
    silently recorded as whatever it does now."""
    snapshot = build_extra_snapshot()

    # The cached case must NOT have rebuilt the mapping - if it did, it is
    # exercising the scan again and proves nothing about the cached path.
    assert snapshot["cached_mapping"]["scraping_mapper"] == {
        "1234:0:1/Outside": ["heat_pump-outside"]
    }
    # And where nothing matched, the fallback assignment must have happened.
    assert snapshot["no_row_matches"]["scraping_mapper"] == {
        "1234:0:1/Outside": ["Heat pump-Outside"]
    }


def test_the_extra_mapper_output_is_unchanged(request):
    current = _normalise(build_extra_snapshot())

    if request.config.getoption("--update-golden"):
        EXTRA_GOLDEN.parent.mkdir(exist_ok=True)
        EXTRA_GOLDEN.write_text(
            json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pytest.skip("extra snapshot rewritten - review the diff before committing")

    assert EXTRA_GOLDEN.exists(), (
        "no extra snapshot recorded yet - run: pytest tests/test_mapper_golden.py "
        "--update-golden"
    )
    expected = json.loads(EXTRA_GOLDEN.read_text(encoding="utf-8"))

    assert sorted(current) == sorted(expected), "the set of extra cases changed"
    for case in sorted(expected):
        assert current[case] == expected[case], f"output changed for: {case}"
