"""Translations for WEM Portal."""

import re
from typing import NamedTuple

from homeassistant.const import UnitOfTemperature


class KnownParameter(NamedTuple):
    """What the integration knows about a portal parameter id."""

    name: str
    # For ids that are temperature setpoints by what they are called. Used
    # only where the portal sends no unit - see known_unit.
    unit: str | None = None


_CELSIUS = UnitOfTemperature.CELSIUS

# Name and unit side by side, so an id is listed once: the temperatures
# below were already known as such by their names.
_KNOWN_PARAMETERS: dict[str, KnownParameter] = {
    "pp_beginn": KnownParameter("party_beginn"),
    "pp_ende": KnownParameter("party_ende"),
    "pp_funktion": KnownParameter("party_funktion"),
    "pp_raumsoll": KnownParameter("party_raumsoll", _CELSIUS),
    "aktraumsoll": KnownParameter("raumsolltemperatur", _CELSIUS),
    "u_beginn": KnownParameter("urlaub_beginn"),
    "u_ende": KnownParameter("urlaub_ende"),
    "u_funktion": KnownParameter("urlaub_funktion"),
    "u_raumsoll": KnownParameter("urlaub_raumsoll", _CELSIUS),
    "ww-push": KnownParameter("warmwasser_push"),
    "ww-program": KnownParameter("warmwasser_programm"),
    "ww-programm": KnownParameter("warmwasser_programm"),
    "aktwwsoll": KnownParameter("warmwassersolltemperatur", _CELSIUS),
    "leistung": KnownParameter("wärmeleistung"),
    "absenk": KnownParameter("absenktemperatur", _CELSIUS),
    "absenkww": KnownParameter("absenk_warmwasser_temperatur", _CELSIUS),
    "normalww": KnownParameter("normal_warmwasser_temperatur", _CELSIUS),
    "komfort": KnownParameter("komforttemperatur", _CELSIUS),
    "normal": KnownParameter("normaltemperatur", _CELSIUS),
}


def friendly_name_mapper(value: str) -> str:
    normalised = value.casefold()
    known = _KNOWN_PARAMETERS.get(normalised)
    return known.name if known is not None else normalised


def known_unit(parameter_id: str, sent: str | None) -> str | None:
    """The unit the portal sent, or a known parameter's when it sent none.

    The portal sends most adjustable temperatures with an empty Unit while
    the room setpoint comes with "°C", so the same kind of value showed as a
    bare number beside a temperature. Only fills that gap: a unit the
    portal states always stands.
    """
    if sent not in (None, ""):
        return sent
    known = _KNOWN_PARAMETERS.get(parameter_id.casefold())
    if known is None or known.unit is None:
        return sent
    return known.unit


# Phrases spanning more than one word, matched before the words themselves.
_PHRASES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "en": [
        (
            re.compile(r"(?<![\w.])1\s*[._]?\s*wez\b", re.IGNORECASE),
            "1st heat generator",
        ),
        (
            re.compile(r"(?<![\w.])2\s*[._]?\s*wez\b", re.IGNORECASE),
            "2nd heat generator",
        ),
        (re.compile(r"\bso\s*/\s*wi\b", re.IGNORECASE), "summer/winter"),
        (re.compile(r"\bth\.\s*", re.IGNORECASE), "thermal "),
        (re.compile(r"\bbetriebsstd\.\s*", re.IGNORECASE), "operating hours "),
        (re.compile(r"\bel\.\s*", re.IGNORECASE), "electrical "),
        (re.compile(r"\bswitching e2\b", re.IGNORECASE), "switchings e2"),
    ]
}

# Whole words, and the parts a compound is built from. A word is translated
# only when it is one of these or made of them entirely (with the German
# linking "s" between parts); anything else stays as the portal wrote it.
# Matching parts inside words of any length is what produced "Heating Ung"
# for "Heizung" and "Reduced M Outdoor Unit S" for "Absenkmodus".
#
# A compound whose parts translate to the wrong term gets an entry of its
# own: "Druckgas" is discharge gas, not pressure gas.
_VOCABULARY: dict[str, dict[str, str]] = {
    "en": {
        "abtauen": "defrost",
        "absenk": "reduced",
        "absenkmodus": "setback mode",
        "anforderung": "demand",
        "anforderungsüberhöhung": "demand boost",
        "anhebung": "boost",
        "anlage": "system",
        "anlagendruck": "system pressure",
        "aussen": "outside",
        "aussengerät": "outdoor unit",
        "austritt": "outlet",
        "außen": "outside",
        "außenfühler": "outdoor sensor",
        "außengerät": "outdoor unit",
        "beginn": "begin",
        "betrieb": "operation",
        "betriebs": "operating",
        "betriebsart": "operating mode",
        "betriebsstd": "operating hours",
        "betriebsstunden": "operating hours",
        "compresso": "compressor",
        "consuption": "consumption",
        "ctt": "compressor discharge temperature",
        "drehzahl": "speed",
        "druck": "pressure",
        "druckgas": "discharge gas",
        "dynamisch": "dynamic",
        "eintr": "inlet",
        "eintritt": "inlet",
        "elektrische": "electrical",
        "ende": "end",
        "energie": "energy",
        "erzeuger": "generator",
        "estrich": "screed",
        "expansionsventil": "expansion valve",
        "freigabe": "enable",
        "frost": "frost",
        "frostschutz": "frost protection",
        "funktion": "function",
        "gebäude": "building",
        "gerät": "unit",
        "gesamt": "total",
        "heiz": "heating",
        "heizen": "heating",
        "heizkennlinie": "heating curve",
        "heizkreis": "heating circuit",
        "heizung": "heating",
        "hochdruck": "high pressure",
        "ict": "indoor coil temperature",
        "irt": "indoor return temperature",
        "ist": "actual",
        "jahr": "year",
        "jaz": "SPF",
        "kennlinie": "curve",
        "kollektor": "collector",
        "komfort": "comfort",
        "kondensation": "condensing",
        "kältemittel": "refrigerant",
        "kühl": "cooling",
        "kühlen": "cooling",
        "langzeitwert": "long-term value",
        "laufzeit": "runtime",
        "leistung": "output",
        "leistungsanforderung": "power demand",
        "luftansaug": "air intake",
        "lwt": "leaving water temperature",
        "maximal": "maximum",
        "minimal": "minimum",
        "mitteldruck": "intermediate pressure",
        "mittelwert": "average",
        "monat": "month",
        "mont": "month",
        "months": "month",
        "niederdruck": "low pressure",
        "normal": "normal",
        "oat": "outside air temperature",
        "odu": "outdoor unit",
        "omt": "outdoor middle temperature",
        "party": "party",
        "program": "program",
        "programm": "program",
        "pumpe": "pump",
        "push": "push",
        "raum": "room",
        "rücklauf": "return",
        "sauggas": "suction gas",
        "schaltdifferenz": "switching differential",
        "schaltspiele": "switching cycles",
        "schwimmbad": "pool",
        "soll": "setpoint",
        "statistik": "statistics",
        "stellung": "position",
        "störmeldung": "fault message",
        "sättigung": "saturation",
        "tag": "day",
        "tauscher": "exchanger",
        "temp": "temp",
        "temperatur": "temperature",
        "umschaltung": "changeover",
        "umschaltventil": "diverter valve",
        "urlaub": "holiday",
        "variante": "variant",
        "verdampfung": "evaporating",
        "verdichter": "compressor",
        "volumenstrom": "flow rate",
        "vorlauf": "flow",
        "warmwasser": "hot water",
        "wasser": "water",
        "wez": "heat generator",
        "wärme": "heat",
        "wärmeerzeuger": "heat generator",
        "wärmemenge": "heat quantity",
        "wärmepumpe": "heat pump",
        "wärmetauscher": "heat exchanger",
        "zeit": "time",
        "zuordnung": "assignment",
        "öffnungsgrad": "opening",
        "ölsumpf": "oil sump",
        "überhitzung": "superheat",
    }
}

# Letters only, so digits, hyphens and dots stay where the portal put them.
_WORD = re.compile(r"[^\W\d_]+")
# The German linking element between two parts of a compound.
_LINKING_S = "s"


def _parts(word: str, vocabulary: dict[str, str]) -> list[str] | None:
    """The vocabulary parts `word` is made of, longest first, or None when
    any of it is not covered - a half-translated word is worse than none."""
    if word in vocabulary:
        return [word]
    for length in range(len(word) - 1, 1, -1):
        head = word[:length]
        if head not in vocabulary:
            continue
        rest = word[length:]
        tail = _parts(rest, vocabulary)
        if tail is None and rest.startswith(_LINKING_S):
            tail = _parts(rest[len(_LINKING_S) :], vocabulary)
        if tail is not None:
            return [head, *tail]
    return None


def _spelled(english: str) -> str:
    """An English word for a name: capitalised, unless it is spelled with
    capitals of its own (an abbreviation)."""
    return english if english != english.lower() else english.capitalize()


def _translated_word(match: re.Match[str], vocabulary: dict[str, str]) -> str:
    word = match.group(0)
    before = match.string[match.start() - 1 : match.start()]
    after = match.string[match.end() : match.end() + 1]
    # lower(), not casefold(): casefold spells ß as ss, and "außen" would
    # never meet its own entry.
    parts = _parts(word.lower(), vocabulary)
    if parts is None:
        # The portal's own spelling: "EVI" stays EVI. Only a word that
        # arrived lowercased (parameter ids are) gets its capital back - and
        # not the letters of an ordinal or a counter ("1st", "E2" stay).
        return word.capitalize() if word.islower() and not before.isdigit() else word
    english = " ".join(
        _spelled(piece) for part in parts for piece in vocabulary[part].split()
    )
    # Apart from a digit glued to it ("Heizprogramm1"); punctuation stays
    # where the portal put it ("E-Heizung", "Party/Pause").
    return f"{' ' if before.isalnum() else ''}{english}{' ' if after.isalnum() else ''}"


def translate(language: str, value: str) -> str:
    """A portal name in the configured language.

    The portal names everything in German. For "en" each word is translated
    on its own, whole or as a compound of known parts, and a word that is
    neither stays German - see _VOCABULARY. For any other language the name
    stays the portal's.
    """
    text = value.replace("_", " ")
    for pattern, replacement in _PHRASES.get(language, []):
        text = pattern.sub(replacement, text)
    vocabulary = _VOCABULARY.get(language, {})
    text = _WORD.sub(lambda match: _translated_word(match, vocabulary), text)
    return " ".join(text.split())
