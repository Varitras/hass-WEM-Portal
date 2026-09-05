"""The CI matrix must test the Home Assistant releases it claims to test.

The "current HA" job installed the plugin unpinned, which reads like "newest
Home Assistant" but is not: the plugin ships a normal, non-prerelease version
for every Home Assistant BETA too, so pip resolved to one pinning
homeassistant==2026.8.0b3. Nothing then covered the release people actually
run, and the job stayed green while saying "current HA".

Only the selection logic is exercised here - no PyPI access, so this stays a
fast unit test rather than a network-dependent one.
"""

import importlib.util
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "resolve_phcc.py"


def _load():
    spec = importlib.util.spec_from_file_location("resolve_phcc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_phcc = _load()


@pytest.mark.parametrize(
    ("pin", "expected"),
    [
        ("homeassistant==2026.7.4", False),
        ("homeassistant==2026.8.0b3", True),
        ("homeassistant==2026.8.0b0", True),
        ("homeassistant==2026.8.0rc1", True),
        ("homeassistant==2026.8.0a1", True),
        # No pin at all: we cannot tell what would be installed, and the whole
        # point of the script is not having to guess.
        (None, True),
    ],
)
def test_a_prerelease_pin_is_recognised(pin, expected):
    assert resolve_phcc.is_prerelease(pin) is expected


def test_the_newest_final_release_wins_over_newer_betas():
    """The real situation at the time this was written: three plugin releases
    newer than the newest one pinning a final Home Assistant."""
    pins = {
        "0.13.351": "homeassistant==2026.8.0b3",
        "0.13.350": "homeassistant==2026.8.0b2",
        "0.13.349": "homeassistant==2026.8.0b0",
        "0.13.348": "homeassistant==2026.7.4",
        "0.13.347": "homeassistant==2026.7.3",
    }

    assert resolve_phcc.newest_stable(pins) == "0.13.348"


def test_versions_are_compared_numerically_not_as_text():
    """ "0.13.9" must not outrank "0.13.348" - which it does as a string."""
    pins = {
        "0.13.9": "homeassistant==2026.1.0",
        "0.13.348": "homeassistant==2026.7.4",
    }

    assert resolve_phcc.newest_stable(pins) == "0.13.348"


def test_nothing_usable_fails_loudly():
    """Falling back to "install whatever" would put the beta straight back."""
    with pytest.raises(SystemExit):
        resolve_phcc.newest_stable({"0.13.351": "homeassistant==2026.8.0b3"})


def test_an_explicit_requirement_is_passed_through(capsys):
    """The pinned minimum-version entry must reach pip unchanged - and must
    not trigger a PyPI lookup."""
    resolve_phcc.main(
        ["resolve_phcc.py", "pytest-homeassistant-custom-component==0.13.190"]
    )

    assert capsys.readouterr().out.strip() == (
        "PHCC_SPEC=pytest-homeassistant-custom-component==0.13.190"
    )


# --- the linter has to agree with the floor the matrix tests ------------

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "test.yaml"


def test_ruff_targets_the_python_the_minimum_job_runs():
    """Two files that have to move together.

    ruff.toml names the oldest Python this integration supports, and the
    minimum job in the matrix runs exactly that one. Raised in one place
    only, the linter would start allowing syntax the supported floor cannot
    parse - and the job that exists to catch precisely that would not see it,
    because Ruff never runs against the older version at all.
    """
    config = (REPO / "ruff.toml").read_text(encoding="utf-8")
    target = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', config)
    assert target, "ruff.toml names no target version"
    ruff_python = f"{target[1]}.{target[2]}"

    workflow = WORKFLOW.read_text(encoding="utf-8")
    minimum = re.search(
        r'name:\s*"minimum HA[^"]*"\s*\n\s*python-version:\s*"([\d.]+)"', workflow
    )
    assert minimum, "the matrix has no minimum job to compare against"

    assert ruff_python == minimum[1], (
        f"Ruff targets Python {ruff_python} but the minimum job runs {minimum[1]}"
    )


def test_the_workflow_still_runs_ruff():
    """Formatting that only one machine checks survives until the first
    commit written somewhere else."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "ruff check ." in workflow
    assert "ruff format --check ." in workflow
    assert re.search(r'pip install "ruff==[\d.]+"', workflow), (
        "an unpinned Ruff lets the CI enforce whatever it decides this week"
    )


def _minimum_job_label() -> str:
    """The name of the matrix job that tests the oldest supported release."""
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "test.yaml"
    ).read_text(encoding="utf-8")
    labels = re.findall(r'- name: "(minimum[^"]*)"', workflow)
    assert len(labels) == 1, f"expected one minimum job, found {labels}"
    return labels[0]


def test_the_minimum_job_names_the_version_hacs_declares():
    """Three places say which Home Assistant is the oldest supported one -
    hacs.json, the pinned plugin release, and the job's own name - and the
    only thing holding them together was a comment saying "raise both
    together, never one alone".

    The pin cannot be checked here without asking PyPI which Home Assistant
    a plugin release ships, and this file stays network-free on purpose. The
    label can: raise hacs.json alone and the job goes on announcing the
    version it no longer tests, which is the half that lies to a reader.
    """
    import json

    declared = json.loads(
        (Path(__file__).resolve().parents[1] / "hacs.json").read_text(encoding="utf-8")
    )["homeassistant"]
    # "2024.12.0" -> "2024.12"; the label names the release, not the patch.
    release = ".".join(declared.split(".")[:2])

    assert release in _minimum_job_label(), (
        f"hacs.json declares {declared} as the minimum, but the matrix job is "
        f'called "{_minimum_job_label()}". Raise the job name AND its pinned '
        "plugin release together - the pin is what decides what is tested."
    )


CHECK_MIN_HA = REPO / ".github" / "scripts" / "check_min_ha.py"


def _load_min_ha_check():
    """The version comparison, without importing Home Assistant.

    The script reads homeassistant.const at import time, which is the point
    of it - here only the comparison is wanted, so it is loaded as source.
    """
    namespace: dict = {}
    source = CHECK_MIN_HA.read_text(encoding="utf-8")
    body = source[source.index("def feature_release") : source.index("def main")]
    exec(compile(body, str(CHECK_MIN_HA), "exec"), namespace)  # noqa: S102
    return namespace["feature_release"]


def test_a_patch_release_still_counts_as_the_declared_minimum():
    """hacs.json names a patch; the plugin pins whichever patch of that
    feature release it ships. Comparing them literally would fail the job for
    being right."""
    feature_release = _load_min_ha_check()

    assert feature_release("2024.12.3") == feature_release("2024.12.0")
    assert feature_release("2025.1.0") != feature_release("2024.12.0")


def test_the_minimum_job_actually_checks_the_version_it_installed():
    """The half the offline test above cannot do.

    Only PyPI knows which Home Assistant a given plugin release ships, so
    nothing here can tell whether the pin still matches hacs.json - the job
    would go on announcing a minimum it no longer tests. Inside the job the
    version is installed and can just be read, which is what this wires up.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "check_min_ha.py" in workflow, (
        "the minimum job does not verify the Home Assistant it installed, so "
        "a wrong pin runs green under the right label"
    )
    assert re.search(r"if:\s*matrix\.check-declared-minimum", workflow), (
        "the check has to be bound to the minimum job, not run for every one"
    )
    assert "check-declared-minimum: true" in workflow, (
        "no matrix entry opts into the check, so it never runs"
    )


CHECK_CURRENT_HA = REPO / ".github" / "scripts" / "check_current_ha.py"
CHECK_SH = REPO / ".github" / "scripts" / "check.sh"


def _load_current_ha_check():
    """The pin parser alone, without Home Assistant or the network."""
    namespace: dict = {"re": re}
    source = CHECK_CURRENT_HA.read_text(encoding="utf-8")
    body = source[source.index("def pinned_home_assistant") : source.index("def main")]
    exec(compile(body, str(CHECK_CURRENT_HA), "exec"), namespace)  # noqa: S102
    return namespace["pinned_home_assistant"]


def test_the_current_version_check_reads_the_resolved_pin():
    """The plugin pins one exact release; that is the version to compare with.

    Anything looser has to be refused rather than guessed at - a range would
    hand the comparison nothing definite, and "probably fine" is how the
    current leg came to run last month's release unnoticed.
    """
    pinned_home_assistant = _load_current_ha_check()

    assert pinned_home_assistant("homeassistant==2026.9.0") == "2026.9.0"
    with pytest.raises(SystemExit):
        pinned_home_assistant("homeassistant>=2026.9")


def test_the_current_leg_checks_the_version_it_runs():
    """The local twin of the minimum job's check, and it runs BEFORE the suite
    whose result it qualifies - a check after a green run is a footnote."""
    script = CHECK_SH.read_text(encoding="utf-8")

    assert "check_current_ha.py" in script, (
        "check.sh does not compare its current interpreter with what CI "
        "resolves, so a venv that aged past CI runs green"
    )
    assert script.index("check_current_ha.py") < script.index(
        'pytest tests/ -q -m ""'
    ), "the version check has to come before the suite it vouches for"


def test_the_type_check_runs_the_same_home_assistant_as_the_current_job():
    """A checker that vouches for another release vouches for nothing.

    requirements_test.txt is deliberately unpinned - a pin there once hid a
    real incompatibility for months - so it resolves to whatever plugin
    release is newest, including one that ships a Home Assistant BETA. The
    test job goes through resolve_phcc.py to stay on the last final release,
    and mypy installing from the requirements file instead put the two jobs
    on different type surfaces without either of them saying so.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    mypy_job = workflow[workflow.index("  mypy:") : workflow.index("  pytest:")]
    # Lines that DO something, not lines that talk about it. The comment
    # above the install step names the requirements file to explain why it is
    # not used, and a plain substring search read that as the file being used
    # - the same blindness as a guard satisfied by its own banner, arriving
    # from the other side.
    steps = [line for line in mypy_job.splitlines() if not line.strip().startswith("#")]

    assert any("resolve_phcc.py latest-stable-ha" in line for line in steps), (
        "the type check resolves its Home Assistant some other way than the "
        "job whose type surface it is supposed to be checking"
    )
    assert not [line for line in steps if "requirements_test.txt" in line], (
        "the unpinned requirements file is back, so this job can install a "
        "Home Assistant beta the tests never run against"
    )
