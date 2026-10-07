"""Translations for WEM Portal."""

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


def translate(language: str, value: str) -> str:
    value = value.lower()

    # Safe word fragments and full words, sorted by length later to match longest first
    vocab = {
        "en": {
            "betriebsart": "operating mode",
            "wärmeerzeuger": "heat generator",
            "heizkreis": "heating circuit",
            "warmwasser": "hot water",
            "außentemperatur": "outside temperature",
            "aussentemperatur": "outside temperature",
            "raumtemperatur": "room temperature",
            "vorlauftemperatur": "flow temperature",
            "warmwassertemperatur": "hot water temperature",
            "kollektortemperatur": "collector temperature",
            "anlagendruck": "system pressure",
            "wärmeleistung": "heat output",
            "raumsolltemperatur": "room setpoint temperature",
            "warmwassersolltemperatur": "hot water setpoint temperature",
            "temperatur": "temperature",
            "vorlauf": "flow",
            "rücklauf": "return",
            "raum": "room",
            "außen": "outside",
            "aussen": "outside",
            "anlage": "system",
            "kollektor": "collector",
            "betriebs": "operating",
            "wärme": "heat",
            "1_wez": "1st heat generator",
            "1.wez": "1st heat generator",
            "2_wez": "2nd heat generator",
            "2.wez": "2nd heat generator",
            "wez": "heat generator",
            "erzeuger": "generator",
            "druck": "pressure",
            "leistung": "output",
            "soll": "setpoint",
            "absenk": "reduced",
            "normal": "normal",
            "komfort": "comfort",
            "party": "party",
            "urlaub": "holiday",
            "funktion": "function",
            "beginn": "begin",
            "ende": "end",
            "push": "push",
            "programm": "program",
            "program": "program",
            "gesamt": "total",
            "energie": "energy",
            "el.": "electrical",
            "kühlen": "cooling",
            "heizen": "heating",
            "kühl": "cooling",
            "heiz": "heating",
            "wasser": "water",
            "consuption": "consumption",
            "compresso": "compressor",
            "mont": "month",
            "months": "month",
            "switching_e2": "switchings e2",
            "oat": "outside air temperature",
            "ctt": "compressor discharge temperature",
            "ict": "indoor coil temperature",
            "irt": "indoor return temperature",
            "omt": "outdoor middle temperature",
            "lwt": "leaving water temperature",
            "odu": "outdoor unit",
            "wwp sg": "wwp sg",
            "wwp em hk": "wwp em hk",
            "r130": "r130",
        }
    }

    out = value

    if language in vocab:
        # Sort replacements by length descending so longer compound words match first
        replacements = sorted(
            vocab[language].items(), key=lambda entry: len(entry[0]), reverse=True
        )

        # Use placeholders to prevent cascading translation bugs
        placeholders = {}
        for position, (de_word, en_word) in enumerate(replacements):
            if de_word in out:
                placeholder = f"__TOKEN_{position}__"
                placeholders[placeholder] = f" {en_word} "
                out = out.replace(de_word, placeholder)

        # Resolve placeholders back to English words
        for placeholder, en_word in placeholders.items():
            out = out.replace(placeholder, en_word)

    out = out.replace("_", " ")
    # Clean up extra spaces caused by multiple replacements
    out = " ".join(out.split()).title()
    out = out.replace("1St ", "1st ").replace("2Nd ", "2nd ")

    return out
