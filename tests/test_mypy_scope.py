"""Every module is type-checked, or says in one line why it is not yet.

mypy.ini states the intent already: "Widening the file list is part of each
phase's definition of done - not a separate chore that can slip." Nothing
enforced it, so a module added to the package simply was not checked, and
nobody found out - the job stayed green because the file was never named.
That is the same shape as a guard that goes blind: the check does not fail,
it just stops covering anything.

This does not demand that everything be typed. It demands that leaving a
module out be a decision somebody wrote down.

The scope is READ from mypy.ini rather than repeated here. A copy would be
the third place the list lives, and the first to drift.
"""

import configparser
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = REPO / "custom_components" / "wemportal"
MYPY_INI = REPO / "mypy.ini"

# What is deliberately still outside, and why. Emptying this list is the
# point; every entry is a promise, not a permanent exemption.
NOT_TYPE_CHECKED_YET = {
    "wemportalapi.py": "the remaining god module - typing it is the next rebuild phase, not a side quest",
    "expert_writer.py": "a Telerik form protocol written out longhand; mostly literal field ids",
    "config_flow.py": "voluptuous schemas type-check to Any, so strict mode says little here",
    "__init__.py": "the lifecycle, which is Home Assistant's own untyped surface",
    "migration.py": "the unique_id migration lifted out of __init__; typed with the lifecycle it belongs to",
    "coordinator.py": "waiting for wemportalapi: it returns what the god module builds",
    "expert_controller.py": "waiting for expert_writer, whose entities it holds",
    "scraper.py": "lxml and curl_cffi are both untyped; the noise would drown the signal",
    "utils.py": "helpers for the modules above, typed when their callers are",
    "holiday.py": "waiting for the api surface it writes through",
    "expert_options.py": "options parsing for the expert path, typed with it",
    "translations.py": "a table of strings",
    "const.py": "constants only",
}


def _modules_in_the_mypy_scope() -> set:
    """The file list from mypy.ini, by module name."""
    config = configparser.ConfigParser()
    config.read(MYPY_INI, encoding="utf-8")
    return {
        line.strip().rstrip(",").split("/")[-1]
        for line in config["mypy"]["files"].splitlines()
        if line.strip()
    }


def _modules_in_the_package() -> set:
    return {source.name for source in PACKAGE.glob("*.py")}


def test_every_module_is_either_checked_or_declared():
    """A module in neither list is one nobody decided about."""
    unaccounted = (
        _modules_in_the_package()
        - _modules_in_the_mypy_scope()
        - set(NOT_TYPE_CHECKED_YET)
    )

    assert not unaccounted, (
        f"{sorted(unaccounted)} are neither in mypy.ini nor declared here. Add "
        "them to the scope, or add a line above saying what is in the way."
    )


def test_nothing_is_declared_unchecked_and_checked_at_once():
    """The contradiction that would otherwise sit there quietly: an entry
    kept after its module was typed reads like a warning that no longer
    applies, and the next person believes it."""
    both = _modules_in_the_mypy_scope() & set(NOT_TYPE_CHECKED_YET)

    assert not both, (
        f"{sorted(both)} are type-checked now - drop them from "
        "NOT_TYPE_CHECKED_YET, which is what emptying it looks like."
    )


def test_no_promise_outlives_its_module():
    """A note about a file that is gone is a note nobody can act on."""
    ghosts = set(NOT_TYPE_CHECKED_YET) - _modules_in_the_package()

    assert not ghosts, f"{sorted(ghosts)} no longer exist; drop the entries."


def test_the_scan_would_notice_a_module_nobody_decided_about():
    """A guard that passes proves nothing.

    The shape it exists to catch, fed to it directly: a new module in the
    package that neither list mentions. Spelled out rather than created on
    disk, because writing into the package during a test run is exactly the
    kind of side effect a suite should not have.
    """
    package = {"models.py", "brand_new.py"}
    scope = {"models.py"}
    declared = set()

    assert package - scope - declared == {"brand_new.py"}
    # And the same sets with the new module declared leave nothing over.
    assert package - scope - {"brand_new.py"} == set()
