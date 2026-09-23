"""Every error an action raises at the user says so through the catalogue.

Home Assistant shows a HomeAssistantError or ServiceValidationError to the
person who triggered the action, and it can only show it in their language if
it carries a translation key. Written as a literal English sentence it cannot
be translated, and a key that has no entry shows as the bare key - neither is
visible from inside the code, so this scans for both.

Two kinds, and which one is not decoration either: ServiceValidationError is
"the call was wrong" and stays out of the error log, HomeAssistantError is
"the system failed" and goes into it. That choice is checked where it is
made, in the tests of each action; this guard holds the catalogue side.

Scans the package, not a list of files, so a raise added to a new module is
covered without anyone remembering to come back.
"""

import ast
import json
import pathlib
import re

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)
CATALOGUES = ("en", "de")
USER_FACING = {"HomeAssistantError", "ServiceValidationError"}
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _catalogue(language: str) -> dict:
    path = PACKAGE / "translations" / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8")).get("exceptions", {})


def _keyword(call: ast.Call, name: str):
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _user_facing_raises(source: str, name: str):
    """(where, key, placeholder names or None) for each raise of a user-facing
    error. key is None when the raise carries no literal key at all."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if called not in USER_FACING:
            continue
        key_node = _keyword(node.exc, "translation_key")
        key = key_node.value if isinstance(key_node, ast.Constant) else None
        placeholders_node = _keyword(node.exc, "translation_placeholders")
        placeholders = None
        if isinstance(placeholders_node, ast.Dict):
            placeholders = {k.value for k in placeholders_node.keys if k is not None}
        elif placeholders_node is None:
            placeholders = set()
        found.append((f"{name}:{node.lineno}", key, placeholders))
    return found


def _all_raises():
    raises = []
    for path in sorted(PACKAGE.glob("*.py")):
        raises += _user_facing_raises(path.read_text(encoding="utf-8"), path.name)
    return raises


def test_the_scan_finds_the_shape_it_exists_for():
    """Proof the guard can fail: a literal English message has no key."""
    literal = 'raise HomeAssistantError(f"{name}: it did not work.")\n'
    keyed = (
        "raise ServiceValidationError(\n"
        '    translation_domain=DOMAIN, translation_key="x",\n'
        '    translation_placeholders={"name": name},\n'
        ")\n"
    )

    assert _user_facing_raises(literal, "m") == [("m:1", None, set())]
    assert _user_facing_raises(keyed, "m") == [("m:1", "x", {"name"})]


def test_every_user_facing_error_carries_a_translation_key():
    untranslated = [where for where, key, _ in _all_raises() if key is None]

    assert not untranslated, (
        f"{untranslated} raise(s) a user-facing error without a translation "
        "key, so it reaches the user in English only. Give it "
        "translation_domain=DOMAIN, a translation_key and an entry under "
        '"exceptions" in every catalogue.'
    )


def test_every_key_has_a_message_in_every_catalogue():
    missing = [
        f"{where} -> {key} ({language})"
        for where, key, _ in _all_raises()
        if key is not None
        for language in CATALOGUES
        if not _catalogue(language).get(key, {}).get("message")
    ]

    assert not missing, (
        f"{missing}: Home Assistant shows the bare key where the message is missing."
    )


def test_placeholders_in_the_code_and_the_messages_agree():
    """A placeholder the message names but the code does not pass is left in
    the text as "{name}"; one passed but not named is silently dropped."""
    mismatched = []
    for where, key, passed in _all_raises():
        if key is None or passed is None:
            continue
        for language in CATALOGUES:
            message = _catalogue(language).get(key, {}).get("message", "")
            named = set(PLACEHOLDER.findall(message))
            if named != passed:
                mismatched.append(f"{where} {key} ({language}): {named} vs {passed}")

    assert not mismatched, mismatched


def test_no_catalogue_entry_outlives_its_raise():
    used = {key for _, key, _ in _all_raises() if key is not None}
    for language in CATALOGUES:
        orphaned = set(_catalogue(language)) - used
        assert not orphaned, f"{language}: {sorted(orphaned)} are raised nowhere"
