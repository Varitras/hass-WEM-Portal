"""The portal's date encoding, in both directions.

Holiday begin and end travel as Unix epoch seconds. Getting the conversion
wrong is silent in the reading direction and destructive in the writing one:
a write goes to a real heating system, and an off-by-one-day is not something
the user can see in the entity afterwards - it will simply show what it wrote.
"""

import asyncio
import types
from datetime import date

import pytest

from custom_components.wemportal.date import (
    WemPortalDate,
    async_setup_entry,
    date_to_epoch,
    epoch_to_date,
)

# Measured on a live installation, not constructed: holiday begin and end came
# back as these two values, exactly 86400 apart and both exactly on a midnight
# UTC boundary.
BEGIN_EPOCH = 1785715200.0
END_EPOCH = 1785801600.0


def test_the_measured_values_decode_to_the_days_they_stood_for():
    assert epoch_to_date(BEGIN_EPOCH) == date(2026, 8, 3)
    assert epoch_to_date(END_EPOCH) == date(2026, 8, 4)


def test_a_day_survives_the_round_trip():
    for day in (date(2026, 8, 3), date(2026, 1, 1), date(2026, 12, 31)):
        assert epoch_to_date(date_to_epoch(day)) == day


def test_writing_uses_the_encoding_the_portal_sent():
    """Midnight UTC, and a whole multiple of a day.

    Local midnight would look identical in a test run in UTC and shift every
    write by the offset anywhere else - east of Greenwich onto the previous
    day.
    """
    assert date_to_epoch(date(2026, 8, 3)) == BEGIN_EPOCH
    assert date_to_epoch(date(2026, 8, 3)) % 86400 == 0


@pytest.mark.parametrize("value", [None, "", "not a number", [], {}])
def test_an_unusable_value_reads_as_no_date(value):
    """None, not a fallback date: "no holiday set" and "1st of January 1970"
    must not look the same to an automation."""
    assert epoch_to_date(value) is None


def test_an_out_of_range_epoch_reads_as_no_date():
    assert epoch_to_date(1e30) is None


def test_a_numeric_string_is_still_accepted():
    """The portal has been observed sending numbers as strings elsewhere
    (the value's own Timestamp field is one), so this stays tolerant."""
    assert epoch_to_date(str(int(BEGIN_EPOCH))) == date(2026, 8, 3)


# --- the entity, including the write that reaches the heating system ----


class _Coordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True
        self.api = types.SimpleNamespace(device_types={}, api_version=None)


class _Entry:
    entry_id = "entry-1"


def _entity(value=BEGIN_EPOCH):
    data = {"1234": {"Heat pump-U_Beginn": {
        "friendlyName": "Holiday begin", "ParameterID": "U_Beginn",
        "value": value, "unit": None, "platform": "date",
        "ModuleIndex": 0, "ModuleType": 1,
    }}}
    entity = WemPortalDate(
        _Coordinator(data), _Entry(), "1234", "Heat pump-U_Beginn",
        data["1234"]["Heat pump-U_Beginn"],
    )
    entity.async_write_ha_state = lambda: None
    return entity, data


def test_the_entity_shows_the_day_the_portal_sent():
    entity, _ = _entity()
    assert entity.native_value == date(2026, 8, 3)


def test_a_parameter_with_no_usable_value_shows_no_date():
    entity, _ = _entity(value="")
    assert entity.native_value is None


async def test_setting_a_day_writes_the_portal_encoding():
    """The highest-stakes line in this platform: it reaches the heat pump.

    A date entity hands Home Assistant a `date`, and what has to go out is
    the whole-day epoch the portal itself uses - not a local-midnight one,
    and not the number of days.
    """
    entity, _ = _entity()
    written = []
    async def record(value):
        written.append(value)

    entity.async_write_parameter = record

    await entity.async_set_value(date(2026, 8, 4))

    assert written == [END_EPOCH]
    assert entity.native_value == date(2026, 8, 4)


async def test_a_write_is_not_reported_before_the_portal_took_it():
    """If the write raises, the entity must not show the new day: it would
    claim a holiday the heating system never got."""
    entity, _ = _entity()

    async def refuse(_value):
        raise RuntimeError("portal said no")

    entity.async_write_parameter = refuse

    with pytest.raises(RuntimeError):
        await entity.async_set_value(date(2026, 12, 24))

    assert entity.native_value == date(2026, 8, 3), "a refused write was displayed"


def test_a_later_cycle_updates_the_day():
    entity, data = _entity()
    data["1234"]["Heat pump-U_Beginn"]["value"] = END_EPOCH

    entity._handle_coordinator_update()

    assert entity.native_value == date(2026, 8, 4)


def test_a_disappearing_parameter_clears_the_day_instead_of_keeping_it():
    """Better no date than yesterday's presented as today's."""
    entity, data = _entity()
    del data["1234"]["Heat pump-U_Beginn"]

    entity._handle_coordinator_update()

    assert entity.native_value is None


def test_only_date_rows_become_date_entities():
    added = []
    data = {"1234": {
        "Heat pump-U_Beginn": {"platform": "date", "value": BEGIN_EPOCH,
                               "friendlyName": "Holiday begin",
                               "ParameterID": "U_Beginn"},
        "Heat pump-Pump": {"platform": "switch", "value": 1.0,
                           "friendlyName": "Pump", "ParameterID": "Pump"},
        "ConnectionStatus": 0,
    }}
    entry = _Entry()
    entry.runtime_data = types.SimpleNamespace(coordinator=_Coordinator(data))

    asyncio.run(async_setup_entry(None, entry, lambda e: added.extend(e)))

    assert [e._data_key for e in added] == ["Heat pump-U_Beginn"]
