"""A frozen record of what a statistics cycle does - including its traffic.

get_statistics is 179 lines and, before this file, lines 1663-1767 of
wemportalapi.py ran in no test at all: the existing tests cover the rate-limit
guard at the top and the retry bookkeeping at the bottom, and nothing in
between. That middle is where the group names, the sensor attributes and the
never-invent-a-zero rule live.

What makes this function different from the mapper is that its output is not
the only thing worth freezing. Weishaupt blocks by IP, and the pacing here -
one refresh per device, one read per group, a fixed sleep between reads, and
an hourly guard - is the whole reason the integration is tolerated. A
restructuring that quietly turns one request into two, or that lets a routine
group rejection abort the remaining groups so the cycle back-dates to the
15-minute retry, would show up as four times the traffic and nothing in a
green suite would say so.

So each case records BOTH the resulting sensor data AND the exact sequence of
API calls: url, payload, headers. A change in traffic is then a change in the
fixture.

Regenerate only when a change is intended, and read the diff:

    python -m pytest tests/test_statistics_golden.py --update-golden
"""

import json
import time
from pathlib import Path

import pytest

from custom_components.wemportal.const import (
    API_STATISTICS_READ_URL,
    API_STATISTICS_REFRESH_URL,
    STATISTICS_REFRESH_INTERVAL_SECONDS,
    STATISTICS_RETRY_INTERVAL_SECONDS,
    WEM_INVALID_PARAMETER_STATUS,
)
from custom_components.wemportal.models import Reading
from custom_components.wemportal.wemportalapi import WemPortalApi

GOLDEN = Path(__file__).parent / "fixtures" / "statistics_golden.json"

DEVICE = "1234"


class _Rejected(Exception):
    """What make_api_call raises for an application-level rejection."""

    def __init__(self, message, server_status=None):
        super().__init__(message)
        self.server_status = server_status


def _api(script, devices=(DEVICE,)):
    """An api whose portal answers from `script`, recording every request.

    `script` maps a url to a list of responses (or exceptions) handed out in
    order, so a case can make the second group behave differently from the
    first.
    """
    api = WemPortalApi("user@example.org", "secret")
    api.data = {device: {} for device in devices}
    api.modules = {device: {} for device in devices}
    api.last_statistics_fetch = 0.0
    calls = []
    counters = {}

    def make_api_call(url, headers=None, data=None, do_retry=True, delay=5):
        calls.append({"url": url, "data": data, "headers": headers})
        index = counters.get(url, 0)
        counters[url] = index + 1
        answers = script.get(url, [])
        answer = answers[min(index, len(answers) - 1)] if answers else {}
        if isinstance(answer, Exception):
            raise answer

        class _Response:
            @staticmethod
            def json():
                return answer

        return _Response()

    api.make_api_call = make_api_call
    return api, calls


def _refresh(*group_types):
    return {"GroupTypeDescriptions": list(group_types)}


def _read(values, unit="kWh"):
    payload = {"Values": values}
    if unit is not None:
        payload["Unit"] = unit
    return payload


def _entry(value, date="2026-04-27T00:00:00"):
    return {"Value": value, "Date": date}


def _run(script, devices=(DEVICE,), enabled=None, existing=None):
    api, calls = _api(script, devices)
    if existing:
        for device, data in existing.items():
            api.data[device].update(data)
    api.get_statistics(enabled_devices=enabled)
    return {
        "data": {
            device: {
                key: row.as_dict() if isinstance(row, Reading) else row
                for key, row in api.data[device].items()
            }
            for device in sorted(api.data)
        },
        "calls": calls,
        # Whether the cycle asked to be retried early. Recorded as the
        # decision, not the timestamp, so the case stays deterministic.
        "retry_shortened": api.last_statistics_fetch < 0,
    }


def build_snapshot():
    snapshot = {}

    # A described group and an unnamed one, so both name paths are recorded:
    # the translation with its "Energy" suffix rule, and the numeric fallback
    # map for a group the portal did not name.
    snapshot["named_and_unnamed_groups"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh(
                    {"GroupType": 1, "Description": "Heizung"},
                    {"GroupType": 4, "Description": ""},
                ),
            ],
            API_STATISTICS_READ_URL: [
                _read([_entry(12.5)]),
                _read([_entry(99.0)]),
            ],
        }
    )

    # A group type outside the fallback map falls back to "Energy <id>".
    snapshot["unknown_group_id"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 42, "Description": ""})
            ],
            API_STATISTICS_READ_URL: [_read([_entry(1.0)])],
        }
    )

    # The newest entry is chosen by its Date, not by list position.
    snapshot["latest_by_date_not_position"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 1, "Description": "Heizung"})
            ],
            API_STATISTICS_READ_URL: [
                _read(
                    [
                        _entry(30.0, "2026-04-27T00:00:00"),
                        _entry(10.0, "2026-04-25T00:00:00"),
                    ]
                ),
            ],
        }
    )

    # No values at all: nothing is written, and the group is simply skipped.
    snapshot["empty_values"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 1, "Description": "Heizung"})
            ],
            API_STATISTICS_READ_URL: [_read([])],
        }
    )

    # A missing reading keeps the previous value rather than reporting 0 -
    # the Energy Dashboard reads a drop to zero on a total_increasing sensor
    # as a meter reset.
    snapshot["missing_value_keeps_the_previous"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 1, "Description": "Heizung"})
            ],
            API_STATISTICS_READ_URL: [_read([_entry(None)])],
        },
        existing={DEVICE: {f"{DEVICE}-Energy_1": Reading(value=77.0)}},
    )
    # And with no previous value it is skipped entirely rather than invented.
    snapshot["missing_value_without_history_is_skipped"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 1, "Description": "Heizung"})
            ],
            API_STATISTICS_READ_URL: [_read([_entry(None)])],
        }
    )

    # No Unit in the response: kWh is the documented default.
    snapshot["unit_defaults_to_kwh"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 1, "Description": "Heizung"})
            ],
            API_STATISTICS_READ_URL: [_read([_entry(5.0)], unit=None)],
        }
    )

    # Status 3001 means "this group does not apply to this module". It is
    # routine, and it must NOT stop the groups that follow - if it did, the
    # cycle would report no success at all and back-date to the short retry,
    # quadrupling the traffic.
    snapshot["routine_rejection_does_not_stop_the_rest"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh(
                    {"GroupType": 1, "Description": "Heizung"},
                    {"GroupType": 2, "Description": "Warmwasser"},
                ),
            ],
            API_STATISTICS_READ_URL: [
                _Rejected("not valid for this module", WEM_INVALID_PARAMETER_STATUS),
                _read([_entry(8.0)]),
            ],
        }
    )

    # Any other per-group error is reported but likewise does not abort the
    # device.
    snapshot["other_group_error_does_not_stop_the_rest"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh(
                    {"GroupType": 1, "Description": "Heizung"},
                    {"GroupType": 2, "Description": "Warmwasser"},
                ),
            ],
            API_STATISTICS_READ_URL: [
                _Rejected("boom", 500),
                _read([_entry(8.0)]),
            ],
        }
    )

    # A failing refresh takes the whole device down, and with no device
    # succeeding the cycle asks to be retried early.
    snapshot["refresh_failure_shortens_the_retry"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [_Rejected("portal unavailable", 500)],
        }
    )

    # Two devices, one broken: a partial success must NOT shorten the retry.
    snapshot["partial_success_keeps_the_full_interval"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _Rejected("portal unavailable", 500),
                _refresh({"GroupType": 1, "Description": "Heizung"}),
            ],
            API_STATISTICS_READ_URL: [_read([_entry(3.0)])],
        },
        devices=(DEVICE, "5678"),
    )

    # An empty filter means "every device is disabled" and must poll nothing.
    snapshot["empty_filter_polls_nothing"] = _run(
        {API_STATISTICS_REFRESH_URL: [_refresh({"GroupType": 1, "Description": "x"})]},
        enabled=[],
    )

    # A device the caller names but that this api has never seen: skipped
    # rather than sent to the portal.
    snapshot["unknown_device_is_skipped"] = _run(
        {API_STATISTICS_REFRESH_URL: [_refresh({"GroupType": 1, "Description": "x"})]},
        enabled=["9999"],
    )

    # A description that already says "energy" must not be given a second
    # "Energy" suffix.
    snapshot["name_already_contains_energy"] = _run(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 1, "Description": "Heating Energy"}),
            ],
            API_STATISTICS_READ_URL: [_read([_entry(4.0)])],
        }
    )
    return snapshot


def _normalise(obj):
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


# The cases that must produce NO traffic at all. Named explicitly rather than
# derived, so a case that silently stops polling is a failure instead of a
# quietly accepted new member of this set.
SILENT_CASES = {"empty_filter_polls_nothing", "unknown_device_is_skipped"}


def test_every_case_records_its_traffic():
    """Guards the guard: the request log is the half that protects the portal,
    so a case that stopped recording calls has to be noticed."""
    snapshot = build_snapshot()

    for case, result in snapshot.items():
        if case in SILENT_CASES:
            assert result["calls"] == [], f"{case}: reached the portal after all"
        else:
            assert result["calls"], f"{case}: no requests recorded"


def test_one_read_per_group_and_one_refresh_per_device():
    """The pacing itself, stated as a rule rather than left implicit in the
    fixture: this is what a decomposition is most likely to change, and what
    the portal punishes."""
    result = build_snapshot()["named_and_unnamed_groups"]
    urls = [c["url"] for c in result["calls"]]

    assert urls.count(API_STATISTICS_REFRESH_URL) == 1
    assert urls.count(API_STATISTICS_READ_URL) == 2
    assert urls[0] == API_STATISTICS_REFRESH_URL, "read before refresh"


def test_a_routine_rejection_still_counts_the_device_as_succeeded():
    """Otherwise the cycle back-dates to the 15-minute retry and the portal
    sees four times the traffic for a condition that is entirely normal."""
    api, _ = _api(
        {
            API_STATISTICS_REFRESH_URL: [
                _refresh({"GroupType": 1, "Description": "x"})
            ],
            API_STATISTICS_READ_URL: [
                _Rejected("not valid", WEM_INVALID_PARAMETER_STATUS)
            ],
        }
    )
    before = time.time()
    api.last_statistics_fetch = 0.0

    api.get_statistics()

    # Not back-dated: the timestamp is "just now", not an hour minus the
    # retry interval.
    assert api.last_statistics_fetch >= before
    assert api.last_statistics_fetch > before - (
        STATISTICS_REFRESH_INTERVAL_SECONDS - STATISTICS_RETRY_INTERVAL_SECONDS
    )


def test_the_statistics_output_is_unchanged(request):
    current = _normalise(build_snapshot())

    if request.config.getoption("--update-golden"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(
            json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pytest.skip("golden snapshot rewritten - review the diff before committing")

    assert GOLDEN.exists(), (
        "no snapshot recorded yet - run: pytest tests/test_statistics_golden.py "
        "--update-golden"
    )
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert sorted(current) == sorted(expected), "the set of covered cases changed"
    for case in sorted(expected):
        assert current[case] == expected[case], f"output or traffic changed for: {case}"
