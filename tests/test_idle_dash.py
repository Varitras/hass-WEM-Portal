"""The portal's "--": 0 where nothing is active, nothing on a counter.

The portal shows "--" for a setpoint without demand and for a fault field
without a fault, and a sensor at unknown whenever the heat pump is idle was
the result of reading it as missing data. On a counter, though, 0 is a reset
to Home Assistant's long-term statistics: the next real reading is booked as
consumption from zero. Which of the two a sensor is shows only in the unit it
ends up with - and that unit is often not the one the "--" arrived with: a
scraped "--" cell carries none, an API answer may omit it or spell it the
portal's way, and merge, entity and restore all keep the previous one.

So these go through the real readers and the real sensor, never a reading
built by hand at the end of the chain.
"""

import types

from tests.test_mapper import DEVICE, _modules, _parameter, _process, _value, _values
from tests.test_scraper_parse import _page, _panel

USER = "user@example.org"


def _sensor(data, key):
    """The sensor entity Home Assistant would build from `data[key]`."""
    from custom_components.wemportal.sensor import WemPortalSensor

    coordinator = types.SimpleNamespace(
        data={DEVICE: data},
        api=types.SimpleNamespace(api_version="2.0", modules={}),
        last_update_success=True,
        async_add_listener=lambda *_args, **_kwargs: None,
    )
    sensor = WemPortalSensor(
        coordinator,
        types.SimpleNamespace(entry_id="e1", data={"username": USER}),
        DEVICE,
        key,
        data[key],
    )
    sensor.async_write_ha_state = lambda: None
    return sensor


def _scraped(api, cell):
    """One scrape of a page with an hour counter row showing `cell`, merged
    the way the poll merges it; returns the row's key."""
    from custom_components.wemportal.scraper import WemPortalScraper

    page = _page(_panel("Heat pump", [("Betriebsstd. Verdichter", cell)]))
    rows = WemPortalScraper(USER, "secret").parse_expert_page(page)[0]
    rows.pop("cookie")
    api._merge_webscraping_data(DEVICE, rows)
    return next(iter(rows))


def test_a_scraped_hour_counter_going_to_dash_is_not_a_reset():
    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi(USER, "secret")
    key = _scraped(api, "16846 h")
    sensor = _sensor(api.data[DEVICE], key)
    assert sensor.native_value == 16846, "the control case never read anything"

    _scraped(api, "--")
    sensor.coordinator.data = api.data
    sensor._handle_coordinator_update()

    assert sensor.native_value is None, (
        "an hour counter dropped to 0, which the statistics book as a reset"
    )


def test_a_scraped_setpoint_going_to_dash_reads_zero_degrees():
    from custom_components.wemportal.scraper import WemPortalScraper
    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi(USER, "secret")
    page = _page(_panel("Heat pump", [("Vorlaufsolltemperatur", "35 °C")]))
    rows = WemPortalScraper(USER, "secret").parse_expert_page(page)[0]
    rows.pop("cookie")
    api._merge_webscraping_data(DEVICE, rows)
    key = next(iter(rows))
    sensor = _sensor(api.data[DEVICE], key)

    idle = _page(_panel("Heat pump", [("Vorlaufsolltemperatur", "--")]))
    rows = WemPortalScraper(USER, "secret").parse_expert_page(idle)[0]
    rows.pop("cookie")
    api._merge_webscraping_data(DEVICE, rows)
    sensor._handle_coordinator_update()

    assert sensor.native_value == 0
    assert sensor.native_unit_of_measurement == "°C"


def _api_counter(first_unit, dash_unit):
    """An API energy reading, then "--" answered with `dash_unit`."""
    modules = _modules(_parameter("Energy"))
    first = _process(
        modules, _values(_value("Energy", numeric=1234.0, unit=first_unit))
    )
    sensor = _sensor(first, "Heat pump-Energy")
    assert sensor.native_value == 1234, "the control case never read anything"

    second = _process(
        modules,
        _values(_value("Energy", string="--", unit=dash_unit)),
        existing=first,
    )
    sensor.coordinator.data = {DEVICE: second}
    sensor._handle_coordinator_update()
    return sensor


def test_an_api_counter_in_the_portals_own_spelling_is_not_reset():
    """ "kW (W)h" becomes Wh only on its way to the entity, so a check on the
    unit as it arrived did not know it for a counter."""
    sensor = _api_counter("kW (W)h", "kW (W)h")

    assert sensor.native_value is None


def test_an_api_counter_whose_dash_comes_without_a_unit_is_not_reset():
    """The mapper keeps the previous unit for an answer without one, so the
    reading the sensor gets is a counter's either way."""
    sensor = _api_counter("kWh", None)

    assert sensor.native_value is None


def test_a_counter_whose_unit_first_arrives_with_a_dash_is_not_reset():
    """The sensor took a new unit only after deciding the value, so a
    counter whose first answer had no unit decided its first "--" with the
    kWh still on its way in - as 0, a reset."""
    modules = _modules(_parameter("Energy"))
    first = _process(modules, _values(_value("Energy", string="--", unit=None)))
    sensor = _sensor(first, "Heat pump-Energy")

    second = _process(
        modules, _values(_value("Energy", string="--", unit="kWh")), existing=first
    )
    sensor.coordinator.data = {DEVICE: second}
    sensor._handle_coordinator_update()

    assert sensor.native_unit_of_measurement == "kWh"
    assert sensor.native_value is None


async def test_a_counter_whose_first_reading_after_a_restart_is_dash_is_not_reset(
    monkeypatch,
):
    """The unit comes back from the last session only after the value was
    first decided - without one, as a plain 0."""
    from homeassistant.components.sensor import SensorExtraStoredData
    from homeassistant.helpers.restore_state import RestoreEntity

    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi(USER, "secret")
    key = _scraped(api, "--")
    sensor = _sensor(api.data[DEVICE], key)
    sensor.hass = None

    async def last_sensor_data():
        return SensorExtraStoredData(native_value=16846, native_unit_of_measurement="h")

    async def no_home_assistant(_self):
        return None

    sensor.async_get_last_sensor_data = last_sensor_data
    monkeypatch.setattr(RestoreEntity, "async_added_to_hass", no_home_assistant)
    await sensor.async_added_to_hass()

    assert sensor.native_unit_of_measurement == "h"
    assert sensor.native_value is None, "the restored counter started at 0"


def test_a_statistics_energy_counter_with_a_dash_is_not_reset():
    """The statistics rows state their class themselves, as the plain text
    Home Assistant also accepts, and the counter check compared it by
    identity with the enum member - never the same object, so a "--" there
    read 0 on the energy dashboard's own counter."""
    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi(USER, "secret")
    api.data = {DEVICE: {}}
    api._store_statistics_group(
        DEVICE,
        1,
        "Heating Energy Yield",
        {"Unit": "kWh", "Values": [{"Date": "2026-10-04T00:00:00", "Value": "--"}]},
    )
    key = f"{DEVICE}-Energy_1"
    assert key in api.data[DEVICE], "the control case stored no statistics row"
    sensor = _sensor(api.data[DEVICE], key)

    assert sensor.native_value is None


def test_an_idle_number_reads_zero():
    """A writeable setpoint shows "--" the same way; a number entity has no
    text state, so it is 0 there too."""
    from custom_components.wemportal.number import WemPortalNumber

    from custom_components.wemportal.const import WemDataType

    modules = _modules(
        _parameter(
            "Setpoint",
            IsWriteable=True,
            DataType=WemDataType.NUMBER_STEP_ONE,
            MinValue=20.0,
            MaxValue=60.0,
        )
    )
    data = _process(modules, _values(_value("Setpoint", string="--", unit="°C")))
    key = "Heat pump-Setpoint"
    assert data[key].platform == "number", "the control case built no number"
    coordinator = types.SimpleNamespace(
        data={DEVICE: data},
        api=types.SimpleNamespace(api_version="2.0", modules={}),
        last_update_success=True,
        async_add_listener=lambda *_args, **_kwargs: None,
    )
    number = WemPortalNumber(
        coordinator,
        types.SimpleNamespace(entry_id="e1", data={"username": USER}),
        DEVICE,
        key,
        data[key],
    )

    assert number.native_value == 0
