"""Shared pytest configuration for the CI suite.

Declares the Home Assistant custom-component test plugin (which provides the
`hass` fixture and installs a matching Home Assistant), and centralises the
`time.sleep` mock so the integration's real server-load pacing sleeps never
run in tests. Importing the modules here also makes collection fail loudly
if the integration cannot be imported against the installed HA version.

Also holds the runtime budget per test - see durations.py for the accident
that one exists for.
"""

import pytest

from custom_components.wemportal import expert_writer, wemportalapi

from .durations import SLOW_TEST_SECONDS, over_budget

pytest_plugins = ("pytest_homeassistant_custom_component",)

# Summed per test across setup, call and teardown, and read at the end of
# the session. A dict at module level because that is what a pytest hook
# has: the hooks are functions, not a fixture with somewhere to keep state.
_durations: dict = {}


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
    parser.addoption(
        "--slow-test-seconds",
        type=float,
        default=SLOW_TEST_SECONDS,
        help=(
            "fail the session if a single test takes longer than this "
            "(0 makes every test late, which is how the check is tested)"
        ),
    )


def pytest_runtest_logreport(report):
    """Add up what one test costs, fixtures included."""
    _durations[report.nodeid] = _durations.get(report.nodeid, 0.0) + report.duration


def pytest_sessionfinish(session, exitstatus):
    """Turn a green run red when a test ran far longer than it should.

    Only a green one: a failing suite has more urgent news, and a test that
    is slow *because* it failed is not the subject here.
    """
    if exitstatus != pytest.ExitCode.OK:
        return

    late = over_budget(_durations, session.config.getoption("--slow-test-seconds"))
    if not late:
        return

    listed = "\n  ".join(f"{seconds:7.2f}s {node_id}" for node_id, seconds in late)
    print(
        f"\nSLOWER THAN THE BUDGET ALLOWS:\n  {listed}\n\n"
        "A test in the minutes is nearly always a wait that was meant to be "
        "shortened and no longer is - check what the test patches against "
        "where the production code now reads it. If the time is genuinely "
        "warranted, raise SLOW_TEST_SECONDS in tests/durations.py and say "
        "in the commit why."
    )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


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
    """Neutralise real time.sleep() for the whole test process.

    The production code deliberately sleeps between portal requests; in tests
    those waits must be instant. Mocking centrally (not per test) keeps later
    tests that hit the same code paths fast too.

    Reaching only these two modules is not on offer, and the wording that
    suggested it cost a test: both attributes ARE the one `time` module, so
    this replaces time.sleep everywhere, for every test of the run. A test that
    needs one thing to happen after another therefore cannot get it from a
    sleep - it has to wait for the thing itself (tests/test_mutation_harness.py
    does, after its sleeps turned out to be returning instantly).
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
    from custom_components.wemportal import models, transport

    transport.reset_cooldowns_for_tests()
    models.reset_account_states_for_tests()
    yield
    transport.reset_cooldowns_for_tests()
    models.reset_account_states_for_tests()
