"""The rules the four entity platforms share, enforced rather than assumed.

Identity, device assignment and availability used to be copy-pasted into
sensor.py, number.py, select.py and switch.py. A fix then had to be applied
four times, and the diagnostic-availability rule really was fixed in three of
them only. WemPortalEntity now owns those rules; these tests fail if a
platform drifts back out of it.
"""

import asyncio
import types
from dataclasses import replace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.wemportal.entity import WemPortalEntity
from custom_components.wemportal.models import Reading
from custom_components.wemportal.number import WemPortalNumber
from custom_components.wemportal.date import WemPortalDate
from custom_components.wemportal.select import WemPortalSelect
from custom_components.wemportal.sensor import WemPortalSensor
from custom_components.wemportal.switch import WemPortalSwitch

PLATFORMS = {
    "sensor": WemPortalSensor,
    "number": WemPortalNumber,
    "select": WemPortalSelect,
    "switch": WemPortalSwitch,
    "date": WemPortalDate,
}

# The rules that belong to the base. sensor.py overrides both on purpose: it
# reports the portal's api version as the device's software version, and it
# keeps the three diagnostic entities available while the device is not.
SHARED = ("device_info", "available")
ALLOWED_OVERRIDES = {"sensor"}


def _entity(cls, reachable=True, last_update_success=True, num_failed=0, **overrides):
    """One entity of `cls`, built without Home Assistant."""
    row = replace(
        Reading(
            value=1.0,
            unit="°C",
            friendly_name="Pump",
            parameter_id="P1",
            module_index=0,
            module_type=1,
            min_value=0.0,
            max_value=100.0,
            step=1,
            options=["0", "1"],
            options_names=["Aus", "Ein"],
        ),
        **overrides,
    )
    # The status row is what device_is_reachable() reads; "offline" is one of
    # utils.UNREACHABLE_CONNECTION_STATES.
    status = Reading(value="online" if reachable else "offline")
    coordinator = types.SimpleNamespace(
        data={"1234": {"Pump": row, "1234-ConnectionStatus": status}},
        api=types.SimpleNamespace(api_version="2.0", modules={}),
        last_update_success=last_update_success,
        num_failed=num_failed,
        async_add_listener=lambda *_args, **_kwargs: None,
    )
    entry = types.SimpleNamespace(entry_id="e1")
    return cls(coordinator, entry, "1234", "Pump", row)


def _run(method, *args):
    """Drive one coroutine to completion from a synchronous test.

    Takes the method and its arguments so a `pytest.raises` block around it
    contains a single call - spelled out as asyncio.run(entity.method(x)) it
    contains two, and a failure in the outer one would satisfy the test just
    as well.
    """
    return asyncio.run(method(*args))


def _select_capturing_its_writes(**overrides):
    """A select entity whose write is recorded instead of performed.

    The write itself belongs to the shared base and is tested there; what
    this file is asking is which VALUE the platform decides to hand it.
    """
    entity = _entity(WemPortalSelect, **overrides)
    written = []

    async def _capture(value, together_with=None):
        written.append(value)

    entity.async_write_parameter = _capture
    entity.async_write_ha_state = lambda: None
    return entity, written


def _entity_that_can_actually_write(cls, **overrides):
    """An entity whose existing write gate is satisfied, so a refusal below
    can only come from the check under test.

    Spelled out rather than assumed: `raise_if_not_writable` refuses an entry
    with no runtime_data, and a test built on that stub would pass for a
    reason that has nothing to do with what it claims to check.
    """
    entity = _entity(cls, **overrides)
    entity._config_entry.runtime_data = types.SimpleNamespace(
        why_not_current=lambda _entry: None
    )
    # Bound before the executor runs, so it has to exist even in the case
    # where the write must never happen.
    entity.coordinator.api.change_value = lambda *args, **kwargs: None
    reached_the_portal = []

    async def _executor(call):
        reached_the_portal.append(call)

    entity.hass = types.SimpleNamespace(async_add_executor_job=_executor)
    return entity, reached_the_portal


def test_a_control_writes_while_its_reading_is_there():
    """The control case for the test below - without it, a refusal proves
    nothing about the reason for the refusal."""
    entity, reached_the_portal = _entity_that_can_actually_write(WemPortalNumber)

    _run(entity.async_write_parameter, 21.0)

    assert len(reached_the_portal) == 1


def test_a_control_whose_reading_is_gone_does_not_write():
    """A parameter the portal stopped describing has its reading removed -
    and the entity outlives it until the next reload.

    It kept the module address it was built with, so a click still sent a
    write for a parameter that is no longer there. Removing the reading is
    recent (it used to linger with a stale value), which is what turned this
    from a wrong display into a wrong write.
    """
    entity, reached_the_portal = _entity_that_can_actually_write(WemPortalNumber)
    del entity.coordinator.data["1234"]["Pump"]

    with pytest.raises(HomeAssistantError):
        _run(entity.async_write_parameter, 21.0)

    assert reached_the_portal == [], "a write was sent for a reading that is gone"


def test_a_control_does_not_write_when_its_row_became_another_platform():
    """The row can stay and still stop belonging to this entity.

    The daily re-discovery re-reads what the portal says a parameter is, and
    that classification decides the platform. When it changes, the listener
    builds an entity of the NEW platform for the same row - and the old one
    stays loaded until the next reload, holding the module address it was
    built with. The existing gate only asks whether a row is still there,
    which it is, so a leftover switch could go on sending 0/1 to a parameter
    the portal now describes as something else.
    """
    entity, reached_the_portal = _entity_that_can_actually_write(
        WemPortalNumber, platform="number"
    )
    entity.coordinator.data["1234"]["Pump"].platform = "date"

    with pytest.raises(HomeAssistantError):
        _run(entity.async_write_parameter, 21.0)

    assert reached_the_portal == [], (
        "a write was sent for a row that is no longer this platform's"
    )


# What each platform publishes, asked the way Home Assistant asks it. The
# attribute behind it differs per platform, and pinning the attribute would
# make this test agree with an implementation detail rather than with what a
# dashboard shows.
DISPLAYED_VALUE = {
    "sensor": lambda entity: entity.native_value,
    "number": lambda entity: entity.native_value,
    "date": lambda entity: entity.native_value,
    "select": lambda entity: entity.current_option,
    "switch": lambda entity: entity.is_on,
}


def _entity_showing_a_value(name):
    """One entity per platform, built so it actually publishes something.

    select resolves its reading against its option list, and the numeric
    value the shared fixture carries is not one of them - so it sits at None
    whatever the guard below does. A subject that publishes nothing anyway
    would make the test pass for a reason that has nothing to do with what it
    claims to check, which is why the control case beside it exists.
    """
    return _entity(
        PLATFORMS[name], platform=name, value="1" if name == "select" else 1.0
    )


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_a_row_that_became_another_platform_is_not_displayed(name):
    """The reading half of the write gate above.

    Both entities exist at once after a reclassification - the listener builds
    the new platform's entity while the old one stays until the next reload -
    and both read the same row. The write path asks whether the row is still
    this entity's; the update handlers only asked whether it was there, so the
    leftover entity went on rendering someone else's value as its own type: a
    holiday epoch shown as a switch that is on, a 0/1 shown as a date in 1970.

    Parametrised over all five because the handlers are five copies of the
    same lookup - which is exactly how the availability rule once reached
    three platforms out of four.
    """
    entity = _entity_showing_a_value(name)
    entity.async_write_ha_state = lambda: None
    entity.coordinator.data["1234"]["Pump"].platform = "something else"

    entity._handle_coordinator_update()

    assert DISPLAYED_VALUE[name](entity) is None, (
        f"{name} published a value from a row that is no longer its own"
    )


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_a_row_of_this_platform_is_still_displayed(name):
    """The control case: without it, an entity that publishes nothing at all
    would satisfy the test above."""
    entity = _entity_showing_a_value(name)
    entity.async_write_ha_state = lambda: None

    entity._handle_coordinator_update()

    assert DISPLAYED_VALUE[name](entity) is not None


def test_the_chosen_name_writes_the_value_that_belongs_to_it():
    entity, written = _select_capturing_its_writes(
        options=["0", "1"], options_names=["Aus", "Ein"]
    )

    _run(entity.async_select_option, "Ein")

    assert written == ["1"]


def test_two_options_with_one_name_do_not_write_a_guessed_value():
    """The value was chosen by the POSITION of the first matching name.

    The portal decides what an EnumValues list looks like, and two entries
    sharing a display name make that position a coin toss: picking the
    second one wrote the first one's value into the heating system, while
    the entity went on showing the name that was clicked. Nothing in the
    log, nothing in the state - the setting was simply not what was asked
    for.

    Refusing is the only honest answer here: which of the two the user meant
    is not knowable from what the portal sent.
    """
    entity, written = _select_capturing_its_writes(
        options=["0", "1"], options_names=["Automatik", "Automatik"]
    )

    with pytest.raises(HomeAssistantError):
        _run(entity.async_select_option, "Automatik")

    assert written == [], "a value was written for a name that names two of them"


def test_an_option_the_lists_no_longer_agree_on_is_not_written_either():
    """The two lists are refreshed under separate guards, so they can end up
    paired by position without being paired by content. A name with no value
    of its own must not fall back to whatever sits at that index."""
    entity, written = _select_capturing_its_writes(
        options=["0"], options_names=["Aus", "Ein"]
    )

    with pytest.raises(HomeAssistantError):
        _run(entity.async_select_option, "Ein")

    assert written == []


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_every_platform_builds_on_the_shared_base(name):
    """A new platform that copies the old pattern instead of inheriting is
    exactly how the duplication grew in the first place."""
    assert issubclass(PLATFORMS[name], WemPortalEntity)


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_no_platform_redefines_a_shared_rule(name):
    """Guards the deduplication itself.

    A platform that defines `available` again is not a compile error and not
    a test failure anywhere else - it just silently stops following the
    shared rule, which is the state this refactor removed.
    """
    redefined = [rule for rule in SHARED if rule in vars(PLATFORMS[name])]
    if name in ALLOWED_OVERRIDES:
        assert redefined, (
            f"{name} no longer overrides {SHARED} - if that is intended, drop "
            "it from ALLOWED_OVERRIDES here"
        )
    else:
        assert not redefined, f"{name} redefines {redefined} instead of using the base"


def test_a_device_named_like_a_status_row_is_not_diagnostic():
    """The category is decided on the parameter id alone.

    The former substring match ran over the whole unique_id - which also
    carries the entry id and the DEVICE id - so a device whose portal name
    contains a status word turned every one of its sensors into a
    diagnostic entity."""
    from homeassistant.const import EntityCategory

    row = Reading(value=1.0, friendly_name="Flow", parameter_id="P1")
    coordinator = types.SimpleNamespace(
        data={"HasErrors-Unit": {"flow": row}},
        api=types.SimpleNamespace(api_version=None, modules={}),
        last_update_success=True,
        num_failed=0,
        async_add_listener=lambda *_args, **_kwargs: None,
    )
    entity = WemPortalSensor(
        coordinator, types.SimpleNamespace(entry_id="e1"), "HasErrors-Unit", "flow", row
    )

    assert entity.entity_category is None, (
        "a status word in the DEVICE name made an ordinary sensor diagnostic"
    )
    assert (
        _entity(WemPortalSensor, parameter_id="HasErrors").entity_category
        is EntityCategory.DIAGNOSTIC
    )


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_the_entities_are_never_polled(name):
    """Three of the four used to set `_attr_should_poll = False` by hand.

    CoordinatorEntity answers should_poll with a property, so those
    assignments were never read - which is why they could be dropped. If a
    Home Assistant release ever changed that, every entity would start
    polling, and each poll asks the portal again: the account is blocked for
    12 hours after 10,000 requests.
    """
    assert _entity(PLATFORMS[name]).should_poll is False


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_identity_is_built_the_same_way_everywhere(name):
    """The unique_id is what ties an entity to its recorded history; four
    copies of the formula is four chances for one to drift."""
    entity = _entity(PLATFORMS[name])
    assert entity.unique_id == "e1:1234:Pump"
    assert entity.name == "Pump"
    assert entity._parameter_id == "P1"
    # Without this the entity_id becomes device_name_entity_name instead of
    # just entity_name, which renames every entity in the installation.
    assert entity.has_entity_name is True


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_an_icon_from_the_data_is_used(name):
    """Only the icon the row actually carries is applied.

    Defaulting to "mdi:flash" here used to override the icon Home Assistant
    derives from the device class on every entity that has one. The fix has
    lived in four copies since, untested in all four.
    """
    entity = _entity(PLATFORMS[name], icon="mdi:thermometer")
    assert entity.icon == "mdi:thermometer"

    plain = _entity(PLATFORMS[name], icon=None)
    # Checked as "was never assigned" rather than "is None": Home Assistant
    # reads _attr_icon, so pinning it to None looks the same from the
    # outside while still shutting out the device-class icon.
    assert "_attr_icon" not in vars(plain)


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_a_missing_reading_is_not_reported_as_a_fault(name, caplog):
    """`None` means the portal sent no value for this parameter this cycle.

    That is a normal condition - the portal regularly omits a parameter, and
    _clear_unanswered deliberately blanks one it left out so a stale reading
    is not published as current. Two of the four platforms warned about it
    anyway, so the integration's own bookkeeping was reported as a fault;
    select even attached all 49 option names to say "no value". Parametrised
    because that is precisely how this drifted apart: the rule is stated in
    sensor.py, and was applied to two platforms out of four.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _entity(PLATFORMS[name], value=None)

    assert not caplog.records, f"{name} warned about a missing reading: {caplog.text}"


def test_a_number_that_is_present_but_unusable_still_warns(caplog):
    """The other half of the rule - without this, a platform that never warns
    at all would pass the test above."""
    import logging

    with caplog.at_level(logging.WARNING):
        _entity(WemPortalNumber, value="Ein")

    assert caplog.records, "an unusable number value passed silently"


def test_a_select_value_outside_its_options_still_warns(caplog):
    """Same, for the platform whose warning started this."""
    import logging

    with caplog.at_level(logging.WARNING):
        _entity(WemPortalSelect, value="nothing like an option")

    assert caplog.records, "an unresolvable option passed silently"


def _warnings(caplog):
    """Only what a user would see as a fault.

    Filtered by level rather than trusting `caplog.at_level` to keep the rest
    out: the debug line each platform logs while being CONSTRUCTED lands in
    the same record list, and a test reading all of them fails for a reason
    that has nothing to do with what it asks.
    """
    import logging

    return [record for record in caplog.records if record.levelno >= logging.WARNING]


def _entity_whose_row_moved_platform(cls):
    """An entity of the platform its parameter no longer is.

    Built from a row of its own platform and then handed a reclassified one,
    because that is the order it happens in: `_platform` is taken at
    construction, and the re-discovery changes the row underneath a loaded
    entity.
    """
    entity = _entity(cls)
    entity.async_write_ha_state = lambda: None
    rows = entity.coordinator.data["1234"]
    rows["Pump"] = replace(rows["Pump"], platform="somewhere-else")
    return entity


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_a_row_that_moved_platform_is_not_reported_as_a_missing_one(name, caplog):
    """ "Can't find" is the wrong thing to say, and it says it forever.

    A parameter's platform is decided from the value of a single cycle, so one
    odd answer moves a row and the next one moves it back - and a
    reclassification that STICKS leaves this entity loaded until the next
    reload. Both cases had every affected entity warning once per cycle about
    a row that is right there, just not its own. The condition is known,
    expected and self-correcting; what it is not is a fault to report.
    """
    import logging

    entity = _entity_whose_row_moved_platform(PLATFORMS[name])

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        entity._handle_coordinator_update()

    assert not _warnings(caplog), (
        f"{name} reported a reclassified row as missing: {caplog.text}"
    )
    # Not silence either: the entity IS showing nothing, and the reason has to
    # be findable. Asserted here so the fix cannot be "drop the line".
    assert "somewhere-else" in caplog.text, (
        f"{name} says nothing at all about why it has no value"
    )


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_a_row_that_is_really_gone_is_still_reported(name, caplog):
    """The other half, without which a platform that stopped warning at all
    would pass the test above."""
    import logging

    entity = _entity(PLATFORMS[name])
    entity.async_write_ha_state = lambda: None
    entity.coordinator.data["1234"].pop("Pump")

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        entity._handle_coordinator_update()

    assert _warnings(caplog), f"{name} lost its reading without saying so"


def test_an_unreachable_device_takes_its_entities_with_it():
    """The availability rule now lives in one place; this is that place
    doing its job for a platform that no longer carries its own copy."""
    assert _entity(WemPortalNumber, reachable=True).available is True
    assert _entity(WemPortalNumber, reachable=False).available is False


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_one_failed_cycle_does_not_take_every_entity_with_it(name):
    """The portal answers a cycle with "Unbekannter Fehler" and the next one
    works.

    Every entity of the account went unavailable for that - half an hour of
    every graph at the default interval, plus a state change out and back for
    anything automating on it. The web scrape already tolerated three failures
    before ageing its values; the API side tolerated none.

    Every platform, not just one: sensor.py overrides `available` for its
    diagnostic entities and spelled the cycle rule out again, so the tolerance
    reached numbers, selects and switches but not the sensors - which are most
    of the entities on an installation. A test on one platform could not see
    that.
    """
    survives = _entity(PLATFORMS[name], last_update_success=False, num_failed=1)

    assert survives.available is True


@pytest.mark.parametrize("name", sorted(PLATFORMS))
def test_a_second_failed_cycle_does_take_them(name):
    """The tolerance is one cycle, not an open licence to show old numbers."""
    gone = _entity(PLATFORMS[name], last_update_success=False, num_failed=2)

    assert gone.available is False


def test_an_unreachable_device_stays_gone_through_a_tolerated_failure():
    """The two rules are independent: a device that is off does not come back
    just because the cycle it missed was the first one to fail."""
    still_gone = _entity(
        WemPortalNumber, reachable=False, last_update_success=False, num_failed=1
    )

    assert still_gone.available is False


# --- the one write path, and its gate ----------------------------------
#
# `unloading` is set at the top of async_unload_entry, before the platforms
# come down. Tearing four platforms and their entities down is real time, so a
# click can land inside it - and only the domain service used to check. Number,
# Select and Switch each had their own copy of the write call and none of them
# asked, so a write could start into a session about to be closed.

WRITEABLE = {name: cls for name, cls in PLATFORMS.items() if name != "sensor"}


def _writeable(cls, unloading=False, calls=None):
    """A writeable entity whose entry carries runtime data."""
    from custom_components.wemportal.models import WemPortalData

    entity = _entity(cls)
    # Records keyword arguments too: the write goes through functools.partial
    # now, because the date platform sends companion parameters by name.
    api = types.SimpleNamespace(
        change_value=lambda *args, **kwargs: (
            calls if calls is not None else []
        ).append((args, kwargs))
    )
    data = WemPortalData(api=api, coordinator=None)
    data.unloading = unloading
    entity._config_entry.runtime_data = data
    entity.coordinator.api = api

    async def _executor(function, *args):
        return function(*args)

    entity.hass = types.SimpleNamespace(async_add_executor_job=_executor)
    return entity


@pytest.mark.parametrize("name", sorted(WRITEABLE))
async def test_no_platform_writes_while_the_entry_is_unloading(name):
    from homeassistant.exceptions import HomeAssistantError

    calls = []
    entity = _writeable(WRITEABLE[name], unloading=True, calls=calls)

    with pytest.raises(HomeAssistantError):
        await entity.async_write_parameter(21.0)

    assert calls == [], f"{name} started a write while the entry was unloading"


@pytest.mark.parametrize("name", sorted(WRITEABLE))
async def test_no_platform_writes_after_the_entry_is_gone(name):
    from homeassistant.exceptions import HomeAssistantError

    calls = []
    entity = _writeable(WRITEABLE[name], calls=calls)
    del entity._config_entry.runtime_data

    with pytest.raises(HomeAssistantError):
        await entity.async_write_parameter(21.0)

    assert calls == []


@pytest.mark.parametrize("name", sorted(WRITEABLE))
async def test_the_write_reaches_the_api_with_the_parameter_address(name):
    """The other half: three copies became one, so the call it makes has to
    be pinned or the deduplication could quietly change it."""
    calls = []
    entity = _writeable(WRITEABLE[name], calls=calls)

    await entity.async_write_parameter(21.0)

    assert calls == [(("1234", "P1", 0, 1, 21.0), {"together_with": None})]


@pytest.mark.parametrize("name", sorted(WRITEABLE))
async def test_a_write_brings_the_coordinators_copy_up_to_date(name):
    """The row keeps the value from the last poll, minutes ago - and the
    date platform reads it to build the companions of the NEXT write. Two
    writes inside one poll interval put a superseded value on the wire."""
    entity = _writeable(WRITEABLE[name])

    await entity.async_write_parameter(42.0)

    assert entity.coordinator.data["1234"]["Pump"].value == 42.0


@pytest.mark.parametrize("name", sorted(WRITEABLE))
async def test_a_refused_write_leaves_the_coordinators_copy_alone(name):
    """Recording a value the heating system never took would make the
    integration certain of the wrong thing - and hand it to the next write
    as a companion."""
    entity = _writeable(WRITEABLE[name])

    def refuse(*_args, **_kwargs):
        raise RuntimeError("portal said no")

    entity.coordinator.api.change_value = refuse

    with pytest.raises(RuntimeError):
        await entity.async_write_parameter(42.0)

    assert entity.coordinator.data["1234"]["Pump"].value == 1.0


def test_a_reloaded_entry_invalidates_an_operation_holding_the_old_state():
    """The half the unloading flag cannot answer.

    A reload puts a NEW runtime state under the SAME entry id. An operation
    that captured the old one - an expert write in an executor thread, say -
    would otherwise keep going with the credentials and options of a
    configuration that no longer exists, and the id would tell it nothing.
    """
    from custom_components.wemportal.models import WemPortalData

    entity = _writeable(WemPortalNumber)
    old_state = entity._config_entry.runtime_data

    assert old_state.why_not_current(entity._config_entry) is None

    entity._config_entry.runtime_data = WemPortalData(api=None, coordinator=None)

    assert old_state.why_not_current(entity._config_entry) is not None
    assert "reloaded" in old_state.why_not_current(entity._config_entry)


def test_the_teardown_is_announced_as_an_operation():
    """begin_unload is what the four gates read, so it has to be the thing
    that sets the flag - not a second way of writing the same assignment."""
    from custom_components.wemportal.models import WemPortalData

    data = WemPortalData(api=None, coordinator=None)
    entry = types.SimpleNamespace(entry_id="e1")
    entry.runtime_data = data

    assert data.why_not_current(entry) is None

    data.begin_unload()

    assert data.why_not_current(entry) is not None
    assert "unload" in data.why_not_current(entry)
