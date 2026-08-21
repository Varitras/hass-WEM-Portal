"""One parser for every number the portal writes.

The portal spells decimals with a dot in dialog labels and with a comma in
scraped cells and typed service values. Umbau phase 1 (K5) put that
knowledge in exactly one place - `utils.parse_portal_number` - after a
second parser let the dialog accept "0,55" while the service refused it.
"""

import pathlib
import re

import pytest

from custom_components.wemportal import utils

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("21,5", 21.5),
        ("0,55", 0.55),
        ("0.55", 0.55),
        ("30", 30.0),
        ("-19,5", -19.5),
        (" 1,5 ", 1.5),
        (30, 30.0),
        (1.5, 1.5),
    ],
)
def test_a_portal_number_reads_the_same_in_both_spellings(text, expected):
    assert utils.parse_portal_number(text) == expected


@pytest.mark.parametrize("text", ["Aus", "", "  ", None, "1.234,56", "--"])
def test_what_carries_no_number_says_so(text):
    """None rather than a raise or a guess: every caller has its own answer
    to "no number here" (skip the option, keep the raw string, name the
    accepted words), and an exception would make each of them a try block."""
    assert utils.parse_portal_number(text) is None


def test_a_comma_decimal_from_the_api_reads_as_its_number():
    """sanitize_value routes its numeric attempt through the shared parser.

    An API string value "21,5" used to stay TEXT, because sanitize_value
    called float() directly - the same reading a scraped cell already
    delivered as 21.5. On a numeric sensor the text form crashes entity
    setup; on any sensor it split the two sources over one value.
    """
    assert utils.sanitize_value("21,5") == 21.5


def test_words_and_placeholders_still_survive_the_shared_parser():
    """The counter-test: routing through the parser must not turn the
    non-numeric answers into numbers or errors."""
    assert utils.sanitize_value("Aus") == 0.0, "boolean words keep their mapping"
    assert utils.sanitize_value("--") is None, "missing data stays missing"
    assert utils.sanitize_value("Sommer") == "Sommer", "plain words stay words"


def test_comma_normalisation_lives_in_exactly_one_place():
    """The guard for K5: a second comma parser is how the split started.

    Scans the package for the two spellings a comma conversion takes. The
    one hit must be parse_portal_number's own line; a new one means a reader
    stopped asking the shared parser.
    """
    comma_conversion = re.compile(
        r"replace\(\s*\"?,\"?\s*,\s*\"\.\"\s*\)|split\(\",\"\)"
    )
    hits = []
    for source_file in sorted(PACKAGE.glob("*.py")):
        for line_number, line in enumerate(
            source_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if comma_conversion.search(line):
                hits.append(f"{source_file.name}:{line_number}")

    assert hits == ["utils.py:%d" % _parser_line()], (
        f"comma conversion found at {hits} - every portal number must go "
        "through utils.parse_portal_number"
    )


def _parser_line() -> int:
    """The line inside parse_portal_number that does the conversion."""
    source = (PACKAGE / "utils.py").read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(source, start=1):
        if 'replace(",", ".")' in line:
            return line_number
    raise AssertionError("the parser itself no longer converts a comma")
