"""Which unit a scraped reading ends up with, from the cell to the entity.

The unit of a scraped row is decided in three places: the cell itself ("34 °C"),
the row's name when the cell has none ("Vorlauftemperatur" -> °C), and the
entity, which keeps the unit of the last session through a restart. A word
under a temperature name was given a guessed °C at the first and kept it at
the last, so it showed as unknown even after the guess was fixed; a cell that
carries its unit but no number lost the unit instead.

So these run the real parser, the real merge and the real sensor, and the
restart through the sensor's own restore.
"""

import pytest

from tests.test_idle_dash import DEVICE, USER, _sensor
from tests.test_scraper_parse import _page, _panel


def _scrape(api, name, cell):
    from custom_components.wemportal.scraper import WemPortalScraper

    page = _page(_panel("Heat pump", [(name, cell)]))
    rows = WemPortalScraper(USER, "secret").parse_expert_page(page)[0]
    rows.pop("cookie")
    api._merge_webscraping_data(DEVICE, rows)
    return next(iter(rows))


@pytest.mark.parametrize(
    ("name", "cell", "shown", "unit"),
    [
        ("Temperatur erreicht", "Nein", "Nein", None),
        ("Vorlauftemperatur", "--- °C", None, "°C"),
        ("Vorlauftemperatur", "-- °C", None, "°C"),
        ("Vorlauftemperatur", "NaN °C", None, "°C"),
        ("Vorlauftemperatur", "1e309 °C", None, "°C"),
        ("Vorlauftemperatur", "34", 34, "°C"),
        ("Vorlauftemperatur", "--", 0, "°C"),
        ("Status", "Reduziert Betrieb", "Reduziert Betrieb", None),
    ],
)
def test_a_new_scraped_row_shows_its_reading_with_the_right_unit(
    name, cell, shown, unit
):
    """A word is text and has no unit; a cell that names its unit but holds no
    number is a reading that is not there, in that unit."""
    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi(USER, "secret")
    key = _scrape(api, name, cell)
    sensor = _sensor(api.data[DEVICE], key)

    assert (sensor.native_value, sensor.native_unit_of_measurement) == (shown, unit)


@pytest.mark.parametrize(
    ("name", "cell", "stored", "shown", "unit"),
    [
        ("Temperatur erreicht", "Nein", (None, "°C"), "Nein", None),
        ("Temperatur erreicht", "Ja", (None, "°C"), "Ja", None),
        ("Vorlauftemperatur", "Fehler", (34.0, "°C"), None, "°C"),
        ("Status", "Stop", (None, "kW"), None, "kW"),
        ("Betriebsstd. Verdichter", "--", (16846, "h"), None, "h"),
    ],
)
async def test_a_restart_brings_back_a_unit_only_where_the_reading_has_one(
    monkeypatch, name, cell, stored, shown, unit
):
    """Before the guess was fixed, a word under a temperature name was stored
    with °C. Brought back on the first start after the update, the unit made
    the word unreadable again - for good, because every later answer keeps
    the unit it has. A counter's "--" keeps its stored unit as before."""
    from homeassistant.components.sensor import SensorExtraStoredData
    from homeassistant.helpers.restore_state import RestoreEntity

    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi(USER, "secret")
    key = _scrape(api, name, cell)
    sensor = _sensor(api.data[DEVICE], key)
    sensor.hass = None

    async def last_sensor_data():
        value, stored_unit = stored
        return SensorExtraStoredData(
            native_value=value, native_unit_of_measurement=stored_unit
        )

    async def no_home_assistant(_self):
        return None

    sensor.async_get_last_sensor_data = last_sensor_data
    monkeypatch.setattr(RestoreEntity, "async_added_to_hass", no_home_assistant)
    await sensor.async_added_to_hass()
    sensor._handle_coordinator_update()

    assert (sensor.native_value, sensor.native_unit_of_measurement) == (shown, unit)
