"""Repair issues and their translations stay in lockstep (Umbau P8, K6).

A repair issue is rendered from translations/<lang>.json at display time -
nothing checks the key when the issue is created, so a missing or typo'd
key shows the user a raw placeholder string instead of the explanation the
issue exists for. And every issue_id must start with the config entry's id:
async_remove_entry deletes an entry's issues by exactly that prefix, so an
id shaped any other way becomes an issue nothing ever cleans up.
"""

import ast
import json
import pathlib
import re

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)
TRANSLATIONS = PACKAGE / "translations"

PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")


def _create_issue_calls():
    """Every async_create_issue call in the package, as (file, Call) pairs."""
    calls = []
    for source_file in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", None)
            )
            if name == "async_create_issue":
                calls.append((source_file.name, node))
    return calls


def _keys_and_placeholders_used_by_the_code():
    """Map of translation_key -> placeholder names the code passes for it."""
    calls = _create_issue_calls()
    assert calls, (
        "no async_create_issue call found in the package - either the repair "
        "issues are gone or this scan has gone blind"
    )

    used = {}
    for file_name, node in calls:
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        key_node = keywords.get("translation_key")
        assert isinstance(key_node, ast.Constant) and isinstance(key_node.value, str), (
            f"{file_name}: translation_key must be a string literal so this "
            "test can hold it against the translation files"
        )
        placeholders_node = keywords.get("translation_placeholders")
        if placeholders_node is None:
            placeholders = set()
        else:
            assert isinstance(placeholders_node, ast.Dict), (
                f"{file_name}: translation_placeholders must be a dict literal "
                "with string keys, for the same reason"
            )
            placeholders = {key.value for key in placeholders_node.keys}
        if key_node.value in used:
            assert used[key_node.value] == placeholders, (
                f"two call sites disagree on the placeholders of "
                f"'{key_node.value}' - the translation can only satisfy one"
            )
        used[key_node.value] = placeholders
    return used


def test_every_issue_translation_key_is_translated_in_every_language():
    """A key the code raises but no language defines renders as a raw string;
    a key a language defines but nothing raises is a dead translation."""
    used = _keys_and_placeholders_used_by_the_code()

    language_files = sorted(TRANSLATIONS.glob("*.json"))
    assert language_files, "no translation files found"
    for language_file in language_files:
        issues = json.loads(language_file.read_text(encoding="utf-8")).get("issues", {})
        assert set(issues) == set(used), (
            f"{language_file.name}: issue translations out of step with the "
            f"code: {set(issues) ^ set(used)}"
        )
        for key, code_placeholders in used.items():
            translated = issues[key]["title"] + " " + issues[key]["description"]
            assert set(PLACEHOLDER_PATTERN.findall(translated)) == code_placeholders, (
                f"{language_file.name}: '{key}' uses different placeholders "
                "than the code passes - the frontend would show a hole"
            )


def _assignments_per_file():
    """Every `name = <expression>` in each package module.

    So the check below survives the ordinary act of naming the id before
    using it twice - which is exactly what happened when creating and
    deleting the same issue moved into one place. A scan that only
    understands a literal at the call site goes blind on the first
    refactor, and blind is worse than absent: the suite stays green.
    """
    per_file = {}
    for source_file in sorted(PACKAGE.glob("*.py")):
        assignments = {}
        for node in ast.walk(ast.parse(source_file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        per_file[source_file.name] = assignments
    return per_file


def test_every_issue_id_starts_with_the_config_entry_id():
    """The prefix is the cleanup contract: async_remove_entry drops an
    entry's issues by matching '{entry_id}_', so an id that starts any other
    way survives the entry it belongs to."""
    assignments = _assignments_per_file()

    for file_name, node in _create_issue_calls():
        assert len(node.args) >= 3, (
            f"{file_name}: async_create_issue must pass the issue_id "
            "positionally so this test can inspect it"
        )
        issue_id = node.args[2]
        if isinstance(issue_id, ast.Name):
            resolved = assignments[file_name].get(issue_id.id)
            assert resolved is not None, (
                f"{file_name}: the issue id comes from {issue_id.id}, which is "
                "not assigned in this module - this scan cannot see what it "
                "holds. Build the id here, or assign it in the same file."
            )
            issue_id = resolved
        starts_with_entry_id = (
            isinstance(issue_id, ast.JoinedStr)
            and isinstance(issue_id.values[0], ast.FormattedValue)
            and "entry_id" in ast.unparse(issue_id.values[0])
        )
        assert starts_with_entry_id, (
            f"{file_name}: {ast.unparse(issue_id)} does not start with the "
            "config entry id - async_remove_entry could never clean it up"
        )
