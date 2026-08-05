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
    data = {
        "1234": {
            "Heat pump-U_Beginn": {
                "friendlyName": "Holiday begin",
                "ParameterID": "U_Beginn",
                "value": value,
                "unit": None,
                "platform": "date",
                "ModuleIndex": 0,
                "ModuleType": 1,
            }
        }
    }
    entity = WemPortalDate(
        _Coordinator(data),
        _Entry(),
        "1234",
        "Heat pump-U_Beginn",
        data["1234"]["Heat pump-U_Beginn"],
    )
    entity.async_write_ha_state = lambda: None
    return entity, data


def _with_companion(data, value=END_EPOCH, module=(0, 1), platform="date"):
    """A second parameter on the device, next to the one under test."""
    data["1234"]["Heat pump-U_Ende"] = {
        "friendlyName": "Holiday end",
        "ParameterID": "U_Ende",
        "value": value,
        "unit": None,
        "platform": platform,
        "ModuleIndex": module[0],
        "ModuleType": module[1],
    }
    return data


def _recorder(entity):
    """Capture what the entity hands to the write path."""
    seen = {}

    async def record(value, together_with=None):
        seen["value"] = value
        seen["together_with"] = together_with

    entity.async_write_parameter = record
    return seen


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
    seen = _recorder(entity)

    await entity.async_set_value(date(2026, 8, 4))

    assert seen["value"] == END_EPOCH
    assert entity.native_value == date(2026, 8, 4)


async def test_a_write_is_not_reported_before_the_portal_took_it():
    """If the write raises, the entity must not show the new day: it would
    claim a holiday the heating system never got."""
    entity, _ = _entity()

    async def refuse(_value, together_with=None):
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


# --- a holiday is a range, so it travels as one ------------------------
#
# Measured, not assumed: begin and end are marked writeable and read back
# fine, but written one at a time each write comes back Status -1 with no
# JobID - while an ordinary setpoint on the same account and the same
# endpoint is accepted and answered with one. So the write carries the
# module's other dates along at their current value.


async def test_the_other_date_of_the_module_travels_with_the_write():
    entity, data = _entity()
    _with_companion(data)
    seen = _recorder(entity)

    await entity.async_set_value(date(2026, 8, 4))

    assert seen["together_with"] == {"U_Ende": END_EPOCH}


async def test_a_companion_without_a_readable_value_is_left_out():
    """Sending a guess would put a date on the heating system that nobody
    asked for - worse than writing the one parameter on its own."""
    entity, data = _entity()
    _with_companion(data, value="")
    seen = _recorder(entity)

    await entity.async_set_value(date(2026, 8, 4))

    assert seen["together_with"] == {}


async def test_a_date_belonging_to_another_module_is_not_dragged_in():
    """The portal addresses parameters per module; a date from a different
    one is a different setting on a different circuit."""
    entity, data = _entity()
    _with_companion(data, module=(1, 2))
    seen = _recorder(entity)

    await entity.async_set_value(date(2026, 8, 4))

    assert seen["together_with"] == {}


async def test_only_dates_are_taken_along():
    """A switch of the same module is not part of the range."""
    entity, data = _entity()
    _with_companion(data, platform="switch")
    seen = _recorder(entity)

    await entity.async_set_value(date(2026, 8, 4))

    assert seen["together_with"] == {}


def _wired(entity):
    """Give the entity a write path the portal accepts, and record nothing.

    The point of these two tests is the state left BEHIND by a write, not
    what went out - so the api simply says yes.
    """
    from custom_components.wemportal.models import WemPortalData

    api = types.SimpleNamespace(change_value=lambda *_a, **_k: None)
    entity._config_entry.runtime_data = WemPortalData(api=api, coordinator=None)
    entity.coordinator.api = api

    async def _executor(func, *args):
        return func(*args)

    entity.hass = types.SimpleNamespace(async_add_executor_job=_executor)
    return entity


async def test_the_next_write_sees_what_the_last_one_wrote():
    """The companions come from the coordinator, and the next poll is
    minutes away. Measured on a live installation: two writes four seconds
    apart, and the second carried a begin date two days out of date - the
    one the first write had just replaced. The portal was being asked to
    undo what it had just been told.
    """
    entity, data = _entity()
    _with_companion(data)
    _wired(entity)

    await entity.async_set_value(date(2026, 8, 6))

    sibling = WemPortalDate(
        entity.coordinator,
        entity._config_entry,
        "1234",
        "Heat pump-U_Ende",
        data["1234"]["Heat pump-U_Ende"],
    )
    assert sibling._companion_dates() == {"U_Beginn": date_to_epoch(date(2026, 8, 6))}


def test_only_date_rows_become_date_entities():
    added = []
    data = {
        "1234": {
            "Heat pump-U_Beginn": {
                "platform": "date",
                "value": BEGIN_EPOCH,
                "friendlyName": "Holiday begin",
                "ParameterID": "U_Beginn",
            },
            "Heat pump-Pump": {
                "platform": "switch",
                "value": 1.0,
                "friendlyName": "Pump",
                "ParameterID": "Pump",
            },
            "ConnectionStatus": 0,
        }
    }
    entry = _Entry()
    entry.runtime_data = types.SimpleNamespace(coordinator=_Coordinator(data))

    asyncio.run(async_setup_entry(None, entry, lambda e: added.extend(e)))

    assert [e._data_key for e in added] == ["Heat pump-U_Beginn"]
