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
