"""Robustness fixes: api_login error handling and timeout, discovery-cache
survival on a failed device refresh, and str-normalisation of device ids.
"""

import time

import pytest
import requests as real_requests

from custom_components.wemportal import wemportalapi
from custom_components.wemportal.wemportalapi import WemPortalApi
from custom_components.wemportal import exceptions


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, url="https://www.wemportal.com/app/x"):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.url = url
        self.content = b""

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
    """An expert number entity wired to `api` through a fake hass store."""
    import types

    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.const import DOMAIN

    entry = types.SimpleNamespace(entry_id=entry_id, data={}, options={})
    entity = expert_writer.WemPortalExpertNumber(entry, "expert_parameter_3", "A" * 36)
    entity.hass = types.SimpleNamespace(data={DOMAIN: {entry_id: {"api": api}}})
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
    from custom_components.wemportal.const import DOMAIN

    entry = types.SimpleNamespace(entry_id="gone", data={}, options={})
    entity = expert_writer.WemPortalExpertNumber(entry, "slot", "B" * 36)
    entity.hass = types.SimpleNamespace(data={DOMAIN: {}})

    assert entity._cookie_jar() is None
    assert entity._cooldown_check() is None
    assert entity._cooldown_activate() is None


def test_api_swap_preserves_the_state_that_must_not_reset():
    """The coordinator re-instantiates the api after repeated errors. Every
    piece of state carried across that swap protects something: an active
    backoff, the cached session, the discovered modules and the stable
    scraper device id. None of it was covered by a test.
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


def test_an_empty_write_response_stays_acceptable():
    """A bare acknowledgement with no body is how the portal confirms some
    writes; only a body that is present but unparsable is the error case."""
    api = _api()
    api.make_api_call = lambda *a, **k: _BodyResponse(b"")

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
