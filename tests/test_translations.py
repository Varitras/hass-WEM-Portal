"""The de->en translation of portal names.

It replaced substrings, so a short entry that matched inside a longer word
left the rest of that word behind: "Heizung" came out as "Heating Ung", and
the abbreviation "odu" turned "Absenkmodus" into "Reduced M Outdoor Unit S".
"""

import re

import pytest

from custom_components.wemportal.translations import _VOCABULARY, translate

# Labels the portal shows on a real installation - rows of the expert page,
# their headings, and the statistics group. Generic product wording only:
# no value, id or anything else of the installation. Real names only, as
# the vocabulary is not extended from guesses (a screenshot from another
# integration once misled it).
REAL_PORTAL_NAMES = [
    "2. WEZ",
    "AT Langzeitwert",
    "AT Mittelwert",
    "Absenk",
    "Absenkmodus",
    "Anforderung",
    "Anforderungsüberhöhung",
    "Außenfühlerzuordnung",
    "Außengerät Variante",
    "Außentemperatur",
    "Beginn",
    "Betrieb",
    "Betriebsart",
    "Betriebsstd. Verdichter",
    "Betriebsstunden E1",
    "Drehzahl Pumpe M1",
    "Druckgastemperatur",
    "EVI Sauggastemperatur",
    "Ende",
    "Estrich",
    "Expansionsventil AG Eintr",
    "Freigabe Kühlbetrieb",
    "Frost",
    "Frostbetrieb",
    "Frostschutz",
    "Funktion",
    "Gebäude",
    "Heizkennlinie",
    "Heizkreis 1",
    "Heizung",
    "Hochdruck",
    "Ist Leistung",
    "JAZ Jahr",
    "JAZ gesamt",
    "Komfort",
    "Kondensationstemperatur",
    "Kühlen",
    "Kältemittel IG Austritt",
    "Laufzeit",
    "Leistungsanforderung",
    "Luftansaugtemperatur",
    "Maximaltemperatur",
    "Minimaltemperatur",
    "Mitteldruck",
    "Niederdruck",
    "Normal",
    "Party/Pause",
    "Raumsolltemperatur",
    "Reset",
    "Rücklauftemperatur",
    "SG Ready Anhebung",
    "Schaltdifferenz dynamisch",
    "Schaltspiele Abtauen",
    "Schaltspiele E1",
    "Schaltspiele Verdichter",
    "Schwimmbad",
    "So/Wi Umschaltung",
    "Soll Leistung",
    "Solltemperatur",
    "Statistik",
    "Status E-Heizung 1",
    "Stellung Umschaltventil",
    "Störmeldung",
    "Sättigungstemperatur EVI",
    "Urlaub - Beginn",
    "Urlaub - Ende",
    "Verdampfungstemperatur",
    "Verdichter",
    "Verdichtersauggastemp.",
    "Version WWP-EC WBB",
    "Version WWP-SG",
    "Volumenstrom",
    "Vorlaufsolltemperatur",
    "Vorlauftemperatur",
    "Warmwassertemperatur",
    "Wärmemenge Heizung",
    "Wärmepumpe",
    "Wärmepumpe - Leistungsanforderung",
    "Wärmetauscher AG Austritt",
    "Zeitprogramm",
    "elektrische Energie Jahr",
    "th. Energie Heizen Monat",
    "th. Energie WW Tag",
    "th. Energie gesamt Jahr",
    "Öffnungsgrad EVI",
    "Öffnungsgrad EXV Kühlen",
    "Ölsumpftemperatur",
    "Überhitzung Heizen",
    "Überhitzung Verdichter",
]

# Words a German name keeps on purpose: abbreviations the portal prints and
# a reader of the English page sees the same way.
KEPT_AS_THE_PORTAL_WRITES_THEM = {"ag", "at", "e", "e1", "ec", "evi", "exv", "ig"}
KEPT_AS_THE_PORTAL_WRITES_THEM |= {"m1", "party", "reset", "sg", "status", "version"}
KEPT_AS_THE_PORTAL_WRITES_THEM |= {
    "wbb",
    "ww",
    "wwp",
    "normal",
    "frost",
    "ready",
    "pause",
}

# What the vocabulary puts in: an English word that happens to sit inside the
# German one it translates ("Variant" in "Variante") is no remainder.
ENGLISH_WORDS = {
    word for english in _VOCABULARY["en"].values() for word in english.lower().split()
}


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+\d*|\d+", text.lower())


@pytest.mark.parametrize("name", REAL_PORTAL_NAMES)
def test_no_german_word_is_left_in_pieces(name):
    """A fragment is an output word that is a proper part of an input word -
    the remainder of a word the vocabulary only half knew."""
    source_words = _words(name)
    fragments = [
        word
        for word in _words(translate("en", name))
        if word not in source_words
        and word not in ENGLISH_WORDS
        and any(word in source for source in source_words)
    ]

    assert not fragments, f"{name!r} -> {translate('en', name)!r}: {fragments}"


@pytest.mark.parametrize("name", REAL_PORTAL_NAMES)
def test_every_real_name_comes_out_in_english(name):
    """The same names, now from the other side: a word the vocabulary does
    not know at all stays German, and a real portal name has none of those
    left - each was looked at and given its technical English term."""
    untranslated = [
        word
        for word in _words(translate("en", name))
        if word in _words(name)
        and not word.isdigit()
        and word not in KEPT_AS_THE_PORTAL_WRITES_THEM
    ]

    assert not untranslated, f"{name!r} -> {translate('en', name)!r}: {untranslated}"


@pytest.mark.parametrize(
    ("name", "unwanted"),
    [("Absenkmodus", "Outdoor"), ("Absenkmodus", "Unit")],
)
def test_an_abbreviation_is_not_read_inside_a_word(name, unwanted):
    """'odu' is the outdoor unit as a word of its own, not three letters of
    'Absenkmodus'."""
    assert unwanted not in translate("en", name)


@pytest.mark.parametrize(
    "name",
    [
        "Heat Pump - Flow Temperature",
        "Outside Temperature",
        "Operating Hours Compressor",
        "Hot Water Setpoint Temperature",
        "Heating Energy",
    ],
)
def test_a_name_already_in_english_is_left_as_it_is(name):
    """An account set to English in the portal sends English names, and
    taking those apart for German parts mangles them."""
    assert translate("en", name) == name


@pytest.mark.parametrize("name", ["Version WWP-SG", "Überhitzung EVI", "Heizkreis 1"])
def test_german_keeps_the_portals_spelling(name):
    """Capitalising every word made 'WWP-SG' into 'Wwp-Sg' and 'EVI' into
    'Evi' - names the portal never shows."""
    assert translate("de", name) == name


@pytest.mark.parametrize(
    ("name", "english"),
    [
        ("2. WEZ", "2nd Heat Generator"),
        ("Absenkmodus", "Setback Mode"),
        ("Außenfühlerzuordnung", "Outdoor Sensor Assignment"),
        ("Betriebsstd. Verdichter", "Operating Hours Compressor"),
        ("Druckgastemperatur", "Discharge Gas Temperature"),
        ("Hochdruck", "High Pressure"),
        ("Kondensationstemperatur", "Condensing Temperature"),
        ("So/Wi Umschaltung", "Summer/Winter Changeover"),
        ("Status E-Heizung 1", "Status E-Heating 1"),
        ("heizprogramm1", "Heating Program 1"),
        ("th. Energie WW Tag", "Thermal Energy WW Day"),
        ("Version WWP-SG", "Version WWP-SG"),
        ("Urlaub - Beginn", "Holiday - Start"),
        ("Absenk", "Setback"),
    ],
)
def test_a_real_name_gets_its_technical_term(name, english):
    """Word by word is not always the term: 'Druckgas' is the discharge gas,
    not pressure gas, and the linking 's' of a compound is no word."""
    assert translate("en", name) == english


@pytest.mark.parametrize(
    ("language", "group", "name"),
    [
        ("en", {"GroupType": 5, "Description": ""}, "Heating Electricity Consumption"),
        ("de", {"GroupType": 5, "Description": ""}, "Stromverbrauch Heizen"),
        ("en", {"GroupType": 1, "Description": "Heizung"}, "Heating Energy"),
        ("de", {"GroupType": 1, "Description": "Heizung"}, "Heizung Energie"),
        ("de", {"GroupType": 1, "Description": "Wärmeenergie"}, "Wärmeenergie"),
    ],
)
def test_a_statistics_group_is_named_in_the_names_language(language, group, name):
    """kWh are energy, so a consumption group is electricity consumption, not
    power; and a German name got the English word 'Energy' added to it,
    because 'energy' never appeared in German."""
    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi("user@example.org", "secret")
    api.language = language

    assert api._statistics_group_name(group) == name
