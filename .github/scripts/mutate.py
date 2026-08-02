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


def run_tests(selector: str) -> bool:
    """True if the selected tests FAIL, i.e. the mutation was caught."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-m", "", "-k", selector],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    # A selector that matches nothing exits 5 and would otherwise read as
    # "caught" - the same silent pass this script exists to prevent.
    if "no tests ran" in result.stdout or result.returncode == 5:
        raise SystemExit(f"selector {selector!r} matched no tests")
    return result.returncode != 0


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

    for case in cases:
        label = case.get("label", case["path"])
        target, backup = apply_mutation(case)
        try:
            caught = run_tests(case["tests"])
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
