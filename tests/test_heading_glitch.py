"""The portal sometimes sends the expert page with every panel headed "Info".

Same rows, same labels, only the module headings (heating circuit, heat
pump, statistics ...) replaced by one word, and the next cycle is normal
again. Scraped rows are keyed by heading and label, so that one cycle
relabelled every row: the sensors went unknown, and a full set of new
`info_` sensors was built from it and stayed behind unknown for good, one set
per occurrence. Two rows of different modules with the same label also
collided under one key.

Told apart from a real relabel by its shape, not its length: every label is
one the page already had, at least one key is new, and the rows sit under
fewer distinct headings than before. A language switch changes the labels, a
renamed module keeps the number of headings, and a module that goes away
adds no key - all three stay ordinary relabels or removals.
"""

import logging

import pytest

from custom_components.wemportal.exceptions import WemPortalError
from custom_components.wemportal.models import Reading
from custom_components.wemportal.wemportalapi import WemPortalApi

NORMAL = (
    "heizkreis_1-vorlauftemperatur",
    "heizkreis_1-aussentemperatur",
    "wärmepumpe-vorlauftemperatur",
    "wärmepumpe-hochdruck",
    "statistik-jaz_jahr",
)
# The same rows with every heading "Info"; the two flow temperatures collide.
EVERY_PANEL_INFO = (
    "info-vorlauftemperatur",
    "info-aussentemperatur",
    "info-hochdruck",
    "info-jaz_jahr",
)


class _Portal:
    """A scraper that answers one page per cycle, in order."""

    cookie = {}

    def __init__(self, *pages):
        self.pages = list(pages)

    def scrape(self):
        keys = self.pages.pop(0)
        page = {key: Reading(value=21.5, unit="°C", platform="sensor") for key in keys}
        return [{"cookie": {}, **page}]

    def close(self):
        pass


def _api_reading(*pages):
    api = WemPortalApi("user@example.org", "secret")
    api._scraper = _Portal(*pages)
    return api


def _cycle(api):
    """One scrape cycle; a refused page is a failed scrape, as in production."""
    try:
        api._scrape_and_merge()
    except WemPortalError as exc:
        return exc
    return None


def _rows(api):
    return set(api.data.get("0000", {}))


def test_a_cycle_with_every_panel_headed_info_builds_no_rows():
    api = _api_reading(NORMAL, EVERY_PANEL_INFO, NORMAL)

    _cycle(api)
    refused = _cycle(api)

    assert refused is not None, "the broken page was taken as the expert page"
    assert not any(key.startswith("info-") for key in _rows(api)), (
        "rows from the broken page reach the data, and every one becomes a "
        "sensor that stays unknown after the page is normal again"
    )
    assert api.data["0000"]["wärmepumpe-vorlauftemperatur"].value == 21.5

    assert _cycle(api) is None
    assert api.spider_retry_count == 0


def test_the_page_after_the_broken_one_is_not_reported_as_a_relabel(caplog):
    api = _api_reading(NORMAL, EVERY_PANEL_INFO, NORMAL)
    _cycle(api)
    _cycle(api)

    with caplog.at_level(logging.WARNING):
        _cycle(api)

    assert "labelled scraped rows differently" not in caplog.text


def test_three_cycles_headed_info_in_a_row_build_no_rows_either():
    """However long it lasts - a threshold in cycles would only move the
    line, and the next longer occurrence would leave its set behind."""
    api = _api_reading(NORMAL, EVERY_PANEL_INFO, EVERY_PANEL_INFO, EVERY_PANEL_INFO)
    _cycle(api)

    refusals = [_cycle(api) for _ in range(3)]

    assert all(refusals)
    assert not any(key.startswith("info-") for key in _rows(api))
    assert api.spider_retry_count == 3, "a broken page is a failed scrape"


def test_the_refusal_names_the_headings_the_portal_sent():
    api = _api_reading(NORMAL, EVERY_PANEL_INFO)
    _cycle(api)

    refused = _cycle(api)

    assert "info" in str(refused)
    assert "wärmepumpe" in str(refused)


@pytest.mark.parametrize(
    ("case", "page"),
    [
        (
            "language switch: the labels change",
            (
                "heizkreis_1-flow_temperature",
                "heizkreis_1-outside_temperature",
                "wärmepumpe-flow_temperature",
                "wärmepumpe-high_pressure",
                "statistik-jaz_jahr",
            ),
        ),
        (
            "one module renamed: as many headings as before",
            (
                "heizkreis_1-vorlauftemperatur",
                "heizkreis_1-aussentemperatur",
                "heat_pump-vorlauftemperatur",
                "heat_pump-hochdruck",
                "statistik-jaz_jahr",
            ),
        ),
        (
            "language switch while a module is gone: fewer headings, new labels",
            ("heizkreis_1-flow_temperature", "heizkreis_1-outside_temperature"),
        ),
        (
            "a module gone: nothing new",
            ("heizkreis_1-vorlauftemperatur", "heizkreis_1-aussentemperatur"),
        ),
    ],
)
def test_a_real_change_of_the_page_is_still_taken(case, page):
    api = _api_reading(NORMAL, page)
    _cycle(api)

    assert _cycle(api) is None, case
    assert set(page) <= _rows(api), case
