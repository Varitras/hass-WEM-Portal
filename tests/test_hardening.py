"""Robustness fixes: api_login error handling and timeout, discovery-cache
survival on a failed device refresh, and str-normalisation of device ids.
"""

import json
import time

import pytest
import requests as real_requests

from custom_components.wemportal import wemportalapi
from custom_components.wemportal.wemportalapi import WemPortalApi
from custom_components.wemportal import exceptions


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, url="https://www.wemportal.com/app/x",
                 content=None):
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

    def update(self, *_a, **_k):
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


def test_api_login_network_error_raises_clean_auth_error(monkeypatch):
    """A pure network failure (no response yet) surfaces as UnknownAuthError,
    not an UnboundLocalError in the handler."""
    api = _api()
    session = RecordingSession(post_exc=real_requests.exceptions.ConnectionError("reset"))
    monkeypatch.setattr(wemportalapi.reqs, "Session", lambda: session)
    with pytest.raises(exceptions.UnknownAuthError):
        api.api_login()
    assert api.valid_login is False


def test_api_login_post_has_timeout(monkeypatch):
    api = _api()
    session = RecordingSession()
    monkeypatch.setattr(wemportalapi.reqs, "Session", lambda: session)
    api.api_login()
    assert api.valid_login is True
    assert session.post_kwargs.get("timeout") == wemportalapi.API_REQUEST_TIMEOUT_SECONDS


CACHED_MODULES = {
    "1234": {
        (0, 1): {"Index": 0, "Type": 1, "Name": "Heat pump", "parameters": {"P1": {"ParameterID": "P1"}}}
    }
}


def test_get_devices_failure_keeps_cache():
    """A failing device-list call must not wipe the discovery cache."""
    api = _api(cached_modules=CACHED_MODULES, existing_data={"1234": {"k": "v"}})

    def boom(*_a, **_k):
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
            {"ID": 1234, "ConnectionStatus": 0, "Modules": [{"Index": 0, "Type": 1, "Name": "Heat pump"}]}
        ]
    }
    api.make_api_call = lambda *a, **k: FakeResponse(device_json)
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
    api.make_api_call = lambda url, **k: calls.append(url) or FakeResponse({"GroupTypeDescriptions": []})
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
        "user@example.org", "secret",
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

    def make_api_call(url, **_k):
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
    api.make_api_call = lambda *a, **k: FakeResponse(
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
    api.make_api_call = lambda url, **k: calls.append(url) or FakeResponse(
        {"ConnectionStatus": 50, "Errors": [], "GroupTypeDescriptions": []}
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
    api.make_api_call = lambda url, **k: calls.append(url) or FakeResponse(
        {"ConnectionStatus": 0, "Errors": [], "Modules": [], "GroupTypeDescriptions": []}
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
        "user@example.org", "secret",
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

    def boom(*_a, **_k):
        raise exceptions.WemPortalError("portal down")

    api._fetch_data = boom
    with pytest.raises(exceptions.WemPortalError):
        api.fetch_data()

    # Free again: acquiring must succeed immediately.
    assert api._api_lock.acquire(blocking=False)
    api._api_lock.release()


def _web_api(mode, scraped=None):
    api = _api(config={"mode": mode}, scraper_device_id="0000")
    api.modules = {}
    api._scrape_calls = []

    def fake_scrape():
        api._scrape_calls.append(True)
        return scraped if scraped is not None else [{"cookie": {}}]

    api.fetch_webscraping_data = fake_scrape
    api._merge_webscraping_data = lambda *_a, **_k: None
    api.get_devices = lambda *_a, **_k: None
    api.get_data = lambda *_a, **_k: None
    api.get_statistics = lambda *_a, **_k: None
    api.get_parameters = lambda *_a, **_k: None
    api.api_login = lambda *_a, **_k: None
    api.web_login = lambda *_a, **_k: None
    api._devices_fetched_this_session = True
    api.modules = {"0000": {}}
    return api


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


def test_service_texts_exist_in_every_translation_file():
    """Home Assistant reads service name/description from strings.json, not
    services.yaml. A key missing in one file shows up only as untranslated
    text in the UI, so check all three - including the privacy warning on
    the entityvalue field, which must not get lost in translation."""
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "custom_components" / "wemportal"
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        data = json.loads((base / name).read_text(encoding="utf-8"))
        service = data["services"]["set_expert_parameter"]
        assert service["name"] and service["description"], name
        fields = service["fields"]
        assert set(fields) == {"entityvalue", "value"}, name
        for field in fields.values():
            assert field["name"] and field["description"], name
        # The entityvalue is installation-specific; the warning is part of
        # the contract with the user, not decoration.
        warning = fields["entityvalue"]["description"].lower()
        assert "not share" in warning or "nicht öffentlich" in warning, name


def test_portal_units_are_normalised_to_home_assistant_spelling():
    """A device class is not enough - the UNIT must match what Home Assistant
    accepts for it. The portal writes "BAR"; HA logs a warning for every
    reading unless it is "bar"."""
    from custom_components.wemportal.utils import fix_value_and_uom, uom_to_device_class

    value, unit = fix_value_and_uom(2.5, "BAR")

    assert (value, unit) == (2.5, "bar")
    assert uom_to_device_class(unit) == "pressure"


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

        def get(self, *_a, **_k):
            return FakeResponse_html(MAINTENANCE_PAGE)

        def post(self, *_a, **_k):
            posted.append(True)
            return FakeResponse_html("")

    monkeypatch.setattr(wemportalapi.reqs, "Session", lambda: _Session())
    api = _api()

    with pytest.raises(exceptions.PortalMaintenanceError):
        api.web_login()

    assert posted == [], "credentials were sent to the maintenance page"


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

    def make_api_call(url, data=None, **_k):
        calls.append((url, data))
        if url == wemportalapi.API_REFRESH_URL:
            return FakeResponse({"Status": 0, "JobID": 100000001})
        return FakeResponse({"Modules": []})

    api.make_api_call = make_api_call
    api._fetch_parameter_values("1234")

    read = next(d for url, d in calls if url == wemportalapi.API_DATA_ACCESS_READ_URL)
    assert read["JobID"] == 100000001
    # The refresh itself must NOT carry a JobID - it is what creates one.
    refresh = next(d for url, d in calls if url == wemportalapi.API_REFRESH_URL)
    assert "JobID" not in refresh


def test_data_read_omits_the_job_id_when_refresh_returns_none():
    """A response without a JobID must behave exactly as before rather than
    sending "JobID": null."""
    api = _api()
    api.modules = {"1234": {(1, 2): {"Index": 1, "Type": 2, "parameters": {"P1": {}}}}}
    calls = []

    def make_api_call(url, data=None, **_k):
        calls.append((url, data))
        return FakeResponse({"Modules": []} if "Read" in url else {"Status": 0})

    api.make_api_call = make_api_call
    api._fetch_parameter_values("1234")

    read = next(d for url, d in calls if url == wemportalapi.API_DATA_ACCESS_READ_URL)
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
            {"ID": 1234, "DeviceType": 2, "ConnectionStatus": 0,
             "Modules": [{"Index": 0, "Type": 1, "Name": "Heat pump"}]}
        ]
    }
    api.make_api_call = lambda *a, **k: FakeResponse(device_json)

    api.get_devices()

    assert api.device_types == {"1234": 2}
    assert "DeviceType" not in api.data["1234"]


def test_web_mode_validation_does_not_accept_a_config_that_cannot_poll(monkeypatch):
    """`api` and `both` call api_login() on every cycle, so validating them
    with a web login accepted a configuration that could never poll: setup
    succeeded, then every update failed with an API login the user was never
    told about."""
    import asyncio

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
        async def async_add_executor_job(func, *args):
            return func(*args)

    data = {"username": "user@example.org", "password": "secret", CONF_MODE: "both"}
    with pytest.raises(config_flow.InvalidAuth):
        asyncio.run(config_flow.validate_input(_Hass(), data))

    assert tried == ["api"], "a failed API login must not fall back to web"


def _offline_api(status):
    """An api whose single device reports `status` on every call."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.make_api_call = lambda *a, **k: FakeResponse(
        {"ConnectionStatus": status, "Errors": [], "GroupTypeDescriptions": []}
    )
    return api


@pytest.mark.parametrize(
    ("status", "expected"), [(50, "offline"), (7, "wrong_secret"), (8, "busy"), (99, "unknown")]
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
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, f"repeated every cycle: {warnings}"
        assert "offline" in warnings[0]

        caplog.clear()
        api.make_api_call = lambda *a, **k: FakeResponse(
            {"ConnectionStatus": 0, "Errors": [], "Modules": [],
             "GroupTypeDescriptions": []}
        )
        api.get_data(enabled_devices=["1234"])

    assert any(
        "back online" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    ), "recovery went unmentioned"


def _switch(value):
    """A switch entity built from one reading, without Home Assistant."""
    import types

    from custom_components.wemportal.switch import WemPortalSwitch

    coordinator = types.SimpleNamespace(
        data={"1234": {}}, api=_api(), last_update_success=True,
        async_add_listener=lambda *_a, **_k: None,
    )
    entry = types.SimpleNamespace(entry_id="e1")
    return WemPortalSwitch(
        coordinator, entry, "1234", "Pump",
        {"value": value, "unit": None, "friendlyName": "Pump",
         "ParameterID": "P1", "ModuleIndex": 0, "ModuleType": 1},
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
    return {k: {"value": 1, "unit": "°C", "platform": "sensor"} for k in keys}


def test_a_renamed_scraper_row_is_reported(caplog):
    """Scraped sensors are keyed by their portal labels - there is no stable
    id to use instead, since the row's entityvalue embeds the current VALUE
    and changes with every reading. A relabelled row therefore becomes a NEW
    entity and the history stays with the old one. Nothing can prevent that,
    but it must not happen silently."""
    import logging

    api = _api()
    api._merge_webscraping_data("0000", _scraped("pump-flow", "pump-return"))

    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-flow", "pump-return-temp"))

    assert "renamed" in caplog.text
    assert "pump-return" in caplog.text
    assert "pump-return-temp" in caplog.text


def test_a_stable_scrape_says_nothing(caplog):
    """No warning on an unchanged cycle, nor on the very first one - there is
    nothing to compare a first scrape against."""
    import logging

    api = _api()
    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-flow"))
        api._merge_webscraping_data("0000", _scraped("pump-flow"))

    assert "renamed" not in caplog.text


def test_a_purely_added_row_is_not_a_rename(caplog):
    """A genuinely new parameter is not a rename, and saying so would train
    the user to ignore the message."""
    import logging

    api = _api()
    api._merge_webscraping_data("0000", _scraped("pump-flow"))

    with caplog.at_level(logging.WARNING):
        api._merge_webscraping_data("0000", _scraped("pump-flow", "pump-new"))

    assert "renamed" not in caplog.text


def test_the_service_value_field_does_not_impose_a_percent_range():
    """Expert parameters are not all percentages - temperatures, times and
    curves are among them, and the data model knows half steps
    (NUMBER_STEP_HALF), which a step of 1 silently blocked. The real check is
    on write, against the option list the device itself offers."""
    import yaml
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "custom_components" / "wemportal"
    spec = yaml.safe_load((p / "services.yaml").read_text(encoding="utf-8"))
    number = spec["set_expert_parameter"]["fields"]["value"]["selector"]["number"]

    assert "min" not in number and "max" not in number
    assert number.get("step") == "any", "a step of 1 rules out half-step values"


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
            {CONF_SCAN_INTERVAL_API: 1}, CONF_SCAN_INTERVAL_API,
            300, MIN_SCAN_INTERVAL_API_SECONDS,
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
        "user@example.org", "secret",
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
    api.make_api_call = lambda *a, **k: FakeResponse(
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
    api.make_api_call = lambda *a, **k: FakeResponse({"Modules": []})

    assert api._fetch_parameter_values("1234") is False


def test_a_device_without_modules_is_not_turned_into_a_failure():
    """The guard keys on what was REQUESTED, so a device that genuinely has
    no modules must not start failing every cycle."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {}}
    api.make_api_call = lambda *a, **k: FakeResponse({"Modules": []})

    assert api._fetch_parameter_values("1234") is True


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
    api.make_api_call = lambda *a, **k: _BodyResponse(b"<html>Service unavailable</html>")

    with pytest.raises(exceptions.ParameterChangeError):
        api.change_value("1234", "P1", 0, 1, 21.0, login=False)


def test_an_empty_write_response_is_no_longer_taken_for_success():
    """This used to pass, on the assumption that a bare acknowledgement is
    how the portal confirms a write.

    The assumption was never verified, and the real response settles it: a
    successful write answers with a body carrying Status 0. An empty one is
    therefore not a confirmation of anything.
    """
    api = _api()
    api.make_api_call = lambda *a, **k: _BodyResponse(b"")

    with pytest.raises(exceptions.ParameterChangeError):
        api.change_value("1234", "P1", 0, 1, 21.0, login=False)


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
    api.get_data = lambda *_a, **_k: None

    api._fetch_data(enabled_devices=None)

    assert scrapes == [], "the scraper ran while its backoff was active"
    assert api.spider_wait_interval == 2, "the backoff must count down instead"


# --- an unloaded entry must not keep writing to the portal ------------


def _write_entity(api, monkeypatch):
    """An expert entity whose executor runs inline and whose portal client
    records that it was constructed at all."""
    import types

    from custom_components.wemportal import expert_writer

    entity = _expert_entity(api)
    built = []

    class _Client:
        def __init__(self, *_a, **_k):
            built.append(True)

        def write_parameter(self, *_a, **_k):
            return types.SimpleNamespace(current=21.0, min_value=None, max_value=None)

    monkeypatch.setattr(expert_writer, "WemPortalExpertClient", _Client)

    async def run_inline(func, *args):
        return func(*args)

    entity.hass.async_add_executor_job = run_inline
    entity.hass.async_create_task = lambda coro: coro.close()
    entity.async_write_ha_state = lambda: None
    return entity, built


def test_a_removed_entity_does_not_open_a_portal_session(monkeypatch):
    """Cancelling the task cannot stop the write.

    The portal call runs in an executor thread, and cancelling cancels the
    AWAIT, not the thread - so a write kept going against the portal with the
    credentials of an entry that was being torn down. Python cannot kill a
    thread; what it can do is refuse to START, which is exactly the case that
    matters (teardown races the thread pool).
    """
    import asyncio

    api = _api()
    entity, built = _write_entity(api, monkeypatch)
    entity._removed = True

    asyncio.run(entity._async_write_in_background(21.0))

    assert built == [], "a write opened a portal session after removal"
    assert entity._write_in_progress is False


def test_a_normal_write_still_reaches_the_portal(monkeypatch):
    """The guard must not disable writing altogether."""
    import asyncio

    api = _api()
    entity, built = _write_entity(api, monkeypatch)

    asyncio.run(entity._async_write_in_background(21.0))

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

    def make_api_call(url, **_k):
        urls.append(url)
        return FakeResponse({"Status": 3, "Message": "refresh refused"})

    api.make_api_call = make_api_call

    assert api._fetch_parameter_values("1234") is False
    assert len(urls) == 1, "the read must not happen after a refused refresh"


def test_a_refresh_without_a_status_field_still_works():
    """Not every response carries Status; absence must not fail the cycle."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1, "parameters": {"P1": {}}}}}
    api.make_api_call = lambda *a, **k: FakeResponse(
        {"Modules": [{"ModuleIndex": 0, "ModuleType": 1, "Values": []}]}
    )

    assert api._fetch_parameter_values("1234") is True


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
        "user@example.org", "secret", abort_check=gate,
    )
    session = _RecordingSession()
    client.session = session
    client._login = lambda: None
    client.close = lambda: None

    def fetch_form(*_a, **_k):
        # The unload happens WHILE the form is being read, i.e. after every
        # gate except the last one.
        unloaded.append(True)
        return expert_writer.ExpertParameterState(20.0, [20.0, 21.0], {})

    client._fetch_form = fetch_form

    with pytest.raises(expert_writer.ExpertOperationAborted):
        client.write_parameter("A" * 36, 21.0)

    assert session.posts == [], "the parameter was written after the unload"


def test_a_write_without_an_abort_still_goes_through():
    """The gate must not block ordinary writes - without it the test above
    would pass on a client that never writes anything at all."""
    from custom_components.wemportal import expert_writer

    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")
    session = _RecordingSession()
    client.session = session
    client._login = lambda: None
    client.close = lambda: None
    client._fetch_form = lambda *a, **k: expert_writer.ExpertParameterState(
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

    def make_api_call(url, **_k):
        urls.append(url)
        return _BodyResponse(b"<html>gateway timeout</html>")

    api.make_api_call = make_api_call

    assert api._fetch_parameter_values("1234") is False
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
    "JobID": 762338890, "Status": 0, "Message": None, "DetailMessages": None,
}


def test_the_real_success_response_is_accepted():
    """Guarding against the rule that ALMOST got written.

    The only available reference documents `Message` as the failure field, so
    "a Message means it failed" looked reasonable - but the real success
    response carries Message: null. That rule would have failed every single
    legitimate write.
    """
    api = _api()
    api.make_api_call = lambda *a, **k: FakeResponse(REAL_WRITE_SUCCESS)

    api.change_value("1234", "P1", 0, 1, 21.0, login=False)


@pytest.mark.parametrize(
    "payload",
    [
        {"Message": "write failed"},          # error shape, no Status
        {"Status": None},                     # present but says nothing
        {"Status": 3, "Message": "rejected"},  # explicit rejection
        {"JobID": 1},                          # a result, but not a verdict
        {"Status": False},                     # Python says False == 0. The portal does not.
    ],
)
def test_anything_but_an_explicit_success_is_a_rejection(payload):
    """Success carries Status: 0, so nothing else may pass for one.

    Reporting a write as done when it was not is the worse error: it is a
    heating parameter, and the entity shows the requested value until the
    next poll quietly replaces it.
    """
    api = _api()
    api.make_api_call = lambda *a, **k: FakeResponse(payload)

    with pytest.raises(exceptions.ParameterChangeError):
        api.change_value("1234", "P1", 0, 1, 21.0, login=False)


def test_the_rejection_message_carries_the_portal_reason():
    """DetailMessages/Message is what the portal says went wrong - dropping
    it leaves the user with a bare number."""
    api = _api()
    api.make_api_call = lambda *a, **k: FakeResponse(
        {"Status": 3, "Message": "value out of range"}
    )

    with pytest.raises(exceptions.ParameterChangeError) as excinfo:
        api.change_value("1234", "P1", 0, 1, 21.0, login=False)

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
    api.make_api_call = lambda *a, **k: FakeResponse(
        {"Status": 3, "Message": "value out of range"}
    )

    with caplog.at_level(logging.WARNING), pytest.raises(exceptions.ParameterChangeError):
        api.change_value("1234", "P1", 0, 1, 21.0, login=False)

    warnings = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "value out of range" in warnings


def test_a_successful_write_records_the_answer_at_debug(caplog):
    """The success shape matters too: it is the reference the check is built
    on, so a change to it has to be visible."""
    import logging

    api = _api()
    api.make_api_call = lambda *a, **k: FakeResponse(REAL_WRITE_SUCCESS)

    with caplog.at_level(logging.DEBUG):
        api.change_value("1234", "P1", 0, 1, 21.0, login=False)

    assert "Write response for P1" in caplog.text
    # And nothing about the write ends up at warning level.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


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

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"{len(warnings)} reports for one request site"


def test_where_the_check_is_enabled_it_still_raises():
    """The probe must not have replaced the actual detection."""
    scraper = _gate_probe()

    with pytest.raises(exceptions.PortalMaintenanceError):
        scraper._check_response(
            _Page(MAINTENANCE_HTML), "login page", check_maintenance=True
        )


def test_a_healthy_page_is_silent(caplog):
    """No marker, no noise - otherwise the signal would be worthless."""
    import logging

    scraper = _gate_probe()

    with caplog.at_level(logging.WARNING):
        scraper._check_response(_Page("<html><body>fine</body></html>"), "main page")

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


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
TRANSPORT_FIELDS = frozenset({
    "session",
    "_scraper",
    "valid_login",
    "api_version",
    "webscraping_cookie",
    "_devices_fetched_this_session",
})

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
PRESERVED_FIELDS = frozenset({
    "_first_cycle_done",
    "data", "username", "password", "scraper_device_id", "modules", "mode",
    "update_interval", "scan_interval", "scan_interval_api", "language",
    "_api_lock", "headers", "device_types", "_previous_scraper_keys",
    "_last_connection_status", "scraping_mapper", "last_statistics_fetch",
    "_last_circuit_times_fetch",
    "expert_cookies", "spider_wait_interval", "spider_retry_count",
    "last_scraping_update",
})


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
    monkeypatch.setattr(wemportalapi.reqs, "Session", lambda: session)

    with pytest.raises(exceptions.AuthError):
        api.api_login()
    assert api.valid_login is False


def test_a_refresh_answering_false_is_a_refusal():
    """Same field, same verdict, on the third of the three sites."""
    api = _api()
    api.modules = {"1234": {(1, 2): {"Index": 1, "Type": 2, "parameters": {"P1": {}}}}}
    api.make_api_call = lambda url, **_k: FakeResponse(
        {"Status": False} if "Refresh" in url else {"Modules": []}
    )

    assert api._fetch_parameter_values("1234") is False


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
    api.make_api_call = lambda url, **_k: FakeResponse(
        {"Modules": [{"ModuleIndex": 1, "ModuleType": 2, "Values": []}]}
        if "Read" in url else {"Status": 0}
    )

    with caplog.at_level(logging.WARNING):
        assert api._fetch_parameter_values("1234") is True
        assert api._fetch_parameter_values("1234") is True

    hits = [r for r in caplog.records if "without a JobID" in r.getMessage()]
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
    assert any("still in use" in r.getMessage() for r in caplog.records)


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
    import asyncio

    from homeassistant.exceptions import HomeAssistantError

    entity = _expert_entity(_api())
    entity._config_entry.runtime_data.unloading = True

    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(entity.async_set_native_value(21.0))

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

    with pytest.raises(exceptions.ForbiddenError):
        WemPortalApi("someone@example.org", "other").check_cooldown()


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

    with pytest.raises(AttributeError):
        utils.close_api_sessions(object())


# --- a heating schedule that fails must not be re-fetched every cycle ---


def _circuit_times_api(responses):
    """An api with one schedule parameter, answering from `responses`."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0, "Type": 1, "Name": "Heating circuit 1",
                "parameters": {"Heizprogramm1": {"ParameterID": "Heizprogramm1",
                                                 "DataType": 6}},
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
    api, calls = _circuit_times_api([exceptions.WemPortalError("portal unavailable")] * 10)

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
    api, calls = _circuit_times_api([exceptions.WemPortalError("nope")] * 10)

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
    api, calls = _circuit_times_api([
        {"JobID": 7},
        {"CircuitTimesDay": [], "PossibleValues": []},
    ])

    api._fetch_circuit_times("1234")
    stamp = api._last_circuit_times_fetch[("1234", "Heizprogramm1")]

    assert time.time() - stamp < 5, "a successful fetch was back-dated like a failure"


# --- a scraped reading that is gone must not be shown as current -------


def _scraped_row(value, unit="°C"):
    """One row as the scraper hands it over. Named apart from _scraped_row()
    above, which builds a whole scrape from key names - defining a second
    `_scraped` silently rebound the first for every test in this file."""
    return {"value": value, "unit": unit, "friendlyName": "Setpoint",
            "name": "wp-solltemperatur", "icon": None,
            "ParameterID": "wp-solltemperatur", "platform": "sensor"}


def test_a_scraped_row_without_a_value_clears_the_sensor():
    """Measured on a live installation: the portal renders "--" for a value
    it does not have, the scrape maps that to None - and the old value was
    carried over, so a setpoint read 50.5 degrees for three hours while the
    portal and the heat pump both showed nothing."""
    api = _api()
    api._merge_webscraping_data("0000", {"wp-solltemperatur": _scraped_row(50.5)})
    assert api.data["0000"]["wp-solltemperatur"]["value"] == 50.5

    api._merge_webscraping_data("0000", {"wp-solltemperatur": _scraped_row(None, unit="")})

    assert api.data["0000"]["wp-solltemperatur"]["value"] is None, (
        "a reading the portal no longer has was reported as current"
    )


def test_the_unit_is_still_carried_over():
    """The other half of the same block, and it must stay: a "--" row has no
    unit, and Home Assistant complains when one changes."""
    api = _api()
    api._merge_webscraping_data("0000", {"wp-solltemperatur": _scraped_row(50.5)})

    api._merge_webscraping_data("0000", {"wp-solltemperatur": _scraped_row(None, unit="")})

    assert api.data["0000"]["wp-solltemperatur"]["unit"] == "°C"


def test_a_row_that_stops_being_scraped_stops_showing_its_last_value():
    api = _api()
    api._merge_webscraping_data("0000", {
        "wp-solltemperatur": _scraped_row(50.5),
        "wp-vorlauf": _scraped_row(31.0),
    })

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
    api.make_api_call = lambda url, **_k: calls.append(url) or FakeResponse({})

    with caplog.at_level(logging.WARNING):
        refreshed = api._fetch_parameter_values("1234")

    assert calls == [], "a read with an empty module list was sent anyway"
    assert refreshed is False, (
        "a device whose discovery produced nothing was counted as refreshed"
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
    api.make_api_call = lambda url, **_k: calls.append(url) or FakeResponse({})

    assert api._fetch_parameter_values("1234") is True
    assert calls == [], "a read with an empty module list was sent anyway"


def test_a_device_with_parameters_is_still_read():
    """The guard must not swallow the ordinary case."""
    api = _api()
    api.data = {"1234": {}}
    api.modules = {
        "1234": {
            (0, 1): {"Index": 0, "Type": 1, "Name": "Heat pump",
                     "parameters": {"AktRaumSoll": {"ParameterID": "AktRaumSoll"}}}
        }
    }
    calls = []

    def make_api_call(url, **_kwargs):
        calls.append(url)
        if url == wemportalapi.API_REFRESH_URL:
            return FakeResponse({"Status": 0, "JobID": 7})
        return FakeResponse({"Modules": [
            {"ModuleIndex": 0, "ModuleType": 1, "Values": [
                {"ParameterID": "AktRaumSoll", "NumericValue": 21.0, "Unit": "°C"}
            ]}
        ]})

    api.make_api_call = make_api_call

    assert api._fetch_parameter_values("1234") is True
    assert wemportalapi.API_REFRESH_URL in calls


# --- the parameter list is re-read, and a failed re-read keeps it -------


def _discovery_api(answers, fetched_at=None):
    """An api with one cached module, answering EventType/Read from `answers`."""
    api = _api()
    api.data = {"1234": {"ConnectionStatus": 0}}
    module = {"Index": 0, "Type": 1, "Name": "Heat pump",
              "parameters": {"Known": {"ParameterID": "Known"}}}
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
        fetched_at=time.time() - (wemportalapi.PARAMETER_REDISCOVERY_INTERVAL_SECONDS + 60),
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
    api, calls = _discovery_api([{"Parameters": []}, {"Parameters": []}],
                                fetched_at=stale)

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


def test_a_module_the_portal_rejects_is_still_dropped():
    """The other half: a 400 means the portal does not accept the module at
    all, which is not the same as describing it as empty."""
    import requests as real_requests

    rejected = exceptions.WemPortalError("bad request")
    response = FakeResponse({}, status_code=400)
    rejected.__cause__ = real_requests.exceptions.HTTPError(response=response)

    api, _calls = _discovery_api([rejected])
    del api.modules["1234"][(0, 1)]["parameters"]

    api.get_parameters()

    assert (0, 1) not in api.modules["1234"]


def test_get_devices_carries_the_parameter_timestamp_too():
    """It travels with the list it belongs to.

    Left behind, every session starts with the cache looking expired and
    re-reads every module on its first cycle - which is the portal load the
    interval exists to avoid, arriving on every restart instead.
    """
    fetched_at = time.time() - 60
    cached = {
        "1234": {
            (0, 1): {"Index": 0, "Type": 1, "Name": "Heat pump",
                     "parameters": {"P1": {"ParameterID": "P1"}},
                     "parameters_fetched_at": fetched_at},
        }
    }
    api = _api(cached_modules=cached)
    api.make_api_call = lambda *a, **k: FakeResponse({
        "Devices": [{"ID": 1234, "ConnectionStatus": 0,
                     "Modules": [{"Index": 0, "Type": 1, "Name": "Heat pump"}]}]
    })

    api.get_devices()

    assert api.modules["1234"][(0, 1)]["parameters_fetched_at"] == fetched_at


def _cycle_api(fetched_at):
    """An api whose one module has a parameter list of the given age."""
    api = _api()
    api.data = {"1234": {"ConnectionStatus": 0}}
    api.modules = {
        "1234": {(0, 1): {"Index": 0, "Type": 1, "Name": "Heat pump",
                          "parameters": {"Known": {"ParameterID": "Known"}},
                          "parameters_fetched_at": fetched_at}}
    }
    api._devices_fetched_this_session = True
    api.valid_login = True
    read = []
    api.get_parameters = lambda: read.append("read")
    api.get_data = lambda *_a, **_k: None
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

