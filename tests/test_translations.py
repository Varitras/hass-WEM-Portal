"""The de->en vocabulary replaces substrings, so a short entry that matches
inside a longer word leaves the rest of that word behind as a fragment."""

import re

import pytest

from custom_components.wemportal.translations import translate

# Names the portal shows on a real installation - the statistics group and
# rows of the expert page. Real names only: the vocabulary is not extended
# from guesses (a screenshot from another integration once misled it).
REAL_PORTAL_NAMES = [
    "Heizung",
    "Wärmemenge Heizung",
    "2. Wez - Status E-Heizung 1",
    "Wärmepumpe - Leistungsanforderung",
    "2. Wez - Betriebsstunden E1",
]

# English words that happen to sit inside the German one they translate.
ENGLISH_INSIDE_GERMAN = {"pump"}


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


@pytest.mark.parametrize("name", REAL_PORTAL_NAMES)
def test_no_german_word_is_left_in_pieces(name):
    """'Heizung' came out as 'Heating Ung': 'heiz' matched, 'ung' stayed.

    A fragment is an output word that is a proper part of an input word.
    A whole input word passed through untouched is not one - nothing in the
    vocabulary claimed it - and an English word is not a piece of German."""
    source_words = _words(name)
    fragments = [
        word
        for word in _words(translate("en", name))
        if word not in source_words
        and word not in ENGLISH_INSIDE_GERMAN
        and any(word in source for source in source_words)
    ]

    assert not fragments, f"{name!r} -> {translate('en', name)!r}: {fragments}"
