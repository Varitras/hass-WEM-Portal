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
from custom_components.wemportal.const import WemDataType
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

    api._module_answered_at.setdefault("1234", {})[MODULE_B] = time.monotonic() - AGED
    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value is None, (
        "a reading of the silent module is still presented as current"
    )
    assert api.data["1234"]["Heat pump-AktRaumSoll"].value == 21.5, (
        "the answering module was aged along with the silent one"
    )


def test_a_module_the_portal_stopped_listing_still_ages_out(monkeypatch):
    """The one module the ageing pass could never reach: the one that is gone.

    The pass walks the CURRENT module list, and the stamp it reads lives
    inside each module's entry - so a module that drops out of the device
    list takes its own stamp with it and is never visited again. Its
    readings then sit on the dashboard forever: not refreshed, because the
    mapper skips a module it has no description for, and not aged, because
    the only pass that would do it cannot see them.

    Driven through the real stamping call and a clock that moves, so it
    holds whatever the stamps are stored in.
    """
    api = _two_module_api()
    api.data["1234"]["Circuit-Komfort"].value = 24.0
    api.data["1234"]["Heat pump-AktRaumSoll"].value = 21.5
    api._stamp_answered_modules(
        "1234",
        {
            "Modules": [
                {"ModuleIndex": 0, "ModuleType": 1},
                {"ModuleIndex": 1, "ModuleType": 1},
            ]
        },
    )
    # The portal stops listing module B - a re-discovery that no longer
    # describes it is all it takes.
    del api.modules["1234"][MODULE_B]
    later = time.monotonic() + AGED
    monkeypatch.setattr(wemportalapi.time, "monotonic", lambda: later)

    api._forget_unanswered_module_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value is None, (
        "a module that left the device list kept its readings on display "
        "with nothing able to refresh or age them"
    )


def test_a_module_that_is_still_listed_and_answering_keeps_its_readings():
    """The control case for the one above: being visited is not the same as
    being aged."""
    api = _two_module_api()
    api.data["1234"]["Circuit-Komfort"].value = 24.0
    api._stamp_answered_modules(
        "1234", {"Modules": [{"ModuleIndex": 1, "ModuleType": 1}]}
    )

    api._forget_unanswered_module_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value == 24.0


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
    api._module_answered_at.setdefault("1234", {})[MODULE_B] = time.monotonic() - AGED

    api._forget_unanswered_module_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value == 24.0, (
        "a row the scrape is still feeding was blanked by the module ageing"
    )

    # Once the scrape has given up too, nothing is keeping the row fresh.
    api.spider_retry_count = wemportalapi.SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE
    api._module_answered_at.setdefault("1234", {})[MODULE_B] = time.monotonic() - AGED

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
    api._module_answered_at.setdefault("1234", {})[MODULE_B] = time.monotonic() - AGED
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

    assert MODULE_A in api._module_answered_at["1234"]
    assert MODULE_B not in api._module_answered_at["1234"]


def test_a_module_answered_recently_is_left_alone():
    """The counter-half: within the TTL nothing is touched."""
    api = _two_module_api()
    api._module_answered_at.setdefault("1234", {})[MODULE_B] = time.monotonic() - 60
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


# A week the device actually reported a programme for. The switching times
# are what makes it one: a list of bare days renders to nothing, so it is not
# evidence that anything is still feeding the row.
A_FED_WEEK = [{"Day": 1, "CircuitTimes": [{"Start": 6, "End": 22, "Level": 1}]}]


def _programme_row(api, circuit_times_day):
    api.data["1234"]["Circuit-Programme"] = Reading(
        parameter_id="Programme",
        value="MoDiMi",
        data_type=6,
        module_index=1,
        module_type=1,
        circuit_times_day=circuit_times_day,
    )
    api._module_answered_at.setdefault("1234", {})[MODULE_B] = time.monotonic() - AGED
    _answer_only_module_a(api)


def test_an_empty_week_does_not_count_as_being_fed():
    """Asked of the function, not through a caller.

    The schedule read refuses an answer with no week now, so no caller can
    produce this any more - and that is exactly why the contract belongs
    here: the two guards failed independently once already, and a test that
    goes through the write path would stop covering this one.
    """
    from custom_components.wemportal.utils import schedule_fetch_still_feeds

    # With a value, so the empty week is the only thing that can decide it -
    # without one the row fails the value check instead and this asserts
    # nothing about the week at all.
    assert not schedule_fetch_still_feeds(
        Reading(parameter_id="P", value="MoDiMi", circuit_times_day=[])
    )
    assert schedule_fetch_still_feeds(
        Reading(parameter_id="P", value="MoDiMi", circuit_times_day=A_FED_WEEK)
    )


def test_a_week_with_no_switching_times_in_it_does_not_count_as_being_fed():
    """A list of days is not a programme.

    The exemption means "the schedule fetch is keeping this row current", and
    what makes it current is the switching times. A week whose days carry
    none renders to nothing - the reader drops every day it cannot build an
    entry from - so the row shows the raw plan while the exemption keeps it
    from ever ageing. Truthiness of the list said yes to exactly that, and
    the tests asserting the exemption used that very shape.
    """
    from custom_components.wemportal.utils import schedule_fetch_still_feeds

    assert not schedule_fetch_still_feeds(
        Reading(parameter_id="P", value="MoDiMi", circuit_times_day=[{"Day": 1}])
    )


def test_a_programme_with_no_value_left_does_not_count_as_being_fed():
    """The week is read THROUGH the value: the day names come out of it.

    The device-level ageing has no schedule exemption and empties `value`,
    and the schedule read deliberately does not put it back. What was left
    was a row the reader cannot render - no labels, so no week - and an
    exemption still insisting something feeds it.
    """
    from custom_components.wemportal.utils import schedule_fetch_still_feeds

    assert not schedule_fetch_still_feeds(
        Reading(parameter_id="P", value=None, circuit_times_day=A_FED_WEEK)
    )


def test_a_weekly_programme_the_schedule_fetch_still_feeds_survives_the_aging():
    """Programme rows are governed by the schedule fetch, which has its own
    staleness rule - the same split _clear_unanswered already makes. The
    detail it attached is the evidence that it is still delivering."""
    api = _two_module_api()
    _programme_row(api, circuit_times_day=A_FED_WEEK)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value is None
    assert api.data["1234"]["Circuit-Programme"].value == "MoDiMi", (
        "the programme was blanked although the schedule fetch owns it"
    )


def test_a_programme_of_a_module_the_portal_stopped_listing_ages_out():
    """The exemption says "the schedule fetch is still feeding this row", and
    that fetch walks the MODULE LIST.

    A module the re-discovery drops is one it will never visit again, so
    whatever week is attached to its programme is the last one there will
    ever be - and being attached is exactly what the exemption reads as
    proof it is still current. That is the same never-ending exemption the
    empty-week case had, reached from the other side.
    """
    api = _two_module_api()
    _programme_row(api, circuit_times_day=A_FED_WEEK)
    # What a re-discovery that no longer finds this module leaves behind.
    api.modules["1234"].pop(MODULE_B)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Programme"].value is None, (
        "a programme of a module nothing fetches any more is still current"
    )


def test_a_weekly_programme_nobody_refreshes_any_more_ages_with_its_module():
    """The case the exemption did not think of, and it has no end.

    The exemption rests on the schedule fetch keeping the row current. That
    fetch drops its own detail when a due refresh fails - so a row without
    detail is one nothing is refreshing, and the module it belongs to has
    stopped answering too. Both of its sources are silent, and it went on
    presenting the plan from before the outage as the current one for as long
    as that lasted.
    """
    api = _two_module_api()
    _programme_row(api, circuit_times_day=None)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Programme"].value is None, (
        "a programme neither source has refreshed is still shown as current"
    )


def test_the_freshness_stamp_is_not_in_the_module_list_to_begin_with():
    """Stronger than filtering it out on the way to disk: it never gets in.

    The stamp is monotonic time - meaningless across restarts, and it changes
    every cycle, so persisted it would defeat the fingerprint that keeps the
    module cache from being rewritten 288 times a day. It used to live inside
    each module's entry and be dropped again by serialize_modules. That is
    also what made it unreachable: the list is replaced wholesale on every
    re-discovery, so a module that dropped out took its own stamp with it -
    and its readings were then the only ones nothing could age.
    """
    api = _two_module_api()
    api._stamp_answered_modules(
        "1234", {"Modules": [{"ModuleIndex": 1, "ModuleType": 1}]}
    )

    assert MODULE_B in api._module_answered_at["1234"], (
        "the stamp was not recorded at all, so this proves nothing below"
    )
    assert all(
        "values_answered_at" not in entry for entry in api.modules["1234"].values()
    ), "the stamp is back inside the module list, where a re-discovery drops it"
    # And what does go to disk is the module list unchanged.
    assert serialize_modules(api.modules)["1234"]["1:1"]["Name"] == "Circuit"


# --- a failed due schedule refresh drops its stale detail ---------------


def _schedule_row_api():
    api = WemPortalApi("user@example.org", "secret")
    api.data = {
        "1234": {
            "Circuit-Programme": Reading(
                parameter_id="Programme",
                value='{"1": []}',
                circuit_times_day=A_FED_WEEK,
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

    api._record_schedule_attempt("1234", module, "Programme", time.monotonic(), False)

    row = api.data["1234"]["Circuit-Programme"]
    assert row.circuit_times_day is None, "stale detail still overrules the raw plan"
    assert row.possible_values is None
    assert row.value == '{"1": []}', "the raw plan went with the detail"


def test_a_successful_schedule_refresh_keeps_its_attributes():
    """The counter-half: success books the attempt and touches nothing."""
    api = _schedule_row_api()
    module = {"Index": 1, "Type": 1, "Name": "Circuit"}

    api._record_schedule_attempt("1234", module, "Programme", time.monotonic(), True)

    row = api.data["1234"]["Circuit-Programme"]
    assert row.circuit_times_day == A_FED_WEEK
    assert row.possible_values == [1, 2]


def _merged_schedule_row_api():
    """A programme merged into a scraped row in `both` mode: its reading lives
    under the SCRAPED key, and scraping_mapper records the move - exactly what
    the value read leaves behind. The own key `Heat pump-Programme` has no row
    of its own any more."""
    api = WemPortalApi("user@example.org", "secret")
    api.data = {
        "1234": {
            "heat_pump-programme": Reading(
                parameter_id="heat_pump-programme",
                value='{"1": []}',
                circuit_times_day=A_FED_WEEK,
                possible_values=[1, 2],
                module_index=1,
                module_type=1,
            )
        }
    }
    api.scraping_mapper = {
        ("1234", ModuleRef(1, 1), "Programme"): ["heat_pump-programme"]
    }
    return api


def test_a_failed_refresh_drops_the_detail_where_a_merged_programme_lives():
    """The drop half of the schedule seam, in `both` mode.

    A merged programme's detail lives under the scraped key, so looking under
    the reconstructed own key found nothing and left last week's week
    overruling the raw plan on the row that actually carries the entity.
    """
    api = _merged_schedule_row_api()
    module = {"Index": 1, "Type": 1, "Name": "Heat pump"}

    api._record_schedule_attempt("1234", module, "Programme", time.monotonic(), False)

    row = api.data["1234"]["heat_pump-programme"]
    assert row.circuit_times_day is None, (
        "the stale detail on the merged row was never dropped - the fetch "
        "looked under the own key the reading does not live at"
    )
    assert row.possible_values is None


def test_a_schedule_read_writes_the_detail_where_a_merged_programme_lives():
    """The read half of the same seam.

    The detail belongs on the scraped row the programme was merged into, not
    on a second row rebuilt from its own key that no entity is built from -
    which left the visible entity showing only the raw plan.
    """
    api = _merged_schedule_row_api()
    api.data["1234"]["heat_pump-programme"].circuit_times_day = None
    api.data["1234"]["heat_pump-programme"].possible_values = None

    calls = {"n": 0}

    def make_api_call(url, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Answer({"JobID": 7})
        return _Answer({"CircuitTimesDay": A_FED_WEEK, "PossibleValues": [1, 2]})

    api.make_api_call = make_api_call
    module = {"Index": 1, "Type": 1, "Name": "Heat pump"}

    fetched = api._read_one_schedule("1234", module, "Programme")

    assert fetched is True
    assert api.data["1234"]["heat_pump-programme"].circuit_times_day == A_FED_WEEK, (
        "the week landed on a row rebuilt from the own key, not the merged one"
    )
    assert "Heat pump-Programme" not in api.data["1234"], (
        "a second own-key row was created for a programme that already lives "
        "under the scraped key"
    )


def test_a_merged_3130_programme_is_recognised_as_a_schedule():
    """The classify half, and the case that hides completely.

    A 3.1.3.0 portal types every programme as DataType 2 with the schedule as
    JSON in the value, so recognising one means reading that value - and a
    merged programme's value lives under the scraped key. Looking under the own
    key found nothing, so the fetch was never even entered for it, silently.
    """
    api = _merged_schedule_row_api()
    module = {"Index": 1, "Type": 1, "Name": "Heat pump"}
    parameter_data = {"ParameterID": "Programme", "DataType": WemDataType.SWITCH}

    assert api._is_schedule_parameter("1234", module, "Programme", parameter_data), (
        "a merged 3.1.3.0 programme went unrecognised, so its schedule is "
        "never fetched from the device"
    )


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
    api._module_answered_at.setdefault("1234", {})[MODULE_B] = (
        time.monotonic() - 35 * 60
    )
    _answer_only_module_a(api)

    api._fetch_parameter_values("1234")

    assert api.data["1234"]["Circuit-Komfort"].value == 24.0, (
        "a reading was dropped before this installation had a second chance to fetch it"
    )
