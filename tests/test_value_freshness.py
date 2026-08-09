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


from custom_components.wemportal import wemportalapi
from custom_components.wemportal.const import DEVICE_VALUES_STALE_AFTER_SECONDS
from custom_components.wemportal.models import ModuleRef
from custom_components.wemportal.utils import serialize_modules
from custom_components.wemportal.wemportalapi import WemPortalApi

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
            "Heat pump-AktRaumSoll": {
                "ParameterID": "AktRaumSoll",
                "value": 21.0,
                "ModuleIndex": 0,
                "ModuleType": 1,
            },
            "Circuit-Komfort": {
                "ParameterID": "Komfort",
                "value": 24.0,
                "ModuleIndex": 1,
                "ModuleType": 1,
            },
        }
    }
    return api


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
    assert api.data["1234"]["Circuit-Komfort"]["value"] is None, (
        "the missing module's reading is still presented as current"
    )
    assert api.data["1234"]["Heat pump-AktRaumSoll"]["value"] == 21.5, (
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

    assert api.data["1234"]["Circuit-Komfort"]["value"] == 24.0


def test_a_module_never_stamped_is_not_aged():
    """No stamp means no evidence - same rule as the device-level aging,
    which skips a device that was never read this session."""
    api = _two_module_api()
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"]["value"] == 24.0


def test_a_weekly_programme_survives_the_module_aging():
    """Programme rows are governed by the schedule fetch, which has its own
    staleness rule - the same split _clear_unanswered already makes."""
    api = _two_module_api()
    api.data["1234"]["Circuit-Programme"] = {
        "ParameterID": "Programme",
        "value": "MoDiMi",
        "DataType": 6,
        "ModuleIndex": 1,
        "ModuleType": 1,
    }
    api.modules["1234"][MODULE_B]["values_answered_at"] = time.monotonic() - AGED
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"]["value"] is None
    assert api.data["1234"]["Circuit-Programme"]["value"] == "MoDiMi", (
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
            "Circuit-Programme": {
                "ParameterID": "Programme",
                "value": '{"1": []}',
                "CircuitTimesDay": [{"Day": 1}],
                "PossibleValues": [1, 2],
                "ModuleIndex": 1,
                "ModuleType": 1,
            }
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
    assert "CircuitTimesDay" not in row, "stale detail still overrules the raw plan"
    assert "PossibleValues" not in row
    assert row["value"] == '{"1": []}', "the raw plan went with the detail"


def test_a_successful_schedule_refresh_keeps_its_attributes():
    """The counter-half: success books the attempt and touches nothing."""
    api = _schedule_row_api()
    module = {"Index": 1, "Type": 1, "Name": "Circuit"}

    api._record_schedule_attempt("1234", module, "Programme", time.time(), True)

    row = api.data["1234"]["Circuit-Programme"]
    assert row["CircuitTimesDay"] == [{"Day": 1}]
    assert row["PossibleValues"] == [1, 2]
