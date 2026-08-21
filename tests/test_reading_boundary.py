"""Readings are consumed as attributes, never as dict keys (Umbau P6, K1).

The coordinator rows used to be dicts grown by four different writers, so
every reader guarded every access with .get() and a typo'd key returned
None instead of failing. They are models.Reading now - and this scan keeps
the package from quietly growing new dict-key access to them.

The forbidden names are the row keys that never existed anywhere else:
portal payloads spell their fields in PascalCase (ParameterID, DataType,
CircuitTimesDay, ...), so those stay legal for response parsing - but
"friendlyName" and friends were invented by the old mapper and can only
mean a reading row.
"""

import ast
import pathlib

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)

# Keys that only ever existed on the old reading dicts. A subscript or
# .get() with one of these is code talking to a reading as if it were still
# a dict - which now returns nothing it can use, silently.
ROW_ONLY_KEYS = {"friendlyName", "optionsNames", "min_value", "max_value", "step"}


def _dict_key_accesses(tree):
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in ROW_ONLY_KEYS
        ):
            found.append((node.lineno, node.slice.value))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in ROW_ONLY_KEYS
        ):
            found.append((node.lineno, node.args[0].value))
    return found


def test_no_reading_is_accessed_like_a_dict():
    """K1's guard: attribute access fails loudly and mypy can check it; a
    dict access on a Reading returns nothing and shows up as an entity that
    quietly stops updating."""
    offenders = []
    for source_file in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for line, key in _dict_key_accesses(tree):
            offenders.append(f"{source_file.name}:{line} [{key}]")

    assert not offenders, (
        "reading rows are models.Reading now - read the field, not the old "
        f"dict key: {offenders}"
    )


def test_the_scan_catches_both_shapes_it_exists_for():
    """A guard that passes proves nothing.

    Both spellings fed to the detector directly, because the package is
    clean - so the test above passes whether the scan works or not, and
    would go on passing if the walk stopped matching. Written as source
    rather than by mutating a real file: what is under test is the pattern,
    not any module in particular.
    """
    caught = _dict_key_accesses(
        ast.parse("row['friendlyName']\nrow.get('step')\nrow.value\nrow['Unit']\n")
    )

    assert [key for _line, key in caught] == ["friendlyName", "step"], (
        f"the scan reported {caught} - it has to see the subscript AND the "
        "get(), and leave attribute access and portal keys alone"
    )


def test_the_scan_still_knows_the_reading_fields():
    """Self-protection: if Reading loses the fields these keys map to, the
    forbidden list above guards a type that no longer exists."""
    from custom_components.wemportal.models import Reading

    fields = set(Reading.__dataclass_fields__)
    assert {"friendly_name", "options_names", "min_value", "max_value", "step"} <= (
        fields
    )
