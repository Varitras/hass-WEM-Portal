"""No test may quietly start taking minutes.

The check itself lives in conftest.py, because measuring a test run is
something only a pytest hook can do. Which is exactly why the second test
below exists: a hook that is never wired up fails in the one way this
repository keeps getting caught by - silently, with the suite green.
"""

import pathlib
import subprocess
import sys

from .durations import over_budget

REPO = pathlib.Path(__file__).resolve().parents[1]

# The inner run needs a real test to select. This file's first test is fast
# and has no fixtures, so the inner run is a pytest startup and little else.
A_FAST_TEST = "tests/test_durations.py::test_a_test_over_the_budget_is_reported"

# How long the inner pytest may take before something is wrong with it
# rather than with the code under test.
INNER_RUN_TIMEOUT_SECONDS = 300


def test_a_test_over_the_budget_is_reported():
    """The real numbers from the incident: one test at the lock timeout,
    the rest of the suite where tests belong."""
    late = over_budget(
        {
            "test_a_recovery_leaves_a_busy_connection_alone": 330.0,
            "test_update_timeout_is_counted_and_reported": 5.06,
            "test_config_flow_creates_entry": 0.87,
        },
        budget=30.0,
    )

    assert late == [("test_a_recovery_leaves_a_busy_connection_alone", 330.0)]
    # Worst first: the message is read from the top.
    assert over_budget({"slow": 40.0, "slower": 90.0}, budget=30.0) == [
        ("slower", 90.0),
        ("slow", 40.0),
    ]


def test_a_hung_test_is_cut_off_rather_than_only_measured():
    """The half this file cannot do.

    Everything else here measures a test AFTER it has returned - which a
    deadlock, or a patch that landed on a name nothing reads, never does. The
    run then goes to whatever limit CI enforces with no idea which test is
    stuck. pytest-timeout kills it and prints the stack, so the two are
    complements, not alternatives: this one says WHICH test hangs, the budget
    says which one got slow.

    Checked as configuration rather than by hanging a test on purpose - the
    cheapest way to prove that would take the kill limit to demonstrate it.
    """
    import configparser

    config = configparser.ConfigParser()
    config.read(REPO / "pytest.ini", encoding="utf-8")
    limit = config.getint("pytest", "timeout", fallback=None)

    assert limit is not None, "nothing cuts off a run that stops making progress"
    assert config.get("pytest", "timeout_method", fallback=None) == "thread", (
        "the default signal method does not fire for a wait inside a worker "
        "thread, which is where the waits this exists for live"
    )
    from .durations import SLOW_TEST_SECONDS

    assert limit > SLOW_TEST_SECONDS, (
        f"the kill limit ({limit}s) is at or below the reporting budget "
        f"({SLOW_TEST_SECONDS}s), so an ordinary slow test is failed rather "
        "than reported"
    )


def test_the_timeout_plugin_is_declared_where_each_run_installs_from():
    """A transitive dependency that everything relies on and nothing names.

    tests/test_e2e.py already carries `pytest.mark.timeout(120)`. If the
    plugin stops arriving through pytest-homeassistant-custom-component, that
    marker becomes a no-op that raises no error - the exact shape of failure
    this repository keeps getting caught by.
    """

    def _named_outside_a_comment(text, needle):
        """Lines that actually ASK for `needle`, not ones that mention it.

        Both files talk about this plugin in prose - the workflow explains in
        a comment why these jobs name it at all - so a substring search over
        the whole file is satisfied by the explanation of the thing rather
        than the thing. Same shape as a guard reading its own banner.
        """
        return [
            line
            for line in text.splitlines()
            if needle in line and not line.strip().startswith("#")
        ]

    requirements = (REPO / "requirements_test.txt").read_text(encoding="utf-8")
    assert _named_outside_a_comment(requirements, "pytest-timeout"), (
        "requirements_test.txt only mentions the plugin, it does not require it"
    )

    workflow = (REPO / ".github" / "workflows" / "test.yaml").read_text(
        encoding="utf-8"
    )
    installs = [
        line
        for line in _named_outside_a_comment(workflow, "pytest-timeout")
        if "pip install" in line
    ]
    assert installs, (
        "the matrix jobs do not install requirements_test.txt, so they have "
        "to name the plugin in a pip install of their own"
    )


def test_the_budget_is_wired_into_the_session_and_not_just_written_down():
    """A green run with an impossible budget must come back red.

    Run as a real subprocess against this repository, so what is proven is
    the actual conftest wiring - a copy of the hook in a temporary directory
    would prove that the copy works.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", A_FAST_TEST, "-q", "--slow-test-seconds=0"],
        cwd=REPO,
        capture_output=True,
        # Explicit, because the default on Windows is the ANSI codepage and
        # the mismatch shows up as mangled text rather than as an error.
        encoding="utf-8",
        errors="replace",
        timeout=INNER_RUN_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 1, (
        "a run where every test is over budget came back "
        f"{result.returncode}, so the budget decides nothing:\n{result.stdout}"
    )
    assert "SLOWER THAN THE BUDGET ALLOWS" in result.stdout
    assert A_FAST_TEST.split("::")[-1] in result.stdout
