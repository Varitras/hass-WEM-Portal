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

import types

from custom_components.wemportal import (
    _orphaned_by_a_merge,
    _remove_entities_from_a_previous_platform,
    _remove_orphaned_by_a_merge,
    get_wemportal_unique_id,
)
from custom_components.wemportal.const import DOMAIN
from custom_components.wemportal.models import ModuleRef, Reading

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


def test_a_legacy_id_on_a_platform_the_parameter_no_longer_is_goes_too():
    """The leftover that neither half of the migration could see.

    Both halves search under ONE id shape. The rename looks for the old shapes,
    but only on the platform the parameter is today; the removal looks on every
    other platform, but only under the current id. An entity registered by an
    old release AND reclassified since falls between them: registered as
    `<device>-<key>` under switch, wanted as a date, found by neither.

    Renaming it is not an option - a registry entry cannot change its domain,
    so the switch entity can never become the date one and its history goes
    with the reclassification either way. Removing it is the same trade the
    current-id case already makes, and the alternative is an unavailable
    entity sitting beside the working one for the life of the installation.
    """
    registry = FakeRegistry(
        {
            ("switch", f"{DEVICE}-Heat pump-U_Beginn"): "switch.holiday_begin",
            ("date", _uid("Heat pump-U_Beginn")): "date.holiday_begin",
        }
    )

    _run(registry, {"Heat pump-U_Beginn": Reading(platform="date")})

    assert registry.removed == ["switch.holiday_begin"]


def test_a_legacy_id_is_only_followed_into_this_account():
    """The old shapes carry no account prefix, so they name the same entity on
    every WEM account - and this half REMOVES what it finds."""
    registry = FakeRegistry(
        {("switch", f"{DEVICE}-Heat pump-U_Beginn"): "switch.other_accounts_begin"},
        owners={"switch.other_accounts_begin": "entry-2"},
    )

    _run(registry, {"Heat pump-U_Beginn": Reading(platform="date")})

    assert registry.removed == [], (
        f"another WEM account's entity was removed: {registry.removed}"
    )


# --- an entity whose api key a merge retired ----------------------------
#
# In `both` mode the value read merges an api reading into a scraped row and
# drops the api row under its own key. An entity built for that key on an
# earlier cycle then has no reading any more and shows unknown for good - the
# add-only builder never takes it down. This finds and removes exactly those,
# and nothing that is merely absent for a cycle.

MODULE = ModuleRef(module_index=0, module_type=1)
OWN_KEY = "Heat pump-Outside"
SCRAPED = "heat_pump-outside"
MODULES = {DEVICE: {MODULE: {"Name": "Heat pump", "Index": 0, "Type": 1}}}


def _merged(target):
    return {(DEVICE, MODULE, "Outside"): [target]}


def test_a_key_a_merge_retired_is_an_orphan():
    """Merged into a different, scraped key and gone from the data: an entity
    was built for it and now renders nothing."""
    assert _orphaned_by_a_merge(DEVICE, _merged(SCRAPED), MODULES, {}) == [OWN_KEY]


def test_a_key_merged_into_itself_is_not_an_orphan():
    """No scraped row matched, so the fallback kept the api reading under its
    own key - which still carries it."""
    assert _orphaned_by_a_merge(DEVICE, _merged(OWN_KEY), MODULES, {}) == []


def test_a_key_that_still_has_a_row_is_not_an_orphan():
    """A merge target that is somehow still in the data is not retired - only a
    key with no row left is."""
    data = {OWN_KEY: Reading(platform="sensor")}
    assert _orphaned_by_a_merge(DEVICE, _merged(SCRAPED), MODULES, data) == []


def test_a_transiently_absent_key_is_not_an_orphan():
    """The safety property. A reading absent for one cycle - a device
    unreachable, a partial answer - has no merge entry naming it, so it is
    never mistaken for one a merge retired. Removing it would take a live
    entity's history for a single bad cycle, the mistake the add-only builder
    exists to avoid."""
    assert _orphaned_by_a_merge(DEVICE, {}, MODULES, {}) == []


def test_another_devices_merge_is_not_this_devices_orphan():
    """The cache is one dict across devices; a merge belongs to the device
    that made it."""
    other = {("9999", MODULE, "Outside"): [SCRAPED]}
    assert _orphaned_by_a_merge(DEVICE, other, MODULES, {}) == []


def test_the_entity_a_merge_orphaned_is_removed():
    registry = FakeRegistry({("sensor", _uid(OWN_KEY)): "sensor.heat_pump_outside"})

    _remove_orphaned_by_a_merge(registry, FakeConfigEntry(), DEVICE, [OWN_KEY])

    assert registry.removed == ["sensor.heat_pump_outside"]


def test_removing_an_orphan_leaves_another_accounts_entity_alone():
    """The unique_id carries the entry id, but a removal this destructive
    checks the owner anyway - the same guard the platform-change removal has."""
    registry = FakeRegistry(
        {("sensor", _uid(OWN_KEY)): "sensor.other_account"},
        owners={"sensor.other_account": "entry-2"},
    )

    _remove_orphaned_by_a_merge(registry, FakeConfigEntry(), DEVICE, [OWN_KEY])

    assert registry.removed == []


def test_one_leftover_under_several_old_shapes_is_removed_once():
    """The shapes overlap - a parameter whose key IS its ParameterID answers
    the same entity twice. Home Assistant's registry raises on the second
    removal, so the pass has to hand out each entity once."""
    registry = FakeRegistry(
        {
            ("switch", "U_Beginn"): "switch.holiday_begin",
            ("switch", f"{DEVICE}-U_Beginn"): "switch.holiday_begin",
        }
    )

    _run(
        registry,
        {"U_Beginn": Reading(platform="date", parameter_id="U_Beginn")},
    )

    assert registry.removed == ["switch.holiday_begin"]


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


def test_only_ids_this_parameter_could_have_had_are_asked_for():
    """Nothing outside the shapes this integration has itself registered.

    An id belonging to another integration or to another config entry names an
    entity we know nothing about, and this pass removes what it finds - so the
    shapes searched are the ones the id builder produces for THIS entry and
    THIS parameter, never a registry-wide sweep for something that looks
    similar.
    """
    registry = FakeRegistry(
        {
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
        # Production coordinators always hold the api, and the merge-orphan
        # cleanup reads its scraping_mapper and modules. Empty here: these tests
        # carry no merge, so no key is retired and no entity is taken down.
        self.api = types.SimpleNamespace(scraping_mapper={}, modules={})

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


async def test_an_old_id_two_devices_both_answer_to_is_left_alone(monkeypatch):
    """The old shapes name no device, so two devices can claim the same one.

    A bare key and a friendly name are what releases before the entry prefix
    registered under, and "Pump" is "Pump" on every module of every device.
    Whichever device the cycle happens to walk first then adopted the other's
    entity - and since the cleanup learned to search those shapes too, it
    could just as well DELETE it, one device before the device it belongs to
    was even looked at.

    Left alone rather than resolved: an id two readings answer to identifies
    neither, and there is nothing in the registry that says whose history it
    is. The entity stays where it is, under its old id, which is the one
    outcome that loses nothing.
    """
    import custom_components.wemportal as wemportal

    registry = FakeRegistry({("switch", "Pump"): "switch.the_contested_one"})
    monkeypatch.setattr(wemportal.entity_registry, "async_get", lambda _hass: registry)
    coordinator = FakeCoordinator(
        data={
            # Walked first, and a date today - so its CLEANUP is what reaches
            # the switch entity above.
            DEVICE: {"Pump": Reading(value=1.0, platform="date")},
            "5678": {"Pump": Reading(value=1.0, platform="switch")},
        }
    )

    await wemportal.migrate_unique_ids(None, FakeConfigEntry(), coordinator)

    assert registry.removed == [], (
        f"one device deleted an entity the other one may own: {registry.removed}"
    )
    assert registry.renamed == [], (
        f"one device adopted an entity the other one may own: {registry.renamed}"
    )


async def test_a_device_that_changes_later_still_sees_the_other_ones_claim(monkeypatch):
    """The two devices do not have to become due in the same cycle.

    Only rows whose platform CHANGED are handed to the migration, and the
    contested set was built from exactly those - so a device that sits
    unchanged is invisible to it. When the other one is reclassified three
    cycles later, its cleanup searches the shared old shapes with nothing
    left to warn it off, and takes the quiet device's entity down.

    Migrating only what is due stays right; deciding what is contested from
    that same slice is what does not.
    """
    import custom_components.wemportal as wemportal

    registry = FakeRegistry({("switch", "Pump"): "switch.the_contested_one"})
    monkeypatch.setattr(wemportal.entity_registry, "async_get", lambda _hass: registry)
    quiet = {"Pump": Reading(value=1.0, platform="switch")}
    coordinator = FakeCoordinator(
        data={"5678": quiet, DEVICE: {"Pump": Reading(value=1.0, platform="switch")}}
    )

    # First cycle: both are switches, both get handled, nothing is contested
    # into existence yet.
    await wemportal.migrate_unique_ids(None, FakeConfigEntry(), coordinator)
    registry.removed.clear()
    registry.renamed.clear()

    # Later: only DEVICE is reclassified. 5678 is unchanged, so it is not due.
    coordinator.publish(
        {"5678": quiet, DEVICE: {"Pump": Reading(value=1.0, platform="date")}}
    )

    assert registry.removed == [], (
        f"a device that was not due lost its entity: {registry.removed}"
    )


async def test_an_old_id_only_one_device_answers_to_is_still_migrated(monkeypatch):
    """The control case: without it, refusing everything would pass the test
    above just as well."""
    import custom_components.wemportal as wemportal

    registry = FakeRegistry({("sensor", "Outside"): "sensor.old_outside"})
    monkeypatch.setattr(wemportal.entity_registry, "async_get", lambda _hass: registry)
    coordinator = FakeCoordinator(
        data={
            DEVICE: {"Outside": Reading(value=1.0, platform="sensor")},
            "5678": {"Inside": Reading(value=1.0, platform="sensor")},
        }
    )

    await wemportal.migrate_unique_ids(None, FakeConfigEntry(), coordinator)

    assert registry.renamed == [("sensor.old_outside", _uid("Outside"))]


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
