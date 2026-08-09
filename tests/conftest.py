"""Shared pytest configuration for the CI suite.

Declares the Home Assistant custom-component test plugin (which provides the
`hass` fixture and installs a matching Home Assistant), and centralises the
`time.sleep` mock so the integration's real server-load pacing sleeps never
run in tests. Importing the modules here also makes collection fail loudly
if the integration cannot be imported against the installed HA version.
"""

import pytest

from custom_components.wemportal import expert_writer, wemportalapi

pytest_plugins = ("pytest_homeassistant_custom_component",)


def pytest_addoption(parser):
    """--update-golden rewrites the recorded mapper output.

    Deliberately an explicit flag rather than "write the file if it is
    missing": a snapshot that regenerates itself compares against whatever
    the code happens to do today and can never fail.
    """
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite the recorded mapper snapshot instead of comparing to it",
    )


@pytest.fixture(autouse=True)
def _no_real_portal(monkeypatch):
    """Nothing in the suite may reach wemportal.com. Global on purpose.

    The expert client uses curl_cffi, which pytest's socket guard does not
    cover, so an expert path reached by accident really did perform a failed
    login against a live third-party service - on every run, and plausibly a
    contributor to a 403 that took hours to diagnose.

    This lived as a module-local fixture in test_e2e.py, which meant it
    protected exactly one file: a new test module would have had no guard at
    all, and `_mock_sleep` below would have removed the pacing between the
    requests it then made. A protection whose absence is silent belongs where
    every module gets it.

    Blocks the TRANSPORT rather than the client's methods: several of those
    methods are themselves under test, and stubbing them out would disable
    the logic instead of the traffic. A test that installs its own fake
    session on the instance is unaffected.
    """
    from custom_components.wemportal import scraper

    class _NoNetworkSession:
        """Looks like a session, refuses to be one."""

        def __init__(self, *_args, **_kwargs):
            self.cookies = {}
            self.headers = {}

        def _refuse(self, *_args, **_kwargs):
            raise AssertionError(
                "a test reached the real portal - install a fake session or "
                "stub the client method instead"
            )

        get = post = _refuse

        def close(self):
            pass

    for module in (expert_writer, scraper):
        monkeypatch.setattr(module.requests, "Session", _NoNetworkSession)
    yield


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Neutralise real time.sleep() in the modules that pace server load.

    The production code deliberately sleeps between portal requests; in tests
    those waits must be instant. Mocking centrally (not per test) keeps later
    tests that hit the same code paths fast too.
    """
    monkeypatch.setattr(wemportalapi.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(expert_writer.time, "sleep", lambda *_args, **_kwargs: None)
    yield


@pytest.fixture(autouse=True)
def _no_leftover_account_memory():
    """The IP backoff and the per-account state outlive the test that set
    them. Production wants exactly that - a fresh api object must not forget
    a rate limit, and a reload must not repeat every warning. A test run must
    not inherit either: without this, the first test to earn a 403 makes
    every later test's request raise ForbiddenError before it is even sent,
    and the first warning swallows its siblings in every later test.
    """
    from custom_components.wemportal import models

    wemportalapi.reset_cooldowns_for_tests()
    models.reset_account_states_for_tests()
    yield
    wemportalapi.reset_cooldowns_for_tests()
    models.reset_account_states_for_tests()
