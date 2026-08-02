"""A frozen record of what the mapper produces, for every input shape.

process_api_values decides which Home Assistant entity every portal value
becomes and what it carries. It is 229 lines with 28 branches, and a mistake
in it is SILENT: the integration still starts, it just exposes the wrong
entity type, the wrong unit, or a value that quietly stops updating. One of
this session's audit findings lived in exactly that function for that reason.

The branch tests next door say "these cases still behave"; they cannot say
"nothing else changed", which is the question a refactor asks. So this walks a
deterministic matrix over the whole input space - data type, writeability,
value shape, unit, language, mode, and whether the device is the one the
scraper writes into - and compares the ENTIRE resulting structure against a
recorded snapshot.

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


def _case(label, data_type, writeable, value_label, numeric, string, unit,
          language, mode, is_scraper_device, min_value, max_value):
    """One fully specified call, named so a diff points at the exact input."""
    parameter = {
        "ParameterID": "P1",
        "IsWriteable": writeable,
        "DataType": data_type,
        "MinValue": min_value,
        "MaxValue": max_value,
        "EnumValues": ENUMS,
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
            "heat_pump-p1": {
                "value": 11.0, "name": "heat_pump-p1", "unit": "°C",
                "icon": "mdi:thermometer", "friendlyName": "Heat pump - P1",
                "ParameterID": "heat_pump-p1", "platform": "sensor",
            }
        }
    }
    WemPortalDataMapper.process_api_values(
        DEVICE, values, modules, language, {}, mode, api_data,
        DEVICE if is_scraper_device else "9999",
    )
    return label, api_data[DEVICE]


def build_snapshot():
    """The full matrix, in a stable order so diffs stay readable."""
    snapshot = {}
    for type_label, data_type in DATA_TYPES:
      for bounds_label, min_value, max_value in BOUNDS:
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
                            label = "|".join([
                                type_label, bounds_label,
                                "rw" if writeable else "ro",
                                value_label, language, mode,
                                "scraperdev" if is_scraper_device else "otherdev",
                            ])
                            key, result = _case(
                                label, data_type, writeable, value_label,
                                numeric, string, unit, language, mode,
                                is_scraper_device, min_value, max_value,
                            )
                            snapshot[key] = result
    return snapshot


def _normalise(obj):
    """JSON round-trip, so tuple/float/enum shapes compare as they serialise."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


def test_the_matrix_covers_a_meaningful_number_of_shapes():
    """Guards the guard: a matrix that collapsed to a handful of cases would
    still pass its own comparison while covering almost nothing."""
    snapshot = build_snapshot()
    platforms = {
        entity.get("platform")
        for result in snapshot.values()
        for entity in result.values()
    }

    assert len(snapshot) >= 600
    # Every platform the mapper can produce has to appear, or a refactor could
    # break one of them without the recording noticing. The switch platform
    # was missing from the first version of this matrix for exactly that
    # reason: its bounds were never 0/1.
    assert platforms == {"sensor", "number", "select", "switch"}, platforms


def test_the_mapper_output_is_unchanged(request):
    """Every input shape must still produce exactly what it produced before."""
    current = _normalise(build_snapshot())

    if request.config.getoption("--update-golden"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(
            json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pytest.skip("golden snapshot rewritten - review the diff before committing")

    assert GOLDEN.exists(), (
        "no snapshot recorded yet - run: pytest tests/test_mapper_golden.py "
        "--update-golden"
    )
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    # Compared key by key: a whole-dict assertion prints thousands of lines
    # and hides which input actually moved.
    assert sorted(current) == sorted(expected), "the set of covered inputs changed"
    for case in sorted(expected):
        assert current[case] == expected[case], f"output changed for: {case}"
