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
from custom_components.wemportal.models import Reading

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


def _api_that_accepts(data):
    """A REAL api with only the portal request stubbed out.

    Standing in for `change_value` itself would mean reproducing what it does
    around the request - take the lock, read the companions, publish what the
    portal accepted - and the write tests below are about exactly that. A
    double that forgets the publishing half leaves every one of them passing
    while the row keeps its pre-write value.
    """
    from custom_components.wemportal.wemportalapi import WemPortalApi

    api = WemPortalApi("user@example.org", "secret")
    api.valid_login = True
    api.data = data
    api._change_value = lambda *_args, **_kwargs: None
    api.device_types = {}
    api.api_version = None
    api.reread_device_values = lambda *_args, **_kwargs: None
    return api


class _Coordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True
        self.listeners = []
        # A portal that accepts a write and confirms whatever the row already
        # says it stored. Tests that care about either replace them.
        self.api = _api_that_accepts(data)

    def async_add_listener(self, update):
        """Real coordinators hand back a remover; nothing here updates."""
        self.listeners.append(update)
        return lambda: self.listeners.remove(update)

    def async_update_listeners(self):
        pass


class _Entry:
    entry_id = "entry-1"

    def async_on_unload(self, remove) -> None:
        """Home Assistant keeps these to call on unload; nothing here unloads."""


def _entity(value=BEGIN_EPOCH):
    data = {
        "1234": {
            "Heat pump-U_Beginn": Reading(
                friendly_name="Holiday begin",
                parameter_id="U_Beginn",
                value=value,
                unit=None,
                platform="date",
                module_index=0,
                module_type=1,
            )
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
    # Every write now ends in a read-back, which goes through the executor.
    entity.hass = types.SimpleNamespace(async_add_executor_job=_run_now)
    return entity, data


async def _run_now(function, *args):
    """Run an executor job inline - there is no event loop thread here."""
    return function(*args)


def _with_companion(data, value=END_EPOCH, module=(0, 1), platform="date"):
    """A second parameter on the device, next to the one under test."""
    data["1234"]["Heat pump-U_Ende"] = Reading(
        friendly_name="Holiday end",
        parameter_id="U_Ende",
        value=value,
        unit=None,
        platform=platform,
        module_index=module[0],
        module_type=module[1],
    )
    return data


def _recorder(entity):
    """Capture what the entity hands to the write path.

    The companions arrive as a callable and are read once the write holds the
    shared api lock - see WemPortalApi.change_value. Resolved here the same
    way, so these tests keep asking what travels with the write rather than
    how it is passed.
    """
    seen = {}

    async def record(value, together_with=None):
        seen["value"] = value
        seen["together_with"] = (
            together_with() if callable(together_with) else together_with
        )

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


async def test_a_day_the_portal_kept_is_displayed():
    """The ordinary case, and the counter-test to the one below: without it,
    an entity that never displays anything would pass that one."""
    entity, _ = _entity()
    _wired(entity)

    await entity.async_set_value(date(2026, 8, 4))

    assert entity.native_value == date(2026, 8, 4)


async def test_a_day_the_portal_did_not_keep_is_not_displayed():
    """`Status: 0` means the request was ACCEPTED, not that the value was
    stored.

    Measured on a live installation: a holiday range that ends before it
    starts is answered exactly that way and silently discarded. Publishing
    the written day on the strength of that answer showed a holiday nobody
    had - until the next poll took it away again, minutes later and with no
    explanation. So the entity asks what was kept instead of assuming.
    """
    entity, data = _entity()
    _wired(entity)

    def portal_kept_the_old_value(_device_id):
        data["1234"]["Heat pump-U_Beginn"].value = BEGIN_EPOCH

    entity.coordinator.api.reread_device_values = portal_kept_the_old_value

    await entity.async_set_value(date(2026, 12, 24))

    assert entity.native_value == date(2026, 8, 3), (
        "a day the portal discarded was displayed as if it had been set"
    )


async def test_a_failed_read_back_does_not_fail_the_write(caplog):
    """The write went through; what is unknown is whether it was kept. Raising
    here would report a failed service call for a write that happened."""
    import logging

    entity, _ = _entity()
    _wired(entity)

    entity.coordinator.api.reread_device_values = lambda _d: "the portal timed out"

    with caplog.at_level(logging.WARNING):
        await entity.async_set_value(date(2026, 8, 4))

    assert "could not read the value back" in caplog.text


async def test_a_day_that_could_not_be_read_back_is_not_shown_as_set():
    """An unverified write must not be displayed as a verified one.

    The whole reason for the read-back is that `Status: 0` says the request
    was accepted, not that the value was stored. When the read-back itself
    fails, that question is simply unanswered - and the entity showed the
    written day anyway, which is the exact claim the read-back exists to
    avoid making.

    The coordinator row matters more than the displayed value here: the row
    is what the entity reads on every update, so leaving the written day in
    it would put the unconfirmed value straight back at the next poll.
    """
    entity, data = _entity()
    _wired(entity)

    entity.coordinator.api.reread_device_values = lambda _d: "the portal timed out"

    await entity.async_set_value(date(2026, 12, 24))

    assert entity.native_value is None, (
        "a day nobody could confirm was displayed as if it had been set"
    )
    assert data["1234"]["Heat pump-U_Beginn"].value is None, (
        "the unconfirmed day stayed in the coordinator row, so the next "
        "update puts it back on display"
    )


async def test_a_day_that_was_read_back_is_still_shown():
    """The counter-test: a successful read-back must still display."""
    entity, data = _entity()
    _wired(entity)

    def portal_kept_it(_device_id):
        data["1234"]["Heat pump-U_Beginn"].value = date_to_epoch(date(2026, 12, 24))

    entity.coordinator.api.reread_device_values = portal_kept_it

    await entity.async_set_value(date(2026, 12, 24))

    assert entity.native_value == date(2026, 12, 24)


async def test_a_write_is_not_reported_before_the_portal_took_it():
    """If the write raises, the entity must not show the new day: it would
    claim a holiday the heating system never got."""
    entity, _ = _entity()

    async def refuse(_value, together_with=None):
        raise RuntimeError("portal said no")

    entity.async_write_parameter = refuse

    christmas_eve = date(2026, 12, 24)

    with pytest.raises(RuntimeError):
        await entity.async_set_value(christmas_eve)

    assert entity.native_value == date(2026, 8, 3), "a refused write was displayed"


def test_a_later_cycle_updates_the_day():
    entity, data = _entity()
    data["1234"]["Heat pump-U_Beginn"].value = END_EPOCH

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
    """Let the write reach the real write path rather than a recorder.

    The point of these tests is the state left BEHIND by a write, not what
    went out - so the api simply says yes. `runtime_data` is what the
    writable-entry gate reads.
    """
    from custom_components.wemportal.models import WemPortalData

    entity._config_entry.runtime_data = WemPortalData(
        api=entity.coordinator.api, coordinator=None
    )
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
            "Heat pump-U_Beginn": Reading(
                platform="date",
                value=BEGIN_EPOCH,
                friendly_name="Holiday begin",
                parameter_id="U_Beginn",
            ),
            "Heat pump-Pump": Reading(
                platform="switch", value=1.0, friendly_name="Pump", parameter_id="Pump"
            ),
            "ConnectionStatus": 0,
        }
    }
    entry = _Entry()
    entry.runtime_data = types.SimpleNamespace(coordinator=_Coordinator(data))

    asyncio.run(async_setup_entry(None, entry, lambda entities: added.extend(entities)))

    assert [entity._data_key for entity in added] == ["Heat pump-U_Beginn"]
