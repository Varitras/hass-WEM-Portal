"""Robustness fixes: api_login error handling and timeout, discovery-cache
survival on a failed device refresh, and str-normalisation of device ids.
"""

import asyncio
import json
import time

import pytest
import requests as real_requests

from custom_components.wemportal import exceptions, wemportalapi
from custom_components.wemportal.const import WEB_LOGGED_IN_MARKER
from custom_components.wemportal.wemportalapi import WemPortalApi


class FakeResponse:
    def __init__(
        self,
        json_data=None,
        status_code=200,
        url="https://www.wemportal.com/app/x",
        content=None,
    ):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.url = url
        # Derived from the payload rather than left empty: production code
        # logs and inspects `content`, so a double whose body never matches
        # its own json() hides exactly the behaviour under test.
        if content is None:
            content = json.dumps(self._json).encode()
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.exceptions.HTTPError(response=self)


class RecordingSession:
    def __init__(self, post_exc=None, post_json=None):
        self.post_exc = post_exc
        self.post_json = post_json or {"Status": 0, "Version": "3.1"}
        self.post_kwargs = None
        self.cookies = self
        self.headers = {}

    def clear(self):
        pass

    def update(self, *_args, **_kwargs):
        pass

    def post(self, url, **kwargs):
        self.post_kwargs = kwargs
        if self.post_exc is not None:
            raise self.post_exc
        return FakeResponse(self.post_json)

    def close(self):
        pass


def _api(**kwargs):
    return WemPortalApi("user@example.org", "secret", **kwargs)


def _run(method, *args, **kwargs):
    """Drive one coroutine to completion from a synchronous test.

    Takes the method and its arguments rather than a ready-made coroutine, so
    that a `pytest.raises` block around it contains a single call. Spelled out
    as `asyncio.run(entity.some_method(x))` the block contains two, and the
    one meant to raise is the inner one - a failure in the outer call would
    satisfy the test just as well.
    """
    return asyncio.run(method(*args, **kwargs))


def test_api_login_network_error_raises_clean_auth_error(monkeypatch):
    """A pure network failure (no response yet) surfaces as UnknownAuthError,
    not an UnboundLocalError in the handler."""
    api = _api()
    session = RecordingSession(
        post_exc=real_requests.exceptions.ConnectionError("reset")
    )
    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: session)
    with pytest.raises(exceptions.UnknownAuthError):
        api.api_login()
    assert api.valid_login is False


def test_api_login_post_has_timeout(monkeypatch):
    api = _api()
    session = RecordingSession()
    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: session)
    api.api_login()
    assert api.valid_login is True
    assert (
        session.post_kwargs.get("timeout") == wemportalapi.API_REQUEST_TIMEOUT_SECONDS
    )


CACHED_MODULES = {
    "1234": {
        (0, 1): {
            "Index": 0,
            "Type": 1,
            "Name": "Heat pump",
            "parameters": {"P1": {"ParameterID": "P1"}},
        }
    }
}


def test_get_devices_failure_keeps_cache():
    """A failing device-list call must not wipe the discovery cache."""
    api = _api(cached_modules=CACHED_MODULES, existing_data={"1234": {"k": "v"}})

    def boom(*_args, **_kwargs):
        raise exceptions.WemPortalError("403 etc.")

    api.make_api_call = boom
    with pytest.raises(exceptions.WemPortalError):
        api.get_devices()
    assert api.modules == CACHED_MODULES
    assert api.data == {"1234": {"k": "v"}}


def test_get_devices_success_carries_cached_parameters():
    api = _api(cached_modules=CACHED_MODULES)
    device_json = {
        "Devices": [
            {
                "ID": 1234,
                "ConnectionStatus": 0,
                "Modules": [{"Index": 0, "Type": 1, "Name": "Heat pump"}],
            }
        ]
    }
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(device_json)
    api.get_devices()
    assert api.modules["1234"][(0, 1)]["parameters"] == {"P1": {"ParameterID": "P1"}}
    assert api.data["1234"]["ConnectionStatus"] == 0


def test_get_statistics_accepts_int_device_ids():
    """int device ids must not be skipped (self.data is str-keyed)."""
    api = _api()
    api.data = {"1234": {}}
    # A real API device is in self.modules too; get_statistics now skips
    # scraper-only devices (no API modules, e.g. the "0000" placeholder).
    api.modules = {"1234": {}}
    calls = []
    api.make_api_call = lambda url, **_kwargs: (
        calls.append(url) or FakeResponse({"GroupTypeDescriptions": []})
    )
    api.last_statistics_fetch = 0.0
    api.get_statistics(enabled_devices=[1234])
    assert calls, "statistics refresh was skipped for an int device id"


def test_expert_403_does_not_pause_sensor_polling():
    """An expert 403 must back off the expert path ONLY.

    A 403 is not proof of an IP-wide rate limit - it can equally mean the
    portal rejected that particular request. Pausing all polling for it cost
    the user their readings, verified in practice while the portal was
    reachable in a browser at the same time.
    """
    api = _api()
    api.activate_expert_cooldown()

    # Expert path is paused...
    with pytest.raises(exceptions.ForbiddenError):
        api.check_expert_cooldown()
    # ...but polling is not.
    api.check_cooldown()


def test_global_403_still_pauses_the_expert_path():
    """The reverse must keep working: a genuine rate limit seen by the API
    or scraper is the real signal, and must stop expert requests too."""
    api = _api()
    api._activate_cooldown()

    with pytest.raises(exceptions.ForbiddenError):
        api.check_cooldown()
    with pytest.raises(exceptions.ForbiddenError):
        api.check_expert_cooldown()


def test_expert_cooldown_reports_remaining_time():
    """The message is surfaced in the options form, so it has to say how
    long the wait actually is instead of a vague 'try again later'."""
    api = _api()
    api.activate_expert_cooldown(seconds=120)

    with pytest.raises(exceptions.ForbiddenError) as excinfo:
        api.check_expert_cooldown()

    assert "min remaining" in str(excinfo.value)


def test_expert_cooldown_is_never_shortened():
    api = _api()
    api.activate_expert_cooldown(seconds=600)
    before = api._expert_blocked_until
    api.activate_expert_cooldown(seconds=5)

    assert api._expert_blocked_until == before


def test_expert_cooldown_survives_api_reinstantiation():
    """The coordinator swaps the api object on repeated errors; a fresh
    instance must not silently clear an active expert backoff."""
    api = _api()
    api.activate_expert_cooldown()

    replacement = WemPortalApi(
        "user@example.org",
        "secret",
        expert_blocked_until=api._expert_blocked_until,
    )

    with pytest.raises(exceptions.ForbiddenError):
        replacement.check_expert_cooldown()


def _statistics_api(call_recorder, fail=False):
    """An api whose statistics refresh either works or always fails."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.last_statistics_fetch = 0.0

    def make_api_call(url, **_kwargs):
        call_recorder.append(url)
        if fail:
            raise exceptions.WemPortalError("portal unavailable")
        return FakeResponse({"GroupTypeDescriptions": []})

    api.make_api_call = make_api_call
    return api


def test_successful_statistics_fetch_waits_a_full_interval():
    calls = []
    api = _statistics_api(calls)

    api.get_statistics(enabled_devices=["1234"])
    assert len(calls) == 1

    # A second call right away must be rate limited away.
    api.get_statistics(enabled_devices=["1234"])
    assert len(calls) == 1, "statistics were refetched inside the interval"


def test_failed_statistics_cycle_retries_after_the_short_interval():
    """The timestamp is set BEFORE fetching, so a failure would otherwise
    cost a full refresh interval. A cycle that failed for every device
    shortens the wait instead - without giving up the rate limit."""
    calls = []
    api = _statistics_api(calls, fail=True)

    api.get_statistics(enabled_devices=["1234"])
    assert len(calls) == 1

    waited = time.time() - api.last_statistics_fetch
    remaining = wemportalapi.STATISTICS_REFRESH_INTERVAL_SECONDS - waited

    assert remaining <= wemportalapi.STATISTICS_RETRY_INTERVAL_SECONDS + 5
    assert remaining > 0, "the rate limit must not be dropped entirely"


def test_failed_statistics_cycle_is_still_rate_limited():
    """A failing portal must not be retried on every coordinator cycle."""
    calls = []
    api = _statistics_api(calls, fail=True)

    api.get_statistics(enabled_devices=["1234"])
    api.get_statistics(enabled_devices=["1234"])

    assert len(calls) == 1, "a failing portal was retried immediately"


def _statistics_api_with_groups(groups, read_answer):
    """An api whose refresh lists `groups` and whose group reads go through
    `read_answer(group_id)` - returning a payload or raising."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.last_statistics_fetch = 0.0

    def make_api_call(url, **kwargs):
        if url == wemportalapi.API_STATISTICS_REFRESH_URL:
            return FakeResponse(
                {"GroupTypeDescriptions": [{"GroupType": g} for g in groups]}
            )
        return read_answer(kwargs["data"]["GroupType"])

    api.make_api_call = make_api_call
    return api


def _remaining_wait(api):
    """How long until statistics would be fetched again."""
    return wemportalapi.STATISTICS_REFRESH_INTERVAL_SECONDS - (
        time.time() - api.last_statistics_fetch
    )


def _invalid_group_error():
    """The portal's own "this group does not apply here" rejection."""
    error = exceptions.WemPortalError("not valid for this module")
    error.server_status = wemportalapi.WEM_INVALID_PARAMETER_STATUS
    return error


def test_a_device_whose_every_group_failed_is_not_counted_as_a_success():
    """Group errors were swallowed one by one, so a device where ALL of them
    failed still returned normally and counted as a success - and the shorter
    retry, which exists for exactly that case, never engaged."""

    def always_fails(_group_id):
        raise exceptions.WemPortalError("portal unavailable")

    api = _statistics_api_with_groups([1, 2], always_fails)

    api.get_statistics(enabled_devices=["1234"])

    remaining = _remaining_wait(api)
    assert remaining <= wemportalapi.STATISTICS_RETRY_INTERVAL_SECONDS + 5
    assert remaining > 0, "the rate limit must not be dropped entirely"


def test_a_refusal_stops_the_group_loop_instead_of_repeating_itself(caplog):
    """A 403 is a fact about the IP, not about the statistics group.

    Swallowed like any other group error it was re-raised by the cooldown
    check for every remaining group - no extra traffic, but one warning each
    about a single refusal, and a closing message blaming "every group" for
    what was one block. The coordinator has a handler for ForbiddenError;
    this lets it get there.
    """
    import logging

    seen = []

    def refuses(group_id):
        seen.append(group_id)
        raise exceptions.ForbiddenError("rate limited")

    api = _statistics_api_with_groups([1, 2, 3], refuses)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(exceptions.ForbiddenError):
            api._fetch_device_statistics("1234")

    assert seen == [1], f"the loop kept asking after a refusal: {seen}"
    assert not [
        record
        for record in caplog.records
        if "Failed to fetch Statistics" in record.getMessage()
    ], "a refusal was reported as a per-group failure"


def test_one_group_that_worked_keeps_the_device_a_success():
    """The counter-test. Without it, treating any group error as a device
    failure would pass the test above and retry a device that is fine."""
    stats = {"Values": [{"Date": "2026-08-06", "Value": 12.0}], "Unit": "kWh"}

    def one_of_two_fails(group_id):
        if group_id == 1:
            raise exceptions.WemPortalError("portal unavailable")
        return FakeResponse(stats)

    api = _statistics_api_with_groups([1, 2], one_of_two_fails)

    api.get_statistics(enabled_devices=["1234"])

    assert _remaining_wait(api) > wemportalapi.STATISTICS_RETRY_INTERVAL_SECONDS + 5


def test_groups_that_do_not_apply_are_not_failures():
    """Status 3001 means the group does not exist for this module. Retrying
    sooner cannot produce a reading that is not there, it only costs
    requests."""

    def never_applies(_group_id):
        raise _invalid_group_error()

    api = _statistics_api_with_groups([1, 2], never_applies)

    api.get_statistics(enabled_devices=["1234"])

    assert _remaining_wait(api) > wemportalapi.STATISTICS_RETRY_INTERVAL_SECONDS + 5


def test_statistics_timestamp_is_kept_when_nothing_was_attempted():
    """No eligible device means nothing failed - the shorter retry must not
    kick in just because the loop had nothing to do."""
    calls = []
    api = _statistics_api(calls)
    api.modules = {}  # scraper-only: every device is skipped

    api.get_statistics(enabled_devices=["1234"])

    assert calls == []
    waited = time.time() - api.last_statistics_fetch
    assert waited < 5, "timestamp should record this attempt as 'just now'"


def test_get_data_accepts_int_device_ids():
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"ConnectionStatus": 50, "Errors": [], "GroupTypeDescriptions": []}
    )
    api.get_data(enabled_devices=[1234])
    assert api.data["1234"]["1234-ConnectionStatus"]["value"] == "offline"


def test_empty_enabled_devices_polls_nothing():
    """An EMPTY list means "every device is disabled", not "no filter".

    Truthiness made `[]` fall back to polling all devices - the exact
    opposite of what the coordinator asked for.
    """
    calls = []
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.make_api_call = lambda url, **_kwargs: (
        calls.append(url)
        or FakeResponse(
            {"ConnectionStatus": 50, "Errors": [], "GroupTypeDescriptions": []}
        )
    )
    api.last_statistics_fetch = 0.0

    api.get_data(enabled_devices=[])
    api.get_statistics(enabled_devices=[])

    assert calls == [], "a fully disabled installation was still polled"


def test_none_enabled_devices_still_polls_everything():
    """None keeps meaning "no filter given"."""
    calls = []
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    # Online, so this stays a test about the filter and not about the
    # all-devices-offline case.
    api.make_api_call = lambda url, **_kwargs: (
        calls.append(url)
        or FakeResponse(
            {
                "ConnectionStatus": 0,
                "Errors": [],
                "Modules": [],
                "GroupTypeDescriptions": [],
            }
        )
    )

    api.get_data(enabled_devices=None)

    assert calls, "an unfiltered poll must still happen"


def _expert_entity(api, entry_id="e1"):
    """An expert number entity wired to `api` through its entry's runtime data."""
    import types

    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.models import WemPortalData

    entry = types.SimpleNamespace(entry_id=entry_id, data={}, options={})
    entry.runtime_data = WemPortalData(api=api, coordinator=None)
    entity = expert_writer.WemPortalExpertNumber(entry, "expert_parameter_3", "A" * 36)
    entity.hass = types.SimpleNamespace(data={})
    return entity


def _read_state(current, options):
    from custom_components.wemportal import expert_writer

    return expert_writer.ExpertParameterState(current, options, {})


def test_a_parameter_whose_range_is_known_is_a_slider_again():
    """Spinner arrows each fire their own write; a slider sends one value.

    The mode was a fixed BOX on the class, correct for the placeholder range
    that spans 200000 - and never taken back once the portal had stated the
    real one. A 10-to-100 parameter therefore kept a box whose arrows each
    waited on a portal write, one click at a time.
    """
    from homeassistant.components.number import NumberMode

    entity = _expert_entity(_api())
    assert entity.mode == NumberMode.BOX, "an unread slot has no range to slide over"

    entity._apply_state(_read_state(35.0, [10.0, 35.0, 100.0]))

    assert entity.mode == NumberMode.AUTO


def test_an_unread_slot_rasters_finely_enough_for_what_the_portal_offers():
    """The placeholder step was 0.5, and the portal offers 0.05.

    Home Assistant does not enforce the step on a service call - it checks
    only min/max - but the input field and the arrows do, and that is the
    way this gets used. So a heating curve of 0.55 could not be typed at a
    slot the portal had not been asked about yet, and the write that would
    have fetched the real step is exactly what the field refused to compose.
    Same trap as the old 0-to-100 range, one order of magnitude finer.

    Values below are measured at the portal: the heating curve runs 0.05 to
    1.50 in 0.05, the frost protection -20.0 to 17.5 in halves.
    """
    entity = _expert_entity(_api())

    for offered in (0.05, 0.55, 1.5, 17.5, -19.5):
        steps = offered / entity.native_step
        assert abs(steps - round(steps)) < 1e-9, (
            f"{offered} is not on the placeholder raster of "
            f"{entity.native_step}, so it cannot be typed before the first read"
        )


def test_a_postback_asks_the_portal_for_a_delta_not_a_whole_page():
    """Two headers decide what the portal sends back.

    Without them it answers a postback with a complete page, and the parser
    behind every expert read and write is handed HTML where it expects a
    Telerik delta. Three call sites spelled the same six headers out; nothing
    checked any of them.
    """
    from custom_components.wemportal import expert_writer

    headers = expert_writer._ajax_headers("https://www.wemportal.com/Web/Main.aspx")

    assert headers["X-MicrosoftAjax"] == "Delta=true"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Referer"] == "https://www.wemportal.com/Web/Main.aspx"


def _dialog_html(options, factory_default=None):
    """A parameter dialog offering `options` as (value attribute, label)."""
    rendered = "".join(
        f'<option value="{attribute}"{" selected" if selected else ""}>{label}</option>'
        for attribute, label, selected in options
    )
    delivered = (
        ""
        if factory_default is None
        else f'<span id="ctl00_DialogContent_ltDeliveryStatusData">{factory_default}</span>'
    )
    return (
        "<html><body>"
        f'<select id="ctl00_DialogContent_ddlNewValue">{rendered}</select>'
        f"{delivered}"
        '<input type="hidden" name="__VIEWSTATE" value="vs" />'
        "</body></html>"
    )


def _overview_row(group, name, value, readdata):
    """One panel of the Fachmann overview, carrying one editable row."""
    return f"""
    <div class="RadPanelBar">
      <span id="x_HeaderTemplate_lblHeaderText">{group}</span>
      <table><tr>
        <td><span class="simpleDataName">{name}</span></td>
        <td><span class="simpleDataValue">{value}</span></td>
        <td><input class="EditIcon" type="button" onclick="FnContextMenu_Handler_Ext(
            '', 'entryedit', '', '', 'WwpsParameterDetails.aspx',
            'entityvalue={"A" * 36}&readdata={readdata}');return false;"/></td>
      </tr></table>
    </div>"""


def test_a_parameter_alone_in_its_section_is_offered_by_discovery_too():
    """The portal marks Betriebsart and Heizkennlinie readdata=False.

    Measured across a module: the flag is False on a parameter that stands
    alone in its section and True where several share one. It says how the
    PORTAL opens the dialog, not whether there is one - and reading it as
    "aggregate entry with no value dialog" kept exactly those out of
    discovery. They are among the ones most worth having, and fetched with
    readdata=True they answer with the same dropdown as any other.
    """
    from custom_components.wemportal import expert_writer

    page = (
        "<html><body>"
        + _overview_row("Heizkennlinie", "Heizkennlinie", "0.55", "False")
        + _overview_row("Raumsolltemperatur", "Komfort", "26.5 °C", "True")
        + "</body></html>"
    )

    found = {row["name"] for row in expert_writer.parse_parameter_list(page)}

    assert found == {"Heizkennlinie", "Komfort"}


def test_a_special_value_does_not_drag_the_range_off_the_scale():
    """A special value sits beside the scale, not on it.

    Measured at the portal: the heating curve offers it as 0 among 0.05 to
    1.50, the frost protection as -32768 among -20.0 to 17.5. Requiring EVERY
    label to be a number sent both back to the value attributes - so the frost
    protection published -32768 as its minimum, and the curve 0 to 150 instead
    of 0.05 to 1.50.
    """
    from custom_components.wemportal import expert_writer

    state = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html(
            [
                ("-32768", "Aus", False),
                ("-200", "-20.0", False),
                ("-195", "-19.5", True),
                ("175", "17.5", False),
            ]
        )
    )

    assert (state.min_value, state.max_value) == (-20.0, 17.5)
    assert state.current == -19.5
    assert state.post_value_for(-19.5) == "-195"


def test_a_selected_special_value_reads_as_unknown_and_says_why():
    """The portal had "Aus" selected on the heat pump's manual mode.

    A number entity cannot show a word, so the state is unknown - but silently
    unknown is indistinguishable from a failed read, and the portal's own
    wording is the only thing that tells them apart.
    """
    from custom_components.wemportal import expert_writer

    state = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html(
            [("-32768", "Aus", True), ("200", "20.0", False), ("680", "68.0", False)]
        )
    )

    assert state.current is None
    assert state.portal_text == "Aus"
    assert (state.min_value, state.max_value) == (20.0, 68.0)


def test_labels_that_are_numbers_win_even_where_the_attributes_are_an_index():
    """The quiet mode offers 0=Aus, 1=80, 2=60, 3=40.

    The attributes are an index and run the other way, so no factor relates
    them to the labels - which is why the mapping is kept per option rather
    than derived. Read by attribute this parameter would be a 1-to-3 slider.
    """
    from custom_components.wemportal import expert_writer

    state = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html(
            [
                ("0", "Aus", True),
                ("1", "80", False),
                ("2", "60", False),
                ("3", "40", False),
            ]
        )
    )

    assert sorted(state.options) == [40.0, 60.0, 80.0]
    assert state.post_value_for(80.0) == "1"
    assert state.post_value_for(40.0) == "3"


def test_the_factory_default_is_read_from_the_dialog():
    """The dialog states it beside the dropdown, so it costs no extra request.

    Kept as text: it reads "0.75" on the heating curve but "Aus" on the manual
    mode and "Mittel" on the building type.
    """
    from custom_components.wemportal import expert_writer

    scaled = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html(
            [("55", "0.55", True), ("75", "0.75", False)], factory_default="0.75"
        )
    )
    worded = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html([("0", "Aus", True), ("1", "Ein", False)], factory_default="Aus")
    )

    assert scaled.factory_default == "0.75"
    assert worded.factory_default == "Aus"


def test_the_dialog_wording_reaches_the_entity_that_has_to_show_it():
    """Read out of the form and published on the entity are two steps.

    Only the first was ever asked. The second is what the user sees, and it
    carries the whole answer where a special value is selected: the state
    goes unknown because a number cannot hold "Aus", and unknown without the
    wording beside it is indistinguishable from a read that simply failed.
    """
    from custom_components.wemportal import expert_writer

    state = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html(
            [("-32768", "Aus", True), ("200", "20.0", False), ("680", "68.0", False)],
            factory_default="20.0",
        )
    )
    entity = _expert_entity(_api())

    entity._apply_state(state)

    assert entity.native_value is None, "a word was published as a number"
    assert entity.extra_state_attributes == {
        "portal_value": "Aus",
        "factory_default": "20.0",
    }


def test_a_scaled_parameter_reads_as_the_portal_shows_it_not_ten_times_over():
    """The portal offers 1.5 as the string "15", and only the string was read.

    A parameter whose form said 1.0 to 30.0 in halves was published as 10 to
    300 in fives, so the value copied from the portal went in ten times too
    small - at a heating parameter. The same installation's parameter list,
    which reads the label, disagreed with the entity about the same parameter.
    """
    from custom_components.wemportal import expert_writer

    state = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html([("10", "1.0", False), ("15", "1.5", True), ("20", "2.0", False)])
    )

    assert state.current == 1.5
    assert (state.min_value, state.max_value) == (1.0, 2.0)
    assert state.step == 0.5
    assert state.post_value_for(1.5) == "15", "the form takes back its own string"


def test_a_parameter_whose_label_matches_its_value_is_unchanged():
    """The common case, and the one that must not move: both columns agree."""
    from custom_components.wemportal import expert_writer

    state = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html([("10", "10", False), ("11", "11", True), ("12", "12", False)])
    )

    assert state.current == 11.0
    assert (state.min_value, state.max_value) == (10.0, 12.0)
    assert state.post_value_for(11.0) == "11"


def test_a_dropdown_whose_labels_are_words_falls_back_to_its_values():
    """An enum labels its options "Aus"/"Auto", where the attribute is the
    only number there is. Taking labels then would leave no range at all."""
    from custom_components.wemportal import expert_writer

    state = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html([("0", "Aus", True), ("1", "Auto", False)])
    )

    assert state.current == 0.0
    assert state.options == [0.0, 1.0]


class _WrittenResponse:
    """The portal's answer to a write postback."""

    status_code = 200
    text = "<html><body>ok</body></html>"
    url = "https://www.wemportal.com/Web/UControls/Weishaupt/ExpertParameter.aspx"


def _recording_write_client(monkeypatch, dialogs):
    """A client that records what it posts, answering with `dialogs` in turn.

    `dialogs` are option lists as _dialog_html takes them: the form before the
    write, then the one the verify re-reads.
    """
    from custom_components.wemportal import expert_writer

    sent = {}

    class _Session:
        def post(self, *_args, **kwargs):
            sent.update(kwargs.get("data") or {})
            return _WrittenResponse()

        def close(self):
            pass

    forms = iter(
        [
            expert_writer.WemPortalExpertClient.parse_parameter_form(
                _dialog_html(options)
            )
            for options in dialogs
        ]
    )
    client = expert_writer.WemPortalExpertClient("user@example.org", "pw")
    client.session = _Session()
    monkeypatch.setattr(client, "_login", lambda: None)
    monkeypatch.setattr(client, "_fetch_form", lambda *_a, **_k: next(forms))
    return client, sent


def test_a_scaled_parameter_posts_the_string_the_form_offered(monkeypatch):
    """The one line where a number reaches the heating system.

    Rebuilding the string from the float posts "1.5" where the form expects
    "15" - the portal answers that with the value unchanged, and the write is
    reported as refused. Every test of this path mocked write_parameter out,
    so nothing watched what was actually sent.
    """
    client, sent = _recording_write_client(
        monkeypatch,
        [
            [("10", "1.0", True), ("15", "1.5", False)],
            [("10", "1.0", False), ("15", "1.5", True)],
        ],
    )

    client.write_parameter("A" * 36, 1.5)

    assert sent["ctl00$DialogContent$ddlNewValue"] == "15"


def test_a_special_value_can_be_written_by_the_word_the_portal_shows(monkeypatch):
    """ "Aus" is a real setting that no route could reach.

    It sits beside the scale rather than on it, so the number entity cannot
    offer it and Home Assistant refuses it against the published range. The
    admin service goes around that check - but the write itself took a float,
    so the one route that could have reached the value did not either.

    Typed in lower case on purpose: the word comes from a human copying what
    the portal displays, and "aus" failing where "Aus" works would be a puzzle
    with no clue in it.
    """
    off_selected = [("-32768", "Aus", True), ("200", "20.0", False)]
    client, sent = _recording_write_client(
        monkeypatch,
        [[("-32768", "Aus", False), ("200", "20.0", True)], off_selected],
    )

    client.write_parameter("A" * 36, "aus")

    assert sent["ctl00$DialogContent$ddlNewValue"] == "-32768", (
        "the word was not translated into the token the form expects back"
    )


def test_a_refused_value_carries_the_range_that_refused_it(monkeypatch):
    """The refusal is the one failure that knows what the portal offers.

    An entity holding a stale range is exactly the caller that lands here, so
    reporting the failure and dropping the freshly read form leaves it stuck:
    Home Assistant checks the published range before this integration is
    asked, and the value that would be accepted is outside it.
    """
    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.exceptions import ParameterWriteError

    client = expert_writer.WemPortalExpertClient("user@example.org", "pw")
    form = expert_writer.WemPortalExpertClient.parse_parameter_form(
        _dialog_html([("10", "1.0", True), ("15", "1.5", False)])
    )
    monkeypatch.setattr(client, "_login", lambda: None)
    monkeypatch.setattr(client, "_fetch_form", lambda *_a, **_k: form)

    with pytest.raises(ParameterWriteError) as excinfo:
        client.write_parameter("A" * 36, 50.0)

    assert excinfo.value.state is not None, "the refusal threw the form away"
    assert excinfo.value.state.max_value == 1.5


class _DialogNotReady:
    """A parameter dialog whose dropdown has not been filled in yet."""

    status_code = 200
    text = "<html><body>not ready</body></html>"
    url = "https://www.wemportal.com/Web/UControls/Weishaupt/ExpertParameter.aspx"


def _expert_login_session(monkeypatch, post_body, on_get=None):
    """A session for _full_login: a healthy form, then `post_body`."""
    from custom_components.wemportal import expert_writer

    posts = []

    class _Response:
        status_code = 200
        url = "https://www.wemportal.com/Web/Login.aspx"

        def __init__(self, text):
            self.text = text

    class _Session:
        def get(self, *_args, **_kwargs):
            if on_get is not None:
                on_get()
            return _Response(NORMAL_LOGIN_PAGE)

        def post(self, *_args, **_kwargs):
            posts.append(True)
            return _Response(post_body)

    monkeypatch.setattr(expert_writer.requests, "Session", lambda **_k: _Session())
    return posts


def test_a_fresh_login_stops_between_its_two_requests(monkeypatch):
    """The gate is asked before the login, and the login is two requests.

    An unload arriving while the first is in flight was not noticed until the
    whole sequence had run, so the credentials went out to a portal on behalf
    of a configuration that no longer existed - the one request in this module
    most worth not making.
    """
    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    torn_down = []

    def abort_check():
        if torn_down:
            raise ExpertOperationAborted("the entry is being unloaded")

    posts = _expert_login_session(
        monkeypatch, NORMAL_LOGIN_PAGE, on_get=lambda: torn_down.append(True)
    )
    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", abort_check=abort_check
    )

    with pytest.raises(ExpertOperationAborted):
        client._full_login()

    assert posts == [], "the credentials went out after the entry had gone away"


def test_a_login_that_succeeded_does_not_navigate_on_after_a_teardown(monkeypatch):
    """The gate closed after the login POST, before the navigation.

    _establish_context runs straight after the POST and opened with an
    unguarded GET, so an unload arriving during the credential exchange was
    answered with one more authenticated request. The write itself was never
    at risk - this is about not talking to the portal on behalf of a
    configuration that is gone.
    """
    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    torn_down = []

    def abort_check():
        if torn_down:
            raise ExpertOperationAborted("the entry is being unloaded")

    gets = []

    class _Response:
        status_code = 200
        url = "https://www.wemportal.com/Web/Login.aspx"

        def __init__(self, text):
            self.text = text

    class _Session:
        def get(self, *_args, **_kwargs):
            gets.append(True)
            return _Response(NORMAL_LOGIN_PAGE)

        def post(self, *_args, **_kwargs):
            # The unload lands while the credentials are on the wire.
            torn_down.append(True)
            return _Response(
                f"<html><body><div id='{WEB_LOGGED_IN_MARKER}'></div></body></html>"
            )

    monkeypatch.setattr(expert_writer.requests, "Session", lambda **_k: _Session())
    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", abort_check=abort_check
    )

    with pytest.raises(ExpertOperationAborted):
        client._full_login()

    assert gets == [True], "the navigation went on after the entry had gone away"


def test_a_portal_error_page_on_the_expert_login_url_is_not_a_wrong_password(
    monkeypatch,
):
    """The same URL-only test the scraper had, in the other client.

    Found by a test for something else: the gate check below could not reach
    its own subject because the login classified an error page as refused
    credentials first.
    """
    from custom_components.wemportal import expert_writer

    _expert_login_session(
        monkeypatch, "<html><body>Service temporarily busy</body></html>"
    )
    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")

    with pytest.raises(exceptions.ServerError):
        client._full_login()


def test_the_expert_login_form_coming_back_is_still_a_wrong_password(monkeypatch):
    """The counter-test: the form means the credentials really were refused."""
    from custom_components.wemportal import expert_writer

    _expert_login_session(monkeypatch, NORMAL_LOGIN_PAGE)
    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")

    with pytest.raises(exceptions.AuthError):
        client._full_login()


def test_maintenance_answering_the_expert_login_post_is_not_a_wrong_password(
    monkeypatch,
):
    """The same gap the scraper had, in the other client.

    Its login GET checks for the maintenance notice; the answer to the
    credential POST did not, and a response on the login URL is what counts
    as "the portal rejected these credentials". Announced downtime therefore
    read as a wrong password here too.
    """
    from custom_components.wemportal import expert_writer

    _expert_login_session(monkeypatch, MAINTENANCE_PAGE)
    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")

    with pytest.raises(exceptions.PortalMaintenanceError):
        client._full_login()


def test_a_batch_read_stops_rather_than_noting_a_failure_per_id():
    """An abort is not "this parameter could not be read".

    read_many catches per id so one bad parameter does not lose the others,
    which is right - but the teardown is not about a parameter. Recorded as
    one, the loop carried on to the next id and the next, each opening more
    portal navigation for a configuration that is already gone, and handed
    back a result full of None as though the reads had simply failed.
    """
    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")
    client._login = lambda: None
    client.close = lambda: None
    attempted = []

    def gone(entityvalue):
        attempted.append(entityvalue)
        raise ExpertOperationAborted("the entry is being unloaded")

    client._fetch_form = gone

    with pytest.raises(ExpertOperationAborted):
        client.read_many(["A" * 36, "B" * 36, "C" * 36])

    assert len(attempted) == 1, "it kept reading after the entry had gone away"


def test_a_form_retry_stops_when_the_entry_went_away_meanwhile(monkeypatch):
    """The abort gate has to be inside the retry loop, not only around it.

    Reading one parameter can take four attempts with a three-second pause
    and a live-value postback between them. The gate was checked before the
    login and between parameters, so a teardown landing inside those attempts
    was not noticed until the whole sequence had finished - the executor kept
    navigating the portal with the credentials of an entry that was gone.
    """
    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    torn_down = []

    def abort_check():
        if torn_down:
            raise ExpertOperationAborted("the entry is being unloaded")

    requests = []

    class _Session:
        def get(self, *_args, **_kwargs):
            requests.append(1)
            # The unload lands while this request is in flight.
            torn_down.append(True)
            return _DialogNotReady()

    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", abort_check=abort_check
    )
    client.session = _Session()
    client._poll_live_values_once = lambda: None
    monkeypatch.setattr(expert_writer.time, "sleep", lambda _seconds: None)

    with pytest.raises(ExpertOperationAborted):
        client._fetch_form("A" * 36)

    assert len(requests) == 1, (
        "a second request went to the portal after the entry was torn down"
    )


def test_a_slot_that_has_never_been_read_forbids_nothing():
    """Made-up bounds do not just mislabel - they lock the parameter out.

    Home Assistant validates against the published range BEFORE the
    integration is asked (components/number: it raises ServiceValidationError
    and never calls async_set_native_value). So a slot still showing the
    assumed 0-100 refuses a perfectly valid 350 - and the write that would
    have fetched the real range is exactly what it refuses. With the hourly
    read off by default, and nothing restored on a fresh install, that is
    permanent.

    The bounds cannot be right before the portal has been asked. They can
    stop being wrong: whatever the entity claims here must not exclude a
    value the portal might accept. What the portal will NOT accept is caught
    where it is actually known - write_parameter checks the value against the
    form's own option list and refuses with a message naming it.
    """
    entity = _expert_entity(_api())

    for plausible in (-40.0, 0.5, 350.0, 1440.0):
        assert entity.native_min_value <= plausible <= entity.native_max_value, (
            f"{plausible} is refused before the portal is ever asked"
        )


def test_a_slot_that_has_never_been_read_allows_a_half_step():
    """Same defect in the other axis: a step of 1 makes every half-value
    unreachable from the UI, including on the parameters that have them."""
    entity = _expert_entity(_api())

    assert entity.native_step <= 0.5


def test_a_read_replaces_the_placeholder_bounds_with_real_ones():
    """The counter-test: the wide range is a placeholder, not a new default.
    Once the portal has said what it accepts, that is what shows."""
    entity = _expert_entity(_api())
    entity.async_write_ha_state = lambda: None

    entity.apply_read_state(_read_state(21.5, [20.0, 20.5, 21.0, 21.5, 22.0]))

    assert entity.native_min_value == 20.0
    assert entity.native_max_value == 22.0
    assert entity.native_step == 0.5


def test_an_expert_parameter_does_not_claim_to_be_a_percentage():
    """Every slot was modelled as a percentage. The portal says no such thing
    - the edit form carries a list of allowed values and no unit at all - so
    a flow temperature, a curve slope and a delay all read as `%`, and Home
    Assistant records their history under that unit."""
    entity = _expert_entity(_api())

    assert entity.native_unit_of_measurement is None, (
        "a unit the portal never sent is still being published"
    )


def test_a_half_step_parameter_is_not_forced_to_whole_numbers():
    """Step was fixed at 1, so a parameter the portal offers in halves could
    only be set to half of its values - the other half was unreachable from
    the UI.

    Half-steps are also what an unread slot now carries as its placeholder,
    so this passes whether or not the step was derived. It stays as the
    statement of intent; the tests that actually pin the derivation are the
    two below, which assert steps the placeholder cannot produce.
    """
    entity = _expert_entity(_api())
    entity.async_write_ha_state = lambda: None

    entity.apply_read_state(_read_state(21.5, [20.0, 20.5, 21.0, 21.5, 22.0]))

    assert entity.native_step == 0.5, (
        "the step is still the assumed 1, so half-step values cannot be set"
    )


def test_the_step_of_a_whole_number_parameter_stays_whole():
    """The counter-test: deriving the step must not turn every parameter into
    a half-step one."""
    entity = _expert_entity(_api())
    entity.async_write_ha_state = lambda: None

    entity.apply_read_state(_read_state(60.0, [40.0, 50.0, 60.0, 70.0]))

    assert entity.native_step == 10.0


def test_an_unevenly_spaced_option_list_takes_its_smallest_gap():
    """The closest pair decides, not the first one.

    A step larger than the true one makes the values in between unreachable
    from the UI, which is the failure this is here to avoid; a smaller one
    only offers a value the portal then rejects, which the write path already
    checks against the form's own option list.
    """
    entity = _expert_entity(_api())
    entity.async_write_ha_state = lambda: None

    # 0.25 rather than 0.5: 0.5 is the placeholder step a slot carries before
    # it has been read, so a test asserting it cannot tell a derived step from
    # a derivation that never ran. Two mutations survived on exactly that.
    entity.apply_read_state(_read_state(30.0, [0.0, 10.0, 20.0, 20.25, 30.0]))

    assert entity.native_step == 0.25


def test_a_single_option_leaves_the_step_alone():
    """One option gives nothing to measure a step from. Guessing from a list
    of one would be the same mistake in a new place."""
    entity = _expert_entity(_api())
    entity.async_write_ha_state = lambda: None
    before = entity.native_step

    entity.apply_read_state(_read_state(5.0, [5.0]))

    assert entity.native_step == before


def test_a_restore_from_before_the_fix_does_not_bring_the_lock_back():
    """The upgrade path the wide placeholders would otherwise miss.

    RestoreNumber persists min, max and step whether or not there is a value,
    so a slot that existed before this was fixed has 0/100/1 in storage even
    though it was never read. Taking that back on the first start after the
    upgrade would overwrite the placeholders and lock the parameter out
    again - for exactly the installations the fix is for.

    Recognised by what it is: bounds that ARE the old made-up ones, on an
    entity that has no value to go with them. A real range that happens to be
    0 to 100 comes with a value, because it can only have been learnt by
    reading or writing one.
    """
    import types

    entity = _expert_entity(_api())

    entity._restore_from(
        types.SimpleNamespace(
            native_value=None,
            native_min_value=0,
            native_max_value=100,
            native_step=1,
        )
    )

    assert entity.native_min_value <= 350 <= entity.native_max_value, (
        "the pre-fix bounds came back and locked the parameter out again"
    )
    assert entity.native_step <= 0.5


def test_a_restored_range_that_was_really_read_is_kept():
    """The counter-test. 0 to 100 IS a plausible range - a percentage - and
    a stored one that came with a value was learnt from the portal."""
    import types

    entity = _expert_entity(_api())

    entity._restore_from(
        types.SimpleNamespace(
            native_value=42.0,
            native_min_value=0,
            native_max_value=100,
            native_step=1,
        )
    )

    assert entity.native_min_value == 0
    assert entity.native_max_value == 100
    assert entity.native_step == 1


def test_the_restored_range_comes_back_with_the_value():
    """Restore took the value and left the range behind, so after a restart a
    parameter whose real range is 200-800 sat at its stored value inside the
    assumed 0-100 - unsettable until the next successful read."""
    import types

    entity = _expert_entity(_api())
    entity._restore_from(
        types.SimpleNamespace(
            native_value=350.0,
            native_min_value=200.0,
            native_max_value=800.0,
            native_step=10.0,
        )
    )

    assert entity.native_value == 350.0
    assert entity.native_min_value == 200.0, "the restored range was dropped"
    assert entity.native_max_value == 800.0
    assert entity.native_step == 10.0


def test_entity_write_uses_the_expert_gate_not_the_global_one():
    """A rejected slider write must back off the EXPERT path only.

    This call site kept the GLOBAL cooldown when the expert-only backoff was
    introduced, so one rejected write paused all sensor polling - the very
    behaviour the expert backoff exists to end. The service and the auto-poll
    were converted; this one was missed and no test noticed.
    """
    api = _api()
    entity = _expert_entity(api)

    assert entity._cooldown_activate() == api.activate_expert_cooldown
    assert entity._cooldown_activate() != api._activate_cooldown
    assert entity._cooldown_check() == api.check_expert_cooldown
    assert entity._cooldown_check() != api.check_cooldown


def test_entity_write_reuses_the_shared_session_cache():
    """Without the shared jar every slider write performs a full login -
    the request the portal rejects most readily."""
    api = _api()
    api.expert_cookies = {"cookies": {"ASP.NET_SessionId": "abc"}, "saved_at": 1.0}
    entity = _expert_entity(api)

    assert entity._cookie_jar() is api.expert_cookies


def test_expert_accessors_degrade_safely_without_a_store():
    """During unload the entry store is gone; the accessors must return None
    rather than raise."""
    import types

    from custom_components.wemportal import expert_writer

    # No runtime_data at all: exactly what an unloaded entry looks like.
    entry = types.SimpleNamespace(entry_id="gone", data={}, options={})
    entity = expert_writer.WemPortalExpertNumber(entry, "slot", "B" * 36)
    entity.hass = types.SimpleNamespace(data={})

    assert entity._cookie_jar() is None
    assert entity._cooldown_check() is None
    assert entity._cooldown_activate() is None


def test_the_constructor_restores_the_state_that_was_persisted():
    """What the constructor arguments are still for, now that nothing swaps.

    Recovery stopped rebuilding the api object - it resets the transport in
    place (see the classification tests at the end of this file). These
    arguments survive a different boundary: Home Assistant restarts, where
    setup rebuilds the api from what the coordinator persisted. An active
    backoff, the cached modules and the stable scraper device id all have to
    come back, or the first cycle after a restart hits the portal as if
    nothing had happened.

    Note what this does NOT show: it builds the second object itself, so it
    proves the arguments are applied, not that any caller passes them. That
    caller is covered by the e2e setup tests.
    """
    old = _api(cached_modules=CACHED_MODULES, scraper_device_id="0000")
    old._activate_cooldown()
    old.activate_expert_cooldown()
    old.expert_cookies = {"cookies": {"ASP.NET_SessionId": "keep-me"}, "saved_at": 1.0}

    new = WemPortalApi(
        "user@example.org",
        "secret",
        cached_modules=old.modules,
        blocked_until=old._blocked_until,
        expert_blocked_until=old._expert_blocked_until,
        scraper_device_id=old.scraper_device_id,
    )
    new.expert_cookies = old.expert_cookies

    with pytest.raises(exceptions.ForbiddenError):
        new.check_cooldown()
    with pytest.raises(exceptions.ForbiddenError):
        new.check_expert_cooldown()
    assert new.expert_cookies == old.expert_cookies
    assert new.modules == CACHED_MODULES
    assert new.scraper_device_id == "0000"


def test_api_lock_wait_is_bounded(monkeypatch):
    """A poll whose await timed out keeps its executor thread - and the lock.

    An unbounded acquire parked the next operation behind it indefinitely,
    with no feedback at all. It must fail with a message instead.
    """
    # Shorten the wait: the point is that it is BOUNDED, not that it is 30s.
    monkeypatch.setattr(wemportalapi, "API_LOCK_TIMEOUT_SECONDS", 0.05)
    api = _api()
    api._api_lock.acquire()
    try:
        with pytest.raises(exceptions.ApiBusyError, match="free"):
            api._acquire_api_lock("test")
    finally:
        api._api_lock.release()


def test_api_lock_is_released_after_a_failing_poll():
    """The bounded acquire replaced a `with` block - the release must still
    happen on the error path, or the next cycle blocks forever."""
    api = _api()

    def boom(*_args, **_kwargs):
        raise exceptions.WemPortalError("portal down")

    api._fetch_data = boom
    with pytest.raises(exceptions.WemPortalError):
        api.fetch_data()

    # Free again: acquiring must succeed immediately.
    assert api._api_lock.acquire(blocking=False)
    api._api_lock.release()


class _Clock:
    """A monotonic clock the test moves by hand.

    Real elapsed time would make the deadline tests race their own runtime;
    what they assert is which INSTANT the budget is measured from, and that
    is only checkable when the test owns the clock.
    """

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_cycle_out_of_time_makes_no_further_request(monkeypatch):
    """The worker stops itself rather than spending another request.

    asyncio.timeout cancels the coordinator's await, never the executor
    thread: an overrunning cycle used to run on to completion, holding the
    shared lock and still spending requests at a portal that counts them per
    IP, long after Home Assistant had given up on the answer.
    """
    api = _api()
    session = RecordingSession()
    api.session = session
    api._deadline = time.monotonic() - 1

    with pytest.raises(exceptions.PollDeadlineExceeded, match="budget"):
        api.make_api_call(url="https://example.invalid/data", do_retry=False)

    assert session.post_kwargs is None, "a request was sent after the deadline"


def test_a_login_without_budget_left_sends_nothing(monkeypatch):
    """The login is a request too, and the one most worth not sending late.

    make_api_call asks the deadline before every call, so the ordinary poll
    traffic was covered. api_login is reached without going through it - from
    _ensure_api_session after a long wait for the lock, and again on the
    reauth retry after a request came back expired - and it only asked the
    cooldown. A cycle with nothing left could still start a fresh login and
    run past the coordinator's timeout holding the lock, which is the whole
    thing the deadline exists to stop.
    """
    api = _api()
    session = RecordingSession()
    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: session)
    api._deadline = time.monotonic() - 1

    with pytest.raises(exceptions.PollDeadlineExceeded):
        api.api_login()

    assert session.post_kwargs is None, "credentials went out after the deadline"


def test_a_login_outside_a_poll_is_not_deadlined(monkeypatch):
    """The counter-test. The config flow and the reauth flow call api_login
    directly, with a user waiting and no cycle behind it - there is no budget
    to be out of, and refusing there would break setup."""
    api = _api()
    session = RecordingSession()
    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: session)

    api.api_login()

    assert api.valid_login is True


def test_the_scrape_is_not_started_without_budget_left():
    """The scrape has its own session and never passes through
    make_api_call, so the cycle's deadline has to be checked here too. It is
    also the most expensive thing a cycle does - starting one with no budget
    left guarantees it is abandoned half-way."""
    api = _api()
    api._deadline = time.monotonic() - 1

    with pytest.raises(exceptions.PollDeadlineExceeded, match="budget"):
        api.fetch_webscraping_data()

    assert api._scraper is None, "the scraper was built despite no budget left"


def test_the_deadline_counts_the_wait_for_the_lock(monkeypatch):
    """The budget starts when fetch_data is ENTERED, not when it finally
    gets the lock.

    A cycle that spent its whole budget queueing has nothing left to spend at
    the portal - it would only be cut off mid-request. Measuring from the
    acquire instead would let a pile-up of queued cycles each claim a full
    budget of their own, which is the situation this exists to end.
    """
    api = _api()
    clock = _Clock()
    monkeypatch.setattr(wemportalapi.time, "monotonic", clock)
    monkeypatch.setattr(wemportalapi, "POLL_DEADLINE_SECONDS", 100)

    def slow_acquire(_what):
        clock.now += 90  # queued behind a running operation
        api._api_lock.acquire()

    seen = {}

    def record(*_args, **_kwargs):
        seen["deadline"] = api._deadline
        return {}

    api._acquire_api_lock = slow_acquire
    api._fetch_data = record
    api.fetch_data()

    assert seen["deadline"] == 1100, (
        "the deadline was measured from the acquire (1090+100), not from "
        "entry (1000+100), so queueing cost the cycle nothing"
    )


def test_the_deadline_is_cleared_after_a_failing_poll():
    """It must not outlive its cycle. Left standing, the next on-demand write
    would inherit a deadline that expired long ago and refuse to run."""
    api = _api()

    def boom(*_args, **_kwargs):
        raise exceptions.WemPortalError("portal down")

    api._fetch_data = boom
    with pytest.raises(exceptions.WemPortalError):
        api.fetch_data()

    assert api._deadline is None


def _out_of_time(*_args, **_kwargs):
    """Stand-in for make_api_call once the budget is gone."""
    raise exceptions.PollDeadlineExceeded("passed its 330s budget and stopped")


def test_no_broad_handler_can_swallow_the_deadline():
    """The invariant the four tests below are each one instance of.

    wemportalapi has twelve `except Exception` handlers and every one of them
    is right on its own terms, so the fix cannot be "remember to re-raise in
    each" - that holds until the thirteenth is written. Stating it once here
    says what the type is FOR, where four failing handler tests would only
    say that something broke.
    """
    try:
        raise exceptions.PollDeadlineExceeded("out of time")
    except Exception:  # noqa: BLE001
        pytest.fail(
            "the deadline was caught by a plain `except Exception`, so every "
            "broad handler in the poll path swallows it again"
        )
    except exceptions.PollDeadlineExceeded:
        pass


def test_the_deadline_is_not_reported_as_one_device_failing():
    """The per-device handler must let the deadline past.

    It catches Exception so one device's failure does not take the others
    with it, which is right - but the deadline is not a device failing. Caught
    here it becomes a returned string, the caller counts a failure, and a
    cycle where any OTHER device answered is then reported as a success. The
    worker keeps running, the lock stays held, and coordinator's handler for
    exactly this never sees it.
    """
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {"p1": {}}}}}
    api.make_api_call = _out_of_time

    with pytest.raises(exceptions.PollDeadlineExceeded):
        api._fetch_parameter_values("1234")


def test_the_deadline_is_not_reported_as_an_unreadable_status():
    """Same handler shape, one step earlier: an unreadable status must not
    stop the poll, but a spent budget must."""
    api = _offline_api(0)
    api.make_api_call = _out_of_time

    with pytest.raises(exceptions.PollDeadlineExceeded):
        api._fetch_device_status("1234")


def test_the_deadline_is_not_swallowed_by_the_schedule_fetch():
    """One heating programme failing is not a reason to skip the rest - but
    running out of time is a reason to stop all of them.

    The recognition and throttle steps are stubbed past on purpose: what is
    under test is the handler around the read, and building a module the real
    ones accept would put the test's own setup between it and the handler.
    """
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {"p1": {}}}}}
    api._is_schedule_parameter = lambda *_args: True
    api._schedule_is_due = lambda *_args: True
    api._record_schedule_attempt = lambda *_args: None
    api._read_one_schedule = _out_of_time

    with pytest.raises(exceptions.PollDeadlineExceeded):
        api._fetch_circuit_times("1234")


def test_the_deadline_is_not_swallowed_by_the_statistics_fetch(monkeypatch):
    """The statistics path already lets ForbiddenError past its catch-all for
    the same reason. The deadline was not given the same treatment.

    The refresh call has to SUCCEED for this to mean anything: it sits outside
    every try block, so a stub that fails on the first call reaches no handler
    at all and the test passes while proving nothing. Written that way first,
    and it was green before the fix.
    """
    monkeypatch.setattr(wemportalapi.time, "sleep", lambda _seconds: None)
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    calls = []

    def refresh_then_run_out(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return FakeResponse({"GroupTypeDescriptions": [{"GroupType": 1}]})
        raise exceptions.PollDeadlineExceeded("passed its 330s budget and stopped")

    api.make_api_call = refresh_then_run_out

    with pytest.raises(exceptions.PollDeadlineExceeded):
        api._fetch_device_statistics("1234")

    assert len(calls) == 2, "the group read was never reached, so no handler was"


class _BudgetedPage:
    """A main page that carries no form fields, so the scrape stops after
    one request - the timeout it was given is all this needs to see."""

    status_code = 200
    text = "<html><body>no form here</body></html>"
    url = "https://www.wemportal.com/Web/Default.aspx"


class _TimeoutRecordingSession:
    def __init__(self):
        self.timeouts = []

    def get(self, *_args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return _BudgetedPage()

    def post(self, *_args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return _BudgetedPage()


def _scraper_with_budget(remaining):
    from custom_components.wemportal.scraper import WemPortalScraper

    scraper = WemPortalScraper("user@example.org", "secret", budget=lambda: remaining)
    scraper.session = _TimeoutRecordingSession()
    return scraper


def test_a_scrape_request_may_not_outlast_what_is_left_of_the_budget():
    """A request started near the deadline runs past it by its own timeout.

    The scrape is checked once before it starts, and then does up to six
    requests of 30s each - so a scrape begun at 329s of a 330s budget ends
    around 509s, against a coordinator that gave up at 360s. The worker keeps
    the shared lock for the whole overrun, which is the exact situation the
    deadline exists to prevent.
    """
    scraper = _scraper_with_budget(5.0)

    scraper._load_expert_page()

    assert scraper.session.timeouts == [5.0], (
        "the request was allowed its full timeout, so it can outlast the "
        "cycle it belongs to"
    )


def test_a_request_that_ran_out_the_budget_is_reported_as_the_deadline():
    """The other end of capping a request at the remaining budget.

    Capped, the request times out ON that budget - and reported as an
    ordinary transport failure it is the coordinator's second failure, which
    discards the warm session and sends the next cycle through a cold login.
    Its deadline branch exists to avoid exactly that.
    """
    from custom_components.wemportal.scraper import WemPortalScraper

    spent = [5.0]

    class _TimesOutSession:
        def get(self, *_args, **_kwargs):
            # The budget is gone by the time the request gives up.
            spent[0] = 0.0
            raise TimeoutError("timed out")

    scraper = WemPortalScraper("user@example.org", "secret", budget=lambda: spent[0])
    scraper.session = _TimesOutSession()

    with pytest.raises(exceptions.PollDeadlineExceeded):
        scraper.scrape()


def test_a_reused_session_that_runs_out_of_time_is_the_deadline_too():
    """Only the login GET classified its transport failure; three did not.

    This one does the most damage. The session-reuse fast path re-raises the
    three answers that must not be retried and treats everything else as
    "reuse failed, log in fresh" - so a raw timeout here fires two MORE
    requests at a portal that just failed to answer one. The deadline is a
    BaseException precisely so it travels past that catch-all, and it only
    becomes one if the request site says so.
    """
    from custom_components.wemportal.scraper import WemPortalScraper

    spent = [5.0]

    class _TimesOutSession:
        def get(self, *_args, **_kwargs):
            # The budget is gone by the time the request gives up.
            spent[0] = 0.0
            raise TimeoutError("timed out")

    scraper = WemPortalScraper("user@example.org", "secret", budget=lambda: spent[0])
    scraper.session = _TimesOutSession()

    with pytest.raises(exceptions.PollDeadlineExceeded):
        scraper._load_expert_page()


def test_a_plain_network_failure_is_still_a_server_error():
    """The counter-test: with budget left, a timeout is the portal's problem
    and must keep its own classification."""
    from custom_components.wemportal.scraper import WemPortalScraper

    class _TimesOutSession:
        def get(self, *_args, **_kwargs):
            raise TimeoutError("timed out")

    scraper = WemPortalScraper("user@example.org", "secret", budget=lambda: 30.0)
    scraper.session = _TimesOutSession()

    with pytest.raises(exceptions.ServerError):
        scraper.scrape()


def test_a_scrape_does_not_start_a_request_it_has_no_budget_for():
    """Checked before EVERY request, not only before the scrape: the budget
    can run out between them, and the next one is what spends it."""
    scraper = _scraper_with_budget(0.0)

    with pytest.raises(exceptions.PollDeadlineExceeded):
        scraper._load_expert_page()

    assert scraper.session.timeouts == [], "a request was sent with no budget left"


def test_a_scrape_outside_a_poll_keeps_its_normal_timeout():
    """The counter-test. An on-demand scrape has no cycle behind it, so
    capping it at a leftover budget would cut short an operation with a user
    waiting on it."""
    from custom_components.wemportal import scraper as scraper_module

    scraper = _scraper_with_budget(None)

    scraper._load_expert_page()

    assert scraper.session.timeouts == [
        scraper_module.SCRAPER_REQUEST_TIMEOUT_SECONDS
    ], "a scrape with no poll behind it was capped anyway"


def test_the_scrape_is_built_with_the_cycles_budget(monkeypatch):
    """The wiring, which the three tests above take as given.

    They hand the scraper a budget themselves, so all three stay green if the
    api never passes one - and then nothing caps anything in production.
    """
    from custom_components.wemportal import scraper as scraper_module

    monkeypatch.setattr(
        scraper_module.WemPortalScraper, "scrape", lambda _self: [{"panel": {}}]
    )
    api = _api()
    api._deadline = time.monotonic() + 42

    api.fetch_webscraping_data()

    assert api._scraper._budget is not None, (
        "the scraper was built without a budget, so its requests are uncapped"
    )
    assert 41 < api._scraper._budget() <= 42


def test_a_login_page_without_its_form_is_not_blamed_on_the_password():
    """A 200 that is not the login form says nothing about the credentials.

    The password has not even been sent at this point - the fields being
    extracted are what it would be sent WITH. Reported as an AuthError it fed
    the reauth counter, so three such portal hiccups in a row could ask the
    user to re-enter a password that was correct all along.

    The transport half of this was already fixed one screen above (a timeout
    or a reset raises ServerError for exactly this reason). The structural
    half was left, which is why the release note claiming an unreadable login
    page no longer costs the password was too broadly worded.
    """
    from custom_components.wemportal.scraper import WemPortalScraper

    scraper = WemPortalScraper("user@example.org", "secret")
    scraper.session = _TimeoutRecordingSession()

    with pytest.raises(exceptions.ServerError):
        scraper.scrape()


class _LoginForm:
    """The login page, complete with the fields the POST needs."""

    status_code = 200
    text = (
        '<html><body><input id="__VIEWSTATE" value="vs"/>'
        '<input id="__EVENTVALIDATION" value="ev"/></body></html>'
    )
    url = "https://www.wemportal.com/Web/Login.aspx"


class _PageWithoutTheExpertView:
    """A main page the portal served without its form state.

    Carries the logged-in marker on purpose. The login POST is classified by
    those markers now, so a page with neither is refused there - and a test
    aiming at what happens AFTER a successful login would never get past it.
    A mutation caught this: it stayed green while the line it targets was
    unreachable.
    """

    status_code = 200
    text = f"<html><body><div id='{WEB_LOGGED_IN_MARKER}'></div></body></html>"
    url = "https://www.wemportal.com/Web/Default.aspx"


def test_maintenance_answering_the_login_post_is_not_a_wrong_password(monkeypatch):
    """Announced downtime can start between the GET and the POST.

    The login page is checked for the maintenance notice, and so is the main
    page - both are pages that carry one. The answer to the credential POST
    is one of those two, and it was the only one not checked. What happened
    instead: the response comes back on the login URL, which is the test for
    "the portal rejected these credentials", so planned downtime was reported
    as a wrong password and counted towards re-authentication. Three cycles
    inside one maintenance window, and Home Assistant asks for a password
    that was right all along.
    """
    import types

    from custom_components.wemportal.scraper import WemPortalScraper

    class _Response:
        status_code = 200
        url = "https://www.wemportal.com/Web/Login.aspx"

        def __init__(self, text):
            self.text = text

    class _Session:
        cookies = types.SimpleNamespace(clear=lambda: None)

        def get(self, *_args, **_kwargs):
            # The GET still sees a healthy form; downtime begins after it.
            return _Response(NORMAL_LOGIN_PAGE)

        def post(self, *_args, **_kwargs):
            return _Response(MAINTENANCE_PAGE)

    scraper = WemPortalScraper("user@example.org", "secret")
    scraper.session = _Session()
    monkeypatch.setattr(
        "custom_components.wemportal.scraper.time.sleep", lambda _seconds: None
    )

    with pytest.raises(exceptions.PortalMaintenanceError):
        scraper.scrape()


def test_a_portal_error_page_on_the_login_url_is_not_a_wrong_password(monkeypatch):
    """The URL alone does not say the credentials were refused.

    A portal error or interstitial answers with HTTP 200 and stays on
    Login.aspx, which was the whole test - so three of those in web mode walk
    into a re-authentication prompt. web_login in wemportalapi has had the
    right answer all along: look for the logged-in marker, then the
    maintenance notice, then the login FORM. Only the form is evidence that
    credentials were seen and rejected.
    """
    import types

    from custom_components.wemportal.scraper import WemPortalScraper

    class _Response:
        status_code = 200
        url = "https://www.wemportal.com/Web/Login.aspx"

        def __init__(self, text):
            self.text = text

    class _Session:
        cookies = types.SimpleNamespace(clear=lambda: None)

        def get(self, *_args, **_kwargs):
            return _Response(NORMAL_LOGIN_PAGE)

        def post(self, *_args, **_kwargs):
            # Neither logged in, nor maintenance, nor the login form: an
            # error page the portal happened to serve on this URL.
            return _Response("<html><body>Service temporarily busy</body></html>")

    scraper = WemPortalScraper("user@example.org", "secret")
    scraper.session = _Session()
    monkeypatch.setattr(
        "custom_components.wemportal.scraper.time.sleep", lambda _seconds: None
    )

    with pytest.raises(exceptions.ServerError):
        scraper.scrape()


def test_the_login_form_coming_back_is_still_a_wrong_password(monkeypatch):
    """The counter-test: when the portal DOES show the form again, the
    credentials really were refused and reauth is the right escalation."""
    import types

    from custom_components.wemportal.scraper import WemPortalScraper

    class _Response:
        status_code = 200
        url = "https://www.wemportal.com/Web/Login.aspx"

        def __init__(self, text):
            self.text = text

    class _Session:
        cookies = types.SimpleNamespace(clear=lambda: None)

        def get(self, *_args, **_kwargs):
            return _Response(NORMAL_LOGIN_PAGE)

        def post(self, *_args, **_kwargs):
            return _Response(NORMAL_LOGIN_PAGE)

    scraper = WemPortalScraper("user@example.org", "secret")
    scraper.session = _Session()
    monkeypatch.setattr(
        "custom_components.wemportal.scraper.time.sleep", lambda _seconds: None
    )

    with pytest.raises(exceptions.AuthError):
        scraper.scrape()


def test_a_main_page_without_its_state_after_a_login_is_not_a_wrong_password(
    monkeypatch,
):
    """Whatever this is, the credentials are not it - they just worked.

    `_load_expert_page` answers None for two different things: the session is
    no longer valid, and the page came back without the form state. Before
    the login that ambiguity is harmless, because the caller's answer to both
    is "log in fresh". After one it is not: the login has just succeeded, so
    reporting an AuthError blames credentials the portal accepted seconds
    ago - and feeds the counter that eventually asks the user to re-enter
    them.
    """
    import types

    from custom_components.wemportal.scraper import WemPortalScraper

    class _Session:
        cookies = types.SimpleNamespace(clear=lambda: None)

        def get(self, url, **_kwargs):
            if "Login" in url:
                return _LoginForm()
            return _PageWithoutTheExpertView()

        def post(self, *_args, **_kwargs):
            return _PageWithoutTheExpertView()

    scraper = WemPortalScraper("user@example.org", "secret")
    scraper.session = _Session()
    monkeypatch.setattr(
        "custom_components.wemportal.scraper.time.sleep", lambda _seconds: None
    )

    with pytest.raises(exceptions.ServerError):
        scraper.scrape()


class _NewRegistry:
    """A device registry of the generation that has the unambiguous lookup."""

    def __init__(self, answer=None):
        self.answer = answer
        self.asked = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.asked.append((identifier, config_entry_id))
        return self.answer

    def async_get_device(self, identifiers=None):
        raise AssertionError(
            "the deprecated lookup was used on a registry that offers the replacement"
        )


class _OldRegistry:
    """A 2024.12 registry, which has only the ambiguous one."""

    def __init__(self, answer=None):
        self.answer = answer
        self.asked = []

    def async_get_device(self, identifiers=None):
        self.asked.append(identifiers)
        return self.answer


def test_a_device_is_looked_up_by_entry_where_that_is_possible():
    """async_get_device matches on the identifier alone, so two integrations
    registering the same one are indistinguishable - which is why Home
    Assistant deprecates it. The replacement takes the config entry too."""
    from custom_components.wemportal.coordinator import device_by_identifier

    registry = _NewRegistry(answer="the device")

    found = device_by_identifier(registry, ("wemportal", "e1:1234"), "e1")

    assert found == "the device"
    assert registry.asked == [(("wemportal", "e1:1234"), "e1")]


def test_a_device_is_still_found_on_the_minimum_supported_version():
    """2024.12 has no such method, and the minimum stays supported. Detected
    by asking the registry rather than by comparing versions - a version
    number says what release this is, not what this object can do."""
    from custom_components.wemportal.coordinator import device_by_identifier

    registry = _OldRegistry(answer="the device")

    found = device_by_identifier(registry, ("wemportal", "e1:1234"), "e1")

    assert found == "the device"
    assert registry.asked == [{("wemportal", "e1:1234")}]


def test_a_forbidden_error_is_a_wemportal_error():
    """What the coordinator's single WemPortalError clause rests on.

    It used to name `(WemPortalError, ForbiddenError)`, which reads as two
    cases and is one - the second derives from the first. Naming only the
    parent is correct exactly as long as that holds. Should the inheritance
    ever change, a 403 would fall past that clause into the generic handler,
    which resets the transport after two failures: a cold login forced by a
    rate limit, which is the one response guaranteed to make it worse.
    """
    assert issubclass(exceptions.ForbiddenError, exceptions.WemPortalError)


def test_an_operation_outside_a_poll_is_not_deadlined():
    """Only fetch_data sets a deadline. An on-demand write has a user waiting
    on it and no coordinator timeout behind it, so it must run even when the
    last poll's budget would long since have expired."""
    api = _api()
    session = RecordingSession()
    api.session = session

    api.change_value("1234", "P1", 0, 1, 21.0)

    assert api._deadline is None
    assert session.post_kwargs is not None, "the write was refused without a poll"


def _web_api(mode, scraped=None):
    api = _api(config={"mode": mode}, scraper_device_id="0000")
    api.modules = {}
    api._scrape_calls = []

    def fake_scrape():
        api._scrape_calls.append(True)
        return scraped if scraped is not None else [{"cookie": {}}]

    api.fetch_webscraping_data = fake_scrape
    api._merge_webscraping_data = lambda *_args, **_kwargs: None
    api.get_devices = lambda *_args, **_kwargs: None
    api.get_data = lambda *_args, **_kwargs: None
    api.get_statistics = lambda *_args, **_kwargs: None
    api.get_parameters = lambda *_args, **_kwargs: None
    api.api_login = lambda *_args, **_kwargs: None
    api.web_login = lambda *_args, **_kwargs: None
    api._devices_fetched_this_session = True
    api.modules = {"0000": {}}
    return api


def test_the_scrape_timestamp_carries_its_timezone():
    """The scrape interval is a difference between two of these timestamps.

    Naive local times are subtracted as if the clock never moved, so a
    daylight-saving change lands squarely in that difference: in spring it
    reads an hour too LONG and the next scrape fires at once, in autumn an
    hour too SHORT and a whole hour of cycles is skipped. An aware timestamp
    carries its offset, so Python normalises both sides to UTC first.

    Asserted on the stored value rather than by simulating a DST change: the
    offset is the property that makes the arithmetic right, and a test that
    moved the clock would only be testing Python's own subtraction.
    """
    api = _web_api("both")
    api.spider_wait_interval = 0
    api.last_scraping_update = None

    api._scrape_and_merge()

    assert api.last_scraping_update.tzinfo is not None, (
        "a naive timestamp puts the DST jump straight into the scrape interval"
    )


def test_web_mode_honours_a_fully_disabled_installation():
    """Scraping is the heaviest request the integration makes, and it
    ignored the device filter entirely - so disabling every device still
    triggered a full portal scrape."""
    api = _web_api("web")

    api.fetch_data(enabled_devices=[])

    assert api._scrape_calls == [], "a disabled installation was still scraped"


def test_both_mode_honours_a_fully_disabled_installation():
    api = _web_api("both")

    api.fetch_data(enabled_devices=[])

    assert api._scrape_calls == [], "a disabled installation was still scraped"


def test_web_mode_still_scrapes_when_its_device_is_enabled():
    """The filter must not switch scraping off wholesale."""
    api = _web_api("web")

    api.fetch_data(enabled_devices=["0000"])

    assert api._scrape_calls, "an enabled scraper device was skipped"


def test_web_mode_scrapes_when_no_filter_is_given():
    """None means "no filter" - and the first scrape is what decides the
    scraper device id in the first place."""
    api = _web_api("web")
    api.scraper_device_id = None

    api.fetch_data(enabled_devices=None)

    assert api._scrape_calls, "an unfiltered cycle skipped the scrape"


def test_lock_timeout_is_not_treated_as_a_corrupted_session():
    """ApiBusyError must NOT be a plain WemPortalError to the coordinator's
    recovery heuristic: re-instantiating the api would close the sessions the
    still-running thread is using and hand the next poll a fresh lock,
    removing the serialization and doubling the load on a slow portal."""
    assert issubclass(exceptions.ApiBusyError, exceptions.WemPortalError)
    # ...but it is a distinct type the coordinator can single out first.
    assert exceptions.ApiBusyError is not exceptions.WemPortalError


def test_disabled_installation_is_honoured_even_before_the_id_is_known():
    """The undecided-scraper-id escape must not override an EXPLICIT
    "everything is disabled" filter - that was the guard's own failure mode
    (reachable after an api swap where no scrape ever succeeded)."""
    api = _web_api("web")
    api.scraper_device_id = None

    api.fetch_data(enabled_devices=[])

    assert api._scrape_calls == [], "scraped despite an all-disabled filter"


def test_scrape_is_skipped_when_only_its_own_device_is_disabled():
    """The realistic case: other devices stay enabled, the scraper's own
    pseudo-device is switched off.

    The all-disabled tests pass an empty list, which an early return handles;
    the positive tests only guard against over-blocking. Neither exercises the
    membership check itself, so the gate could be reduced to "always allow"
    without either noticing.
    """
    api = _web_api("both")

    api.fetch_data(enabled_devices=["1234", "5678"])

    assert api._scrape_calls == [], (
        "the scraper device was disabled but the portal was scraped anyway"
    )


def _catalogue(name):
    """One shipped translation catalogue."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "custom_components" / "wemportal"
    return json.loads((base / name).read_text(encoding="utf-8"))


def _keys(node, prefix=""):
    """Every leaf path of a catalogue, so two of them can be compared."""
    if not isinstance(node, dict):
        return {prefix}
    return {
        key for name, value in node.items() for key in _keys(value, f"{prefix}/{name}")
    }


def test_the_two_translation_catalogues_carry_the_same_keys():
    """The check that would have caught the drift this repository shipped.

    A third catalogue, strings.json, sat beside these two: Home Assistant
    never reads it for a custom integration - only translations/ - and
    hassfest skips it when it is absent, so nothing anywhere compared it
    against them. It fell two keys and one text behind without a single
    warning, and was removed. What matters is that the two files people DO
    see stay in step, and only a test can say so: a key missing in one shows
    up as untranslated text in the UI and nowhere else.
    """
    missing = _keys(_catalogue("translations/en.json")) ^ _keys(
        _catalogue("translations/de.json")
    )

    assert not missing, f"these keys exist in only one language: {sorted(missing)}"


def test_service_texts_exist_in_every_translation_file():
    """Home Assistant reads service name/description from the translation
    catalogue, not from services.yaml. A key missing in one file shows up only
    as untranslated text in the UI, so check both - including the privacy
    warning on the entityvalue field, which must not get lost in
    translation."""
    for name in ("translations/en.json", "translations/de.json"):
        data = _catalogue(name)
        service = data["services"]["set_expert_parameter"]
        assert service["name"], name
        assert service["description"], name
        fields = service["fields"]
        assert set(fields) == {"entityvalue", "value"}, name
        for field in fields.values():
            assert field["name"], name
            assert field["description"], name
        # The entityvalue is installation-specific; the warning is part of
        # the contract with the user, not decoration.
        warning = fields["entityvalue"]["description"].lower()
        assert "not share" in warning or "nicht öffentlich" in warning, name


def test_portal_units_are_normalised_to_home_assistant_spelling():
    """A device class is not enough - the UNIT must match what Home Assistant
    accepts for it. The portal writes "BAR"; HA logs a warning for every
    reading unless it is "bar"."""
    from custom_components.wemportal.utils import (
        fix_value_and_unit,
        unit_to_device_class,
    )

    value, unit = fix_value_and_unit(2.5, "BAR")

    assert (value, unit) == (2.5, "bar")
    assert unit_to_device_class(unit) == "pressure"


# Trimmed from a real maintenance page. The login form stays fully present
# and submittable - only the backend behind it is down - so the notice
# container is the only thing that sets the two states apart.
MAINTENANCE_PAGE = """<html><body>
  <div id="ctl00_MasterContent" class="MasterContentLogin">
    <div class="offlinecontent">
      Sehr geehrter Kunde,<br><br>
      aufgrund von Wartungsarbeiten ist der Server zwischen 28.07.2026 16:00 Uhr
      und 28.07.2026 19:00 Uhr nicht erreichbar.
    </div>
    <div id="ctl00_content_dvLogin" class="dvLogin">
      <input type="hidden" id="__VIEWSTATE" name="__VIEWSTATE" value="x">
      <input type="hidden" id="__EVENTVALIDATION" name="__EVENTVALIDATION" value="y">
      <input name="ctl00$content$tbxUserName" type="text">
      <input name="ctl00$content$tbxPassword" type="password">
      <input name="ctl00$content$btnLogin" type="submit" value="Anmelden">
    </div>
  </div>
</body></html>"""

NORMAL_LOGIN_PAGE = MAINTENANCE_PAGE.replace("offlinecontent", "someothercontent")


def test_maintenance_notice_is_detected_and_quoted():
    from custom_components.wemportal.utils import maintenance_notice

    notice = maintenance_notice(MAINTENANCE_PAGE)

    assert notice is not None
    # The actual window belongs in the log - that is what tells the user
    # when to expect the integration back.
    assert "28.07.2026 19:00" in notice


def test_a_normal_login_page_is_not_mistaken_for_maintenance():
    """The dangerous direction: wrong credentials must still reach the reauth
    flow. Matching loosely (e.g. on the word "Wartungsarbeiten" anywhere)
    would risk swallowing a genuine credential failure forever."""
    from custom_components.wemportal.utils import maintenance_notice

    assert maintenance_notice(NORMAL_LOGIN_PAGE) is None
    assert maintenance_notice("") is None
    assert maintenance_notice(None) is None


def test_web_login_reports_maintenance_without_sending_credentials(monkeypatch):
    """Bailing out before the POST matters twice: the credentials are not
    sent to a page that cannot process them, and the failure is not
    misreported as "invalid username or password"."""
    posted = []

    class _Session:
        cookies = {}

        def get(self, *_args, **_kwargs):
            return FakeResponse_html(MAINTENANCE_PAGE)

        def post(self, *_args, **_kwargs):
            posted.append(True)
            return FakeResponse_html("")

    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: _Session())
    api = _api()

    with pytest.raises(exceptions.PortalMaintenanceError):
        api.web_login()

    assert posted == [], "credentials were sent to the maintenance page"


def _web_login_answering(post_answer, get_answer=None, monkeypatch=None):
    """An api whose web login gets `get_answer`, then posts and gets back
    `post_answer`."""

    class _Session:
        cookies = {}

        def get(self, *_args, **_kwargs):
            # A real login page by default. These tests are about the answer
            # to the POST, and an empty body here is not a shortcut but a
            # different case entirely - the login now refuses to send
            # credentials to a page it could not read.
            return (
                get_answer
                if get_answer is not None
                else FakeResponse_html(NORMAL_LOGIN_PAGE)
            )

        def post(self, *_args, **_kwargs):
            return post_answer

    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: _Session())
    return _api()


def test_an_unreadable_login_page_costs_no_credentials(monkeypatch):
    """The portal answering 200 with an empty body.

    The previous HTML parser returned no fields for it and let the POST go
    ahead, so the password went to a page that had said nothing, with none of
    the ASP.NET state it demands back - a request that could only be refused.
    Same rule as the maintenance bail-out: do not hand credentials to a page
    that cannot process them.
    """
    posted = []

    class _Session:
        cookies = {}

        def get(self, *_args, **_kwargs):
            return FakeResponse_html("")

        def post(self, *_args, **_kwargs):
            posted.append(True)
            return FakeResponse_html("")

    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: _Session())

    api = _api()

    with pytest.raises(exceptions.UnknownAuthError):
        api.web_login()

    assert posted == [], "credentials were sent to a page that could not be read"


def test_a_forbidden_login_page_is_a_refusal_not_a_network_problem(monkeypatch):
    """This is the request that MEETS a blocked IP - it comes before the POST
    the 403 handling was written for. Reported as "could not load the page" it
    read like a hiccup, invited an immediate retry and started no cooldown, so
    the next cycle walked into the same wall."""

    class _Refused(FakeResponse_html):
        def raise_for_status(self):
            raise real_requests.exceptions.HTTPError("403", response=self)

    class _Session:
        cookies = {}

        def get(self, *_args, **_kwargs):
            return _Refused("forbidden", status_code=403)

        def post(self, *_args, **_kwargs):
            raise AssertionError("credentials were sent to a refusing portal")

    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: _Session())
    api = _api()

    with pytest.raises(exceptions.ForbiddenError):
        api.web_login()

    with pytest.raises(exceptions.ForbiddenError):
        api.check_cooldown()


def test_a_page_that_is_neither_login_nor_session_is_not_a_wrong_password(monkeypatch):
    """The portal answers HTTP 200 for a rejected login AND for the odd error
    or interstitial page. Only the first is about the credentials; counting
    the second towards the reauth escalation asks the user to re-enter a
    password that was right all along."""
    api = _web_login_answering(
        FakeResponse_html("<html><body>Something else entirely</body></html>"),
        monkeypatch=monkeypatch,
    )

    with pytest.raises(exceptions.UnknownAuthError):
        api.web_login()


def test_maintenance_starting_between_the_two_requests_is_recognised(monkeypatch):
    """The window can open after the login page was fetched. Only the first
    answer was checked, so the second was read as a wrong password."""
    api = _web_login_answering(
        FakeResponse_html(MAINTENANCE_PAGE), monkeypatch=monkeypatch
    )

    with pytest.raises(exceptions.PortalMaintenanceError):
        api.web_login()


def test_a_login_is_not_attempted_during_a_cooldown(monkeypatch):
    """Both logins, because both are reachable from the config and reauth
    flows - which is where somebody lands after deleting and re-adding the
    integration to "fix" a blockade, extending it with every attempt."""

    class _Session:
        cookies = {}

        def get(self, *_args, **_kwargs):
            raise AssertionError("a request was sent during the cooldown")

        def post(self, *_args, **_kwargs):
            raise AssertionError("a request was sent during the cooldown")

    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: _Session())
    api = _api()
    api._activate_cooldown()

    with pytest.raises(exceptions.ForbiddenError):
        api.web_login()
    with pytest.raises(exceptions.ForbiddenError):
        api.api_login()


FORM_PAGE = """<html><body>
  <input type="hidden" name="__VIEWSTATE" value="vs">
  <input type="hidden" name="__EVENTVALIDATION" value="ev">
  <input type="hidden" name="__EMPTY">
  <input type="hidden" value="no name of its own">
  <input type="hidden" name="" value="blank name">
  <input name="ctl00$content$tbxUserName" type="text" value="visible">
  <input name="ctl00$content$btnLogin" type="submit" value="Anmelden">
  <input name="ctl00$content$chkStayLoggedIn" type="checkbox" value="stray">
</body></html>"""


def test_the_login_form_carries_exactly_the_hidden_fields(monkeypatch):
    """ASP.NET rejects a post that does not echo its own hidden state back,
    so which fields are collected IS the login.

    Written against the behaviour before the HTML parser was swapped, so the
    swap has something to be equal to - each of these is a way the two
    parsers could differ: a hidden field with no value attribute, one with no
    name, one whose name is empty, and the visible fields that must not come
    along.
    """
    posted = {}

    class _Session:
        cookies = {}

        def get(self, *_args, **_kwargs):
            return FakeResponse_html(FORM_PAGE)

        def post(self, _url, data=None, **_kwargs):
            posted.update(data)
            return FakeResponse_html("<html>ctl00_btnLogout</html>")

    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: _Session())
    _api().web_login()

    hidden = {
        key: value
        for key, value in posted.items()
        if not key.startswith("ctl00$content$")
    }
    assert hidden == {"__VIEWSTATE": "vs", "__EVENTVALIDATION": "ev", "__EMPTY": ""}
    # The credentials and the button are added by the login itself, not read
    # off the page.
    assert posted["ctl00$content$tbxUserName"] == "user@example.org"
    # A visible field the login does NOT overwrite. The first version of this
    # test only had visible fields whose names the login sets anyway, so
    # dropping the type filter from the selector changed nothing it could see
    # - the assertion passed while every visible field was being collected.
    assert "ctl00$content$chkStayLoggedIn" not in posted


def test_maintenance_is_not_an_auth_error():
    """It must not feed the reauth escalation: the portal serves a working
    login form during maintenance, so three cycles of it used to ask the user
    to re-enter credentials that were correct all along."""
    assert not issubclass(exceptions.PortalMaintenanceError, exceptions.AuthError)
    assert issubclass(exceptions.PortalMaintenanceError, exceptions.WemPortalError)


class FakeResponse_html:
    """A response carrying HTML rather than JSON."""

    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.url = "https://www.wemportal.com/Web/Login.aspx"

    def raise_for_status(self):
        pass


def test_data_read_carries_the_job_id_from_refresh():
    """/Refresh answers with the JobID of the measurement it started, and
    /Read accepts it. Reading without it works - the server falls back to
    the newest job - but two overlapping refreshes can then hand back the
    other one's values."""
    api = _api()
    api.modules = {"1234": {(1, 2): {"Index": 1, "Type": 2, "parameters": {"P1": {}}}}}
    calls = []

    def make_api_call(url, data=None, **_kwargs):
        calls.append((url, data))
        if url == wemportalapi.API_REFRESH_URL:
            return FakeResponse({"Status": 0, "JobID": 100000001})
        return FakeResponse({"Modules": []})

    api.make_api_call = make_api_call
    api._fetch_parameter_values("1234")

    read = next(
        payload
        for url, payload in calls
        if url == wemportalapi.API_DATA_ACCESS_READ_URL
    )
    assert read["JobID"] == 100000001
    # The refresh itself must NOT carry a JobID - it is what creates one.
    refresh = next(
        payload for url, payload in calls if url == wemportalapi.API_REFRESH_URL
    )
    assert "JobID" not in refresh


def test_data_read_omits_the_job_id_when_refresh_returns_none():
    """A response without a JobID must behave exactly as before rather than
    sending "JobID": null."""
    api = _api()
    api.modules = {"1234": {(1, 2): {"Index": 1, "Type": 2, "parameters": {"P1": {}}}}}
    calls = []

    def make_api_call(url, data=None, **_kwargs):
        calls.append((url, data))
        return FakeResponse({"Modules": []} if "Read" in url else {"Status": 0})

    api.make_api_call = make_api_call
    api._fetch_parameter_values("1234")

    read = next(
        payload
        for url, payload in calls
        if url == wemportalapi.API_DATA_ACCESS_READ_URL
    )
    assert "JobID" not in read


def test_statistics_entry_is_chosen_by_date_not_position():
    """The API happens to return the newest day last, so the code took
    values[-1] and never looked at Date. That is an assumption about
    ordering: a differently sorted response yields the wrong day's reading
    with no sign that anything went wrong."""
    from custom_components.wemportal.utils import latest_statistics_entry

    out_of_order = [
        {"Date": "2026-04-28T00:00:00", "Value": 8.0},
        {"Date": "2026-04-26T00:00:00", "Value": 5.0},
        {"Date": "2026-04-27T00:00:00", "Value": 7.3},
    ]

    assert latest_statistics_entry(out_of_order)["Value"] == 8.0


def test_statistics_falls_back_to_the_last_entry_without_dates():
    """No Date means no better information - keep the previous behaviour
    rather than guessing."""
    from custom_components.wemportal.utils import latest_statistics_entry

    assert latest_statistics_entry([{"Value": 1.0}, {"Value": 2.0}])["Value"] == 2.0
    assert latest_statistics_entry([]) is None


def test_device_model_comes_from_the_reported_device_type():
    """DeviceType 2 is a heat pump; Home Assistant showed the generic
    "WEM Portal" for every device regardless."""
    from custom_components.wemportal.utils import build_device_info, device_model

    api = _api()
    api.device_types = {"1234": 2, "5678": 1}

    assert device_model(api, "1234") == "Heat pump"
    assert device_model(api, 1234) == "Heat pump", "int device ids must work too"
    assert device_model(api, "5678") == "Combi boiler"
    # Unknown or unreported type -> generic name via build_device_info.
    assert device_model(api, "9999") is None
    assert build_device_info("e1", "9999", model=None)["model"] == "WEM Portal"
    assert build_device_info("e1", "1234", model="Heat pump")["model"] == "Heat pump"


def test_device_type_is_recorded_but_kept_out_of_the_entity_data():
    """The entity platforms iterate the data dict and would try to build an
    entity from a stray value."""
    api = _api()
    device_json = {
        "Devices": [
            {
                "ID": 1234,
                "DeviceType": 2,
                "ConnectionStatus": 0,
                "Modules": [{"Index": 0, "Type": 1, "Name": "Heat pump"}],
            }
        ]
    }
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(device_json)

    api.get_devices()

    assert api.device_types == {"1234": 2}
    assert "DeviceType" not in api.data["1234"]


def test_web_mode_validation_does_not_accept_a_config_that_cannot_poll(monkeypatch):
    """`api` and `both` call api_login() on every cycle, so validating them
    with a web login accepted a configuration that could never poll: setup
    succeeded, then every update failed with an API login the user was never
    told about."""

    from custom_components.wemportal import config_flow
    from custom_components.wemportal.const import CONF_MODE

    tried = []

    def api_login(self):
        tried.append("api")
        raise exceptions.AuthError("no api access")

    def web_login(self):
        tried.append("web")

    monkeypatch.setattr(WemPortalApi, "api_login", api_login)
    monkeypatch.setattr(WemPortalApi, "web_login", web_login)

    class _Hass:
        @staticmethod
        async def async_add_executor_job(function, *args):
            return function(*args)

    data = {"username": "user@example.org", "password": "secret", CONF_MODE: "both"}
    hass = _Hass()

    with pytest.raises(config_flow.InvalidAuth):
        _run(config_flow.validate_input, hass, data)

    assert tried == ["api"], "a failed API login must not fall back to web"


# --- a blocked IP is not a connection problem ---------------------------


def _validate_with(monkeypatch, error):
    """Run the config-flow validation against a login that raises `error`."""
    import asyncio

    from custom_components.wemportal import config_flow
    from custom_components.wemportal.const import CONF_MODE

    def api_login(self):
        raise error

    monkeypatch.setattr(WemPortalApi, "api_login", api_login)

    class _Hass:
        @staticmethod
        async def async_add_executor_job(function, *args):
            return function(*args)

    data = {"username": "user@example.org", "password": "secret", CONF_MODE: "api"}
    return asyncio.run(config_flow.validate_input(_Hass(), data))


def test_a_blocked_ip_is_not_reported_as_a_connection_problem(monkeypatch):
    """Upstream #138. "Failed to connect" reads like a network fault and
    invites an immediate retry - against an IP the portal is refusing right
    now, and refuses per IP for twelve hours past its request limit. Every
    retry makes the situation it describes last longer, which is how people
    end up deleting and re-adding the integration to "fix" a blockade."""
    from custom_components.wemportal import config_flow

    refusal = exceptions.ForbiddenError("403")

    with pytest.raises(config_flow.RateLimited):
        _validate_with(monkeypatch, refusal)


def test_an_ordinary_failure_is_still_a_connection_problem(monkeypatch):
    """The broad handler stays for everything that is not a refusal."""
    from custom_components.wemportal import config_flow

    outage = OSError("network down")

    with pytest.raises(config_flow.CannotConnect):
        _validate_with(monkeypatch, outage)


def test_wrong_credentials_are_still_wrong_credentials(monkeypatch):
    from custom_components.wemportal import config_flow

    wrong_password = exceptions.AuthError("bad password")

    with pytest.raises(config_flow.InvalidAuth):
        _validate_with(monkeypatch, wrong_password)


def test_both_flows_have_a_message_for_a_blocked_ip():
    """The step catches it; without the translation the user gets a raw key."""
    import pathlib

    from custom_components.wemportal import config_flow

    source = pathlib.Path(config_flow.__file__).read_text(encoding="utf-8")
    assert source.count('errors["base"] = "rate_limited"') == 2, (
        "the setup step and the re-authentication step must both say it"
    )

    for name in ("translations/en.json", "translations/de.json"):
        assert _catalogue(name)["config"]["error"].get("rate_limited"), name


def _offline_api(status):
    """An api whose single device reports `status` on every call."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"ConnectionStatus": status, "Errors": [], "GroupTypeDescriptions": []}
    )
    return api


# --- a status nobody could read must not be published as current --------


def _api_with_a_read_status():
    """An api that has read its device status once, successfully."""
    api = _offline_api(0)
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"ConnectionStatus": 0, "Errors": [], "GroupTypeDescriptions": []}
    )
    api._fetch_device_status("1234")
    return api


def test_a_single_fault_reads_as_itself():
    from custom_components.wemportal.utils import error_state_and_detail

    assert error_state_and_detail(["E12 Sensor defect"]) == (
        "E12 Sensor defect",
        ["E12 Sensor defect"],
    )
    assert error_state_and_detail([]) == ("None", [])


def test_a_fault_list_too_long_for_a_state_says_how_many_it_dropped():
    """Slicing the joined text at 255 characters and saying nothing is how a
    second active fault disappears while the first still reads like the whole
    story."""
    from homeassistant.const import MAX_LENGTH_STATE_STATE

    from custom_components.wemportal.utils import error_state_and_detail

    faults = [
        f"E{index:02d} something is wrong with a component" for index in range(12)
    ]

    state, detail = error_state_and_detail(faults)

    assert len(state) <= MAX_LENGTH_STATE_STATE
    assert state.endswith(" more)"), state
    assert detail == faults, "the attribute lost faults the state could not hold"
    # The count has to be the real one, not a placeholder.
    kept = state.split(" (+")[0].split(", ")
    assert f"(+{len(faults) - len(kept)} more)" in state


def test_one_fault_too_long_on_its_own_is_cut_and_says_so():
    """Better a marked cut than no state at all - Home Assistant refuses an
    over-long state outright."""
    from homeassistant.const import MAX_LENGTH_STATE_STATE

    from custom_components.wemportal.utils import error_state_and_detail

    faults = ["E01 " + "x" * 400, "E02 second"]

    state, detail = error_state_and_detail(faults)

    assert len(state) <= MAX_LENGTH_STATE_STATE
    assert "..." in state
    assert "(+1 more)" in state
    assert detail == faults


def test_the_full_fault_list_reaches_the_attribute():
    api = _api_with_a_read_status()
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {
            "ConnectionStatus": 0,
            "Errors": ["E12 one", "E13 two"],
            "GroupTypeDescriptions": [],
        }
    )

    api._fetch_device_status("1234")

    row = api.data["1234"]["1234-ErrorMessages"]
    assert row["value"] == "E12 one, E13 two"
    assert row["Errors"] == ["E12 one", "E13 two"]


def test_the_error_attribute_reaches_the_entity():
    """The full list is only worth carrying if it gets past the row."""
    entity = _sensor_from_row(
        "1234-ErrorMessages",
        {
            "value": "E12 one (+3 more)",
            "unit": None,
            "friendlyName": "Error Messages",
            "ParameterID": "ErrorMessages",
            "Errors": ["E12 one", "E13 two", "E14 three", "E15 four"],
        },
    )

    assert entity.extra_state_attributes["Errors"] == [
        "E12 one",
        "E13 two",
        "E14 three",
        "E15 four",
    ]


def test_a_status_that_could_not_be_read_stops_claiming_no_fault():
    """The one direction a fault sensor must never fail in.

    The status rows are written only by a successful read. Left alone when
    one fails, "Has Errors" goes on answering "No" - because nothing is
    known, not because nothing is wrong - and an automation waiting for a
    fault sees the quiet and concludes there is none.
    """
    api = _api_with_a_read_status()
    assert api.data["1234"]["1234-HasErrors"]["value"] == "No"

    def refuse(*_args, **_kwargs):
        raise exceptions.WemPortalError("portal unavailable")

    api.make_api_call = refuse
    api._fetch_device_status("1234")

    assert api.data["1234"]["1234-HasErrors"]["value"] is None
    assert api.data["1234"]["1234-ErrorMessages"]["value"] is None
    assert api.data["1234"]["1234-ConnectionStatus"]["value"] is None


def test_a_status_nobody_could_read_leaves_the_entities_available():
    """ "Unknown" is the honest answer; unavailable would hide the entity
    that exists to explain the situation."""
    from custom_components.wemportal.utils import device_is_reachable

    api = _api_with_a_read_status()

    def refuse(*_args, **_kwargs):
        raise exceptions.WemPortalError("portal unavailable")

    api.make_api_call = refuse
    api._fetch_device_status("1234")

    assert device_is_reachable(api.data, "1234") is True


def test_a_failed_status_read_does_not_stop_parameter_discovery():
    """The raw gate stays: a failed status read says nothing about whether
    the device is there, and stopping discovery over it would turn one
    missed request into an installation with no parameters at all."""
    api = _api_with_a_read_status()

    def refuse(*_args, **_kwargs):
        raise exceptions.WemPortalError("portal unavailable")

    api.make_api_call = refuse
    api._fetch_device_status("1234")

    assert api.data["1234"]["ConnectionStatus"] == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [(50, "offline"), (7, "wrong_secret"), (8, "busy"), (99, "unknown")],
)
def test_a_device_that_is_not_online_does_not_fail_the_whole_cycle(status, expected):
    """An unreachable device is reported by its OWN entities, not by
    failing the cycle.

    Failing it took every other entity down as well - including the
    connection-status sensor that would have explained the situation -
    discarded a web scrape that had already succeeded this cycle in `both`
    mode, and put the coordinator into a backoff of up to six hours, so the
    device coming back was noticed late. utils.device_is_reachable and the
    platforms' `available` carry this instead.
    """
    api = _offline_api(status)

    api.get_data(enabled_devices=["1234"])

    # Recorded, because that is what the entities read to go unavailable.
    assert api.data["1234"]["1234-ConnectionStatus"]["value"] == expected


def test_the_offline_warning_is_logged_once_per_change(caplog):
    """With the cycle no longer failing, this line is the only running
    commentary - so it must not repeat every few minutes for as long as the
    device stays away, and it must say when the device comes back.
    """
    import logging

    api = _offline_api(50)

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            api.get_data(enabled_devices=["1234"])
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, f"repeated every cycle: {warnings}"
        assert "offline" in warnings[0]

        caplog.clear()
        api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
            {
                "ConnectionStatus": 0,
                "Errors": [],
                "Modules": [],
                "GroupTypeDescriptions": [],
            }
        )
        api.get_data(enabled_devices=["1234"])

    assert any(
        "back online" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "recovery went unmentioned"


# --- a failed cycle has to say why ------------------------------------
#
# Note the polarity of _fetch_parameter_values throughout these tests: it
# answers None when the values were refreshed and the REASON when they were
# not, so `is None` is the success case.


def _pollable_module():
    return {
        (0, 1): {
            "Index": 0,
            "Type": 1,
            "Name": "Heat pump",
            "parameters": {"P1": {"ParameterID": "P1"}},
        }
    }


def _api_with_one_pollable_device():
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": _pollable_module()}
    api.get_statistics = lambda *_args, **_kwargs: None
    api._fetch_circuit_times = lambda *_args, **_kwargs: None
    return api


def test_a_failed_cycle_says_why_it_failed():
    """The reason used to stop at a warning and go no further.

    Home Assistant shows the raised message and nothing else, so "all API
    parameter fetches failed this cycle; see the warnings above" asked the
    user to correlate two log lines by timestamp - for something the code
    was holding in a variable at the time.
    """
    api = _api_with_one_pollable_device()

    def make_api_call(url, **_kwargs):
        raise exceptions.WemPortalError("Read timed out. (read timeout=12)")

    api.make_api_call = make_api_call

    with pytest.raises(exceptions.WemPortalError) as excinfo:
        api.get_data(enabled_devices=["1234"])

    message = str(excinfo.value)
    assert "Read timed out" in message, "the cycle failed without saying why"
    assert "…34" in message, "which device failed is not named"
    # The message ends with "open an issue at <tracker>", so it is written to
    # be pasted somewhere public - and a device id belongs to one installation.
    assert "1234" not in message, "the full device id went into a shareable message"


def test_every_failing_device_is_named_not_just_the_first():
    """A cap would read as "that was all of it" to whoever reads the log."""
    api = _api_with_one_pollable_device()
    api.data["5678"] = {}
    api.modules["5678"] = _pollable_module()

    def make_api_call(url, data=None, **_kwargs):
        raise exceptions.WemPortalError(f"device {data['DeviceID']} is unhappy")

    api.make_api_call = make_api_call

    with pytest.raises(exceptions.WemPortalError) as excinfo:
        api.get_data(enabled_devices=["1234", "5678"])

    message = str(excinfo.value)
    assert "1234" in message, message
    assert "5678" in message, message


def test_one_device_that_worked_still_keeps_the_cycle_green():
    """Unchanged by the new message, and the reason the check is
    `failures and not successes` rather than `failures`."""
    api = _api_with_one_pollable_device()
    api.data["5678"] = {}
    api.modules["5678"] = _pollable_module()

    # One answer that satisfies the status read, the refresh and the value
    # read alike - which of the three is being served does not matter here.
    healthy = {
        "ConnectionStatus": 0,
        "Errors": [],
        "Status": 0,
        "JobID": 1,
        "Modules": [{"ModuleIndex": 0, "ModuleType": 1, "Values": []}],
    }

    def make_api_call(url, data=None, **_kwargs):
        if data.get("DeviceID") == 5678:
            raise exceptions.WemPortalError("this one is unhappy")
        return FakeResponse(healthy)

    api.make_api_call = make_api_call

    api.get_data(enabled_devices=["1234", "5678"])


def _switch(value):
    """A switch entity built from one reading, without Home Assistant."""
    import types

    from custom_components.wemportal.switch import WemPortalSwitch

    coordinator = types.SimpleNamespace(
        data={"1234": {}},
        api=_api(),
        last_update_success=True,
        async_add_listener=lambda *_args, **_kwargs: None,
    )
    entry = types.SimpleNamespace(entry_id="e1")
    return WemPortalSwitch(
        coordinator,
        entry,
        "1234",
        "Pump",
        {
            "value": value,
            "unit": None,
            "friendlyName": "Pump",
            "ParameterID": "P1",
            "ModuleIndex": 0,
            "ModuleType": 1,
        },
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.0, True), ("Ein", True), (0.0, False), ("Aus", False), (None, None)],
)
def test_missing_switch_reading_is_unknown_not_off(value, expected):
    """`None in WEM_SWITCH_ON_VALUES` is False, so a missing reading looked
    exactly like a real switch-off to any automation watching it."""
    assert _switch(value).is_on is expected


@pytest.mark.parametrize(
    ("value", "expected"), [(1.0, True), (0.0, False), (None, None)]
)
def test_a_missing_reading_is_unknown_on_update_too(value, expected, monkeypatch):
    """The same distinction on the UPDATE path, which is where it matters.

    The constructor runs once, with whatever the first cycle happened to
    deliver; every later reading arrives through _handle_coordinator_update.
    Guarding only the former left a dropped reading looking like a real
    switch-off from the second cycle onwards - and `except KeyError` does
    not help, because the key is there, it just carries no value.
    """
    switch = _switch(1.0)
    monkeypatch.setattr(
        type(switch), "async_write_ha_state", lambda self: None, raising=False
    )
    switch.coordinator.data = {"1234": {"Pump": {"value": value}}}

    switch._handle_coordinator_update()

    assert switch.is_on is expected


def _status(value):
    return {"1234": {"1234-ConnectionStatus": {"value": value}}}


@pytest.mark.parametrize("state", ["offline", "wrong_secret"])
def test_a_dead_device_is_not_reachable(state):
    """`last_update_success` covers the CYCLE, not the device: with several
    devices, one offline for days still counted as available and kept
    presenting its last reading as current."""
    from custom_components.wemportal.utils import device_is_reachable

    assert device_is_reachable(_status(state), "1234") is False


@pytest.mark.parametrize("state", ["online", "busy", "unknown"])
def test_a_transient_state_stays_reachable(state):
    """Only definitively-dead states count. `busy` is momentary and
    `unknown` just means the status could not be read - treating either as
    unavailable would make entities flicker on a healthy system."""
    from custom_components.wemportal.utils import device_is_reachable

    assert device_is_reachable(_status(state), "1234") is True


def test_a_device_without_a_status_stays_reachable():
    """The scraper's pseudo device never gets a ConnectionStatus. Being
    strict here would mark every scraped sensor unavailable forever and take
    out `web` mode entirely."""
    from custom_components.wemportal.utils import device_is_reachable

    assert device_is_reachable({"0000": {"some-sensor": {"value": 1}}}, "0000") is True
    assert device_is_reachable({}, "0000") is True
    assert device_is_reachable(None, "0000") is True


def _scraped(*keys):
    return {key: {"value": 1, "unit": "°C", "platform": "sensor"} for key in keys}


def test_a_relabelled_scraper_row_is_reported(caplog):
    """Scraped sensors are keyed by their portal labels - there is no stable
    id to use instead, since the row's entityvalue embeds the current VALUE
    and changes with every reading. A relabelled row therefore lands under a
    different key. Nothing can prevent that, but it must not happen
    silently."""
    import logging

    api = _api()
    api._merge_webscraping_data("0000", _scraped("pump-flow", "pump-return"))

    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-flow", "pump-return-temp"))

    assert "no longer appear" in caplog.text
    assert "pump-return" in caplog.text
    assert "pump-return-temp" in caplog.text


def test_the_relabel_warning_names_the_language_setting(caplog):
    """The likeliest cause by far, and the only one the reader can undo.

    Reported as a portal rename, a language switch someone made themselves in
    the portal's account settings reads like a fault in the integration - it
    cost an evening of looking for one.
    """
    import logging

    api = _api()
    api._merge_webscraping_data("0000", _scraped("pump-flow"))

    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-vorlauf"))

    assert "language" in caplog.text


def test_the_relabel_warning_does_not_promise_entities_that_appear_now(caplog):
    """Entities are created once, during setup. The message claimed the new
    labels "become NEW entities", so the reader went looking for entities that
    cannot exist yet - what actually happens is that the existing sensors lose
    their row and go unknown until a restart builds the new ones."""
    import logging

    api = _api()
    api._merge_webscraping_data("0000", _scraped("pump-flow"))

    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-vorlauf"))

    assert "unknown" in caplog.text
    assert "restart" in caplog.text


def test_a_stable_scrape_says_nothing(caplog):
    """No warning on an unchanged cycle, nor on the very first one - there is
    nothing to compare a first scrape against."""
    import logging

    api = _api()
    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-flow"))
        api._merge_webscraping_data("0000", _scraped("pump-flow"))

    assert "no longer appear" not in caplog.text


def test_a_purely_added_row_is_not_a_relabel(caplog):
    """A genuinely new parameter is not a relabel, and saying so would train
    the user to ignore the message."""
    import logging

    api = _api()
    api._merge_webscraping_data("0000", _scraped("pump-flow"))

    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-flow", "pump-new"))

    assert "no longer appear" not in caplog.text


def test_the_service_value_field_does_not_impose_a_percent_range():
    """Expert parameters are not all percentages - temperatures, times and
    curves are among them, and the data model knows half steps
    (NUMBER_STEP_HALF), which a step of 1 silently blocked. The real check is
    on write, against the option list the device itself offers.

    A text field rather than a number one, because an option that sits beside
    the scale is named by its word ("Aus") and a number selector cannot
    express that. It settles the original question by construction too: a
    text field states no minimum, no maximum and no step at all.
    """
    from pathlib import Path

    import yaml

    p = Path(__file__).resolve().parent.parent / "custom_components" / "wemportal"
    spec = yaml.safe_load((p / "services.yaml").read_text(encoding="utf-8"))
    selector = spec["set_expert_parameter"]["fields"]["value"]["selector"]

    assert "number" not in selector, (
        "a number field cannot express the options that are not numbers, and "
        "carries a range and a step this parameter may not have"
    )
    assert "text" in selector


# --- stored options are held to their floors --------------------------


def test_a_stored_interval_below_the_floor_is_raised(caplog):
    """The floors live in the options-flow schema, which only validates what
    the user types NOW.

    An interval saved by an older release - back when the API floor was ten
    seconds - was read back verbatim on every start, so an installation
    configured once at one second kept polling at one second forever. The UI
    does not reveal it either: the form shows the stored value as if it were
    legal.
    """
    import logging

    from custom_components.wemportal.const import (
        CONF_SCAN_INTERVAL_API,
        MIN_SCAN_INTERVAL_API_SECONDS,
    )
    from custom_components.wemportal.utils import clamped_scan_interval

    with caplog.at_level(logging.WARNING):
        value = clamped_scan_interval(
            {CONF_SCAN_INTERVAL_API: 1},
            CONF_SCAN_INTERVAL_API,
            300,
            MIN_SCAN_INTERVAL_API_SECONDS,
        )

    assert value == MIN_SCAN_INTERVAL_API_SECONDS
    assert "minimum" in caplog.text, "silently corrected values are hard to diagnose"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(900, 900), (60, 60), (None, 300), ("", 300), ("abc", 300), ({}, 300)],
)
def test_a_usable_stored_interval_survives(stored, expected):
    """Only values below the floor - and unusable ones - are touched."""
    from custom_components.wemportal.utils import clamped_scan_interval

    options = {} if stored is None else {"k": stored}
    assert clamped_scan_interval(options, "k", 300, 60) == expected


def test_the_api_applies_the_floor_to_a_stored_interval():
    """End of the same path: the value the api actually polls with."""
    from datetime import timedelta

    from homeassistant.const import CONF_SCAN_INTERVAL

    from custom_components.wemportal.const import CONF_MODE, CONF_SCAN_INTERVAL_API

    api = WemPortalApi(
        "user@example.org",
        "secret",
        config={CONF_MODE: "both", CONF_SCAN_INTERVAL: 1, CONF_SCAN_INTERVAL_API: 1},
    )

    assert api.scan_interval == timedelta(seconds=60)
    assert api.scan_interval_api == timedelta(seconds=60)
    assert api.update_interval == timedelta(seconds=60)


def test_yaml_configuration_is_declared_unsupported(caplog):
    """hassfest warns on every run when async_setup exists without a
    CONFIG_SCHEMA, and a stray `wemportal:` block would otherwise be
    accepted in silence instead of being called out.

    Home Assistant's helper does not raise on such a block - it reports it
    and carries on - so this pins the report, not an exception.
    """
    import logging

    from custom_components.wemportal import CONFIG_SCHEMA
    from custom_components.wemportal.const import DOMAIN

    # An empty configuration.yaml is fine - the integration is UI-only.
    CONFIG_SCHEMA({})

    with caplog.at_level(logging.ERROR):
        CONFIG_SCHEMA({DOMAIN: {"username": "user@example.org"}})

    assert "does not support YAML setup" in caplog.text


def test_a_returning_device_becomes_eligible_for_parameter_discovery():
    """get_parameters() gates on the UNPREFIXED ConnectionStatus, which used
    to be written only by get_devices() - once per session.

    A device that was offline when Home Assistant started therefore never had
    its parameters discovered, not even after it came back, because the gate
    still saw the status from the moment of startup. Only a reload fixed it.
    """
    api = _api()
    api.data = {"1234": {"ConnectionStatus": 50}}
    api.modules = {"1234": {}}
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"ConnectionStatus": 0, "Errors": [], "GroupTypeDescriptions": []}
    )

    assert api._fetch_device_status("1234") is True
    assert api.data["1234"]["ConnectionStatus"] == 0, "the discovery gate stayed stale"


# --- what counts as a successful API answer ---------------------------


def test_an_empty_value_read_is_not_a_refreshed_device():
    """HTTP 200 with no modules in it is not data.

    The mapper simply finds nothing to walk, so this returned True and the
    cycle was reported as successful - leaving every entity presenting its
    previous reading as current.
    """
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {"P1": {}}}}}
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse({"Modules": []})

    assert api._fetch_parameter_values("1234") is not None


def test_a_device_without_modules_is_not_turned_into_a_failure():
    """The guard keys on what was REQUESTED, so a device that genuinely has
    no modules must not start failing every cycle."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse({"Modules": []})

    assert api._fetch_parameter_values("1234") is None


class _BodyResponse(FakeResponse):
    """A response whose body is present but is not JSON - an HTML error or
    maintenance page served with HTTP 200."""

    def __init__(self, content):
        super().__init__({})
        self.content = content

    def json(self):
        raise ValueError("not json")


def test_a_write_answered_with_a_page_is_not_a_completed_write():
    """Reported as success, this told the user their heating parameter had
    been changed when it had not."""
    api = _api()
    api.make_api_call = lambda *_args, **_kwargs: _BodyResponse(
        b"<html>Service unavailable</html>"
    )

    with pytest.raises(exceptions.ParameterChangeError):
        api.change_value("1234", "P1", 0, 1, 21.0)


def test_an_empty_write_response_is_no_longer_taken_for_success():
    """This used to pass, on the assumption that a bare acknowledgement is
    how the portal confirms a write.

    The assumption was never verified, and the real response settles it: a
    successful write answers with a body carrying Status 0. An empty one is
    therefore not a confirmation of anything.
    """
    api = _api()
    api.make_api_call = lambda *_args, **_kwargs: _BodyResponse(b"")

    with pytest.raises(exceptions.ParameterChangeError):
        api.change_value("1234", "P1", 0, 1, 21.0)


# --- the scrape backoff is not skipped after a recovery ---------------


def test_the_scrape_backoff_survives_a_fresh_api_instance():
    """`last_scraping_update is None` short-circuited past the backoff check.

    That is true on every fresh WemPortalApi - and the coordinator builds one
    whenever it recovers from repeated errors, so the scrape backoff was
    skipped precisely after the failures that set it.
    """
    from custom_components.wemportal.const import CONF_MODE

    api = _api(config={CONF_MODE: "both"})
    api.spider_wait_interval = 3
    api.last_scraping_update = None
    api.valid_login = True
    api._devices_fetched_this_session = True
    api.modules = {}
    scrapes = []
    api.fetch_webscraping_data = lambda: scrapes.append(True) or {}
    api.get_data = lambda *_args, **_kwargs: None

    api._fetch_data(enabled_devices=None)

    assert scrapes == [], "the scraper ran while its backoff was active"
    assert api.spider_wait_interval == 2, "the backoff must count down instead"


# --- an unloaded entry must not keep writing to the portal ------------


def _write_entity(api, monkeypatch):
    """An expert entity whose executor runs inline and whose portal client
    records that it was constructed at all."""
    from custom_components.wemportal import expert_writer

    entity = _expert_entity(api)
    built = []

    class _Client:
        def __init__(self, *_args, **_kwargs):
            built.append(True)

        def write_parameter(self, *_args, **_kwargs):
            # The real type, not a stand-in with the fields this test happens
            # to need: a stand-in silently goes out of date when the state
            # grows one, and the entity reads whatever it grew.
            return expert_writer.ExpertParameterState(21.0, [], {})

    monkeypatch.setattr(expert_writer, "WemPortalExpertClient", _Client)

    async def run_inline(function, *args):
        return function(*args)

    entity.hass.async_add_executor_job = run_inline
    entity.hass.async_create_task = lambda coro: coro.close()
    entity.async_write_ha_state = lambda: None
    return entity, built


def _refusing_write_entity(api, monkeypatch, error):
    """The same entity, with a portal that refuses the write."""
    from custom_components.wemportal import expert_writer

    entity, built = _write_entity(api, monkeypatch)

    class _RefusingClient:
        def __init__(self, *_args, **_kwargs):
            built.append(True)

        def write_parameter(self, *_args, **_kwargs):
            raise error

    monkeypatch.setattr(expert_writer, "WemPortalExpertClient", _RefusingClient)
    return entity


def test_a_failed_write_reaches_whoever_asked_for_it(monkeypatch):
    """A write that the portal refused must fail the service call.

    The write ran as a background task and the call returned at once, so the
    outcome only ever reached the log and a notification. An automation was
    told its write succeeded whatever happened, and could carry on as if the
    heating had been set. Home Assistant's own rule for entity methods is
    that a communication failure raises HomeAssistantError.
    """

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.wemportal import exceptions as wem_exceptions

    api = _api()
    entity = _refusing_write_entity(
        api, monkeypatch, wem_exceptions.ParameterWriteError("the portal refused it")
    )

    with pytest.raises(HomeAssistantError):
        _run(entity.async_set_native_value, 21.0)


def test_a_refusal_corrects_the_range_that_caused_it(monkeypatch):
    """Otherwise the entity keeps offering a range the portal has moved past.

    A heating parameter's limits can depend on other settings, so a range read
    weeks ago need not still hold - and the auto-poll that would notice is off
    by default.

    Calls the entity method directly, which is NOT the full path: Home
    Assistant validates a value against the published min/max before asking
    the entity, so a value outside the published range never reaches this
    code. What is covered here is the case that does reach it - a value the
    published range still admits and the portal refuses.
    """

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.wemportal import exceptions as wem_exceptions

    entity = _refusing_write_entity(
        _api(),
        monkeypatch,
        wem_exceptions.ParameterWriteError(
            "value not allowed", state=_read_state(5.0, [1.0, 1.5, 2.0])
        ),
    )
    entity._apply_state(_read_state(50.0, [10.0, 15.0, 20.0]))

    with pytest.raises(HomeAssistantError):
        _run(entity.async_set_native_value, 50.0)

    assert entity.native_min_value == 1.0
    assert entity.native_max_value == 2.0
    assert entity.native_value == 5.0


def test_a_write_that_fails_with_a_plain_error_still_reaches_the_caller(monkeypatch):
    """Not every failure arrives as a HomeAssistantError.

    A socket timeout, a parse error, anything the portal client did not
    anticipate - Home Assistant only surfaces HomeAssistantError to the
    caller, so those have to be wrapped rather than passed on as they are.

    Its own test because the one above cannot cover it: ParameterWriteError
    IS a HomeAssistantError, via WemPortalError, so it exercises only the
    pass-through branch. A mutation turning the wrapping branch into a bare
    `return` stayed green until this existed.
    """

    from homeassistant.exceptions import HomeAssistantError

    api = _api()
    entity = _refusing_write_entity(
        api, monkeypatch, TimeoutError("the portal did not answer")
    )

    with pytest.raises(HomeAssistantError):
        _run(entity.async_set_native_value, 21.0)


def test_a_successful_write_leaves_the_verified_value_behind(monkeypatch):
    """The counter-test: awaiting the write must not lose what it returns."""
    import asyncio

    api = _api()
    entity, built = _write_entity(api, monkeypatch)

    asyncio.run(entity.async_set_native_value(21.0))

    assert built == [True]
    assert entity.native_value == 21.0
    assert entity._write_in_progress is False


def test_a_write_stopped_by_a_teardown_reaches_the_caller_too(monkeypatch):
    """Reversed today, and the earlier reasoning was wrong.

    It said nobody was waiting on the outcome. Somebody is: the write is
    awaited now, so the service call or automation that asked for it is still
    holding on, and returning quietly told it the heating had been set when
    nothing reached the portal. That the configuration went away is a reason
    for the write not to happen, not a reason to report that it did.
    """

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.wemportal import exceptions as wem_exceptions

    api = _api()
    entity = _refusing_write_entity(
        api, monkeypatch, wem_exceptions.ExpertOperationAborted("entry is unloading")
    )

    with pytest.raises(HomeAssistantError):
        _run(entity.async_set_native_value, 21.0)

    assert entity._write_in_progress is False


def test_a_removed_entity_does_not_open_a_portal_session(monkeypatch):
    """Cancelling the task cannot stop the write.

    The portal call runs in an executor thread, and cancelling cancels the
    AWAIT, not the thread - so a write kept going against the portal with the
    credentials of an entry that was being torn down. Python cannot kill a
    thread; what it can do is refuse to START, which is exactly the case that
    matters (teardown races the thread pool).
    """

    from homeassistant.exceptions import HomeAssistantError

    api = _api()
    entity, built = _write_entity(api, monkeypatch)
    entity._removed = True

    # Raising is the point of the abort, not a side effect of it: whoever is
    # waiting on the call must not be told the heating was set. What this
    # test is about is the line below it - no session was opened at all.
    with pytest.raises(HomeAssistantError):
        _run(entity._async_write, 21.0)

    assert built == [], "a write opened a portal session after removal"
    assert entity._write_in_progress is False


def test_a_normal_write_still_reaches_the_portal(monkeypatch):
    """The guard must not disable writing altogether."""
    import asyncio

    api = _api()
    entity, built = _write_entity(api, monkeypatch)

    asyncio.run(entity._async_write(21.0))

    assert built == [True]
    assert entity.native_value == 21.0


def test_a_rejected_refresh_is_not_read_as_a_fresh_measurement():
    """The portal answers a REFUSED refresh with HTTP 200 and a non-zero
    Status, exactly like the login does.

    Only JobID was read, so a rejection went unnoticed - and without a JobID
    the read falls back to the most recent job, which is the PREVIOUS
    measurement. Its values were then booked as a fresh reading.
    """
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {"P1": {}}}}}
    urls = []

    def make_api_call(url, **_kwargs):
        urls.append(url)
        return FakeResponse({"Status": 3, "Message": "refresh refused"})

    api.make_api_call = make_api_call

    assert api._fetch_parameter_values("1234") is not None
    assert len(urls) == 1, "the read must not happen after a refused refresh"


def test_a_refresh_without_a_status_field_still_works():
    """Not every response carries Status; absence must not fail the cycle."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {"P1": {}}}}}
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"Modules": [{"ModuleIndex": 0, "ModuleType": 1, "Values": []}]}
    )

    assert api._fetch_parameter_values("1234") is None


def test_a_fresh_api_without_carried_state_starts_clean():
    """A normal first start must not inherit anything."""
    api = _api()

    assert api.scraper_backoff == (0, 0, None)


class _RecordingSession:
    """Fails the test if anything is actually sent."""

    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append(url)
        raise AssertionError("the write reached the portal")

    def close(self):
        pass


def test_the_write_is_stopped_directly_before_the_portal_is_changed():
    """The gate that matters sits immediately before the writing request.

    An unload can land at any point, and everything up to that request is
    reads - the login alone is several requests and takes seconds. So the
    abort is made TRUE only once the form has been read, which no earlier
    check can catch: only the gate in front of the POST can.
    """
    from custom_components.wemportal import expert_writer

    unloaded = []

    def gate():
        if unloaded:
            raise expert_writer.ExpertOperationAborted("unloaded")

    client = expert_writer.WemPortalExpertClient(
        "user@example.org",
        "secret",
        abort_check=gate,
    )
    session = _RecordingSession()
    client.session = session
    client._login = lambda: None
    client.close = lambda: None

    def fetch_form(*_args, **_kwargs):
        # The unload happens WHILE the form is being read, i.e. after every
        # gate except the last one.
        unloaded.append(True)
        return expert_writer.ExpertParameterState(20.0, [20.0, 21.0], {})

    client._fetch_form = fetch_form

    with pytest.raises(expert_writer.ExpertOperationAborted):
        client.write_parameter("A" * 36, 21.0)

    assert session.posts == [], "the parameter was written after the unload"


def test_a_batch_read_stops_between_parameters_when_the_entry_goes_away():
    """The auto-poll reads several ids on one session, so an unload halfway
    through kept navigating the portal for the rest of the batch - with the
    credentials of an entry being torn down. Cancelling the poll cancels the
    await, not this thread; only looking before the next request can stop it.
    """
    from custom_components.wemportal import expert_writer

    read = []
    unloaded = []

    def gate():
        if unloaded:
            raise expert_writer.ExpertOperationAborted("unloaded")

    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", abort_check=gate
    )
    client._login = lambda: None
    client.close = lambda: None

    def fetch_form(entityvalue, *_args, **_kwargs):
        read.append(entityvalue)
        unloaded.append(True)
        return expert_writer.ExpertParameterState(20.0, [20.0], {})

    client._fetch_form = fetch_form

    with pytest.raises(expert_writer.ExpertOperationAborted):
        client.read_many(["a" * 36, "b" * 36, "c" * 36])

    assert len(read) == 1, f"the batch kept reading after the unload: {len(read)}"


def test_the_auto_poll_hands_its_read_a_stop_gate(monkeypatch):
    """The gate above only helps if one is actually handed over.

    The write path passed one and the read path did not, so the batch read had
    nothing to check - the entity write could be stopped mid-teardown and the
    scheduled read could not.
    """
    import types

    from custom_components.wemportal import expert_controller, expert_writer

    class _Client:
        def __init__(self, *_args, **kwargs):
            self._abort = kwargs.get("abort_check")

        def read_many(self, _ids):
            if self._abort is not None:
                self._abort()
            return {}

    monkeypatch.setattr(expert_writer, "WemPortalExpertClient", _Client)

    def gate():
        raise exceptions.ExpertOperationAborted("unloaded")

    entry = types.SimpleNamespace(data={}, options={})
    api = types.SimpleNamespace(
        check_expert_cooldown=lambda: None,
        activate_expert_cooldown=lambda *_args: None,
        expert_cookies={},
    )

    with pytest.raises(exceptions.ExpertOperationAborted):
        expert_controller.read_expert_values(entry, api, ["a" * 36], gate)


def test_a_batch_read_without_an_abort_reads_everything():
    """The gate must not cost the ordinary case - without this the test above
    would pass on a client that reads nothing at all."""
    from custom_components.wemportal import expert_writer

    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")
    client._login = lambda: None
    client.close = lambda: None
    client._fetch_form = lambda *_args, **_kwargs: expert_writer.ExpertParameterState(
        20.0, [20.0], {}
    )

    result = client.read_many(["a" * 36, "b" * 36])

    assert len(result) == 2


def test_a_write_without_an_abort_still_goes_through():
    """The gate must not block ordinary writes - without it the test above
    would pass on a client that never writes anything at all."""
    from custom_components.wemportal import expert_writer

    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")
    session = _RecordingSession()
    client.session = session
    client._login = lambda: None
    client.close = lambda: None
    client._fetch_form = lambda *_args, **_kwargs: expert_writer.ExpertParameterState(
        20.0, [20.0, 21.0], {}
    )

    with pytest.raises(AssertionError, match="reached the portal"):
        client.write_parameter("A" * 36, 21.0)

    assert session.posts, "the write never got as far as the portal"


def test_an_unreadable_refresh_answer_does_not_serve_the_previous_job():
    """Unreadable is not the same as "no status given".

    Falling through left the read without a JobID, and without one the server
    returns the most recent job - the PREVIOUS measurement - whose values
    were then booked as fresh.
    """
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {"P1": {}}}}}
    urls = []

    def make_api_call(url, **_kwargs):
        urls.append(url)
        return _BodyResponse(b"<html>gateway timeout</html>")

    api.make_api_call = make_api_call

    assert api._fetch_parameter_values("1234") is not None
    assert len(urls) == 1, "the read ran anyway"


@pytest.mark.parametrize(
    "error",
    [
        exceptions.PortalMaintenanceError("down until 18:00"),
        exceptions.AuthError("wrong password"),
    ],
)
def test_every_failed_scrape_earns_a_backoff(error, monkeypatch):
    """In `both` mode these errors are swallowed so the API can still poll.

    Without a backoff the scraper therefore walked into the same announced
    outage - or the same rejected login - on every single API cycle, which
    for a wrong password is the request the portal is least willing to see
    repeated.
    """
    api = _api()
    api.webscraping_cookie = {"x": "y"}

    class _Scraper:
        cookie = {}

        def scrape(self):
            raise error

        def close(self):
            pass

    api._scraper = _Scraper()

    with pytest.raises(type(error)):
        api.fetch_webscraping_data()

    assert api.spider_retry_count == 1
    assert api.spider_wait_interval == 1


def _scraped_api(*keys):
    """An api holding readings from a scrape that worked."""
    api = _api()
    api.scraper_device_id = "0000"
    api._merge_webscraping_data(
        "0000",
        {key: {"value": 1.0, "unit": "°C", "platform": "sensor"} for key in keys},
    )
    return api


def test_readings_from_a_scrape_that_stopped_working_stop_being_current():
    """A failed scrape produces no answer at all, so nothing in it can be read
    as "that reading ended" - only repeated failure says the values are no
    longer worth presenting. Until then a stale number was published as the
    current one, indefinitely."""
    api = _scraped_api("pump-flow", "pump-return")

    for _ in range(wemportalapi.SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE):
        api._register_scrape_failure()

    assert api.data["0000"]["pump-flow"]["value"] is None
    assert api.data["0000"]["pump-return"]["value"] is None
    # Identity survives: a dropped unit would tell Home Assistant the sensor
    # changed kind.
    assert api.data["0000"]["pump-flow"]["unit"] == "°C"


def _two_device_api(failing_device):
    """Two devices; `failing_device` never answers, None means both do."""
    api = _api()
    api.data = {
        "1234": {"flow": {"value": 21.0, "unit": "°C"}},
        "5678": {"flow": {"value": 42.0, "unit": "°C"}},
    }
    api.modules = {"1234": {}, "5678": {}}
    api._fetch_device_status = lambda device_id: True
    api._fetch_circuit_times = lambda device_id: None
    api.get_statistics = lambda *_args, **_kwargs: None
    api._fetch_parameter_values = lambda device_id: (
        "no answer" if device_id == failing_device else None
    )
    return api


def test_a_device_that_stops_answering_stops_showing_its_last_values():
    """One device failing does not fail the cycle - another one answered, and
    failing would take every device's entities down.

    That is right, and it left the silent device publishing whatever it last
    returned, for as long as the session lasted. mapper._clear_unanswered
    cannot help: it needs a REPLY that left a parameter out, and there is no
    reply here at all.
    """
    api = _two_device_api("5678")
    api.get_data(enabled_devices=["1234", "5678"])
    # Answered once, then went silent longer than the window allows.
    api._last_device_read["5678"] = (
        time.monotonic() - wemportalapi.DEVICE_VALUES_STALE_AFTER_SECONDS - 1
    )

    api.get_data(enabled_devices=["1234", "5678"])

    assert api.data["5678"]["flow"]["value"] is None, (
        "a device that has not answered for half an hour still showed its old "
        "reading as current"
    )
    assert api.data["5678"]["flow"]["unit"] == "°C", (
        "dropping the unit tells Home Assistant the sensor changed kind"
    )
    assert api.data["1234"]["flow"]["value"] == 21.0, (
        "the working device lost its readings too"
    )


def test_a_brief_gap_does_not_throw_a_device_away():
    """The counter-test. A single missed read is normal - the portal refuses
    requests routinely - and clearing on the first one would put a gap in the
    history for every hiccup.

    The device has to ANSWER first. Written without that it proved nothing:
    a device that never answered is skipped by the "nothing on display yet"
    guard before the age is even looked at, so removing the age check
    entirely left this green. The mutation said so.
    """
    api = _two_device_api(None)
    api.get_data(enabled_devices=["1234", "5678"])
    assert api.data["5678"]["flow"]["value"] == 42.0, "the setup did not read"

    # Now it goes quiet - but only just.
    api._fetch_parameter_values = lambda device_id: (
        "no answer" if device_id == "5678" else None
    )
    api.get_data(enabled_devices=["1234", "5678"])

    assert api.data["5678"]["flow"]["value"] == 42.0


def _two_device_api_with_status(failing_device):
    """As above, plus the three diagnostic rows a status read writes."""
    api = _two_device_api(failing_device)
    for device_id in ("1234", "5678"):
        api.data[device_id][f"{device_id}-{wemportalapi.DEVICE_STATUS_CONNECTION}"] = {
            "value": "online"
        }
        api.data[device_id][f"{device_id}-{wemportalapi.DEVICE_STATUS_HAS_ERRORS}"] = {
            "value": "No"
        }
    return api


def test_the_rows_that_explain_the_silence_are_not_blanked_with_it():
    """The diagnostic rows must survive the clear-out that follows them.

    Their whole purpose is to stay available and say WHY a device's readings
    went unknown - that is what the entities promise. They are also the
    freshest thing about that device: the status read succeeded in this very
    cycle, which is the only reason the parameter read was attempted at all.
    Blanking them said "no idea" about the one thing that was known.
    """
    api = _two_device_api_with_status("5678")
    api.get_data(enabled_devices=["1234", "5678"])
    api._last_device_read["5678"] = (
        time.monotonic() - wemportalapi.DEVICE_VALUES_STALE_AFTER_SECONDS - 1
    )

    api.get_data(enabled_devices=["1234", "5678"])

    connection = f"5678-{wemportalapi.DEVICE_STATUS_CONNECTION}"
    assert api.data["5678"]["flow"]["value"] is None, "the stale reading was kept"
    assert api.data["5678"][connection]["value"] == "online", (
        "the status read this cycle was thrown away with the stale readings"
    )


def test_a_fresh_scrape_is_not_cleared_with_a_silent_api_device():
    """In `both` mode the two sources share one device's dict.

    resolve_scraper_device_id deliberately files scraped sensors under a real
    API-discovered device, so they share its history. The consequence is that
    "this device has not answered" is only ever true of the API half - the
    scrape has its own schedule and its own failures, and can be minutes old
    while the API side has been silent for hours.

    Clearing the lot took the fresh scrape with it, and the cycle reported
    success while doing so, because a skipped device counts as neither a
    success nor a failure. Nothing said the web sensors had gone unknown.

    Scraped rows are not left unwatched by this: _forget_scraped_values ages
    them on the scrape's own terms, after three failed attempts in a row.
    """
    api = _api()
    api.data = {
        "1234": {
            "Heat pump-T1": {"value": 21.0, "unit": "°C"},
            "heating_circuit_flow": {"value": 42.0, "unit": "°C"},
        }
    }
    api._previous_scraper_keys = {"heating_circuit_flow"}
    api._last_device_read["1234"] = (
        time.monotonic() - wemportalapi.DEVICE_VALUES_STALE_AFTER_SECONDS - 1
    )

    api._forget_stale_device_values("1234")

    assert api.data["1234"]["Heat pump-T1"]["value"] is None, (
        "the API reading that really was stale was kept"
    )
    assert api.data["1234"]["heating_circuit_flow"]["value"] == 42.0, (
        "a scrape from minutes ago was cleared because the API half of the "
        "same device had gone quiet"
    )


def test_the_third_scrape_failure_before_any_success_is_survivable():
    """_previous_scraper_keys is None until a scrape has succeeded.

    The third failure calls _forget_scraped_values, which iterates it - so
    three failures before the first good scrape raised TypeError instead, and
    that replaced the actual reason (maintenance, credentials, the network)
    on its way out. Reachable in `both` mode, where the API has already put
    the device into self.data so the early return does not fire.
    """
    api = _api()
    api.data = {"1234": {"flow": {"value": 21.0, "unit": "°C"}}}
    api.scraper_device_id = "1234"
    assert api._previous_scraper_keys is None, "the setup does not reproduce it"

    for _attempt in range(wemportalapi.SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE):
        api._register_scrape_failure()

    assert api.spider_retry_count == (
        wemportalapi.SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE
    )


def test_a_shared_row_ages_once_the_scrape_has_given_up_too():
    """The gap the scraped-row exemption left: BOTH sources dead.

    _forget_scraped_values fires on exactly the third scrape failure, not from
    then on. So a shared row is cleared once, the API refills it through the
    merge, and every later scrape failure leaves it alone - while the
    exemption added for a working scrape goes on protecting it from the API's
    own ageing. An old reading then sits there as current with nothing behind
    it at all.

    The exemption has to mean "the scrape is keeping this fresh", and
    _previous_scraper_keys cannot say that: it is only refreshed by a
    SUCCESSFUL scrape, so after a failure it still names every key the last
    good one wrote. spider_retry_count is what knows.
    """
    api = _api()
    api.data = {"1234": {"shared_reading": {"value": 21.0, "unit": "°C"}}}
    api._previous_scraper_keys = {"shared_reading"}
    # Past the point where _forget_scraped_values stopped acting: it fires on
    # the third failure only, so from the fourth on nobody ages this row.
    api.spider_retry_count = wemportalapi.SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE + 1
    api._last_device_read["1234"] = (
        time.monotonic() - wemportalapi.DEVICE_VALUES_STALE_AFTER_SECONDS - 1
    )

    api._forget_stale_device_values("1234")

    assert api.data["1234"]["shared_reading"]["value"] is None, (
        "both sources had stopped answering and the reading was still shown as current"
    )


def test_a_device_that_is_busy_forever_still_stops_showing_old_values():
    """A device that never says "online" never reaches the freshness check.

    The poll skips it before the parameter read, and skipping is right - it
    was read fine, it is simply not answering. But the readings underneath go
    on being published as current, and `busy` is not one of the states that
    make a device unreachable, so its entities stay available too. A device
    stuck like that showed the same numbers indefinitely.
    """
    api = _two_device_api_with_status(None)
    api.get_data(enabled_devices=["1234", "5678"])
    assert api.data["5678"]["flow"]["value"] == 42.0, "the setup did not read"

    api._fetch_device_status = lambda device_id: device_id != "5678"
    api._last_device_read["5678"] = (
        time.monotonic() - wemportalapi.DEVICE_VALUES_STALE_AFTER_SECONDS - 1
    )

    api.get_data(enabled_devices=["1234", "5678"])

    assert api.data["5678"]["flow"]["value"] is None, (
        "a device that has been busy for half an hour still showed its old "
        "readings as current"
    )


def test_a_device_that_answers_again_starts_its_clock_over():
    """Without this the window would be measured from the first success ever,
    so a device answering fine could still be cleared once it had been
    running long enough."""
    api = _two_device_api("5678")
    api.get_data(enabled_devices=["1234", "5678"])
    api._last_device_read["5678"] = (
        time.monotonic() - wemportalapi.DEVICE_VALUES_STALE_AFTER_SECONDS - 1
    )

    # It answers this time.
    api._fetch_parameter_values = lambda device_id: None
    api.get_data(enabled_devices=["1234", "5678"])

    assert api.data["5678"]["flow"]["value"] == 42.0, "a working device was cleared"
    assert (
        time.monotonic() - api._last_device_read["5678"]
        < wemportalapi.DEVICE_VALUES_STALE_AFTER_SECONDS
    ), "the clock was not restarted by a successful read"


def test_one_failed_scrape_does_not_throw_the_readings_away():
    """The counter-test. Scrapes fail transiently all the time - that is what
    the backoff is for - and clearing on the first one would make every
    hiccup a gap in the history."""
    api = _scraped_api("pump-flow")

    api._register_scrape_failure()

    assert api.data["0000"]["pump-flow"]["value"] == 1.0


def test_a_successful_scrape_clears_the_backoff():
    """The counters must come back down, or one hiccup would slow the
    scraper for the rest of the session."""
    api = _api()
    api.spider_retry_count = 3
    api.spider_wait_interval = 3

    class _Scraper:
        cookie = {}

        def scrape(self):
            return [{"cookie": {}, "Heating-Outside": {"value": 11.0}}]

        def close(self):
            pass

    api._scraper = _Scraper()

    api.fetch_webscraping_data()

    assert api.spider_retry_count == 0
    assert api.spider_wait_interval == 0


# The real answer from the portal, captured once instead of derived:
# HTTP 200 {"JobID":762338890,"Status":0,"Message":null,"DetailMessages":null}
REAL_WRITE_SUCCESS = {
    "JobID": 762338890,
    "Status": 0,
    "Message": None,
    "DetailMessages": None,
}


def test_the_real_success_response_is_accepted():
    """Guarding against the rule that ALMOST got written.

    The only available reference documents `Message` as the failure field, so
    "a Message means it failed" looked reasonable - but the real success
    response carries Message: null. That rule would have failed every single
    legitimate write.
    """
    api = _api()
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(REAL_WRITE_SUCCESS)

    api.change_value("1234", "P1", 0, 1, 21.0)


@pytest.mark.parametrize(
    "payload",
    [
        {"Message": "write failed"},  # error shape, no Status
        {"Status": None},  # present but says nothing
        {"Status": 3, "Message": "rejected"},  # explicit rejection
        {"JobID": 1},  # a result, but not a verdict
        {"Status": False},  # Python says False == 0. The portal does not.
    ],
)
def test_anything_but_an_explicit_success_is_a_rejection(payload):
    """Success carries Status: 0, so nothing else may pass for one.

    Reporting a write as done when it was not is the worse error: it is a
    heating parameter, and the entity shows the requested value until the
    next poll quietly replaces it.
    """
    api = _api()
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(payload)

    with pytest.raises(exceptions.ParameterChangeError):
        api.change_value("1234", "P1", 0, 1, 21.0)


def test_the_rejection_message_carries_the_portal_reason():
    """DetailMessages/Message is what the portal says went wrong - dropping
    it leaves the user with a bare number."""
    api = _api()
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"Status": 3, "Message": "value out of range"}
    )

    with pytest.raises(exceptions.ParameterChangeError) as excinfo:
        api.change_value("1234", "P1", 0, 1, 21.0)

    assert "value out of range" in str(excinfo.value)


def test_a_rejected_write_puts_the_portal_answer_in_the_log(caplog):
    """The raw answer is what someone asks for when a write stops working -
    and debug logging is exactly what is not enabled at that moment.

    It also guards the check itself: accepting only Status 0 means the day
    the portal changes its answer, every write fails at once. The body in the
    log turns that from guesswork into one report.
    """
    import logging

    api = _api()
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"Status": 3, "Message": "value out of range"}
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(exceptions.ParameterChangeError),
    ):
        api.change_value("1234", "P1", 0, 1, 21.0)

    warnings = " ".join(
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )
    assert "value out of range" in warnings


def test_a_successful_write_records_the_answer_at_debug(caplog):
    """The success shape matters too: it is the reference the check is built
    on, so a change to it has to be visible."""
    import logging

    api = _api()
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(REAL_WRITE_SUCCESS)

    with caplog.at_level(logging.DEBUG):
        api.change_value("1234", "P1", 0, 1, 21.0)

    assert "Write response for P1" in caplog.text
    # And nothing about the write ends up at warning level.
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


# --- is the maintenance marker safe to check everywhere? --------------


MAINTENANCE_HTML = (
    "<html><body><div class='offlinecontent'>Wartungsarbeiten bis 18:00"
    "</div></body></html>"
)


def _gate_probe():
    """A scraper whose gate is driven directly, with a clean report set."""
    from custom_components.wemportal import utils
    from custom_components.wemportal.scraper import WemPortalScraper

    utils._MARKER_REPORTED.clear()
    return WemPortalScraper("user@example.org", "secret")


class _Page:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.url = "https://www.wemportal.com/Web/Default.aspx"


def test_a_marker_where_it_is_not_acted_on_is_reported(caplog):
    """The open question is whether the marker can appear on a HEALTHY page.

    Enabling the check everywhere on the assumption that it cannot would
    trade a known gap for an unknown false positive - the portal reported as
    down while it is serving fine. So the assumption is measured: this fires
    only if the marker turns up somewhere it is not acted on.
    """
    import logging

    scraper = _gate_probe()

    with caplog.at_level(logging.WARNING):
        scraper._check_response(_Page(MAINTENANCE_HTML), "module page")

    assert "maintenance marker appeared" in caplog.text
    assert "module page" in caplog.text


def test_the_report_does_not_turn_into_a_failure():
    """It is an observation, not a verdict: the request must carry on."""
    scraper = _gate_probe()

    scraper._check_response(_Page(MAINTENANCE_HTML), "module page")


def test_the_report_is_made_once_per_request_label(caplog):
    """If the marker IS on every page, an unbounded report would bury the
    log it is meant to inform."""
    import logging

    scraper = _gate_probe()

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            scraper._check_response(_Page(MAINTENANCE_HTML), "module page")

    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1, f"{len(warnings)} reports for one request site"


def test_where_the_check_is_enabled_it_still_raises():
    """The probe must not have replaced the actual detection."""
    scraper = _gate_probe()

    notice_page = _Page(MAINTENANCE_HTML)

    with pytest.raises(exceptions.PortalMaintenanceError):
        scraper._check_response(notice_page, "login page", check_maintenance=True)


def test_a_healthy_page_is_silent(caplog):
    """No marker, no noise - otherwise the signal would be worthless."""
    import logging

    scraper = _gate_probe()

    with caplog.at_level(logging.WARNING):
        scraper._check_response(_Page("<html><body>fine</body></html>"), "main page")

    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


# --- what a recovery may and may not touch ----------------------------
#
# reset_transport() decides, field by field, what survives the recovery the
# coordinator runs after repeated portal errors. That decision used to live
# only in its docstring, i.e. hand-maintained: a new transport field simply
# would not be reset, silently, and a new field of any other kind would be
# preserved by accident rather than by choice.
#
# So the classification is declared here and enforced against the real
# object. Adding a field to WemPortalApi now fails this test until someone
# says which side it belongs to.

# Dropped by a recovery: the HTTP state and everything that describes the
# session it belonged to.
TRANSPORT_FIELDS = frozenset(
    {
        "session",
        "_scraper",
        "valid_login",
        "api_version",
        "webscraping_cookie",
        "_devices_fetched_this_session",
    }
)

# Kept across a recovery. Two groups here are not merely "not transport",
# they are actively dangerous to reset, and both were reset in practice
# before reset_transport replaced the object rebuild:
#
#   * last_statistics_fetch and _last_circuit_times_fetch are portal RATE
#     LIMITS (an hour each). Resetting them lets the next cycle refetch
#     immediately - on a portal that was just failing.
#   * _api_lock serialises a poll against a write. A fresh lock is an
#     unheld lock, so a write could interleave with the poll it exists to
#     serialise against.
#
# expert_cookies is the deliberate one: it caches a live web session for the
# expert path. Dropping it would force a full Fachmann login on the next
# expert operation, and the portal blocks the IP for 12 hours past 10,000
# requests. The API path failing says nothing about that session, so it
# stays.
#
# _blocked_until and _expert_blocked_until are NOT listed below, and their
# absence is the point. They used to be preserved fields - a recovery had to
# remember to keep them - and that guarantee only ever held where a previous
# instance existed to copy from. A failed first refresh has none: Home
# Assistant throws the object away and calls setup again, so the retry started
# with no backoff at all, after a 403 that had very likely caused the failure.
# They are module state now (see _BLOCKED_UNTIL in wemportalapi), which is
# what makes them survive every path rather than the ones we remembered. A
# guarantee that cannot be broken needs no entry in a list of things not to
# break.
# _first_cycle_done is session bookkeeping, not transport: it says whether a
# cycle has ever completed, which the daily parameter re-read reads to stay
# out of Home Assistant's setup. A recovery does not un-complete the cycles
# that already ran, and clearing it would only postpone that re-read by one
# cycle for no reason.
# _deadline belongs to the poll cycle in progress, not to the connection, so
# replacing the connection must not touch it. In practice a recovery runs
# between cycles and finds it None either way - but "the transport was
# rebuilt" is not a reason for a running cycle to gain more time, and having
# it in TRANSPORT_FIELDS would say it was.
# _last_device_read says when each device last returned values. Not transport:
# replacing the connection does not make readings from half an hour ago any
# fresher, and dropping it would restart the staleness clock on every
# recovery - so a device that never answers again would keep publishing its
# last values for another full window after each reset.
PRESERVED_FIELDS = frozenset(
    {
        "_first_cycle_done",
        "_deadline",
        "_last_device_read",
        "data",
        "username",
        "password",
        "scraper_device_id",
        "modules",
        "mode",
        "update_interval",
        "scan_interval",
        "scan_interval_api",
        "language",
        "_api_lock",
        "headers",
        "device_types",
        "_previous_scraper_keys",
        "_last_connection_status",
        "scraping_mapper",
        "last_statistics_fetch",
        "_last_circuit_times_fetch",
        "expert_cookies",
        "spider_wait_interval",
        "spider_retry_count",
        "last_scraping_update",
    }
)


class _Marker:
    """A value that is its own field.

    It also has to satisfy whatever reset_transport calls on the real thing:
    close() on the two transports, and the lock protocol on _api_lock (an
    always-free lock, so the reset proceeds and the identity check below
    still sees the marker it put there).
    """

    def __init__(self, field):
        self.field = field

    def close(self):
        pass

    def acquire(self, *_args, **_kwargs):
        return True

    def release(self):
        pass


def test_every_api_field_is_classified_as_transport_or_preserved():
    """Guards the guard: an unclassified field would make the test below
    silently cover less than it claims."""
    fields = set(vars(_api()))

    assert fields == TRANSPORT_FIELDS | PRESERVED_FIELDS, (
        "WemPortalApi gained or lost a field: "
        f"{fields ^ (TRANSPORT_FIELDS | PRESERVED_FIELDS)}. Decide whether a "
        "recovery must drop it (TRANSPORT_FIELDS) or keep it "
        "(PRESERVED_FIELDS) - and say why in the comment above."
    )
    assert not TRANSPORT_FIELDS & PRESERVED_FIELDS


def test_a_recovery_touches_exactly_the_transport_fields():
    """The classification, enforced against the real object.

    Every field is replaced by a marker that is unique to it, so the check
    is on IDENTITY: a marker is never the same object as {}, False or None,
    which means a reset is detected whatever value it resets to. Comparing
    values instead would let a field that resets to something equal to its
    old value pass unnoticed.
    """
    api = _api()
    for field in vars(api):
        setattr(api, field, _Marker(field))
    before = dict(vars(api))

    api.reset_transport()

    changed = {f for f, value in before.items() if getattr(api, f) is not value}
    assert changed == TRANSPORT_FIELDS, (
        f"unexpectedly reset: {sorted(changed - TRANSPORT_FIELDS)}; "
        f"not reset: {sorted(TRANSPORT_FIELDS - changed)}"
    )
    assert set(vars(api)) == set(before), "a recovery added or removed a field"


def test_a_recovery_leaves_the_transport_in_the_state_the_next_cycle_expects():
    """Not just "changed" - the values the next cycle actually reads.

    "Something else now" would be satisfied by a reset to a wrong value,
    and the next cycle reads these six directly: a stale api_version or a
    truthy valid_login sends it on without logging in again.
    """
    api = _api()
    for field in vars(api):
        setattr(api, field, _Marker(field))

    api.reset_transport()

    assert api.session is None
    assert api._scraper is None
    assert api.valid_login is False
    assert api.api_version is None
    assert api.webscraping_cookie == {}
    assert api._devices_fetched_this_session is False


def test_the_login_rejects_a_false_status(monkeypatch):
    """The login shares the verdict with the write path, or the two disagree
    about what the same field means."""
    api = _api()
    session = RecordingSession(post_json={"Status": False, "Version": "3.1"})
    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: session)

    with pytest.raises(exceptions.AuthError):
        api.api_login()
    assert api.valid_login is False


def test_a_refresh_answering_false_is_a_refusal():
    """Same field, same verdict, on the third of the three sites."""
    api = _api()
    api.modules = {"1234": {(1, 2): {"Index": 1, "Type": 2, "parameters": {"P1": {}}}}}
    api.make_api_call = lambda url, **_kwargs: FakeResponse(
        {"Status": False} if "Refresh" in url else {"Modules": []}
    )

    assert api._fetch_parameter_values("1234") is not None


def test_a_missing_job_id_is_reported_once_per_device(caplog):
    """Measured rather than enforced.

    Refusing the read would be the strict reading, but a failed device with
    only one device configured fails the whole cycle - so an installation
    whose portal legitimately omits the JobID would go off the air instead of
    degrading. Whether that happens is unknown, so it is reported.
    """
    import logging

    from custom_components.wemportal import wemportalapi as api_module

    api_module._MISSING_JOB_ID_REPORTED.clear()
    api = _api()
    api.modules = {"1234": {(1, 2): {"Index": 1, "Type": 2, "parameters": {"P1": {}}}}}
    api.make_api_call = lambda url, **_kwargs: FakeResponse(
        {"Modules": [{"ModuleIndex": 1, "ModuleType": 2, "Values": []}]}
        if "Read" in url
        else {"Status": 0}
    )

    with caplog.at_level(logging.WARNING):
        assert api._fetch_parameter_values("1234") is None
        assert api._fetch_parameter_values("1234") is None

    hits = [
        record for record in caplog.records if "without a JobID" in record.getMessage()
    ]
    assert len(hits) == 1, f"expected exactly one report, got {len(hits)}"


# --- a recovery must not tear down a write in flight -------------------
#
# fetch_data releases the api lock in its `finally`. A change_value() worker
# waiting on that lock takes it in the same instant - and the recovery, which
# ran on the event loop and took no lock at all, then closed the session out
# from under that write.


class _ClosingSession:
    """A session that records being closed."""

    def __init__(self, closed):
        self._closed = closed

    def close(self):
        self._closed.append("session")


def test_a_recovery_leaves_a_busy_connection_alone(monkeypatch, caplog):
    """The lock is held by a write, so the reset must not happen at all.

    Not "must wait": waiting is what the event loop cannot afford, and the
    recovery is best-effort - the next failed cycle tries again.
    """
    import logging

    monkeypatch.setattr(wemportalapi, "API_LOCK_TIMEOUT_SECONDS", 0.05)
    closed = []
    api = _api()
    api.session = _ClosingSession(closed)
    api.valid_login = True

    assert api._api_lock.acquire(blocking=False), "the lock must start free"
    try:
        with caplog.at_level(logging.WARNING):
            api.reset_transport()
    finally:
        api._api_lock.release()

    assert closed == [], "the recovery closed a session another operation was using"
    assert api.session is not None, "the transport was torn down under a write"
    assert api.valid_login is True
    assert any("still in use" in record.getMessage() for record in caplog.records)


def test_a_recovery_on_a_free_connection_still_resets_and_frees_the_lock():
    """The other half: with nothing running, the reset does its job - and
    gives the lock back, or the next poll blocks forever."""
    closed = []
    api = _api()
    api.session = _ClosingSession(closed)
    api.valid_login = True

    api.reset_transport()

    assert closed == ["session"]
    assert api.session is None
    assert api.valid_login is False
    assert api._api_lock.acquire(blocking=False), "the reset kept the lock"
    api._api_lock.release()


def test_the_expert_entity_does_not_start_a_write_while_unloading():
    """The fourth write path.

    It is not a WemPortalEntity, so it does not get the shared gate by
    inheritance - it has to pass through the same check explicitly, or the
    one platform whose writes take 5-15 seconds is the one that can still
    start one into a session about to be closed.
    """

    from homeassistant.exceptions import HomeAssistantError

    entity = _expert_entity(_api())
    entity._config_entry.runtime_data.unloading = True

    with pytest.raises(HomeAssistantError) as excinfo:
        _run(entity.async_set_native_value, 21.0)

    assert "unload" in str(excinfo.value).lower()
    assert entity._write_in_progress is False, "the entity was left marked as busy"


# --- the 403 backoff must outlive the object that earned it ------------


def test_a_brand_new_api_still_sees_an_active_cooldown():
    """The case a "carry it into the replacement" design cannot cover.

    A failed first refresh makes Home Assistant discard everything and call
    setup again - and the most likely reason for that failure is the 403 that
    just set the backoff. The retry has no previous instance to copy from, so
    it used to start at zero and go straight back at a portal that had just
    said stop.
    """
    api = _api()
    api._activate_cooldown()

    retry = WemPortalApi("user@example.org", "secret")

    with pytest.raises(exceptions.ForbiddenError):
        retry.check_cooldown()


def test_config_flow_validation_sees_it_too():
    """Same hole, different door: validating credentials builds its own api
    and would otherwise send requests during an active rate limit."""
    _api()._activate_cooldown()

    other_account = WemPortalApi("someone@example.org", "other")

    with pytest.raises(exceptions.ForbiddenError):
        other_account.check_cooldown()


def test_an_expert_backoff_does_not_spread_to_another_account():
    """Deliberately unlike the global one: an expert 403 is frequently a
    single rejected request rather than an IP-wide limit, so pausing a second
    account's expert path on the strength of it would cost readings for
    nothing."""
    _api().activate_expert_cooldown()

    other = WemPortalApi("someone-else@example.org", "secret")
    other.check_expert_cooldown()


def test_a_replacement_cannot_shorten_a_running_cooldown():
    """Callers still pass the old value; passing a smaller one - or none -
    must never pull the backoff in."""
    api = _api()
    api._activate_cooldown()
    active = api._blocked_until

    replacement = WemPortalApi("user@example.org", "secret", blocked_until=0.0)

    assert replacement._blocked_until == active
    with pytest.raises(exceptions.ForbiddenError):
        replacement.check_cooldown()


# --- closing the sessions is the api's own job, and it takes the lock ---


def test_closing_the_sessions_waits_for_the_operation_holding_the_lock():
    """An unload can land while a write or a poll is inside make_api_call.

    Closing its session at that moment turns an orderly teardown into a
    connection error, so the close waits for the lock the same way the
    recovery does - it just does not give up when the wait runs out, because
    a session left open is leaked for the life of the process.
    """
    api = _api()
    api.session = _Marker("session")
    order = []

    class RecordingLock:
        def acquire(self, *_args, **kwargs):
            order.append(("acquire", kwargs.get("timeout")))
            return True

        def release(self):
            order.append(("release", None))

    api._api_lock = RecordingLock()
    api._close_sessions = lambda: order.append(("close", None))

    api.close_transport()

    assert [step for step, _ in order] == ["acquire", "close", "release"]


def test_the_sessions_are_closed_even_if_the_lock_never_comes_free():
    api = _api()
    closed = []
    api._close_sessions = lambda: closed.append(True)

    class NeverFree:
        def acquire(self, *_args, **_kwargs):
            return False

        def release(self):  # pragma: no cover - must never be reached
            raise AssertionError("released a lock that was never acquired")

    api._api_lock = NeverFree()

    api.close_transport(timeout=0.01)

    assert closed == [True], "a session was left open because the lock was busy"


def test_close_api_sessions_calls_the_api_rather_than_reaching_inside():
    """The old version read `session` and `_reset_scraper` off the object with
    getattr defaults, so renaming or moving either turned it into a silent
    no-op that closed nothing and failed no test."""
    from custom_components.wemportal import utils

    calls = []

    class Api:
        def close_transport(self):
            calls.append(True)

    utils.close_api_sessions(Api())
    assert calls == [True]

    nothing_like_an_api = object()

    with pytest.raises(AttributeError):
        utils.close_api_sessions(nothing_like_an_api)


# --- a heating schedule that fails must not be re-fetched every cycle ---


SCHEDULE_ROW = "Heating circuit 1-Heizprogramm1"


def _circuit_times_api(responses, data_type=6, value=None):
    """An api with one schedule parameter, answering from `responses`.

    `data_type` and `value` are what the portal declared and what the value
    read already put in the row - the two things that decide whether this
    parameter counts as a programme at all.
    """
    api = _api()
    rows = {}
    if value is not None:
        rows[SCHEDULE_ROW] = {
            "value": value,
            "unit": None,
            "friendlyName": "Heating programme",
            "ParameterID": "Heizprogramm1",
            "platform": "sensor",
        }
    api.data = {"1234": rows}
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heating circuit 1",
                "parameters": {
                    "Heizprogramm1": {
                        "ParameterID": "Heizprogramm1",
                        "DataType": data_type,
                    }
                },
            }
        }
    }
    calls = []

    def make_api_call(url, **_kwargs):
        calls.append(url)
        answer = responses.pop(0) if responses else None
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)

    api.make_api_call = make_api_call
    return api, calls


def test_a_failing_schedule_is_not_refetched_on_every_cycle():
    """The timestamp used to be written after a SUCCESS.

    A schedule that keeps failing therefore never engaged the interval guard,
    so every coordinator cycle spent two more requests on it - at a portal
    that was already failing, against an IP the portal blocks past 10,000
    requests per 12 hours.
    """
    api, calls = _circuit_times_api(
        [exceptions.WemPortalError("portal unavailable")] * 10
    )

    api._fetch_circuit_times("1234")
    after_first = len(calls)
    api._fetch_circuit_times("1234")

    assert after_first >= 1, "the first cycle did not even try"
    assert len(calls) == after_first, (
        "the failed schedule was fetched again on the very next cycle"
    )


def test_a_refresh_without_a_job_id_also_counts_as_an_attempt():
    """The early `continue` costs a request just like a raised error does."""
    api, calls = _circuit_times_api([{"NoJobID": True}] * 10)

    api._fetch_circuit_times("1234")
    after_first = len(calls)
    api._fetch_circuit_times("1234")

    assert after_first == 1, "the refresh should have been the only request"
    assert len(calls) == 1, "a refresh that named no job was repeated at once"


def test_the_failed_schedule_is_tried_again_after_the_retry_interval():
    """Back-dated, not blocked: one bad cycle must not cost a full hour."""
    api, _calls = _circuit_times_api([exceptions.WemPortalError("nope")] * 10)

    api._fetch_circuit_times("1234")
    stamp = api._last_circuit_times_fetch[("1234", "Heizprogramm1")]

    waited = time.time() - stamp
    assert waited >= wemportalapi.CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS - (
        wemportalapi.CIRCUIT_TIMES_RETRY_INTERVAL_SECONDS + 5
    ), "the retry was pushed out further than the retry interval"
    assert waited < wemportalapi.CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS, (
        "the schedule would be retried immediately"
    )


def test_a_successful_schedule_keeps_the_full_interval():
    api, _calls = _circuit_times_api(
        [
            {"JobID": 7},
            {"CircuitTimesDay": [], "PossibleValues": []},
        ]
    )

    api._fetch_circuit_times("1234")
    stamp = api._last_circuit_times_fetch[("1234", "Heizprogramm1")]

    assert time.time() - stamp < 5, "a successful fetch was back-dated like a failure"


# --- the fetch has to recognise a programme the portal types as a switch


def test_a_programme_the_portal_types_as_a_switch_is_still_fetched():
    """DataType 6 is one of two ways the portal types a programme.

    On a 3.1.3.0 portal every one of them arrives as DataType 2 with a JSON
    object in the value, so keying on the declared type alone meant this
    fetch - the only path that asks the DEVICE for its schedule instead of
    reading the portal's stored copy - never ran at all. It did not fail; it
    was never entered, which is why no log ever mentioned it.
    """
    api, calls = _circuit_times_api(
        [{"JobID": 7}, {"CircuitTimesDay": [], "PossibleValues": []}],
        data_type=2,
        value='{"MO-1":"00:00-24:00"}',
    )

    api._fetch_circuit_times("1234")

    assert calls, "a programme typed as a switch was never asked about"


def test_a_plain_switch_is_not_mistaken_for_a_programme():
    """The other half: DataType 2 is the ordinary switch type, and asking the
    portal for the weekly schedule of a pump relay would waste two requests
    per switch per hour against an account it blocks past 10,000."""
    api, calls = _circuit_times_api(
        [{"JobID": 7}, {"CircuitTimesDay": []}],
        data_type=2,
        value=1.0,
    )

    api._fetch_circuit_times("1234")

    assert calls == [], "a plain switch was fetched as if it had a schedule"


def test_the_fetch_adds_to_the_programme_instead_of_replacing_it():
    """It used to write the fixed word "Active" into the same row the value
    read fills, so a readable week was replaced by a placeholder once an
    hour until the next cycle put it back."""
    schedule = '{"MO-1":"00:00-24:00"}'
    api, _calls = _circuit_times_api(
        [{"JobID": 7}, {"CircuitTimesDay": [{"day": "MO"}], "PossibleValues": ["H"]}],
        data_type=2,
        value=schedule,
    )

    api._fetch_circuit_times("1234")

    row = api.data["1234"][SCHEDULE_ROW]
    assert row["value"] == schedule, "the programme was replaced by a placeholder"
    assert row["CircuitTimesDay"] == [{"day": "MO"}]
    assert row["PossibleValues"] == ["H"]


def test_a_row_only_this_fetch_knows_about_still_gets_a_placeholder():
    """Where the value read never delivered the programme, this fetch is the
    only source there is - and a row needs some state to show."""
    api, _calls = _circuit_times_api(
        [{"JobID": 7}, {"CircuitTimesDay": [], "PossibleValues": []}],
    )

    api._fetch_circuit_times("1234")

    assert api.data["1234"][SCHEDULE_ROW]["value"] == "Active"


# --- a scraped reading that is gone must not be shown as current -------


def _scraped_row(value, unit="°C"):
    """One row as the scraper hands it over. Named apart from _scraped_row()
    above, which builds a whole scrape from key names - defining a second
    `_scraped` silently rebound the first for every test in this file."""
    return {
        "value": value,
        "unit": unit,
        "friendlyName": "Setpoint",
        "name": "wp-solltemperatur",
        "icon": None,
        "ParameterID": "wp-solltemperatur",
        "platform": "sensor",
    }


def test_a_scraped_row_without_a_value_clears_the_sensor():
    """Measured on a live installation: the portal renders "--" for a value
    it does not have, the scrape maps that to None - and the old value was
    carried over, so a setpoint read 50.5 degrees for three hours while the
    portal and the heat pump both showed nothing."""
    api = _api()
    api._merge_webscraping_data("0000", {"wp-solltemperatur": _scraped_row(50.5)})
    assert api.data["0000"]["wp-solltemperatur"]["value"] == 50.5

    api._merge_webscraping_data(
        "0000", {"wp-solltemperatur": _scraped_row(None, unit="")}
    )

    assert api.data["0000"]["wp-solltemperatur"]["value"] is None, (
        "a reading the portal no longer has was reported as current"
    )


def test_the_unit_is_still_carried_over():
    """The other half of the same block, and it must stay: a "--" row has no
    unit, and Home Assistant complains when one changes."""
    api = _api()
    api._merge_webscraping_data("0000", {"wp-solltemperatur": _scraped_row(50.5)})

    api._merge_webscraping_data(
        "0000", {"wp-solltemperatur": _scraped_row(None, unit="")}
    )

    assert api.data["0000"]["wp-solltemperatur"]["unit"] == "°C"


def test_a_row_that_stops_being_scraped_stops_showing_its_last_value():
    api = _api()
    api._merge_webscraping_data(
        "0000",
        {
            "wp-solltemperatur": _scraped_row(50.5),
            "wp-vorlauf": _scraped_row(31.0),
        },
    )

    api._merge_webscraping_data("0000", {"wp-vorlauf": _scraped_row(32.0)})

    assert api.data["0000"]["wp-solltemperatur"]["value"] is None
    assert api.data["0000"]["wp-vorlauf"]["value"] == 32.0


def test_the_entity_of_a_vanished_row_is_kept():
    """Cleared, not removed: the entity still exists in Home Assistant, and
    dropping the key makes its platform log "Can't find ..." every cycle."""
    api = _api()
    api._merge_webscraping_data("0000", {"wp-solltemperatur": _scraped_row(50.5)})

    api._merge_webscraping_data("0000", {"wp-vorlauf": _scraped_row(31.0)})

    assert "wp-solltemperatur" in api.data["0000"]


def test_the_first_cycle_clears_nothing():
    """There is nothing to compare against yet.

    The reading just scraped is the one that matters here: an off-by-one in
    which set is returned - the keys that are gone, or the keys that are
    here - wipes every value on the very first cycle of every session, and
    the entities come up empty until the second one.
    """
    api = _api()
    api.data["0000"] = {"left-over": _scraped_row(12.0)}

    api._merge_webscraping_data("0000", {"wp-vorlauf": _scraped_row(31.0)})

    assert api.data["0000"]["wp-vorlauf"]["value"] == 31.0, (
        "the first cycle cleared the values it had just read"
    )
    assert api.data["0000"]["left-over"]["value"] == 12.0


# --- a device with nothing discovered must not be asked for values ------


def test_a_device_with_no_parameters_is_not_asked_for_values(caplog):
    """The portal answers a read with an empty module list with 400.

    Upstream issue #66 is a log of exactly that: {'DeviceID': 424,
    'Modules': []} rejected every cycle until the reporter switched to web
    mode. It is reachable from the other side here too - a module whose
    EventType/Read answers 400 is dropped from the cache as unsupported, so
    a device where that happens to all of them ends up with this list.
    """
    import logging

    api = _api()
    api.data = {"1234": {}}
    # Modules ARE known - discovery just never produced parameters for them.
    # That is what separates this from a device that has no modules at all,
    # which has nothing to poll and is not a failure.
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "Name": "Heat pump"}}}
    calls = []
    api.make_api_call = lambda url, **_kwargs: calls.append(url) or FakeResponse({})

    with caplog.at_level(logging.WARNING):
        failure = api._fetch_parameter_values("1234")

    assert calls == [], "a read with an empty module list was sent anyway"
    assert failure is not None, (
        "a device whose discovery produced nothing was counted as refreshed"
    )
    assert "no known parameters" in failure, (
        "the reason reaching the caller does not say what went wrong"
    )
    assert "no known parameters" in caplog.text


def test_a_device_with_no_modules_at_all_is_not_a_failure():
    """The distinction the first version of the guard missed.

    A device the portal lists but that has nothing to poll is not broken.
    Failing the cycle for it drags every other device into a backoff over a
    device that is simply empty - which three existing tests object to.
    """
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    calls = []
    api.make_api_call = lambda url, **_kwargs: calls.append(url) or FakeResponse({})

    assert api._fetch_parameter_values("1234") is None
    assert calls == [], "a read with an empty module list was sent anyway"


def test_a_refused_description_is_retried_within_the_hour_not_the_day():
    """A module that has nothing yet is the URGENT one, not the patient one.

    A failed RE-read already retried in an hour, because the module keeps
    showing its known parameters meanwhile. A failed FIRST description waited
    a full day - and that module has nothing to show at all, so the device it
    belongs to stayed empty for a day over one refused request. The urgency
    was exactly inverted.
    """
    api = _api()
    values = {"Index": 0, "Type": 1, "Name": "Heat pump"}

    api._note_undescribed_module(
        "1234", values, "the portal rejected the request", unsupported=True
    )

    age = time.time() - values["parameters_fetched_at"]
    due_in = wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS - age
    assert due_in <= wemportalapi.PARAMETER_REDISCOVERY_RETRY_SECONDS + 5, (
        "a refused description waits the full day before it is asked again"
    )
    assert values["description_refused"] is True


def test_a_module_that_says_it_is_empty_keeps_the_daily_round():
    """The other half, and the reason this is not one branch.

    An empty description is an ANSWER: the module says it has nothing to
    poll. Believing it costs one request a day; retrying it hourly would
    spend twenty-four times that on modules that answered correctly the
    first time.
    """
    api = _api()
    values = {"Index": 0, "Type": 1, "Name": "Heat pump"}

    api._note_undescribed_module(
        "1234", values, "it described no parameters", unsupported=False
    )

    assert time.time() - values["parameters_fetched_at"] < 5, (
        "a module that answered was back-dated as if it had refused"
    )
    assert "description_refused" not in values


def test_a_device_whose_modules_were_all_refused_says_so(caplog):
    """ "No entities appeared" must not be the only symptom.

    The cycle deliberately does NOT fail - the device may genuinely have
    nothing, and failing would drag every other device into a backoff. But
    the refusal is the reason there is nothing to show, and it used to be a
    debug line, so the user saw an empty device and no explanation anywhere.
    """
    import logging

    api = _api()
    api.data = {"1234": {}}
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "parameters": {},
                "description_refused": True,
            }
        }
    }
    calls = []
    api.make_api_call = lambda url, **_kwargs: calls.append(url) or FakeResponse({})

    with caplog.at_level(logging.WARNING):
        failure = api._fetch_parameter_values("1234")

    assert failure is None, "a refused description must not fail the whole cycle"
    assert calls == [], "a read with an empty module list was sent anyway"
    assert "refused to describe" in caplog.text, (
        "an empty device gave the user nothing to go on"
    )


def test_a_device_that_is_simply_empty_stays_quiet(caplog):
    """The counterpart: no refusal, no warning.

    Without this the previous test would pass just as well against a warning
    on every empty device, which is noise on an installation where a module
    legitimately has nothing to poll.
    """
    import logging

    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {}}}}

    with caplog.at_level(logging.WARNING):
        assert api._fetch_parameter_values("1234") is None

    assert not caplog.records, f"an empty device warned anyway: {caplog.text}"


def test_a_description_that_arrives_clears_the_refusal():
    """The flag is persisted with the module cache, so a refusal that is
    never cleared outlives the restart that fixed it."""
    api = _api()
    api.modules = {
        "1234": {
            (0, 1): {"Index": 0, "Type": 1, "description_refused": True},
        }
    }

    api._store_module_description(
        "1234",
        (0, 1),
        api.modules["1234"][(0, 1)],
        FakeResponse({"Parameters": [{"ParameterID": "P1"}]}),
    )

    module = api.modules["1234"][(0, 1)]
    assert module["parameters"] == {"P1": {"ParameterID": "P1"}}
    assert "description_refused" not in module


def test_a_device_with_parameters_is_still_read():
    """The guard must not swallow the ordinary case."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {"AktRaumSoll": {"ParameterID": "AktRaumSoll"}},
            }
        }
    }
    calls = []

    def make_api_call(url, **_kwargs):
        calls.append(url)
        if url == wemportalapi.API_REFRESH_URL:
            return FakeResponse({"Status": 0, "JobID": 7})
        return FakeResponse(
            {
                "Modules": [
                    {
                        "ModuleIndex": 0,
                        "ModuleType": 1,
                        "Values": [
                            {
                                "ParameterID": "AktRaumSoll",
                                "NumericValue": 21.0,
                                "Unit": "°C",
                            }
                        ],
                    }
                ]
            }
        )

    api.make_api_call = make_api_call

    assert api._fetch_parameter_values("1234") is None
    assert wemportalapi.API_REFRESH_URL in calls


# --- the parameter list is re-read, and a failed re-read keeps it -------


def _discovery_api(answers, fetched_at=None):
    """An api with one cached module, answering EventType/Read from `answers`."""
    api = _api()
    api.data = {"1234": {"ConnectionStatus": 0}}
    module = {
        "Index": 0,
        "Type": 1,
        "Name": "Heat pump",
        "parameters": {"Known": {"ParameterID": "Known"}},
    }
    if fetched_at is not None:
        module["parameters_fetched_at"] = fetched_at
    api.modules = {"1234": {(0, 1): module}}
    calls = []

    def make_api_call(url, **_kwargs):
        calls.append(url)
        answer = answers.pop(0) if answers else None
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)

    api.make_api_call = make_api_call
    return api, calls


def test_a_fresh_parameter_list_is_not_re_read():
    api, calls = _discovery_api([], fetched_at=time.time())

    api.get_parameters()

    assert calls == [], "a list read minutes ago was read again"


def test_a_stale_parameter_list_is_re_read():
    """Cached forever, a parameter added on a module the integration already
    knew - activating an input or output in the portal - was never
    discovered, with no error and no way to force a re-scan."""
    api, calls = _discovery_api(
        [{"Parameters": [{"ParameterID": "Known"}, {"ParameterID": "New"}]}],
        fetched_at=time.time()
        - (wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS + 60),
    )

    api.get_parameters()

    assert calls == [wemportalapi.API_EVENT_TYPE_READ_URL]
    assert set(api.modules["1234"][(0, 1)]["parameters"]) == {"Known", "New"}


def test_a_list_with_no_timestamp_is_re_read_once():
    """A cache written before this existed must heal itself."""
    api, calls = _discovery_api([{"Parameters": [{"ParameterID": "Known"}]}])

    api.get_parameters()

    assert calls == [wemportalapi.API_EVENT_TYPE_READ_URL]
    assert api.modules["1234"][(0, 1)]["parameters_fetched_at"] > 0


def test_a_failed_re_read_keeps_the_parameters_it_had():
    """The condition the whole interval rests on.

    Discovery used to run once per session, so dropping a module the portal
    would not describe was harmless. On a timer that path runs again and
    again, and one 403 or maintenance window would throw away a working
    parameter list - straight into "device has no parameters".
    """
    stale = time.time() - (wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS + 60)
    api, _calls = _discovery_api([{"Parameters": []}], fetched_at=stale)

    api.get_parameters()

    assert (0, 1) in api.modules["1234"], "a working module was deleted"
    assert set(api.modules["1234"][(0, 1)]["parameters"]) == {"Known"}


def test_a_failed_re_read_is_not_retried_on_the_very_next_cycle():
    stale = time.time() - (wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS + 60)
    api, calls = _discovery_api(
        [{"Parameters": []}, {"Parameters": []}], fetched_at=stale
    )

    api.get_parameters()
    after_first = len(calls)
    api.get_parameters()

    assert after_first == 1
    assert len(calls) == 1, "a refused module was asked again immediately"


def test_a_module_described_as_empty_is_kept_and_not_asked_again():
    """Deleting it achieved nothing: Device/Read brings the module back on
    the next start, it is asked again, described as empty again and dropped
    again - one wasted request and one warning per session, for ever, about a
    module that is merely empty. Kept with an empty list and a timestamp, it
    falls under the normal interval like everything else."""
    api, calls = _discovery_api([{"Parameters": []}, {"Parameters": []}])
    del api.modules["1234"][(0, 1)]["parameters"]

    api.get_parameters()

    assert (0, 1) in api.modules["1234"], "an empty module was deleted"
    assert api.modules["1234"][(0, 1)]["parameters"] == {}
    assert len(calls) == 1

    api.get_parameters()
    assert len(calls) == 1, "the empty module was asked again on the next cycle"


def test_an_unreadable_description_is_not_asked_again_every_cycle():
    """An answer without the expected shape has to be BOOKED, like every other
    unusable one.

    Skipping it with only a log line left the module without a timestamp, so
    the age check never held it back: a portal answering nonsense - an HTML
    error page, a truncated payload - was asked again on every single cycle,
    without limit. The retry budget the 400 and the empty description already
    fall under simply never covered this branch.
    """
    api, calls = _discovery_api([{"NotParameters": []}, {"NotParameters": []}])
    del api.modules["1234"][(0, 1)]["parameters"]

    api.get_parameters()

    assert len(calls) == 1
    assert api.modules["1234"][(0, 1)]["parameters_fetched_at"] > 0, (
        "the attempt was not recorded, so nothing can bound the next one"
    )

    api.get_parameters()
    assert len(calls) == 1, "an unreadable module was asked again immediately"


def test_an_unreadable_description_keeps_the_parameters_it_had():
    """Same rule as a failed re-read: one bad answer must not empty a working
    list. The retry is the shorter one, because this module DID work."""
    stale = time.time() - (wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS + 60)
    api, calls = _discovery_api([{"NotParameters": []}], fetched_at=stale)

    api.get_parameters()

    assert set(api.modules["1234"][(0, 1)]["parameters"]) == {"Known"}
    assert len(calls) == 1


def _rejected_description():
    """A 400 from EventType/Read, as the portal delivers it."""
    rejected = exceptions.WemPortalError("bad request")
    response = FakeResponse({}, status_code=400)
    rejected.__cause__ = real_requests.exceptions.HTTPError(response=response)
    return rejected


def test_a_module_the_portal_rejects_is_kept_and_asked_again():
    """Upstream #126: a heating circuit whose first description answered 400
    was deleted from the cache, and only get_devices() could bring it back -
    which runs once per session. Whether an installation showed one circuit
    or two therefore came down to what the portal answered in the second the
    integration started."""
    api, _calls = _discovery_api([_rejected_description()])
    del api.modules["1234"][(0, 1)]["parameters"]

    api.get_parameters()

    assert (0, 1) in api.modules["1234"], "the module was thrown away"
    assert api.modules["1234"][(0, 1)]["parameters"] == {}


def test_a_rejected_module_is_not_asked_again_on_the_next_cycle():
    """Kept, not re-asked: it carries a timestamp like everything else, so
    the daily interval applies. Retrying at once would spend two requests a
    cycle on a module the portal has just refused."""
    api, calls = _discovery_api([_rejected_description()])
    del api.modules["1234"][(0, 1)]["parameters"]

    api.get_parameters()
    api.get_parameters()

    assert len(calls) == 1, "the rejected module was asked again immediately"


def test_a_device_whose_every_module_was_rejected_is_not_a_failed_cycle():
    """The trap in keeping them, and the reason this needed a second change.

    A rejected module used to be deleted, so an installation whose every
    module the portal refuses ended up with an empty module dict - "nothing
    to poll", a quiet success. Keeping them instead lands in the "modules but
    no parameters" branch, which reports a FAILED refresh. With one device
    that fails the whole cycle, every cycle, for ever: backoff, recovery,
    eventually a re-authentication prompt. That would have been a worse bug
    than the one being fixed.
    """
    api, _calls = _discovery_api([_rejected_description()])
    del api.modules["1234"][(0, 1)]["parameters"]
    api.get_parameters()

    reads = []
    api.make_api_call = lambda url, **_kwargs: reads.append(url) or FakeResponse({})

    assert api._fetch_parameter_values("1234") is None, (
        "a device with nothing to poll was reported as a failed refresh"
    )
    assert reads == [], "a module with no known parameters was read anyway"


def test_a_module_still_awaiting_its_description_does_fail_the_cycle():
    """The other side of that line: a module that has never been described -
    as opposed to described as empty - means discovery has not run yet, and
    reporting the cycle as successful would present nothing as everything."""
    api, _calls = _discovery_api([{"Parameters": []}])
    del api.modules["1234"][(0, 1)]["parameters"]

    assert api._fetch_parameter_values("1234") is not None


def test_get_devices_carries_the_parameter_timestamp_too():
    """It travels with the list it belongs to.

    Left behind, every session starts with the cache looking expired and
    re-reads every module on its first cycle - which is the portal load the
    interval exists to avoid, arriving on every restart instead.
    """
    fetched_at = time.time() - 60
    cached = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {"P1": {"ParameterID": "P1"}},
                "parameters_fetched_at": fetched_at,
            },
        }
    }
    api = _api(cached_modules=cached)
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {
            "Devices": [
                {
                    "ID": 1234,
                    "ConnectionStatus": 0,
                    "Modules": [{"Index": 0, "Type": 1, "Name": "Heat pump"}],
                }
            ]
        }
    )

    api.get_devices()

    assert api.modules["1234"][(0, 1)]["parameters_fetched_at"] == fetched_at


def _cycle_api(fetched_at):
    """An api whose one module has a parameter list of the given age."""
    api = _api()
    api.data = {"1234": {"ConnectionStatus": 0}}
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {"Known": {"ParameterID": "Known"}},
                "parameters_fetched_at": fetched_at,
            }
        }
    }
    api._devices_fetched_this_session = True
    api.valid_login = True
    read = []
    api.get_parameters = lambda: read.append("read")
    api.get_data = lambda *_args, **_kwargs: None
    return api, read


def test_stale_definitions_wait_for_the_second_cycle():
    """The first refresh runs inside Home Assistant's setup, and this
    discovery sleeps five seconds per module - a re-read there turns every
    restart with an expired cache into a slow startup, which Home Assistant
    then complains about. Nothing is lost by waiting one interval for
    something that is already a day old."""
    api, read = _cycle_api(
        time.time() - (wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS + 60)
    )

    api.fetch_data()

    assert read == [], "the daily re-read ran during the first refresh"


def test_stale_definitions_are_read_on_a_later_cycle():
    api, read = _cycle_api(
        time.time() - (wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS + 60)
    )

    api.fetch_data()
    api.fetch_data()

    assert read == ["read"], "the daily re-read never happened"


def test_missing_definitions_are_read_at_once():
    """A module with no list at all is a different urgency: without it there
    is nothing to read and nothing to show, so it cannot wait a cycle."""
    api, read = _cycle_api(0)
    del api.modules["1234"][(0, 1)]["parameters"]

    api.fetch_data()

    assert read == ["read"]


# --- a weekly programme has to be readable, not just present -----------


DAYS = ["MO", "DI", "MI", "DO", "FR", "SA", "SO"]


def _row(raw, **extra):
    """One coordinator row carrying a programme, as the mapper builds it.

    The schedule helpers take the ROW, not the raw value: the device's own
    view of the same programme arrives beside it, and it is the better of the
    two sources.
    """
    return {
        "value": raw,
        "unit": None,
        "friendlyName": "Programme",
        "ParameterID": "Programm",
        "platform": "sensor",
        **extra,
    }


def _week_payload():
    """The shape a real installation sends, transfer id replaced.

    Written in the portal's own order - every window first, then the letters,
    then the transfer fields - because that order is what the first version
    of the parser tripped over.
    """
    payload = {}
    for day in DAYS:
        payload[f"{day}-1"] = "00:00-24:00"
        payload[f"{day}-2"] = "00:00-00:00"
        payload[f"{day}-3"] = "00:00-00:00"
    for day in DAYS:
        payload[day] = "HLL"
    payload.update(
        {
            "zone": "1",
            "type": "Functionlist",
            "TransferId": "00000000",
            "mode": "cycletime",
            "cmd": "load",
            "status": "ok",
        }
    )
    return json.dumps(payload)


def _sensor_from_row(key, row):
    """One sensor entity built from one coordinator row, without Home
    Assistant. The row IS the object the coordinator holds, so whatever the
    entity reads back out of it, it reads the way production does."""
    import types

    from custom_components.wemportal.sensor import WemPortalSensor

    coordinator = types.SimpleNamespace(
        data={"1234": {key: row}},
        api=types.SimpleNamespace(api_version="2.0", modules={}),
        last_update_success=True,
        async_add_listener=lambda *_args, **_kwargs: None,
    )
    return WemPortalSensor(
        coordinator, types.SimpleNamespace(entry_id="e1"), "1234", key, row
    )


def _schedule_sensor(raw):
    """A sensor built from one programme reading."""
    return _sensor_from_row(
        "Programm",
        {
            "value": raw,
            "unit": None,
            "friendlyName": "Heating programme",
            "ParameterID": "Programm",
            "ModuleIndex": 0,
            "ModuleType": 1,
        },
    )


# --- a word the portal knows and this integration does not --------------


@pytest.fixture
def _forget_unreadable_reports():
    from custom_components.wemportal import sensor as sensor_module

    sensor_module._UNREADABLE_REPORTED.clear()
    yield
    sensor_module._UNREADABLE_REPORTED.clear()


def _numeric_sensor(value):
    """A sensor that must hold a number - it carries a unit."""
    return _sensor_from_row(
        "Pump",
        {
            "value": value,
            "unit": "%",
            "friendlyName": "Pump speed",
            "ParameterID": "Drehzahl",
            "ModuleIndex": 0,
            "ModuleType": 1,
        },
    )


def test_a_word_that_is_not_a_number_shows_as_unknown(_forget_unreadable_reports):
    """Upstream #146: a pump speed reading "Stop" on a portal that writes
    "Aus" everywhere else. Not a fault - a state we do not know."""
    assert _numeric_sensor("Stop").native_value is None


def test_an_unreadable_word_is_reported_once_not_every_cycle(
    _forget_unreadable_reports, caplog
):
    """It arrives on every cycle for as long as the condition lasts, and a
    warning each time buries everything else in the log."""
    import logging

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            _numeric_sensor("Stop")

    hits = [record for record in caplog.records if "as a number" in record.getMessage()]
    assert len(hits) == 1, f"reported {len(hits)} times"
    assert "Stop" in hits[0].getMessage(), "the unknown word was not named"


def test_a_different_unreadable_word_is_reported_on_its_own(
    _forget_unreadable_reports, caplog
):
    """Silencing the sensor rather than the word would hide the second state
    this installation turns out to have."""
    import logging

    with caplog.at_level(logging.WARNING):
        _numeric_sensor("Stop")
        _numeric_sensor("Blockiert")

    hits = [record for record in caplog.records if "as a number" in record.getMessage()]
    assert len(hits) == 2


def test_a_word_this_integration_does_know_is_not_reported(
    _forget_unreadable_reports, caplog
):
    import logging

    with caplog.at_level(logging.WARNING):
        entity = _numeric_sensor(0.0)

    assert entity.native_value == 0.0
    assert "as a number" not in caplog.text


def test_a_weekly_programme_is_grouped_by_day():
    from custom_components.wemportal.sensor import _readable_schedule

    raw = (
        '{"MO-1":"15:00-18:00","MO-2":"00:00-00:00","MO-3":"00:00-00:00",'
        '"DI-1":"06:00-07:00","DI-2":"17:00-19:00","DI-3":"00:00-00:00"}'
    )

    assert _readable_schedule(_row(raw)) == {
        "MO": ["15:00-18:00"],
        "DI": ["06:00-07:00", "17:00-19:00"],
    }


def test_the_transfer_fields_do_not_become_weekdays():
    """The defect the first version shipped with.

    Grouping on key.split("-")[0] put three unrelated kinds of key in one
    basket: the per-window letters were rendered as a period of Monday, and
    zone, type, mode, cmd, status and the transfer id each became a day of
    the week of their own.
    """
    from custom_components.wemportal.sensor import _readable_schedule

    schedule = _readable_schedule(_row(_week_payload()))

    assert list(schedule) == DAYS
    assert schedule["MO"] == ["00:00-24:00 (H)"]


def test_a_letter_belongs_to_the_window_it_numbers():
    """By slot number, not by position among the windows that survived.

    Reading them off the filtered list would hand the second slot's letter
    to a day whose FIRST slot is the unused one - a plausible-looking value
    that is simply the wrong one.
    """
    from custom_components.wemportal.sensor import _readable_schedule

    raw = '{"MO-1":"00:00-00:00","MO-2":"17:00-19:00","MO-3":"00:00-00:00","MO":"LHL"}'

    assert _readable_schedule(_row(raw)) == {"MO": ["17:00-19:00 (H)"]}


def test_a_letter_that_is_not_there_is_not_invented():
    """A payload whose letters do not line up must not borrow one."""
    from custom_components.wemportal.sensor import _readable_schedule

    raw = '{"MO-1":"06:00-07:00","MO-2":"17:00-19:00","MO":"H"}'

    assert _readable_schedule(_row(raw)) == {"MO": ["06:00-07:00 (H)", "17:00-19:00"]}


def test_the_summary_collapses_days_that_are_the_same():
    from custom_components.wemportal.sensor import _schedule_summary

    assert _schedule_summary(_row(_week_payload())) == "MO-SO 00:00-24:00 (H)"


def test_the_summary_keeps_days_that_differ_apart():
    from custom_components.wemportal.sensor import _schedule_summary

    raw = '{"MO-1":"06:00-07:00","DI-1":"08:00-09:00","MI-1":"08:00-09:00"}'

    assert _schedule_summary(_row(raw)) == "MO 06:00-07:00; DI-MI 08:00-09:00"


def test_the_state_is_the_week_and_not_the_word():
    """The whole point: "Programmed" said only that the parameter exists."""
    assert _schedule_sensor(_week_payload()).native_value == "MO-SO 00:00-24:00 (H)"


# --- the device's own view of a programme, which is the complete one -----
#
# Measured on a live installation. CircuitTimes carries a list of stretches
# per day: MinutesSinceMidnight is the END of a stretch, Value its level, and
# PossibleValues names the levels in the portal's own words. The heating
# programme came back as 360/610/810/1440 carrying 3/2/1/3 - exactly the four
# cycles the portal lists, including the reduced stretch from ten past ten
# until half one, which has NO window in the JSON at all.


HEATING_LEVELS = [
    {"Value": 1, "Text": "Absenk"},
    {"Value": 2, "Text": "Normal"},
    {"Value": 3, "Text": "Komfort"},
]


def _measured_monday():
    """The heating programme of a real installation: Monday split four ways,
    the rest of the week comfort throughout."""
    payload = {}
    windows = {"MO": ["00:00-06:00", "06:00-10:10", "13:30-24:00"]}
    for day in DAYS:
        for slot in (1, 2, 3):
            payload[f"{day}-{slot}"] = (
                windows.get(day, ["00:00-24:00"])[slot - 1]
                if slot <= len(windows.get(day, ["00:00-24:00"]))
                else "00:00-00:00"
            )
    for day in DAYS:
        payload[day] = "HLH" if day == "MO" else "HLL"
    payload.update({"zone": "1", "type": "Functionlist", "mode": "cycletime"})

    circuit_times = [
        {
            "Day": 1,
            "BlockNumber": 1,
            "CircuitTimes": [
                {"MinutesSinceMidnight": 360, "Value": 3},
                {"MinutesSinceMidnight": 610, "Value": 2},
                {"MinutesSinceMidnight": 810, "Value": 1},
                {"MinutesSinceMidnight": 1440, "Value": 3},
            ],
        }
    ]
    for number in (2, 3, 4, 5, 6, 0):
        circuit_times.append(
            {
                "Day": number,
                "BlockNumber": 2,
                "CircuitTimes": [{"MinutesSinceMidnight": 1440, "Value": 3}],
            }
        )
    return _row(
        json.dumps(payload),
        CircuitTimesDay=circuit_times,
        PossibleValues=HEATING_LEVELS,
    )


def test_the_reduced_stretch_the_json_does_not_carry_is_shown():
    """The whole reason this source is preferred: between the second and the
    third window the programme is reduced for three hours, and the JSON says
    nothing at all about them."""
    from custom_components.wemportal.sensor import _readable_schedule

    assert _readable_schedule(_measured_monday())["MO"] == [
        "00:00-06:00 Komfort",
        "06:00-10:10 Normal",
        "10:10-13:30 Absenk",
        "13:30-24:00 Komfort",
    ]


def test_the_levels_are_named_in_the_portals_own_words():
    """PossibleValues ships with the programme, so the names are the portal's
    rather than a table in this repository guessing at letters."""
    from custom_components.wemportal.sensor import _readable_schedule

    row = _measured_monday()
    row["PossibleValues"] = [{"Value": 3, "Text": "Fest"}]

    assert _readable_schedule(row)["DI"] == ["00:00-24:00 Fest"]


def test_a_level_the_portal_did_not_name_keeps_its_times():
    """A stretch is worth showing even when its level has no word."""
    from custom_components.wemportal.sensor import _readable_schedule

    row = _measured_monday()
    row["PossibleValues"] = []

    assert _readable_schedule(row)["DI"] == ["00:00-24:00"]


def test_the_days_are_labelled_the_way_the_portal_labels_them():
    """1..6 is Monday to Saturday and 0 is Sunday, and the names come from
    the JSON rather than from a weekday table in here - which is what keeps
    this working whatever language the portal speaks."""
    from custom_components.wemportal.sensor import _readable_schedule

    assert list(_readable_schedule(_measured_monday())) == DAYS


def test_the_state_carries_the_whole_week():
    from custom_components.wemportal.sensor import _schedule_summary

    assert _schedule_summary(_measured_monday()) == (
        "MO 00:00-06:00 Komfort, 06:00-10:10 Normal, "
        "10:10-13:30 Absenk, 13:30-24:00 Komfort; "
        "DI-SO 00:00-24:00 Komfort"
    )


def test_without_the_device_view_the_json_still_answers():
    """The hour after a restart, before the schedule fetch has run. Less
    detail - no reduced stretches, bare letters - but a reading."""
    from custom_components.wemportal.sensor import _readable_schedule

    row = _measured_monday()
    del row["CircuitTimesDay"]

    assert _readable_schedule(row)["MO"] == [
        "00:00-06:00 (H)",
        "06:00-10:10 (L)",
        "13:30-24:00 (H)",
    ]


def test_the_readable_week_needs_the_value_to_name_its_days():
    """Why the freshness guard, not the sensor, is where this is solved.

    The device's own view carries stretches per day NUMBER; which name each
    number has is read from the window keys of the JSON payload, so that the
    weekday names stay the portal's own and no language-specific table lives
    here. Without the value there are therefore no day names and no week to
    show - keeping the value is the only fix, and mapper._clear_unanswered is
    where that is done.
    """
    row = _measured_monday()
    row["value"] = None

    assert _sensor_from_row("Programm", row).native_value is None


def test_a_week_that_does_not_line_up_falls_back_instead_of_mislabelling():
    """Filing a day's programme under the wrong heading is worse than showing
    the poorer view, so anything but a full seven days hands over."""
    from custom_components.wemportal.sensor import _readable_schedule

    row = _measured_monday()
    row["value"] = '{"MO-1":"00:00-24:00","MO":"H"}'

    assert _readable_schedule(row) == {"MO": ["00:00-24:00 (H)"]}


def test_a_week_too_long_for_a_state_falls_back_to_the_word():
    """Home Assistant refuses a state over 255 characters, and a refused
    state is no reading at all. Seven days that all differ, three windows
    each, get there."""
    from homeassistant.const import MAX_LENGTH_STATE_STATE

    from custom_components.wemportal.sensor import _schedule_summary

    payload = {}
    for index, day in enumerate(DAYS):
        for slot in (1, 2, 3):
            hour = index * 3 + slot
            payload[f"{day}-{slot}"] = f"{hour:02d}:00-{hour:02d}:30"
    raw = json.dumps(payload)

    # Asserted, not assumed: if the summary ever gets shorter than the limit
    # this test would quietly stop exercising the fallback at all.
    assert len(_schedule_summary(_row(raw))) > MAX_LENGTH_STATE_STATE

    assert _schedule_sensor(raw).native_value == "Programmed"


def test_an_unused_slot_is_left_out():
    """Three fixed slots a day, mostly empty - keeping them would bury the
    two or three periods that are actually set."""
    from custom_components.wemportal.sensor import _readable_schedule

    assert (
        _readable_schedule(_row('{"MO-1":"00:00-00:00","MO-2":"00:00-00:00"}')) is None
    )


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '"text"', "{}", ""])
def test_something_that_is_not_a_programme_adds_no_attribute(raw):
    """Attributes are decoration: getting one wrong must never cost the
    reading itself."""
    from custom_components.wemportal.sensor import _readable_schedule

    assert _readable_schedule(_row(raw)) is None


def test_a_refused_write_says_what_the_portal_answered():
    """Home Assistant shows a failed service call as str(exception) and
    nothing else. "Error changing parameter X value" therefore discarded the
    portal's own words at exactly the moment somebody wanted them - a
    rejected holiday date read precisely that, while "Status -1: Unbekannter
    Fehler" sat one exception deeper.
    """
    api = _api()
    api.session = object()

    def refuse(*_args, **_kwargs):
        raise exceptions.WemPortalError(
            "Server returned status code: -1 and message: Unbekannter Fehler"
        )

    api.make_api_call = refuse

    with pytest.raises(exceptions.ParameterChangeError) as excinfo:
        api.change_value("1234", "U_Beginn", 1, 2, 1785801600.0)

    message = str(excinfo.value)
    assert "U_Beginn" in message
    assert "Unbekannter Fehler" in message, (
        "the portal's own answer was dropped from the error the user sees"
    )


def _write_recorder(api):
    """Capture the payloads a write puts on the wire."""
    sent = []

    def make_api_call(url, data=None, **_kwargs):
        sent.append(data)
        return FakeResponse({"Status": 0, "JobID": 1})

    api.session = object()
    api.make_api_call = make_api_call
    return sent


def test_companion_parameters_travel_in_the_same_request():
    """A holiday is a range, and the portal refuses one half of it.

    Written one date at a time, each write comes back Status -1 with no
    JobID, while an ordinary setpoint on the same account and the same
    endpoint is accepted. The portal's payload is a list of parameters per
    module, so the pair fits in one request - which is what the app is
    assumed to send.
    """
    api = _api()
    sent = _write_recorder(api)

    api.change_value(
        "1234",
        "U_Ende",
        1,
        2,
        1785974400.0,
        together_with={"U_Beginn": 1785715200.0},
    )

    assert len(sent) == 1, "the pair went out as two separate writes"
    module = sent[0]["Modules"][0]
    assert (module["ModuleIndex"], module["ModuleType"]) == (1, 2)
    assert {
        parameter["ParameterID"]: parameter["NumericValue"]
        for parameter in module["Parameters"]
    } == {
        "U_Beginn": 1785715200.0,
        "U_Ende": 1785974400.0,
    }


def test_the_parameter_being_changed_wins_over_a_companion():
    """The companions carry CURRENT values. One of them repeating the
    parameter under change would otherwise write the old value back over the
    new one, and the entity would show a day the portal never took."""
    api = _api()
    sent = _write_recorder(api)

    api.change_value(
        "1234",
        "U_Ende",
        1,
        2,
        1785974400.0,
        together_with={"U_Ende": 1785801600.0},
    )

    parameters = sent[0]["Modules"][0]["Parameters"]
    assert [parameter["NumericValue"] for parameter in parameters] == [1785974400.0]


def test_a_write_without_companions_is_unchanged():
    """Number, Select and Switch send nothing along, and their payload must
    look exactly as it did."""
    api = _api()
    sent = _write_recorder(api)

    api.change_value("1234", "P1", 0, 1, 21.0)

    assert sent[0] == {
        "DeviceID": 1234,
        "Modules": [
            {
                "ModuleIndex": 0,
                "ModuleType": 1,
                "Parameters": [{"ParameterID": "P1", "NumericValue": 21.0}],
            }
        ],
    }
