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
    api.make_api_call = lambda url, **k: calls.append(url) or FakeResponse(
        {"ConnectionStatus": 50, "Errors": [], "GroupTypeDescriptions": []}
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
