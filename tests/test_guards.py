"""The guards that watch the guards.

The rebuild left a structural guard behind for each of the eight diseases
it closed. Two failure modes threaten that arrangement, and both have
already happened here:

  A guard goes BLIND. It scans one source file by name, code moves to a
  new module, and the scan keeps passing over the file it still knows.
  Twice: the scraper transport check after the shared request helper
  (phase 3), and the retry opt-in check after the statistics split (phase
  9). Both times the mutation run noticed, not a person.

  A guard goes MISSING. Deleting a test is a green diff. Nothing says the
  protection went with it.

So: no test may bind itself to a single package source file, and every
guard file has to be listed here by name.
"""

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent
PACKAGE = TESTS.parents[0] / "custom_components" / "wemportal"

# Guard files, and what each one holds. Deleting one of these files - or
# emptying it - fails the index test below.
#
# This list is the answer to "what stops the old problems coming back",
# for whoever asks that question in six months.
GUARD_FILES = {
    "test_account_state.py": "no mutable module-level state outside the registry",
    "test_budgets.py": "no module or function grows past its frozen budget",
    "test_durations.py": "no test quietly starts taking minutes",
    "test_platform_entities.py": "every platform adds entities as readings appear, and reads its rows through the shared lookup",
    "test_portal_boundaries.py": "every json read and html parse is a declared boundary",
    "test_portal_values.py": "comma normalisation lives in exactly one place",
    "test_reading_boundary.py": "readings are read as attributes, never as dict keys",
    "test_reading_invariants.py": "every mapped reading carries the fields other paths read",
    "test_repairs.py": "every repair issue is translated and entry-prefixed",
    "test_requirements.py": "the manifest and the runtime file name the same dependencies",
    "test_security.py": "no module reaches past the redaction, and the expert client stays behind its own import",
    "test_transport_boundary.py": "transport imports no domain module",
    "test_transport_errors.py": "only the value path opts into a transport retry",
    "test_guards.py": "the guards stay package-wide and stay present",
}

# Reading ONE source file is right where the subject genuinely is one file.
# Each exemption names why; anything else has to scan the package.
SINGLE_FILE_EXEMPTIONS = {
    # The config flow's user-facing strings are the config flow's alone.
    ("test_hardening.py", "config_flow"),
    # The line number OF the parser, to say the one conversion left in the
    # package is that one. The other half of that test scans the package;
    # this half has to name the file the parser lives in.
    ("test_portal_values.py", "utils"),
    # The scraper's own response handling: the subject is that module's
    # boundary, and it is the module the gate is about.
    ("test_response_gate.py", "scraper"),
    # "transport.py imports no domain module" is a statement ABOUT that one
    # file - there is nothing to scan for, and a package-wide version would
    # be a different guard. Listed here only since the detector learned to
    # see a path bound to a name first; it read this file all along.
    ("test_transport_boundary.py", "transport"),
}


def _a_source_file_of_this_package(target) -> set:
    """`<path> / "utils.py"` -> {"utils"}, when utils.py really is one of ours.

    Asked of the PACKAGE rather than of the variable name: keying on the
    literal `PACKAGE` would go blind the moment somebody calls it something
    else, which is the failure this whole file exists against. And the
    question is genuinely "is this a module of the package" - the mutation
    harness builds a throwaway `root / "module.py"` in a temp directory to
    test itself, which looks identical and is not a binding to anything.
    """
    if not (isinstance(target, ast.BinOp) and isinstance(target.op, ast.Div)):
        return set()
    name = target.right
    if not (isinstance(name, ast.Constant) and str(name.value).endswith(".py")):
        return set()
    if not (PACKAGE / str(name.value)).exists():
        return set()
    return {str(name.value).removesuffix(".py")}


def _tests_that_read_one_source_file(source: str):
    """The modules whose source `source` reads as ONE named file.

    Two spellings, because both are in use here and only the first was
    seen: `Path(module.__file__).read_text()` is what the incidents
    happened with, `(PACKAGE / "module.py").read_text()` is what the tests
    written since actually do - so this was watching an idiom the
    repository had moved away from.

    `Path(module.__file__).parent` is not a hit: that resolves the PACKAGE
    and is exactly the shape a package-wide scan starts from.

    A path BOUND to a name first is the third spelling, and it hid a real
    one: test_transport_boundary.py assembles its path at module level and
    reads it further down, so the read call carries a bare Name and nothing
    matched. Bindings are collected in a first pass, which also means the
    order of the two statements does not matter.
    """
    tree = ast.parse(source)
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                module = _a_source_file_of_this_package(node.value)
                if module:
                    bound[target.id] = module

    found = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_text"
        ):
            continue
        found |= _a_source_file_of_this_package(node.func.value)
        if isinstance(node.func.value, ast.Name):
            found |= bound.get(node.func.value.id, set())
        target = ast.unparse(node.func.value)
        if "__file__" not in target or ".parent" in target:
            continue
        for inner in ast.walk(node.func.value):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "__file__"
                and isinstance(inner.value, ast.Name)
            ):
                found.add(inner.value.id)
    return found


def test_no_guard_is_pinned_to_a_single_source_file():
    """A scan that names one file goes blind the moment code moves - and
    a blind guard is worse than none, because the suite stays green."""
    offenders = []
    for test_file in sorted(TESTS.glob("test_*.py")):
        for module in _tests_that_read_one_source_file(
            test_file.read_text(encoding="utf-8")
        ):
            if (test_file.name, module) in SINGLE_FILE_EXEMPTIONS:
                continue
            offenders.append(f"{test_file.name} reads {module}.__file__")

    assert not offenders, (
        f"{offenders} - scan the package instead (PACKAGE.glob('*.py')), or "
        "add an exemption saying why this subject really is one file."
    )


def test_the_scan_sees_a_path_bound_to_a_name_first():
    """The third spelling, and it was in this repository the whole time.

    `test_transport_boundary.py` builds its path at module level and reads it
    later - so the read call carries a bare Name, not the `PACKAGE / "x.py"`
    expression the detector matched, and the guard walked straight past a
    guard pinned to one file. Verbatim from that file.
    """
    real = (
        "TRANSPORT = (\n"
        "    pathlib.Path(__file__).resolve().parents[1]\n"
        '    / "custom_components"\n'
        '    / "wemportal"\n'
        '    / "transport.py"\n'
        ")\n"
        "def test_it():\n"
        "    tree = ast.parse(TRANSPORT.read_text(encoding='utf-8'))\n"
    )

    assert _tests_that_read_one_source_file(real) == {"transport"}


def test_the_scan_does_not_claim_a_path_that_is_not_ours():
    """The counterweight: the mutation harness builds a throwaway
    `root / "module.py"` in a temp directory, which looks identical and binds
    to nothing this package owns."""
    not_ours = (
        "SOMEWHERE = tmp_path / 'module.py'\n"
        "def test_it():\n"
        "    SOMEWHERE.read_text()\n"
    )

    assert _tests_that_read_one_source_file(not_ours) == set()


def test_the_scan_catches_the_blindness_it_was_written_for():
    """Both directions, against the real lines from the incident.

    A guard that passes proves nothing; a guard that fails on the shape it
    exists for proves it works. The first line below is verbatim what went
    blind when the statistics path moved out; the second is the shape that
    replaced it and must stay allowed.
    """
    went_blind = (
        'API_SOURCE = pathlib.Path(wemportalapi.__file__).read_text(encoding="utf-8")'
    )
    assert _tests_that_read_one_source_file(went_blind) == {"wemportalapi"}

    package_wide = (
        "PACKAGE = pathlib.Path(wemportalapi.__file__).parent\n"
        'sources = {f.name: f.read_text(encoding="utf-8") for f in PACKAGE.glob("*.py")}'
    )
    assert _tests_that_read_one_source_file(package_wide) == set()

    # The second spelling, which is what the tests written since actually
    # use - and which this scan was blind to until it was asked for.
    by_package_path = 'source = (PACKAGE / "transport.py").read_text(encoding="utf-8")'
    assert _tests_that_read_one_source_file(by_package_path) == {"transport"}

    # And the one that looks exactly like it and is not: the mutation
    # harness writes a throwaway module into a temp directory to test
    # itself. Counted as a pinned guard, that cost a working change a
    # revert - the shape is identical, only the package knows the
    # difference.
    a_throwaway = 'text = (root / "module.py").read_text(encoding="utf-8")'
    assert _tests_that_read_one_source_file(a_throwaway) == set()


def test_every_guard_file_is_listed_and_present():
    """Deleting a guard is otherwise a green diff."""
    missing = [
        name
        for name in GUARD_FILES
        if not (TESTS / name).exists()
        or "def test_" not in (TESTS / name).read_text(encoding="utf-8")
    ]

    assert not missing, (
        f"guard file(s) gone or emptied: {missing}. If the protection is "
        "genuinely obsolete, remove the entry here in the same commit and "
        "say in the message what replaced it."
    )


def test_every_guard_is_described_in_the_readme():
    """The index says a guard exists; the README says what to do about it.

    Documentation that nothing checks is documentation that quietly stops
    being true - and this particular text exists for the person who has no
    memory of any of this, so being wrong is worse than being absent.
    """
    readme = (TESTS / "README.md").read_text(encoding="utf-8")

    undocumented = [name for name in GUARD_FILES if name not in readme]
    assert not undocumented, (
        f"guard file(s) missing from tests/README.md: {undocumented}. Add a "
        "row to the guard table saying what each one holds."
    )


def test_the_local_check_runs_every_tool_ci_runs():
    """`check.sh` is only worth trusting while it is the same set of gates.

    A tool added to CI and forgotten here turns the local run into a claim
    it cannot back - which is worse than not having it, because it is the
    run people believe before pushing.
    """
    workflow = (TESTS.parents[0] / ".github" / "workflows" / "test.yaml").read_text(
        encoding="utf-8"
    )
    check = (TESTS.parents[0] / ".github" / "scripts" / "check.sh").read_text(
        encoding="utf-8"
    )

    tools = {
        "ruff check": "ruff check",
        "ruff format --check": "ruff format --check",
        "mypy": "mypy",
        "pytest": "pytest tests/",
        "mutate.py": "mutate.py",
    }
    in_ci = {name for name, needle in tools.items() if needle in workflow}
    missing_locally = {name for name in in_ci if tools[name] not in check}

    assert not missing_locally, (
        f"CI runs {missing_locally} but .github/scripts/check.sh does not. "
        "Add it there too, or the local run promises more than it checks."
    )


def _scans_the_package(source: str) -> bool:
    """Whether `source` walks the package's modules, however it spells it.

    Asked of the CALL, not of the variable in front of it. Matching the text
    "PACKAGE.glob" is the same mistake the detector above was written
    against, one file further down - and it had the same effect: the
    package-wide scan in test_security.py binds its path to a lowercase
    `package` and was therefore missing from the index for as long as it has
    existed.
    """
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "glob"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            # Exactly "*.py" - every module. `test_*.py` is a scan of the
            # test directory, which several files here do and which says
            # nothing about the package.
            and node.args[0].value == "*.py"
        ):
            return True
    return False


def test_the_index_scan_is_not_pinned_to_a_variable_name():
    """The self-test for the check below, in every direction that matters."""
    lowercase = 'for module in sorted(package.glob("*.py")):\n    pass\n'
    assert _scans_the_package(lowercase), "the real spelling in test_security"
    assert _scans_the_package('PACKAGE.glob("*.py")')
    assert not _scans_the_package('TESTS.glob("test_*.py")'), (
        "a walk of the test directory is not a scan of the package"
    )
    assert not _scans_the_package("PACKAGE.read_text()")


def test_a_new_package_wide_scan_is_added_to_the_index():
    """The index only stays useful while it is complete: a guard nobody
    lists is a guard nobody knows to keep."""
    scanning = {
        test_file.name
        for test_file in sorted(TESTS.glob("test_*.py"))
        if _scans_the_package(test_file.read_text(encoding="utf-8"))
    }

    unlisted = scanning - set(GUARD_FILES)
    assert not unlisted, (
        f"{unlisted} scan(s) the package but are not in GUARD_FILES - add a "
        "line saying what each one holds."
    )
