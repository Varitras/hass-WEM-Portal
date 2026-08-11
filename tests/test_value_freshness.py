"""Validity lives on the data (Umbau phase 4, K2).

A device answer carrying module A but not module B used to refresh the
DEVICE's freshness, so B's readings never aged: `_clear_unanswered` only
touches modules the answer names, and `_forget_stale_device_values` only
fires when the whole device goes silent. Freshness is tracked per ModuleRef
now: the module the portal stopped answering for ages out on the existing
TTL while its siblings stay - and a weekly programme whose due refresh
failed loses its stale detail attributes instead of overruling the newer
raw plan forever.
"""

import logging
import time
from datetime import timedelta


from custom_components.wemportal import wemportalapi
from custom_components.wemportal.models import ModuleRef, Reading
from custom_components.wemportal.utils import serialize_modules
from custom_components.wemportal.wemportalapi import WemPortalApi
from custom_components.wemportal.wemportalapi import (
    DEVICE_VALUES_STALE_AFTER_SECONDS,
)

MODULE_A = ModuleRef(module_index=0, module_type=1)
MODULE_B = ModuleRef(module_index=1, module_type=1)

AGED = DEVICE_VALUES_STALE_AFTER_SECONDS + 60


class _Answer:
    """A portal answer carrying exactly one JSON payload."""

    status_code = 200
    url = "https://api.example/answer"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _two_module_api():
    """One device, two modules, one plain reading each."""
    api = WemPortalApi("user@example.org", "secret")
    api.modules = {
        "1234": {
            MODULE_A: {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {"AktRaumSoll": {"ParameterID": "AktRaumSoll"}},
            },
            MODULE_B: {
                "Index": 1,
                "Type": 1,
                "Name": "Circuit",
                "parameters": {"Komfort": {"ParameterID": "Komfort"}},
            },
        }
    }
    api.data = {
        "1234": {
            "Heat pump-AktRaumSoll": Reading(
                parameter_id="AktRaumSoll", value=21.0, module_index=0, module_type=1
            ),
            "Circuit-Komfort": Reading(
                parameter_id="Komfort", value=24.0, module_index=1, module_type=1
            ),
        }
    }
    return api


def _api_that_fills_its_own_data():
    """The same two modules - but with `data` EMPTY.

    The fixture above hands the rows to `api.data` ready-made, module
    address included. Production does not: the mapper writes them, and
    what it puts in each row is exactly the question. A test that fills
    the row itself cannot ask it.
    """
    api = _two_module_api()
    api.data = {"1234": {}}
    return api


def _answer_both_then_only_module_a(api):
    """Cycle 1 names both modules, every cycle after that only A.

    That is the shape the whole per-module freshness exists for - the
    DEVICE keeps answering, one module stops being in the answer.
    """
    calls = {"values": 0}

    def make_api_call(url, **_kwargs):
        if url == wemportalapi.API_REFRESH_URL:
            return _Answer({"Status": 0, "JobID": 7})
        calls["values"] += 1
        modules = [
            {
                "ModuleIndex": 0,
                "ModuleType": 1,
                "Values": [
                    {"ParameterID": "AktRaumSoll", "NumericValue": 21.5, "Unit": "°C"}
                ],
            }
        ]
        if calls["values"] == 1:
            modules.append(
                {
                    "ModuleIndex": 1,
                    "ModuleType": 1,
                    "Values": [
                        {"ParameterID": "Komfort", "NumericValue": 24.0, "Unit": "°C"}
                    ],
                }
            )
        return _Answer({"Modules": modules})

    api.make_api_call = make_api_call


def test_a_reading_the_mapper_wrote_ages_out_like_any_other():
    """The freshness has to find the rows PRODUCTION writes.

    Every row in this test is written by the mapper, not by the test. That
    is the whole point: the mapper builds an ordinary read-only sensor
    without the module address the ageing pass matches on, so module B
    could fall silent forever and its reading stayed on display as current
    - while the test next door passed, because it had filled the address
    in by hand.
    """
    api = _api_that_fills_its_own_data()
    _answer_both_then_only_module_a(api)

    api._fetch_parameter_values("1234")
    assert api.data["1234"]["Circuit-Komfort"].value == 24.0, (
        "precondition: the first cycle wrote both modules' readings"
    )

    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - AGED
    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value is None, (
        "a reading of the silent module is still presented as current"
    )
    assert api.data["1234"]["Heat pump-AktRaumSoll"].value == 21.5, (
        "the answering module was aged along with the silent one"
    )


def test_a_row_the_scrape_still_feeds_is_not_aged_by_its_module():
    """The counterweight to the fix above.

    In `both` mode one row can carry an api reading AND a scraped one. Now
    that such a row knows its module, the ageing pass can reach it - and
    while the scrape is still delivering, blanking it would throw away a
    value that arrived seconds ago. Same exemption the device-level pass
    has made all along; without it the fix for the silent module would have
    created a fresh defect on the web path.
    """
    api = _two_module_api()
    api.data["1234"]["Circuit-Komfort"].value = 24.0
    api._previous_scraper_keys = {"Circuit-Komfort"}
    api.spider_retry_count = 0
    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - AGED

    api._forget_unanswered_module_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value == 24.0, (
        "a row the scrape is still feeding was blanked by the module ageing"
    )

    # Once the scrape has given up too, nothing is keeping the row fresh.
    api.spider_retry_count = wemportalapi.SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE
    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - AGED

    api._forget_unanswered_module_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value is None, (
        "with both sources dead the reading is still presented as current"
    )


def _answer_only_module_a(api):
    """Stub the portal: the values answer names module A and nothing else."""

    def make_api_call(url, **_kwargs):
        if url == wemportalapi.API_REFRESH_URL:
            return _Answer({"Status": 0, "JobID": 7})
        return _Answer(
            {
                "Modules": [
                    {
                        "ModuleIndex": 0,
                        "ModuleType": 1,
                        "Values": [
                            {
                                "ParameterID": "AktRaumSoll",
                                "NumericValue": 21.5,
                                "Unit": "°C",
                            }
                        ],
                    }
                ]
            }
        )

    api.make_api_call = make_api_call


def test_the_module_the_portal_stopped_answering_for_ages_out(caplog):
    """The behaviour contract of the phase: A answers, B does not.

    B's readings outlive the TTL and go unknown; A's stay. The device-level
    stamp cannot see this case - the device DID answer.
    """
    api = _two_module_api()
    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - AGED
    _answer_only_module_a(api)

    with caplog.at_level(logging.WARNING):
        failure = api._fetch_parameter_values("1234")

    assert failure is None
    assert api.data["1234"]["Circuit-Komfort"].value is None, (
        "the missing module's reading is still presented as current"
    )
    assert api.data["1234"]["Heat pump-AktRaumSoll"].value == 21.5, (
        "the answering module was aged along with the silent one"
    )
    assert "module" in caplog.text.lower(), "nobody was told why the values went"


def test_an_answering_module_is_stamped_and_a_silent_one_is_not():
    """The stamp is what the aging pass reads. Without it a module that
    answers today and goes silent next week never ages - the pass skips
    unstamped modules as never-seen. And stamping the SILENT module would
    start its clock without evidence."""
    api = _two_module_api()
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert "values_answered_at" in api.modules["1234"][MODULE_A]
    assert "values_answered_at" not in api.modules["1234"][MODULE_B]


def test_a_module_answered_recently_is_left_alone():
    """The counter-half: within the TTL nothing is touched."""
    api = _two_module_api()
    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - 60
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value == 24.0


def test_a_module_never_stamped_is_not_aged():
    """No stamp means no evidence - same rule as the device-level aging,
    which skips a device that was never read this session."""
    api = _two_module_api()
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value == 24.0


def test_a_weekly_programme_survives_the_module_aging():
    """Programme rows are governed by the schedule fetch, which has its own
    staleness rule - the same split _clear_unanswered already makes."""
    api = _two_module_api()
    api.data["1234"]["Circuit-Programme"] = Reading(
        parameter_id="Programme",
        value="MoDiMi",
        data_type=6,
        module_index=1,
        module_type=1,
    )
    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - AGED
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value is None
    assert api.data["1234"]["Circuit-Programme"].value == "MoDiMi", (
        "the programme was blanked although the schedule fetch owns it"
    )


def test_the_freshness_stamp_stays_out_of_the_persisted_cache():
    """The stamp is monotonic time: meaningless across restarts, and worse,
    it changes every cycle - persisted, it would defeat the fingerprint that
    keeps the module cache from being rewritten 288 times a day."""
    serialized = serialize_modules(
        {"1234": {MODULE_A: {"Name": "Heat pump", "values_answered_at": 12.5}}}
    )

    assert "values_answered_at" not in serialized["1234"]["0:1"]
    assert serialized["1234"]["0:1"]["Name"] == "Heat pump"


# --- a failed due schedule refresh drops its stale detail ---------------


def _schedule_row_api():
    api = WemPortalApi("user@example.org", "secret")
    api.data = {
        "1234": {
            "Circuit-Programme": Reading(
                parameter_id="Programme",
                value='{"1": []}',
                circuit_times_day=[{"Day": 1}],
                possible_values=[1, 2],
                module_index=1,
                module_type=1,
            )
        }
    }
    return api


def test_a_failed_due_schedule_refresh_drops_the_stale_attributes():
    """The sensor prefers CircuitTimesDay over the raw value, so detail from
    last week overruled a newer plan for as long as the refresh kept
    failing. Without the detail the existing JSON fallback takes over."""
    api = _schedule_row_api()
    module = {"Index": 1, "Type": 1, "Name": "Circuit"}

    api._record_schedule_attempt("1234", module, "Programme", time.time(), False)

    row = api.data["1234"]["Circuit-Programme"]
    assert row.circuit_times_day is None, "stale detail still overrules the raw plan"
    assert row.possible_values is None
    assert row.value == '{"1": []}', "the raw plan went with the detail"


def test_a_successful_schedule_refresh_keeps_its_attributes():
    """The counter-half: success books the attempt and touches nothing."""
    api = _schedule_row_api()
    module = {"Index": 1, "Type": 1, "Name": "Circuit"}

    api._record_schedule_attempt("1234", module, "Programme", time.time(), True)

    row = api.data["1234"]["Circuit-Programme"]
    assert row.circuit_times_day == [{"Day": 1}]
    assert row.possible_values == [1, 2]


def test_a_long_poll_interval_gets_a_staleness_limit_it_can_reach():
    """The limit is a duration, and it was a fixed thirty minutes.

    Nothing caps the API interval from above - the options only enforce a
    floor - so an installation polling every 45 minutes was already past the
    limit before its next attempt ran. The first miss then emptied
    everything, which is the opposite of the one-failed-cycle tolerance the
    limit exists for.
    """
    api = _two_module_api()
    api.scan_interval_api = timedelta(seconds=45 * 60)
    # Silent for 35 minutes: past the old fixed limit, well inside one
    # interval of this installation.
    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - 35 * 60
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value == 24.0, (
        "a reading was dropped before this installation had a second chance to fetch it"
    )
