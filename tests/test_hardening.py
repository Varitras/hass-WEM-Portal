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
