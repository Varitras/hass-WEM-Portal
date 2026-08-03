"""The mutation harness has to fail loudly rather than report success.

Its whole value is the sentence "all mutations caught". Every way that
sentence can be printed without being true is a way to be lied to about test
quality - which is exactly the problem the harness exists to solve. So the
silent-pass routes are pinned here: a snippet that no longer matches, a
selector that matches no tests, and a selector clause that names a test which
does not exist.

The subprocess call is stubbed; running the real suite inside the suite would
add minutes for no extra confidence about this logic.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "mutate.py"


def _load():
    spec = importlib.util.spec_from_file_location("mutate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mutate = _load()


class _Result:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_a_snippet_that_no_longer_matches_is_an_error(tmp_path, monkeypatch):
    """The dangerous case: the code moved on, the mutation quietly does
    nothing, and the run reports the test as verified."""
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        mutate.apply_mutation(
            {"path": "module.py", "old": "value = 2", "new": "value = 3"}
        )

    assert "expected once" in str(excinfo.value)


def test_an_ambiguous_snippet_is_an_error(tmp_path, monkeypatch):
    """Two matches means the mutation lands somewhere unintended."""
    target = tmp_path / "module.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)

    with pytest.raises(SystemExit):
        mutate.apply_mutation({"path": "module.py", "old": "x = 1", "new": "x = 2"})


def test_a_selector_matching_no_tests_is_an_error(monkeypatch):
    """pytest exits 5 for "no tests ran", which is non-zero - and non-zero is
    how the harness recognises a caught mutation. A typo in the selector
    would therefore certify every mutation as caught."""
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *a, **k: _Result(5, "no tests ran")
    )

    with pytest.raises(SystemExit) as excinfo:
        mutate.run_tests("nothing_matches_this")

    assert "matched no tests" in str(excinfo.value)


def test_a_failing_suite_counts_as_caught(monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *a, **k: _Result(1, "1 failed")
    )

    assert mutate.run_tests("something") is True


def test_a_passing_suite_counts_as_survived(monkeypatch):
    """The mutation was applied, the code was broken, and the tests stayed
    green - that is the finding, not a success."""
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *a, **k: _Result(0, "3 passed")
    )

    assert mutate.run_tests("something") is False


def test_the_file_is_restored_even_when_the_run_explodes(tmp_path, monkeypatch):
    """A harness that leaves mutated source behind would poison every later
    run - and the next commit."""
    target = tmp_path / "module.py"
    original = "value = 1\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(
        mutate, "run_tests", lambda selector: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps([{"path": "module.py", "old": "value = 1", "new": "value = 2",
                     "tests": "anything"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan)])

    with pytest.raises(RuntimeError):
        mutate.main()

    assert target.read_text(encoding="utf-8") == original


def test_every_selector_clause_names_a_real_test():
    """The third silent-pass route, and the one the harness cannot see.

    mutate.py refuses a selector that matches NOTHING. It cannot refuse a
    selector where one clause of an `or` matches nothing - the other clause
    carries the run and the dead name sits there looking meaningful. A plan
    shipped with `... or entity_stays_unavailable` did exactly that: the
    named test had never existed, so a rename of the real one would have
    quietly downgraded the mutation to whatever the survivor selected.
    """
    plan = json.loads(
        (SCRIPT.parent.parent / "mutations" / "response-gate.json").read_text(
            encoding="utf-8"
        )
    )
    names = set()
    for module in (Path(__file__).parent).glob("test_*.py"):
        names.update(
            re.findall(
                r"^\s*(?:async )?def (test_\w+)",
                module.read_text(encoding="utf-8"),
                re.M,
            )
        )
    assert names, "no test functions found - the check would pass vacuously"

    for case in plan:
        for clause in re.split(r"\s+(?:or|and)\s+", case["tests"]):
            clause = clause.strip()
            assert any(clause in name for name in names), (
                f"{case['label']}: selector clause {clause!r} matches no test"
            )


def test_the_shipped_plan_still_matches_the_code():
    """The plan is only useful while its snippets exist. Left to rot it would
    fail at the worst moment - when someone finally runs it."""
    plan = json.loads(
        (SCRIPT.parent.parent / "mutations" / "response-gate.json").read_text(
            encoding="utf-8"
        )
    )
    repo = SCRIPT.resolve().parents[2]

    for case in plan:
        source = (repo / case["path"]).read_text(encoding="utf-8")
        assert source.count(case["old"]) == 1, (
            f"{case['label']}: the snippet no longer matches {case['path']} "
            "exactly once - update the plan"
        )


@pytest.mark.parametrize(
    ("code", "reason"),
    [(2, "interrupted"), (3, "internal error"), (4, "usage error")],
)
def test_a_broken_test_run_is_not_evidence(monkeypatch, code, reason):
    """The fourth silent-pass route, and the one that flatters the harness.

    `returncode != 0` counted a usage error, an internal error and an
    interrupted run all as "the tests noticed". Those say nothing about the
    mutation - and they are exactly what a half-broken environment produces,
    which is when a green report is most convincing and least true.
    """
    mutate = _load()

    class _BrokenRun:
        returncode = code
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(mutate.subprocess, "run", lambda *a, **k: _BrokenRun())

    with pytest.raises(SystemExit) as excinfo:
        mutate.run_tests("anything")

    assert reason in str(excinfo.value)


def test_a_hung_test_run_is_not_evidence_either(monkeypatch):
    """A run that never finishes would otherwise hold a mutated source file
    for as long as it hangs, and report nothing at all."""
    mutate = _load()

    def _hang(*_a, **_k):
        raise mutate.subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(mutate.subprocess, "run", _hang)

    with pytest.raises(SystemExit) as excinfo:
        mutate.run_tests("anything")

    assert "did not finish" in str(excinfo.value)


def test_every_mutation_produces_code_that_still_parses():
    """A mutation that breaks the SYNTAX proves nothing.

    `finally:` -> `else:` after a try with no except is a SyntaxError, so
    pytest exits with a usage error and the old harness scored it as
    "caught" - one of the 56 mutations had been passing that way, testing
    nothing at all. Hardening the exit codes surfaced it; this keeps it
    surfaced at the point where the plan is written rather than run.
    """
    import ast

    plan = json.loads(
        (SCRIPT.parent.parent / "mutations" / "response-gate.json").read_text(
            encoding="utf-8"
        )
    )
    repo = SCRIPT.resolve().parents[2]

    for case in plan:
        if not case["path"].endswith(".py"):
            continue
        source = (repo / case["path"]).read_text(encoding="utf-8")
        mutated = source.replace(case["old"], case["new"], 1)
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            raise AssertionError(
                f"{case['label']}: the mutation does not parse "
                f"({exc.msg} at line {exc.lineno}). A broken parser is not a "
                "broken behaviour - write a mutation that runs."
            ) from exc
