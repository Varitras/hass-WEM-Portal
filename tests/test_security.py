"""Privacy/security hardening: entityvalue digests, shortened ids in error
text, and auth-error message hygiene.
"""

import logging

import pytest
import requests as real_requests

from custom_components.wemportal import wemportalapi
from custom_components.wemportal.wemportalapi import WemPortalApi
from custom_components.wemportal import exceptions
from custom_components.wemportal.expert_writer import (
    WemPortalExpertClient,
    ev_digest,
)


def test_invalid_entityvalue_error_hides_full_id():
    """A nearly-correct id must not appear (almost) in full in the error."""
    nearly_real = "3A7F91C2E0B48D5619F2A0C7B4E83D105C2" + "Z"  # non-hex tail
    with pytest.raises(ValueError) as excinfo:
        WemPortalExpertClient._validate_entityvalue(nearly_real)
    message = str(excinfo.value)
    assert nearly_real not in message
    assert nearly_real[:20] not in message
    assert "3A7F91" in message
    assert "…" in message


def test_valid_entityvalue_passes_unchanged():
    WemPortalExpertClient._validate_entityvalue("3A7F91C2E0B48D5619F2A0C7B4E83D105C2A")


def test_ev_digest_is_stable_and_opaque():
    ev = "3A7F91C2E0B48D5619F2A0C7B4E83D105C2A"
    d1 = ev_digest(ev)
    assert d1 == ev_digest(ev)
    assert len(d1) == 16
    assert d1 not in ev and ev not in d1
    assert ev_digest(f"  {ev}  ") == d1
    assert ev_digest(ev[:-1] + "B") != d1


class FakeResponse:
    def __init__(self, status_code, json_data, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.url = "https://www.wemportal.com/app/Account/Login"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.exceptions.HTTPError(response=self)


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.cookies = self
        self.headers = {}

    def clear(self):
        pass

    def update(self, *_a, **_k):
        pass

    def post(self, url, **kwargs):
        return self._response

    def close(self):
        pass


def test_login_error_message_excludes_response_body(monkeypatch, caplog):
    """400 on login keeps status fields but drops the raw body; and no
    warning-level log line contains the account email."""
    body = b"<html>SECRET-SERVER-PAGE</html>"
    response = FakeResponse(400, {"Status": 5, "Message": "bad credentials"}, body)
    api = WemPortalApi("user@example.org", "secret")
    monkeypatch.setattr(wemportalapi.reqs, "Session", lambda: FakeSession(response))

    with caplog.at_level(logging.WARNING), pytest.raises(exceptions.AuthError) as excinfo:
        api.api_login()

    message = str(excinfo.value)
    assert "SECRET-SERVER-PAGE" not in message
    assert "400" in message and "bad credentials" in message
    warning_text = " ".join(
        rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING
    )
    assert "user@example.org" not in warning_text


def test_forbidden_url_is_redacted_before_it_reaches_a_log_or_message():
    """A 403 must stay diagnosable without publishing the entityvalue.

    The parameter-dialog requests carry the full installation-specific ID in
    the query string, so logging `response.url` verbatim leaked it to the
    WARNING log, to persistent notifications and to service-call errors.
    """
    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.exceptions import ForbiddenError

    secret = "D" * 36
    url = (
        "https://www.wemportal.com/Web/UControls/Weishaupt/DataDisplay/"
        f"WwpsParameterDetails.aspx?entityvalue={secret}&readdata=True"
    )

    class _Resp:
        status_code = 403

    resp = _Resp()
    resp.url = url

    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")
    with pytest.raises(ForbiddenError) as excinfo:
        client._raise_if_forbidden(resp)

    assert secret not in str(excinfo.value)
    assert "entityvalue" not in str(excinfo.value)
    # The endpoint must survive - that is the whole point of naming the request.
    assert "WwpsParameterDetails.aspx" in str(excinfo.value)


def test_forbidden_url_drops_a_cookieless_session_id():
    """ASP.NET can put the session id in the PATH; that is credential-grade."""
    from custom_components.wemportal import expert_writer

    redacted = expert_writer.redact_url(
        "https://www.wemportal.com/(S(livesessiontoken))/Web/Default.aspx"
    )

    assert "livesessiontoken" not in redacted
    assert redacted.endswith("/Web/Default.aspx")
