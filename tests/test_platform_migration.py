"""What happens to an entity whose platform changed between releases.

Our unique_id does not carry the platform, so a parameter that moves from one
platform to another leaves its old registry entry behind: it stops being
provided, goes unavailable, and sits next to the working entity forever.
Holiday begin and end did exactly that when the portal's parameter list showed
them to be dates rather than switches.

Removal is destructive, so the rules are pinned here rather than assumed: only
our own unique_ids, only our own platforms, and never the entity that is
currently correct.
"""

from custom_components.wemportal import (
    _remove_entities_from_a_previous_platform,
    get_wemportal_unique_id,
)
from custom_components.wemportal.const import DOMAIN
from custom_components.wemportal.models import Reading

ENTRY_ID = "entry-1"
DEVICE = "1234"


class FakeConfigEntry:
    entry_id = ENTRY_ID

    def async_on_unload(self, remove) -> None:
        """Home Assistant keeps these; nothing here unloads."""


class FakeRegistry:
    """Just enough registry: a lookup by (platform, unique_id) and a remove."""

    def __init__(self, entries, owners=None):
        self.entries = dict(entries)
        # Every entity belongs to this entry unless a test says otherwise.
        self.owners = dict(owners or {entity: ENTRY_ID for entity in entries.values()})
        self.removed = []
        self.renamed = []
        self.domains_asked = set()

    def async_get_entity_id(self, platform, domain, unique_id):
        self.domains_asked.add(domain)
        return self.entries.get((platform, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)

    def async_get(self, entity_id):
        """Which config entry an entity belongs to.

        The migration searches by unique_id shapes that predate the account
        prefix, so the registry can answer with an entity of a DIFFERENT WEM
        account. Only this object knows that, which is why the fake has to.
        """
        import types

        owner = self.owners.get(entity_id)
        return None if owner is None else types.SimpleNamespace(config_entry_id=owner)

    def async_update_entity(self, entity_id, new_unique_id):
        self.renamed.append((entity_id, new_unique_id))


def _uid(key):
    return get_wemportal_unique_id(ENTRY_ID, DEVICE, key)


def _run(registry, data):
    _remove_entities_from_a_previous_platform(registry, FakeConfigEntry(), DEVICE, data)


def test_the_entity_left_behind_by_a_platform_change_is_removed():
    registry = FakeRegistry(
        {
            ("switch", _uid("Heat pump-U_Beginn")): "switch.heat_pump_holiday_begin",
            ("date", _uid("Heat pump-U_Beginn")): "date.heat_pump_holiday_begin",
        }
    )

    _run(registry, {"Heat pump-U_Beginn": Reading(platform="date")})

    assert registry.removed == ["switch.heat_pump_holiday_begin"]


def test_the_entity_that_is_currently_correct_is_never_removed():
    registry = FakeRegistry(
        {
            ("date", _uid("Heat pump-U_Beginn")): "date.heat_pump_holiday_begin",
        }
    )

    _run(registry, {"Heat pump-U_Beginn": Reading(platform="date")})

    assert registry.removed == []


def test_an_unchanged_platform_removes_nothing():
    registry = FakeRegistry(
        {
            ("switch", _uid("Heat pump-Pump")): "switch.heat_pump_pump",
        }
    )

    _run(registry, {"Heat pump-Pump": Reading(platform="switch")})

    assert registry.removed == []


def test_only_our_own_unique_ids_are_touched():
    """The lookup must use the FULL unique_id, not the bare data key.

    The bare key is not a hypothetical: releases before the current id format
    registered entities under exactly that, which is why the migration next
    door still looks for it. An entry found under it belongs to an entity we
    are supposed to be preserving history for - removing it here would delete
    the very thing the migration exists to keep.

    Another config entry's copy of the same parameter is the same mistake one
    step further out.
    """
    registry = FakeRegistry(
        {
            ("switch", "Heat pump-U_Beginn"): "switch.registered_under_the_bare_key",
            ("switch", "some-other-integrations-id"): "switch.someone_elses",
            (
                "switch",
                get_wemportal_unique_id("entry-2", DEVICE, "Heat pump-U_Beginn"),
            ): "switch.other_entry",
        }
    )

    _run(registry, {"Heat pump-U_Beginn": Reading(platform="date")})

    assert registry.removed == []


def test_the_lookup_stays_inside_this_integration():
    registry = FakeRegistry({})

    _run(registry, {"Heat pump-U_Beginn": Reading(platform="date")})

    assert registry.domains_asked == {DOMAIN}


def test_a_plain_counter_in_the_data_is_skipped():
    """coordinator.data carries non-dict bookkeeping entries alongside the
    entities; walking into one would raise."""
    registry = FakeRegistry({})

    _run(registry, {"ConnectionStatus": 0})

    assert registry.removed == []


def test_a_value_without_a_platform_counts_as_a_sensor():
    """The default the platforms themselves use, so a sensor is not mistaken
    for a leftover and deleted."""
    registry = FakeRegistry(
        {
            ("sensor", _uid("Heat pump-Outside")): "sensor.heat_pump_outside",
        }
    )

    _run(registry, {"Heat pump-Outside": Reading(friendly_name="Outside")})

    assert registry.removed == []


class FakeCoordinator:
    """Enough of the coordinator to hand out an update and be asked for one."""

    def __init__(self, data):
        self.data = data
        self.listeners: list = []
        self.refreshes = 0

    def async_add_listener(self, update):
        self.listeners.append(update)
        return lambda: self.listeners.remove(update)

    async def async_request_refresh(self):
        self.refreshes += 1

    def publish(self, data):
        """A later cycle, as the coordinator delivers one."""
        self.data = data
        for update in list(self.listeners):
            update()


async def test_a_reading_that_appears_later_is_migrated_too(monkeypatch):
    """Migration ran once, during setup - and the readings it has to reach
    are precisely the ones that are not there yet.

    A device unreachable at that moment is skipped by get_parameters, the
    parameter re-discovery deliberately waits for the second cycle, and the
    hourly statistics appear minutes later. The listener that builds entities
    for those was added and fixed exactly that half; this one never ran again,
    so a legacy entity that showed up late got a brand new entity - under the
    current unique_id, with none of the history the old one carries. Nothing
    looks wrong afterwards: there IS an entity, it just starts at zero.
    """
    import custom_components.wemportal as wemportal

    registry = FakeRegistry({("sensor", "Outside"): "sensor.old_outside"})
    monkeypatch.setattr(wemportal.entity_registry, "async_get", lambda _hass: registry)
    coordinator = FakeCoordinator(data={})

    await wemportal.migrate_unique_ids(None, FakeConfigEntry(), coordinator)
    coordinator.publish(
        {
            DEVICE: {
                "Outside": Reading(value=1.0, platform="sensor", parameter_id="Outside")
            }
        }
    )

    assert registry.renamed == [("sensor.old_outside", _uid("Outside"))], (
        "a reading that arrived after setup kept none of its entity's history"
    )


async def test_a_reading_already_migrated_is_not_walked_again(monkeypatch):
    """Every cycle delivers the same rows, and the registry lookup costs eight
    queries per reading. Only what is new is worth asking about."""
    import custom_components.wemportal as wemportal

    registry = FakeRegistry({("sensor", "Outside"): "sensor.old_outside"})
    monkeypatch.setattr(wemportal.entity_registry, "async_get", lambda _hass: registry)
    rows = {
        DEVICE: {
            "Outside": Reading(value=1.0, platform="sensor", parameter_id="Outside")
        }
    }
    coordinator = FakeCoordinator(data=rows)

    await wemportal.migrate_unique_ids(None, FakeConfigEntry(), coordinator)
    coordinator.publish(rows)
    coordinator.publish(rows)

    assert len(registry.renamed) == 1, (
        f"the same reading was migrated {len(registry.renamed)} times"
    )


async def test_a_reclassification_after_setup_takes_the_old_entity_down(monkeypatch):
    """The reclassification the re-discovery performs does not wait for a
    restart, and the record of what was already migrated must not hide it.

    Keyed by reading alone, a row seen once is never looked at again - so the
    entity of the platform it USED to be stays in the registry, unavailable,
    beside the working one. That is the state this whole file exists to
    remove; it just had a way back in through the door the listener opened.
    """
    import custom_components.wemportal as wemportal

    registry = FakeRegistry(
        {("switch", _uid("Heat pump-U_Beginn")): "switch.holiday_begin"}
    )
    monkeypatch.setattr(wemportal.entity_registry, "async_get", lambda _hass: registry)
    coordinator = FakeCoordinator(
        data={DEVICE: {"Heat pump-U_Beginn": Reading(platform="switch")}}
    )

    await wemportal.migrate_unique_ids(None, FakeConfigEntry(), coordinator)
    assert registry.removed == [], "the entity that was still correct was removed"

    coordinator.publish({DEVICE: {"Heat pump-U_Beginn": Reading(platform="date")}})

    assert registry.removed == ["switch.holiday_begin"], (
        "the entity of the platform the parameter no longer is stayed behind"
    )


def _entry_with_a_coordinator(coordinator):
    """A config entry the entity builder accepts, sharing `coordinator`."""
    import types

    entry = FakeConfigEntry()
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    return entry


async def test_a_platform_that_flickers_gets_its_control_entity_back(monkeypatch):
    """The two halves have to agree, and this is the case that proves they do.

    A parameter's platform is decided from the value of THAT cycle, not from
    the description: mapper._writeable_entity asks
    `value.get("NumericValue") is not None`, and tests/fixtures/mapper_golden
    pins both outcomes for one and the same descriptor - `numeric` gives a
    date, `empty`/`energy_missing` give a plain sensor. So one cycle in which
    the portal answers a holiday date with no NumericValue takes the row from
    date to sensor and the next one takes it back.

    Removal is per-cycle and platform-aware; building was neither, so that
    one flicker deleted the date entity's REGISTRY entry - its name, its area,
    its entity_id - and nothing ever built it again. The control was gone for
    the life of the config entry, and both memos are add-only, so a restart
    was the only way back.

    Driven through the real migration AND the real entity builder in the order
    setup wires them, because the defect lives between the two and neither
    alone can show it.
    """
    import custom_components.wemportal as wemportal
    from custom_components.wemportal.entity import async_add_readings_as_they_appear

    registry = FakeRegistry(
        {("date", _uid("Heat pump-U_Beginn")): "date.holiday_begin"}
    )
    monkeypatch.setattr(wemportal.entity_registry, "async_get", lambda _hass: registry)

    as_date = {DEVICE: {"Heat pump-U_Beginn": Reading(value=1.0, platform="date")}}
    as_sensor = {DEVICE: {"Heat pump-U_Beginn": Reading(value=1.0, platform="sensor")}}
    coordinator = FakeCoordinator(data=as_date)
    entry = _entry_with_a_coordinator(coordinator)

    # The migration listener is registered before the platforms are forwarded,
    # so it runs first on every cycle - as it does in async_setup_entry.
    await wemportal.migrate_unique_ids(None, entry, coordinator)
    built = []
    async_add_readings_as_they_appear(
        entry, built.extend, "date", lambda *arguments: arguments
    )
    assert len(built) == 1, "the control case never built the date entity"

    coordinator.publish(as_sensor)
    coordinator.publish(as_date)

    assert len(built) == 2, (
        "the date entity was removed on the flicker and never rebuilt - the "
        "control is gone until the entry is reloaded"
    )


def test_the_migration_leaves_another_accounts_entity_alone():
    """The old unique_id shapes predate the account prefix, so they are the
    same on every WEM account.

    Searched registry-wide, the first entry loaded could therefore adopt the
    other account's entity - renaming it onto its own id, and removing the
    entity that was already correct. Two WEM accounts is unusual; losing the
    other one's entities is not something to find out about afterwards.
    """
    from custom_components.wemportal import _migrate_device_unique_ids

    other_account = "entry-2"
    registry = FakeRegistry(
        # Registered under a shape from before the account prefix existed.
        {("sensor", "Outside"): "sensor.other_account_outside"},
        owners={"sensor.other_account_outside": other_account},
    )

    changed = _migrate_device_unique_ids(
        registry,
        FakeConfigEntry(),
        DEVICE,
        {"Outside": Reading(value=1.0, platform="sensor", parameter_id="Outside")},
    )

    assert registry.renamed == [], (
        f"an entity of another WEM account was migrated: {registry.renamed}"
    )
    assert registry.removed == []
    assert changed is False
