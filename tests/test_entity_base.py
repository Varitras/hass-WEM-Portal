"""The rules the four entity platforms share, enforced rather than assumed.

Identity, device assignment and availability used to be copy-pasted into
sensor.py, number.py, select.py and switch.py. A fix then had to be applied
four times, and the diagnostic-availability rule really was fixed in three of
them only. WemPortalEntity now owns those rules; these tests fail if a
platform drifts back out of it.
"""

import types

import pytest

from custom_components.wemportal.entity import WemPortalEntity
from custom_components.wemportal.number import WemPortalNumber
from custom_components.wemportal.select import WemPortalSelect
from custom_components.wemportal.sensor import WemPortalSensor
from custom_components.wemportal.switch import WemPortalSwitch

PLATFORMS = {
    "sensor": WemPortalSensor,
    "number": WemPortalNumber,
    "select": WemPortalSelect,
    "switch": WemPortalSwitch,
}

# The rules that belong to the base. sensor.py overrides both on purpose: it
# reports the portal's api version as the device's software version, and it
# keeps the three diagnostic entities available while the device is not.
SHARED = ("device_info", "available")
ALLOWED_OVERRIDES = {"sensor"}


def _entity(cls, reachable=True, **overrides):
    """One entity of `cls`, built without Home Assistant."""
    row = {
        "value": 1.0, "unit": "°C", "friendlyName": "Pump",
        "ParameterID": "P1", "ModuleIndex": 0, "ModuleType": 1,
        "min_value": 0.0, "max_value": 100.0, "step": 1,
        "options": ["0", "1"], "optionsNames": ["Aus", "Ein"],
    }
    row.update(overrides)
    # The status row is what device_is_reachable() reads; "offline" is one of
    # utils.UNREACHABLE_CONNECTION_STATES.
    status = {"value": "online" if reachable else "offline"}
    coordinator = types.SimpleNamespace(
        data={"1234": {"Pump": row, "1234-ConnectionStatus": status}},
        api=types.SimpleNamespace(api_version="2.0", modules={}),
        last_update_success=True,
        async_add_listener=lambda *_a, **_k: None,
    )
    entry = types.SimpleNamespace(entry_id="e1")
    return cls(coordinator, entry, "1234", "Pump", row)


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
    redefined = [attr for attr in SHARED if attr in vars(PLATFORMS[name])]
    if name in ALLOWED_OVERRIDES:
        assert redefined, (
            f"{name} no longer overrides {SHARED} - if that is intended, drop "
            "it from ALLOWED_OVERRIDES here"
        )
    else:
        assert not redefined, f"{name} redefines {redefined} instead of using the base"


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


def test_an_unreachable_device_takes_its_entities_with_it():
    """The availability rule now lives in one place; this is that place
    doing its job for a platform that no longer carries its own copy."""
    assert _entity(WemPortalNumber, reachable=True).available is True
    assert _entity(WemPortalNumber, reachable=False).available is False


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

    async def _executor(func, *args):
        return func(*args)

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

    assert old_state.is_current_for(entity._config_entry) is True

    entity._config_entry.runtime_data = WemPortalData(api=None, coordinator=None)

    assert old_state.is_current_for(entity._config_entry) is False
    assert "reloaded" in old_state.why_not_current(entity._config_entry)


def test_the_teardown_is_announced_as_an_operation():
    """begin_unload is what the four gates read, so it has to be the thing
    that sets the flag - not a second way of writing the same assignment."""
    from custom_components.wemportal.models import WemPortalData

    data = WemPortalData(api=None, coordinator=None)
    entry = types.SimpleNamespace(entry_id="e1")
    entry.runtime_data = data

    assert data.is_current_for(entry) is True

    data.begin_unload()

    assert data.is_current_for(entry) is False
    assert "unload" in data.why_not_current(entry)
