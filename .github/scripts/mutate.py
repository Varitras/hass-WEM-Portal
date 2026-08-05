"""Check that a test actually fails when the code it guards is broken.

A test that passes is not evidence. A test that passes *and* fails when its
fix is removed is. Every worthless test written in this repository so far
passed happily while the feature was broken, and every one of them was found
this way rather than by reading it:

  * a swap test that built the replacement object itself, so it only proved
    the constructor accepts an argument,
  * an abort test whose gate tripped one step too early, leaving the step
    that mattered untested,
  * a reload test that checked THAT a reload happened, not that it saw the
    new data - which is where the bug was,
  * a teardown test that set the flag the production code is supposed to set.

The pattern behind all four: if the test establishes the condition the
production code is supposed to establish, it tests nothing.

The harness itself is held to the same standard. "The tests noticed" is
pytest exit code 1 and nothing else - a usage error, an internal error or an
interrupted run are not evidence, and reading any non-zero exit as success
was this script telling itself what it wanted to hear.

Usage
-----
Describe each mutation in a JSON file - a list of objects with:

    path     file to mutate, relative to the repository root
    old      snippet to replace (must appear exactly once; an absent
             snippet is reported as an error, never skipped silently)
    new      replacement, usually "" or a disabling variant
    tests    -k expression selecting the test(s) that must fail

Then:

    python .github/scripts/mutate.py mutations.json

Every mutation is reverted afterwards, including on failure. Exit code is
non-zero if any mutation SURVIVED - that is, the suite stayed green while the
code was broken, which means the test does not test it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


# pytest's exit codes. Only ONE of them means "the tests noticed": 1. The
# script used to read `returncode != 0` as caught, which let a usage error, an
# internal error or an interrupted run all report success - a broken harness
# congratulating itself, which is the failure this whole file exists against.
PYTEST_ALL_PASSED = 0
PYTEST_TESTS_FAILED = 1
PYTEST_INTERRUPTED = 2
PYTEST_INTERNAL_ERROR = 3
PYTEST_USAGE_ERROR = 4
PYTEST_NO_TESTS = 5

# A hung test run would otherwise hold a MUTATED source file indefinitely.
TEST_TIMEOUT_SECONDS = 900


def collect_test_locations() -> dict:
    """Which file each test lives in, collected once before anything is broken.

    Every mutation used to run `pytest tests/`, and collecting the whole tree
    costs about four seconds - repeated for each of a hundred-odd mutations,
    while the tests actually selected are usually one or two. Measured:
    collection was the run. Handing pytest only the files that hold the
    selected tests cuts roughly a third off the total.

    Derived rather than written into the plan by hand: a `file` field per
    mutation is one more thing to keep true when a test moves, and this stays
    correct by construction.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-m", "", "--collect-only"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT_SECONDS,
    )
    if result.returncode != PYTEST_ALL_PASSED:
        raise SystemExit(
            "Could not collect the test suite, so no mutation can be run "
            f"against the right file. pytest exited {result.returncode}:\n"
            + (result.stderr or result.stdout).strip()[-2000:]
        )

    locations: dict = {}
    for line in result.stdout.splitlines():
        path, sep, rest = line.partition("::")
        if not sep or not path.endswith(".py"):
            continue
        # "tests/x.py::test_name[param]" and "tests/x.py::Class::test_name"
        name = rest.split("[")[0].split("::")[-1].strip()
        locations.setdefault(name, set()).add(path)
    return locations


def files_for(selector: str, locations: dict) -> list:
    """The files holding the tests this selector names.

    `-k` matches substrings, so the same rule applies here - a clause selects
    every test whose name contains it.
    """
    files = set()
    for clause in selector.split(" or "):
        clause = clause.strip()
        if not clause:
            continue
        for name, paths in locations.items():
            if clause in name:
                files |= paths
    return sorted(files)


def run_tests(selector: str, paths=None) -> bool:
    """True if the selected tests FAIL, i.e. the mutation was caught."""
    targets = list(paths) if paths else ["tests/"]
    try:
        result = subprocess.run(
            # -x: the question is whether at least one selected test notices,
            # not how many do.
            [
                sys.executable,
                "-m",
                "pytest",
                *targets,
                "-q",
                "-m",
                "",
                "-x",
                "-k",
                selector,
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"selector {selector!r}: the test run did not finish within "
            f"{TEST_TIMEOUT_SECONDS}s. The mutated file is restored, but the "
            "result says nothing - investigate before trusting this plan."
        ) from exc

    # A selector that matches nothing exits 5 and would otherwise read as
    # "caught" - the same silent pass this script exists to prevent.
    if "no tests ran" in result.stdout or result.returncode == PYTEST_NO_TESTS:
        raise SystemExit(f"selector {selector!r} matched no tests")

    if result.returncode == PYTEST_TESTS_FAILED:
        return True
    if result.returncode == PYTEST_ALL_PASSED:
        return False

    raise SystemExit(
        f"selector {selector!r}: pytest exited {result.returncode} "
        f"({_EXIT_REASON.get(result.returncode, 'unknown')}), which says "
        "nothing about the mutation. Last stderr:\n"
        + (result.stderr or "(empty)").strip()[-2000:]
    )


_EXIT_REASON = {
    PYTEST_INTERRUPTED: "interrupted",
    PYTEST_INTERNAL_ERROR: "internal error",
    PYTEST_USAGE_ERROR: "usage error",
}


def apply_mutation(case: dict) -> tuple[Path, Path]:
    target = REPO / case["path"]
    source = target.read_text(encoding="utf-8")
    occurrences = source.count(case["old"])
    if occurrences != 1:
        raise SystemExit(
            f"{case['path']}: snippet found {occurrences} times, expected once. "
            "A mutation that cannot be applied proves nothing - fix the snippet."
        )
    backup = Path(tempfile.mkdtemp()) / target.name
    shutil.copy(target, backup)
    target.write_text(
        source.replace(case["old"], case["new"], 1), encoding="utf-8", newline=""
    )
    return target, backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="JSON file describing the mutations")
    args = parser.parse_args()

    cases = json.loads(args.plan.read_text(encoding="utf-8"))
    survived = []

    locations = collect_test_locations()
    # Resolved for every case BEFORE the first mutation is applied: a selector
    # that names nothing is a broken plan, and finding that out halfway
    # through leaves the run half-done for no reason.
    targets = {}
    for case in cases:
        selector = case["tests"]
        files = files_for(selector, locations)
        if not files:
            raise SystemExit(f"selector {selector!r} matched no tests")
        targets[selector] = files

    for case in cases:
        label = case.get("label", case["path"])
        target, backup = apply_mutation(case)
        try:
            caught = run_tests(case["tests"], targets[case["tests"]])
        finally:
            shutil.copy(backup, target)
        print(f"{'caught  ' if caught else 'SURVIVED'} {label}")
        if not caught:
            survived.append(label)

    if survived:
        print(
            "\nThese mutations survived - the code was broken and the tests "
            "stayed green:\n  " + "\n  ".join(survived)
        )
        return 1
    print(f"\nall {len(cases)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
