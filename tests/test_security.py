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
    entityvalue_digest,
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
    d1 = entityvalue_digest(ev)
    assert d1 == entityvalue_digest(ev)
    assert len(d1) == 16
    assert d1 not in ev and ev not in d1
    assert entityvalue_digest(f"  {ev}  ") == d1
    assert entityvalue_digest(ev[:-1] + "B") != d1


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
    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: FakeSession(response))

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(exceptions.AuthError) as excinfo,
    ):
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


def test_a_successful_login_does_not_log_the_username(monkeypatch, caplog):
    """Debug logs are exactly what people paste into an issue when asking for
    help, and there is one account per config entry - naming it adds nothing
    to the diagnosis.

    Drives the real login rather than emitting the message by hand: a test
    that logs its own line proves nothing about what the code does.
    """
    response = FakeResponse(200, {"Status": 0, "Version": "3.1.3.0"})
    api = WemPortalApi("user@example.org", "secret")
    monkeypatch.setattr(wemportalapi.requests, "Session", lambda: FakeSession(response))

    with caplog.at_level(logging.DEBUG):
        api.api_login()

    assert api.valid_login is True
    assert "login successful" in caplog.text.lower(), "the log line is gone entirely"
    assert "user@example.org" not in caplog.text


def test_no_test_can_reach_the_real_portal():
    """The suite's most expensive mistake, pinned so it cannot come back.

    The expert client uses curl_cffi, which pytest's socket guard does not
    cover, so an expert path reached by accident performed a real failed
    login against wemportal.com on every run - and the guard that stopped it
    lived in a single test module, protecting exactly that file.

    It is global now, and it blocks the transport rather than the client's
    methods, because several of those methods are themselves under test.
    """
    from custom_components.wemportal import expert_writer, scraper

    for module in (expert_writer, scraper):
        session = module.requests.Session()
        with pytest.raises(AssertionError, match="reached the real portal"):
            session.get("https://www.wemportal.com/Web/Login.aspx")
        with pytest.raises(AssertionError, match="reached the real portal"):
            session.post("https://www.wemportal.com/Web/Login.aspx")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.wemportal.com/(S(livesessiontoken))/Web/Default.aspx",
        # ASP.NET does not treat the token letter as case-sensitive.
        "https://www.wemportal.com/(s(livesessiontoken))/Web/Default.aspx",
        # Several tokens can share one segment.
        "https://www.wemportal.com/(A(x)S(livesessiontoken)F(y))/Web/Default.aspx",
    ],
)
def test_every_cookieless_session_form_is_redacted(url):
    """The regex matched only the single upper-case example from the docs.

    A cookieless session id is credential-equivalent - it is the session -
    so any form that ASP.NET actually emits has to go, not just the one that
    happened to be in front of us when the pattern was written.
    """
    from custom_components.wemportal import expert_writer

    redacted = expert_writer.redact_url(url)

    assert "livesessiontoken" not in redacted
    assert redacted.endswith("/Web/Default.aspx")


def test_no_module_imports_the_expert_client_at_module_level():
    """The lazy import has to be structural, or it quietly stops being lazy.

    expert_writer pulls curl_cffi and lxml at import time (~140 ms, measured).
    It was believed to stay out of the load path while expert access was off -
    and it did not: config_flow imported it at module level, and Home
    Assistant loads config_flow during a NORMAL entry setup. Measured before
    the split:

        config_flow imported - before setup: False, after setup: True

    A per-call check cannot catch that, and neither can sys.modules inside
    this suite: tests/conftest.py imports expert_writer itself, so it is
    always present. The property that IS checkable is structural - nobody
    reaches it without asking.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
    offenders = []
    for module in sorted(package.glob("*.py")):
        for node in ast.parse(module.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module == "expert_writer":
                offenders.append(f"{module.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                offenders += [
                    f"{module.name}:{node.lineno}"
                    for alias in node.names
                    if "expert_writer" in alias.name
                ]

    assert not offenders, (
        "expert_writer is imported at module level in "
        f"{offenders} - that loads curl_cffi for every installation. Import "
        "it inside the function that needs it; the pure option helpers live "
        "in expert_options.py precisely so this stays possible."
    )


# --- the documented limits have to be the enforced ones -----------------


def test_the_documented_slot_count_is_the_enforced_one():
    """The README and the service description both say "ten slots".

    A number written out in prose drifts silently: raising EXPERT_SLOT_COUNT
    leaves three texts claiming the old one, and the service description is
    what a user reads before deciding whether this feature fits.
    """
    import json
    from pathlib import Path

    from custom_components.wemportal.const import EXPERT_SLOT_COUNT

    spelled = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    word = spelled.get(EXPERT_SLOT_COUNT)
    assert word, (
        f"EXPERT_SLOT_COUNT is {EXPERT_SLOT_COUNT} and this test only knows "
        "how to spell up to ten - extend it together with the texts."
    )

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert f"{word.capitalize()} slots per account" in readme, (
        f"the README no longer says how many slots there are ({word})"
    )

    strings = json.loads(
        (
            root / "custom_components" / "wemportal" / "translations" / "en.json"
        ).read_text(encoding="utf-8")
    )
    description = strings["services"]["set_expert_parameter"]["description"]
    assert f"one of the {word} expert slots" in description, (
        "the service description no longer states the slot limit it enforces"
    )


def test_the_service_description_states_the_single_account_limit():
    """_resolve_expert_entry refuses when more than one account has expert
    write enabled, and that refusal is invisible until it happens."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    strings = json.loads(
        (
            root / "custom_components" / "wemportal" / "translations" / "en.json"
        ).read_text(encoding="utf-8")
    )
    description = strings["services"]["set_expert_parameter"]["description"]

    assert "more than one account" in description
    assert "One expert account at a time" in (
        (root / "README.md").read_text(encoding="utf-8")
    )
