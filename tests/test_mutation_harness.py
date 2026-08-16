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
import tempfile
import time
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
    """What subprocess.run returns, as far as the harness reads it.

    `stderr` is part of that: the harness quotes it when a run says nothing
    useful about the mutation, and a double without it turns the harness's own
    diagnostics into AttributeErrors that look like harness bugs.
    """

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


def test_the_original_comes_back_byte_for_byte(tmp_path, monkeypatch):
    """The harness edits the checkout it runs in, so restoring has to be
    exact.

    Line endings are the trap: the snippets in the plan are written with \\n,
    so matching needs a newline-normalised read - and writing that back would
    turn a CRLF checkout into an LF one on every run, a whole-file diff in
    every touched file that has nothing to do with any mutation.
    """
    target = tmp_path / "module.py"
    original = b"value = 1\r\nother = 2\r\n"
    target.write_bytes(original)
    monkeypatch.setattr(mutate, "REPO", tmp_path)

    path, kept = mutate.apply_mutation(
        {"path": "module.py", "old": "value = 1", "new": "value = 99"}
    )
    assert b"99" in path.read_bytes(), "the mutation was not applied at all"

    path.write_bytes(kept)

    assert path.read_bytes() == original


def test_restoring_leaves_nothing_behind(tmp_path, monkeypatch):
    """One temp directory per case was created and never removed - a full run
    left as many as the plan has entries, each holding a copy of a source
    file. The original is kept in memory instead.

    Pointed at a temp directory of its own rather than the machine's: the
    system one belongs to every process on the box, so any of them creating a
    file while this runs failed a test about this code.
    """
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    mutate.apply_mutation({"path": "module.py", "old": "value = 1", "new": "value = 2"})

    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def test_a_selector_matching_no_tests_is_an_error(monkeypatch):
    """pytest exits 5 for "no tests ran", which is non-zero - and non-zero is
    how the harness recognises a caught mutation. A typo in the selector
    would therefore certify every mutation as caught."""
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(5, "no tests ran")
    )

    with pytest.raises(SystemExit) as excinfo:
        mutate.run_tests("nothing_matches_this")

    assert "matched no tests" in str(excinfo.value)


def test_a_failing_suite_counts_as_caught(monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(1, "1 failed")
    )

    assert mutate.run_tests("something") is True


def test_a_passing_suite_counts_as_survived(monkeypatch):
    """The mutation was applied, the code was broken, and the tests stayed
    green - that is the finding, not a success."""
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(0, "3 passed")
    )

    assert mutate.run_tests("something") is False


def test_the_run_cannot_be_failed_by_the_duration_budget(monkeypatch):
    """Exit code 1 is the whole evidence, so nothing else may produce it.

    conftest turns an otherwise GREEN run red when a single test ran past
    the duration budget - useful in the everyday suite, and here it is a
    false "caught": the mutation is reported as noticed because a test was
    slow, not because anything failed. The two signals share one exit code,
    so they have to be kept apart at the call.

    Asserted on the command line because that is where the separation lives
    - the alternative would be a real slow run inside the suite.
    """
    seen = {}

    def record(argv, **_kwargs):
        seen["argv"] = argv
        return _Result(0, "3 passed")

    monkeypatch.setattr(mutate.subprocess, "run", record)

    mutate.run_tests("something")

    assert "--slow-test-seconds" in seen["argv"], (
        "a slow test would be reported as a caught mutation"
    )


def test_the_file_is_restored_even_when_the_run_explodes(tmp_path, monkeypatch):
    """A harness that leaves mutated source behind would poison every later
    run - and the next commit."""
    target = tmp_path / "module.py"
    original = "value = 1\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    # main() resolves every selector to its file before touching anything, so
    # the map has to exist here - there is no test suite in tmp_path.
    monkeypatch.setattr(
        mutate, "collect_test_locations", lambda: {"anything": {"tests/x.py"}}
    )
    monkeypatch.setattr(
        mutate,
        "run_tests",
        lambda selector, paths=None, root=None: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            [
                {
                    "path": "module.py",
                    "old": "value = 1",
                    "new": "value = 2",
                    "tests": "anything",
                }
            ]
        ),
        encoding="utf-8",
    )
    # --jobs 1 on purpose: this is about the file in the REPOSITORY coming
    # back. A parallel run breaks a copy instead, and that copy is thrown away
    # afterwards either way - see the sibling test for the restore that
    # matters there, which is between two cases sharing one worker tree.
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan), "--jobs", "1"])

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
                re.MULTILINE,
            )
        )
    assert names, "no test functions found - the check would pass vacuously"

    for case in plan:
        for clause in re.split(r"\s+(?:or|and)\s+", case["tests"]):
            clause = clause.strip()
            assert any(clause in name for name in names), (
                f"{case['label']}: selector clause {clause!r} matches no test"
            )


# How many tests one clause may select before it stops naming anything. A
# clause is matched as a SUBSTRING, so an ordinary word picks up whatever
# else happens to contain it: `word` selected 24 tests across four files,
# and any one of them failing would have counted as this mutation being
# noticed. Three leaves room for a deliberate family of names.
CLAUSE_BREADTH_LIMIT = 3


def test_no_selector_clause_is_a_word_that_means_anything():
    """The fourth silent-pass route: a clause too wide to be evidence.

    The two checks above ask whether a clause matches SOMETHING. This one
    asks whether it matches something in particular - a mutation whose
    selector drags in two dozen unrelated tests is reported as caught by
    whichever of them happens to be red, and says nothing about the code it
    broke.
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
                re.MULTILINE,
            )
        )

    too_wide = []
    for case in plan:
        for clause in re.split(r"\s+(?:or|and)\s+", case["tests"]):
            clause = clause.strip()
            selected = [name for name in names if clause in name]
            if len(selected) > CLAUSE_BREADTH_LIMIT:
                too_wide.append(f"{clause!r} selects {len(selected)}")

    assert not too_wide, (
        f"selector clause(s) too wide to be evidence: {too_wide}. Name the "
        "test the mutation is actually about."
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

    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _BrokenRun()
    )

    with pytest.raises(SystemExit) as excinfo:
        mutate.run_tests("anything")

    assert reason in str(excinfo.value)


def test_a_hung_test_run_is_not_evidence_either(monkeypatch):
    """A run that never finishes would otherwise hold a mutated source file
    for as long as it hangs, and report nothing at all."""
    mutate = _load()

    def _hang(*_args, **_kwargs):
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


# --- the harness only collects what it needs ---------------------------


COLLECTED = """tests/test_alpha.py::test_one
tests/test_alpha.py::test_two[case-a]
tests/test_beta.py::test_two[case-b]
tests/test_gamma.py::TestGroup::test_three
"""


def _locations(monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(0, COLLECTED)
    )
    return mutate.collect_test_locations()


def test_a_test_is_found_in_the_file_it_lives_in(monkeypatch):
    locations = _locations(monkeypatch)

    assert locations["test_one"] == {"tests/test_alpha.py"}
    # Parameters are stripped, and a class in the path does not hide the name.
    assert locations["test_three"] == {"tests/test_gamma.py"}


def test_a_name_shared_by_two_files_yields_both(monkeypatch):
    """Handing pytest only one of them would run half the guard and report
    the other half as passing."""
    locations = _locations(monkeypatch)

    assert locations["test_two"] == {"tests/test_alpha.py", "tests/test_beta.py"}


def test_the_selector_resolves_the_way_pytest_would(monkeypatch):
    """-k matches substrings, so this has to as well - otherwise the harness
    would hand over fewer files than the selector actually reaches."""
    locations = _locations(monkeypatch)

    assert mutate.files_for("test_one", locations) == ["tests/test_alpha.py"]
    assert mutate.files_for("_thr", locations) == ["tests/test_gamma.py"]
    assert mutate.files_for("test_one or test_three", locations) == [
        "tests/test_alpha.py",
        "tests/test_gamma.py",
    ]
    assert mutate.files_for("nothing_matches", locations) == []


def test_a_failed_collection_stops_the_run(monkeypatch):
    """Without the map every mutation would silently fall back to the whole
    suite, or worse, to nothing."""
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(2, "boom")
    )

    with pytest.raises(SystemExit) as excinfo:
        mutate.collect_test_locations()

    assert "Could not collect" in str(excinfo.value)


def test_the_run_is_restricted_to_the_given_files(monkeypatch):
    seen = {}

    def record(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Result(1, "1 failed")

    monkeypatch.setattr(mutate.subprocess, "run", record)

    mutate.run_tests("something", ["tests/test_alpha.py"])

    assert "tests/test_alpha.py" in seen["cmd"]
    assert "tests/" not in seen["cmd"], "the whole suite was collected anyway"
    assert "-x" in seen["cmd"], "the run does not stop at the first failure"


def test_a_dead_selector_stops_before_anything_is_mutated(tmp_path, monkeypatch):
    """Resolved for every case up front, on purpose.

    A selector that names nothing is a broken plan. Finding that out halfway
    through leaves the run half-done for no reason - and falling back to the
    whole suite instead would quietly run a mutation against tests that have
    nothing to do with it, which is worse than stopping.
    """
    target = tmp_path / "module.py"
    original = "value = 1\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(
        mutate, "collect_test_locations", lambda: {"test_real": {"tests/x.py"}}
    )
    # The observable is that no run happens at all. Asserting on the message
    # cannot separate the two: falling back to the whole suite ends in
    # "matched no tests" as well, just several seconds and one applied
    # mutation later.
    runs = []
    monkeypatch.setattr(
        mutate, "run_tests", lambda selector, paths=None: runs.append(paths) or True
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            [
                {
                    "path": "module.py",
                    "old": "value = 1",
                    "new": "value = 2",
                    "tests": "nothing_matches_this",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan)])

    with pytest.raises(SystemExit) as excinfo:
        mutate.main()

    assert "matched no tests" in str(excinfo.value)
    assert runs == [], "the plan was run although a selector named nothing"
    assert target.read_text(encoding="utf-8") == original, (
        "a mutation was applied before the plan was known to be sound"
    )


# --- running the plan in parallel, one copy of the repo per worker ------


def _plan_of(count, tmp_path):
    """A plan of `count` trivially-caught mutations, and the file it breaks."""
    (tmp_path / "module.py").write_text(
        "\n".join(f"value{index} = 1" for index in range(count)) + "\n",
        encoding="utf-8",
    )
    plan = [
        {
            "path": "module.py",
            "old": f"value{index} = 1",
            "new": f"value{index} = 2",
            "tests": "test_real",
            "label": f"case{index}",
        }
        for index in range(count)
    ]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def test_one_job_runs_in_the_repository_itself(tmp_path, monkeypatch):
    """--jobs 1 is the fallback when a parallel run reports something odd, so
    it has to be the OLD behaviour exactly: no copy, no temp directory."""
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(
        mutate, "collect_test_locations", lambda: {"test_real": {"tests/x.py"}}
    )
    seen = []
    monkeypatch.setattr(
        mutate,
        "run_tests",
        lambda selector, paths=None, root=None: seen.append(root) or True,
    )
    copies = []
    monkeypatch.setattr(
        mutate, "build_worktrees", lambda *_a, **_k: copies.append(True) or []
    )
    plan = _plan_of(2, tmp_path)
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan), "--jobs", "1"])

    assert mutate.main() == 0
    assert seen == [None, None], "a copy was used although --jobs 1 was asked for"
    assert copies == [], "worker copies were built for a serial run"


def test_the_default_worker_count_is_bounded(monkeypatch):
    """Every worker is a full pytest process with Home Assistant imported.

    cores-2 is fine on a laptop and not on a build machine: on 64 cores it
    asks for 62 of them at once, each a few hundred megabytes of interpreter,
    and nobody has shown that is faster - the measurement behind this
    stops at six.
    """
    monkeypatch.setattr(mutate.os, "cpu_count", lambda: 64)

    assert mutate.default_jobs() <= mutate.MAX_DEFAULT_JOBS


def test_a_small_machine_still_gets_its_cores(monkeypatch):
    """The counter-test: the cap must not become the number."""
    monkeypatch.setattr(mutate.os, "cpu_count", lambda: 8)

    assert mutate.default_jobs() == 6


def test_a_worktree_that_will_not_go_away_is_reported(tmp_path, monkeypatch, capsys):
    """Silently ignoring the cleanup leaves copies of the repository behind.

    Each is a few megabytes and a full checkout, and a run that cannot remove
    them says so nowhere - so they accumulate under the system temp directory
    with nothing pointing at the cause.
    """
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(
        mutate, "collect_test_locations", lambda: {"test_real": {"tests/x.py"}}
    )
    monkeypatch.setattr(mutate, "run_tests", lambda *_args, **_kwargs: True)

    def refuse_to_remove(path, ignore_errors=False, **_kwargs):
        # Behaves like the real one, INCLUDING ignore_errors - which is the
        # whole point here. A stand-in that raises whatever it is asked
        # cannot tell the two versions apart: it was written that way first
        # and the mutation that puts ignore_errors back stayed green.
        if ignore_errors:
            return
        raise OSError(f"cannot remove {path}")

    monkeypatch.setattr(mutate.shutil, "rmtree", refuse_to_remove)
    plan = _plan_of(1, tmp_path)
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan), "--jobs", "2"])

    mutate.main()

    assert "could not be removed" in capsys.readouterr().err


def test_results_are_reported_in_plan_order(tmp_path, monkeypatch, capsys):
    """Workers finish in whatever order they finish.

    Reporting in that order would make two runs of the same plan produce
    different output, which is a diff nobody can read - and the one case that
    SURVIVED would move around between runs.
    """
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(
        mutate, "collect_test_locations", lambda: {"test_real": {"tests/x.py"}}
    )

    def slowest_first(selector, paths=None, root=None):
        # WHICH case this is comes from the mutated file in this worker's own
        # tree, not from the worker's number: the pool hands cases to whatever
        # worker is free, so the two are only incidentally the same.
        mutated = (root / "module.py").read_text(encoding="utf-8")
        index = next(number for number in range(3) if f"value{number} = 2" in mutated)
        # case0 takes longest, so finishing order is the reverse of plan order.
        time.sleep(0.05 * (3 - index))
        # A DIFFERENT answer per case, which is the point. With every case
        # answering the same, a result attached to the wrong case produces
        # identical output and the assertion below cannot see it - the whole
        # thing passed while proving only that three lines were printed.
        return index != 1

    monkeypatch.setattr(mutate, "run_tests", slowest_first)
    plan = _plan_of(3, tmp_path)
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan), "--jobs", "3"])

    assert mutate.main() == 1, "a surviving case must fail the run"

    reported = [
        (line.split()[0], line.split()[-1])
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(("caught", "SURVIVED"))
    ]
    assert reported == [
        ("caught", "case0"),
        ("SURVIVED", "case1"),
        ("caught", "case2"),
    ], "results were reported in finishing order, or attached to the wrong case"


def test_a_copy_missing_a_mutated_file_is_an_error(tmp_path, monkeypatch):
    """The mistake that produced believable-looking nonsense once already.

    A copy without pytest.ini collects nothing, so every case ends in an
    unrelated pytest error - a wall of exit-code 4 that says nothing about
    the file that is actually missing. Naming it here costs one stat per case.
    """
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        mutate.build_worktrees(
            1, tmp_path / "holding", [{"path": "not-copied.py", "old": "", "new": ""}]
        )

    assert "not-copied.py" in str(excinfo.value)


def test_the_worker_copies_are_removed_afterwards(tmp_path, monkeypatch):
    """A run that leaves 1.7 MB per worker behind fills the temp directory
    one invocation at a time."""
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(
        mutate, "collect_test_locations", lambda: {"test_real": {"tests/x.py"}}
    )
    monkeypatch.setattr(
        mutate, "run_tests", lambda selector, paths=None, root=None: True
    )
    holding = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda **kwargs: holding.append(real_mkdtemp(**kwargs)) or holding[-1],
    )
    plan = _plan_of(2, tmp_path)
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan), "--jobs", "2"])

    assert mutate.main() == 0
    assert holding, "no temp directory was created for the worker copies"
    assert not Path(holding[0]).exists(), "the worker copies were left behind"


def test_a_worker_tree_is_clean_again_for_the_next_case(tmp_path, monkeypatch):
    """Workers reuse their tree, so each case has to hand it back intact.

    Without the restore the second case would run against code broken in two
    places - its own mutation plus whatever the previous case left there - and
    "caught" would then say nothing about the mutation it names.
    """
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    (tmp_path / "module.py").write_text("a = 1\nb = 1\n", encoding="utf-8")

    seen = []

    def record_what_the_tree_looks_like(selector, paths=None, root=None):
        seen.append((root / "module.py").read_text(encoding="utf-8"))
        return True

    monkeypatch.setattr(mutate, "run_tests", record_what_the_tree_looks_like)
    cases = [
        {"path": "module.py", "old": "a = 1", "new": "a = 2", "tests": "t"},
        {"path": "module.py", "old": "b = 1", "new": "b = 2", "tests": "t"},
    ]

    # One worker, so both cases provably share a tree.
    mutate.run_in_parallel(cases, {"t": ["tests/x.py"]}, jobs=1)

    assert seen == ["a = 2\nb = 1\n", "a = 1\nb = 2\n"], (
        "a case ran against a mutation left behind by the previous one"
    )
