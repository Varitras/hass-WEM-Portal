"""The statistics groups report one day's energy, not a running meter.

Home Assistant reads a `total_increasing` sensor as a meter and starts a new
cycle only when the value drops below 90 % of the last one. A day's value
does not drop at midnight here: the portal ships the new day as null until it
has a number, so the sensor holds yesterday's final value and then moves to
today's. Two similar days - hot water charged once a night - therefore
booked the difference between them instead of the second day's energy, and
the Energy Dashboard lost whole days. A `total` with `last_reset` at the start
of the day the value belongs to starts a new cycle on every new day, whatever
its size.
"""

from datetime import datetime

from homeassistant.components.sensor import SensorStateClass
from homeassistant.util import dt as dt_util

from custom_components.wemportal.wemportalapi import WemPortalApi
from tests.test_idle_dash import USER, _sensor
from tests.test_mapper import DEVICE

KEY = f"{DEVICE}-Energy_2"


def _stored(api, values):
    api._store_statistics_group(
        DEVICE, 2, "Hot Water Energy Yield", {"Unit": "kWh", "Values": values}
    )
    return _sensor(api.data[DEVICE], KEY)


def _start_of(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=dt_util.get_default_time_zone())


def test_a_day_of_the_same_size_as_the_last_still_starts_a_new_cycle():
    api = WemPortalApi(USER, "secret")
    api.data = {DEVICE: {}}

    sensor = _stored(api, [{"Date": "2026-10-06T00:00:00", "Value": 2.9}])
    assert sensor.state_class == SensorStateClass.TOTAL
    assert sensor.last_reset == _start_of("2026-10-06T00:00:00")

    _stored(
        api,
        [
            {"Date": "2026-10-06T00:00:00", "Value": 2.9},
            {"Date": "2026-10-07T00:00:00", "Value": 2.9},
        ],
    )
    sensor._handle_coordinator_update()

    assert sensor.last_reset == _start_of("2026-10-07T00:00:00"), (
        "the second day is not a new cycle, so Home Assistant books only the "
        "difference to the first"
    )


def test_a_day_the_portal_has_no_value_for_yet_stays_the_day_before():
    """The new day comes as null at first; the value shown is still the day
    before's, and so is the cycle it belongs to."""
    api = WemPortalApi(USER, "secret")
    api.data = {DEVICE: {}}

    sensor = _stored(
        api,
        [
            {"Date": "2026-10-06T00:00:00", "Value": 2.9},
            {"Date": "2026-10-07T00:00:00", "Value": None},
        ],
    )

    assert sensor.native_value == 2.9
    assert sensor.last_reset == _start_of("2026-10-06T00:00:00")


def test_a_cycle_without_any_value_keeps_the_day_it_kept_the_value_of():
    """No entry carries a value: the last stored one stands - with its day,
    or the next real value would continue yesterday's cycle."""
    api = WemPortalApi(USER, "secret")
    api.data = {DEVICE: {}}
    _stored(api, [{"Date": "2026-10-06T00:00:00", "Value": 2.9}])

    sensor = _stored(api, [{"Date": "2026-10-07T00:00:00", "Value": None}])

    assert sensor.native_value == 2.9
    assert sensor.last_reset == _start_of("2026-10-06T00:00:00")
