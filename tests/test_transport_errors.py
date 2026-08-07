"""A request that never reached the portal is not a server answer.

A live log showed a /DataAccess/Read give up after exactly 10.0s and report
itself as "Server returned status code:  and message: " - two empty fields
where a status and a message should be, because there was no response to read
them from. That reads like the portal answered and said nothing, and it failed
the whole cycle on the first try.

These tests pin both halves of the fix: the wording, and the single retry that
the three value-carrying requests are allowed and the other twelve are not.
"""

import ast
import pathlib

import pytest
import requests as real_requests

from custom_components.wemportal import wemportalapi
from custom_components.wemportal.wemportalapi import WemPortalApi
from custom_components.wemportal import exceptions

from .test_hardening import FakeResponse


API_SOURCE = pathlib.Path(wemportalapi.__file__).read_text(encoding="utf-8")

# The requests allowed a transport retry: the two READS on the value path.
#
# Two different reasons keep this set small, and only one of them is about
# the budget.
#
# Cost keeps statistics and heating schedules out. They are the bulk of an
# hourly cycle, optional, and stale for an hour at worst; letting them retry
# into a timeout as well is what pushes a bad cycle past the coordinator's
# limit.
#
# Safety keeps /Refresh out, and that is not a budget question at all: it
# starts a measurement job. Repeating it because its ANSWER was lost starts a
# SECOND job, and overlapping jobs serving each other's values is exactly what
# the JobID handling exists to prevent. /Refresh was in this set once - a lost
# refresh costs one cycle, a duplicated one can cost the next cycle its
# correctness, which is the worse trade.
VALUE_PATH_URLS = {
    "API_DEVICE_STATUS_READ_URL",
    "API_DATA_ACCESS_READ_URL",
}


class FlakySession:
    """A session whose first N attempts never reach the portal."""

    def __init__(self, failures, exc=None, final=None):
        self.remaining_failures = failures
        self.exc = exc or real_requests.exceptions.ConnectionError("reset by peer")
        self.final = final if final is not None else FakeResponse({"Status": 0})
        self.attempts = 0

    def _attempt(self):
        self.attempts += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise self.exc
        return self.final

    def get(self, url, **_kwargs):
        return self._attempt()

    def post(self, url, **_kwargs):
        return self._attempt()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """make_api_call sleeps a second before every attempt and again between
    them; none of that is what these tests are about."""
    monkeypatch.setattr(wemportalapi.time, "sleep", lambda _s: None)


def _api(session):
    api = WemPortalApi("user@example.org", "secret")
    api.session = session
    # A retry must not silently turn into a re-login; every test asserts on
    # this list rather than trusting the branch it thinks it took.
    api.logins = []
    api.api_login = lambda: api.logins.append(1)
    return api


def test_value_path_retries_a_request_that_never_arrived():
    session = FlakySession(failures=1)
    api = _api(session)

    response = api.make_api_call(
        "https://example.invalid/read", data={"x": 1}, retry_transport=True
    )

    assert response is session.final
    assert session.attempts == 2, "the failed attempt was not retried"


def test_the_retry_does_not_re_authenticate():
    """The session is fine, the network was not.

    Logging in again would spend an extra request at the worst possible
    moment - and throw away a session nothing is wrong with.
    """
    api = _api(FlakySession(failures=1))

    api.make_api_call(
        "https://example.invalid/read", data={"x": 1}, retry_transport=True
    )

    assert api.logins == []


def test_transport_retry_does_not_need_the_session_retry():
    """The two retries are independent, and asking for one must not silently
    require the other.

    No call site passes this combination today. It is a legitimate shape
    though - "the session is not worth re-establishing, but a hiccup is worth
    one more try" - and the way it would fail is the kind this repository
    keeps finding: the argument is accepted, and nothing happens.
    """
    session = FlakySession(failures=1)
    api = _api(session)

    api.make_api_call(
        "https://example.invalid/read",
        data={"x": 1},
        do_retry=False,
        retry_transport=True,
    )

    assert session.attempts == 2
    assert api.logins == []


def test_a_second_failure_gives_up_instead_of_looping():
    session = FlakySession(failures=99)
    api = _api(session)

    with pytest.raises(exceptions.WemPortalError):
        api.make_api_call(
            "https://example.invalid/read", data={"x": 1}, retry_transport=True
        )

    assert session.attempts == 2, "one retry, not a loop"


def test_requests_outside_the_value_path_are_not_retried():
    """do_retry covers an expired session and says nothing about the network.

    Every statistics and schedule call passes do_retry=True; if that alone
    started retrying timeouts, the worst-case cycle would go over budget.
    """
    session = FlakySession(failures=99)
    api = _api(session)

    with pytest.raises(exceptions.WemPortalError):
        api.make_api_call("https://example.invalid/stats", data={"x": 1}, do_retry=True)

    assert session.attempts == 1


def test_transport_failure_does_not_claim_a_server_answer():
    api = _api(FlakySession(failures=99))

    with pytest.raises(exceptions.WemPortalError) as excinfo:
        api.make_api_call("https://example.invalid/read", data={"x": 1}, do_retry=False)

    message = str(excinfo.value)
    assert "Server returned status code" not in message, (
        "a request that never arrived was reported as a server answer"
    )
    assert "Could not reach" in message
    assert "reset by peer" in message, "the actual cause was dropped"


def test_a_real_server_error_still_reports_the_server():
    """The other half of the branch: an answer we did get is still quoted."""
    session = FlakySession(
        failures=0,
        final=FakeResponse({"Status": 9, "Message": "nope"}, status_code=500),
    )
    api = _api(session)

    with pytest.raises(exceptions.WemPortalError) as excinfo:
        api.make_api_call("https://example.invalid/read", data={"x": 1}, do_retry=False)

    assert "Server returned status code: 9" in str(excinfo.value)
    assert excinfo.value.server_status == 9


def test_expired_session_still_re_authenticates_and_retries():
    """The pre-existing retry must keep its own behaviour: this one DOES
    re-login, which is exactly what the transport retry must not do."""
    session = FlakySession(
        failures=0,
        final=FakeResponse({}, url="https://www.wemportal.com/Account/Login"),
    )
    api = _api(session)

    with pytest.raises(exceptions.WemPortalError):
        api.make_api_call("https://example.invalid/read", data={"x": 1}, do_retry=True)

    assert api.logins == [1], "the expired-session retry stopped re-authenticating"
    assert session.attempts == 2


def _retry_transport_by_url():
    """Which URL constant each make_api_call site asks a transport retry for."""
    tree = ast.parse(API_SOURCE)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute) and function.attr == "make_api_call"
        ):
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            raise AssertionError(
                f"make_api_call at line {node.lineno} no longer passes its URL as a "
                "plain constant; this scan can no longer see what it asks for."
            )
        url_name = node.args[0].id
        opted_in = any(
            kw.arg == "retry_transport"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        )
        found[url_name] = found.get(url_name, False) or opted_in
    return found


def test_exactly_the_value_path_asks_for_a_transport_retry():
    """The budget is the reason this list is short, and the budget is invisible
    at the call site. Without this, adding retry_transport=True to statistics
    "for symmetry" is a one-word change that pushes the worst-case cycle past
    the coordinator's timeout, and nothing else would notice.
    """
    by_url = _retry_transport_by_url()

    assert set(by_url) >= VALUE_PATH_URLS, (
        f"expected value-path calls are gone: {VALUE_PATH_URLS - set(by_url)}"
    )
    opted_in = {url for url, yes in by_url.items() if yes}
    assert opted_in == VALUE_PATH_URLS, (
        f"transport retry is on the wrong set of calls: {opted_in}"
    )


def test_the_scan_would_notice_an_added_opt_in():
    """The guard above is only worth having if it can fail."""
    tree = ast.parse(
        "self.make_api_call(API_STATISTICS_READ_URL, data=d, retry_transport=True)"
    )
    call = tree.body[0].value
    assert any(kw.arg == "retry_transport" for kw in call.keywords)
