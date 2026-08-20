"""Migrating entities from older unique_id shapes to the current one.

Lifted wholesale out of __init__.py: these are free functions (no self), so
the move is a real relocation rather than a mixin trick. The lifecycle in
__init__ calls migrate_unique_ids; everything else here is its machinery.

Why the old shapes are searched, why the account check guards every removal,
and why the whole thing is bound to a coordinator listener rather than run
once - each is explained at the function it belongs to.
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry

from .const import DOMAIN, PLATFORMS
from .models import Reading

_LOGGER = logging.getLogger(__name__)


def get_wemportal_unique_id(config_entry_id: str, device_id: str, name: str):
    """Return unique ID for WEM Portal."""
    return f"{config_entry_id}:{device_id}:{name}"


def _migrate_device_unique_ids(
    registry, config_entry, device_id, data, contested=()
) -> bool:
    """Migrate one device's entities from old unique_id formats to the current
    one. Returns True if any entity was updated. Factored out so migration can
    run for EVERY device, not just the first."""
    change = False
    for unique_id, values in data.items():
        if not isinstance(values, Reading):
            continue

        new_id = get_wemportal_unique_id(config_entry.entry_id, device_id, unique_id)
        old_ids = [
            old_id
            for old_id in _possible_old_unique_ids(
                config_entry, device_id, unique_id, values
            )
            if old_id not in contested
        ]
        if _adopt_entity_under_its_old_id(
            registry, config_entry, values.platform, old_ids, new_id
        ):
            change = True
    return change


def _ids_claimed_by_more_than_one_reading(config_entry, per_device) -> set:
    """The id shapes that identify no single reading of this cycle.

    The old shapes carry neither device nor parameter - a bare key, a friendly
    name, a ParameterID - so two devices with a parameter of the same name
    propose exactly the same one, and so do two parameters whose name and
    ParameterID cross over. Whichever the walk reaches first then adopts the
    other's entity; and since the cleanup learned to search those shapes too,
    it can delete it instead, one device before the device it belongs to has
    been looked at. The current id is a claim as well, because the cleanup
    searches under it and another reading's friendly-name shape can BE it.

    Nothing is resolved here, and that is the point: an id two readings answer
    to identifies neither, and nothing in the registry says whose history it
    is. Leaving it alone is the only outcome that loses nothing.
    """
    claimed_once: set = set()
    claimed_twice: set = set()
    for device_id, rows in per_device.items():
        for unique_id, values in (rows or {}).items():
            if not isinstance(values, Reading):
                continue
            claimed = {
                get_wemportal_unique_id(config_entry.entry_id, device_id, unique_id),
                *_possible_old_unique_ids(config_entry, device_id, unique_id, values),
            }
            claimed_twice |= claimed & claimed_once
            claimed_once |= claimed
    return claimed_twice


def _possible_old_unique_ids(config_entry, device_id, unique_id, values) -> list:
    """Every unique_id shape a past release may have registered this under.

    Three sources - the key itself, the friendly name and the ParameterID -
    each in up to three spellings. Assembling the list is a different job
    from searching it, and inline it put the search two levels deep.
    """
    friendly_name = values.friendly_name or ""
    parameter_id = values.parameter_id

    possible_old_ids = []
    if unique_id != "ConnectionStatus":
        possible_old_ids.append(unique_id)
        possible_old_ids.append(f"{device_id}-{unique_id}")

    if friendly_name:
        possible_old_ids.append(friendly_name)
        possible_old_ids.append(f"{device_id}-{friendly_name}")
        possible_old_ids.append(
            get_wemportal_unique_id(config_entry.entry_id, device_id, friendly_name)
        )

    if parameter_id:
        possible_old_ids.append(parameter_id)
        possible_old_ids.append(f"{device_id}-{parameter_id}")
        possible_old_ids.append(
            get_wemportal_unique_id(config_entry.entry_id, device_id, parameter_id)
        )
    return possible_old_ids


def _adopt_entity_under_its_old_id(
    registry, config_entry, platform, old_ids, new_id
) -> bool:
    """Give the first entity of THIS account found under an old id the
    current one.

    Stops at the first hit whether or not it changed anything: the entity has
    been identified, and carrying on would match the same one again under
    another of its old spellings.

    The account check is not decoration. The old shapes predate the account
    prefix, so they are identical on every WEM account - searched
    registry-wide, the entry that loads first would adopt the other
    account's entity, rename it onto its own id, and remove the entity that
    was already correct.
    """
    for old_id in old_ids:
        if not old_id:
            continue
        name_id = registry.async_get_entity_id(platform, DOMAIN, old_id)
        if name_id is None:
            continue
        found = registry.async_get(name_id)
        if found is None or found.config_entry_id != config_entry.entry_id:
            continue

        new_entity_id = registry.async_get_entity_id(platform, DOMAIN, new_id)
        if new_entity_id is not None and new_entity_id != name_id:
            _LOGGER.info(
                "Found entity with old id and an entity with a new unique_id. Preserving old entity..."
            )
            registry.async_remove(new_entity_id)

        if old_id == new_id:
            return False
        _LOGGER.info(
            "Migrating entity %s from old id %s to new unique_id %s",
            name_id,
            old_id,
            new_id,
        )
        registry.async_update_entity(name_id, new_unique_id=new_id)
        return True
    return False


def _entities_of_this_entry_on_other_platforms(
    registry, config_entry, current, unique_ids
) -> list:
    """This account's registry entries for one parameter, on every platform it
    is not.

    Each id is asked for once per platform, and each entity is handed out once
    however many ids answered with it: the shapes overlap - a parameter whose
    key IS its ParameterID produces the same one twice - and Home Assistant's
    registry raises on a second removal.

    The account check is what makes searching the old shapes safe here. They
    predate the account prefix, so they name the same entity on every WEM
    account, and unlike the rename next door this caller REMOVES what it finds.
    """
    found = []
    for platform in PLATFORMS:
        if platform == current:
            continue
        for unique_id in unique_ids:
            stale = registry.async_get_entity_id(platform, DOMAIN, unique_id)
            if stale is None or stale in found:
                continue
            entry = registry.async_get(stale)
            if entry is None or entry.config_entry_id != config_entry.entry_id:
                continue
            found.append(stale)
    return found


def _remove_entities_from_a_previous_platform(
    registry, config_entry, device_id, data, contested=()
) -> None:
    """Drop registry entries this integration no longer provides.

    A parameter can change platform between releases when we learn what it
    actually is. Holiday begin and end were switches until the portal's own
    parameter list showed them to be dates - and because our unique_id does
    not carry the platform, the old switch entry survives the change and sits
    in the registry unavailable, next to the working date entity.

    The old id shapes are searched too, because the two halves of this
    migration each see only one: the rename looks for the old shapes but only
    on the platform the parameter is today, and this half looked on every other
    platform but only under the current id. An entity registered by an old
    release AND reclassified since fell between the two and stayed for good.
    Renaming it was never the answer - a registry entry cannot change its
    domain - so it goes on the same terms as the rest.

    Only entries of THIS account, under OUR id shapes and OUR own platforms,
    and only the ones the current data says belong to a different platform now.
    """
    for unique_id, values in data.items():
        if not isinstance(values, Reading):
            continue
        current = values.platform
        unique_ids = [
            candidate
            for candidate in (
                get_wemportal_unique_id(config_entry.entry_id, device_id, unique_id),
                *_possible_old_unique_ids(config_entry, device_id, unique_id, values),
            )
            # Deletion is the half where a contested id costs the most: an
            # adoption that goes to the wrong reading is at least still an
            # entity.
            if candidate not in contested
        ]
        for stale in _entities_of_this_entry_on_other_platforms(
            registry, config_entry, current, unique_ids
        ):
            _LOGGER.info(
                "%s is a %s now - removing the entity it left behind.",
                stale,
                current,
            )
            registry.async_remove(stale)


def _orphaned_by_a_merge(device_id, scraping_mapper, modules, device_data) -> list:
    """The api keys a merge retired: an entity was built for one on an earlier
    cycle and it renders nothing now.

    In `both` mode _merge_into_scraped feeds an api reading into the scraped row
    that shows the same value and drops the api row under its own key. The
    add-only builder never takes the entity of that key down, so it shows
    unknown for the life of the session. A merge whose target is some OTHER key,
    whose own key is no longer in the data, is exactly one of these - and a key
    that is merely absent for a cycle has no such entry, so it is left alone.
    """
    orphans = []
    for (mapped_device, module_ref, parameter_id), targets in scraping_mapper.items():
        if mapped_device != device_id:
            continue
        module = modules.get(device_id, {}).get(module_ref)
        if not module:
            continue
        own_key = f"{module['Name']}-{parameter_id}"
        if own_key in targets or own_key in device_data:
            continue
        orphans.append(own_key)
    return orphans


def _remove_orphaned_by_a_merge(registry, config_entry, device_id, orphan_keys) -> None:
    """Drop the registry entries of api keys a merge retired.

    Only this account's, only our own platforms, and only the current id shape,
    which is the one this code built the entity under. The account check is the
    same guard _remove_entities_from_a_previous_platform keeps: a removal is
    destructive, and being sure of the owner is cheap.
    """
    for own_key in orphan_keys:
        unique_id = get_wemportal_unique_id(config_entry.entry_id, device_id, own_key)
        for platform in PLATFORMS:
            entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
            if entity_id is None:
                continue
            found = registry.async_get(entity_id)
            if found is None or found.config_entry_id != config_entry.entry_id:
                continue
            _LOGGER.info(
                "%s was merged into another row and left showing unknown - "
                "removing the entity it left behind.",
                entity_id,
            )
            registry.async_remove(entity_id)


def _take_the_readings_not_migrated_yet(device_id, rows, migrated: dict) -> dict:
    """One device's readings whose platform has changed since last time,
    recorded as handled on the way out.

    `migrated` holds the platform each reading was LAST handled as, not every
    platform it has ever had. The re-discovery can reclassify a parameter
    while the entry stays loaded, and that is the moment the entity of the
    platform it used to be has to come down - so a row has to be looked at
    again whenever its platform differs from the recorded one. A set of every
    combination ever seen answered "already done" for a platform the row had
    held before and come back to, which is exactly the round trip a value
    that decides the platform produces (see mapper._writeable_entity).

    Recording here rather than at the call site because the two belong
    together: a reading handed out twice is migrated twice, and the registry
    lookup behind it costs up to eight queries.
    """
    fresh = {}
    for key, row in rows.items():
        if not isinstance(row, Reading):
            continue
        handled_as = migrated.get((device_id, key))
        if handled_as == row.platform:
            continue
        migrated[(device_id, key)] = row.platform
        fresh[key] = row
    return fresh


async def migrate_unique_ids(
    hass: HomeAssistant, config_entry: ConfigEntry, coordinator
):
    registry = entity_registry.async_get(hass)
    migrated: dict[tuple[str, str], str] = {}

    @callback
    def _migrate_the_readings_not_seen_yet() -> bool:
        """Both migration steps, for whatever the coordinator holds now.

        Bound to a listener rather than run once, because the readings this
        has to reach are exactly the ones that are not there during setup: a
        device unreachable at that moment is skipped by get_parameters, the
        parameter re-discovery waits for the second cycle on purpose, and the
        hourly statistics arrive minutes later. The entity-building half was
        given a listener for those; this half was not, so a legacy reading
        that showed up late got a new entity under the current unique_id -
        with none of the history the old one carries, and nothing about the
        result looking wrong.

        Only what is new: every cycle delivers the same rows, and each one
        costs up to eight registry lookups.
        """
        change = False
        # Migrate EVERY device, not just the first: with multiple devices the
        # others' old unique_ids (and their history) were previously left
        # behind.
        everything = coordinator.data or {}
        due = {
            device_id: _take_the_readings_not_migrated_yet(device_id, rows, migrated)
            for device_id, rows in everything.items()
        }
        # From EVERYTHING the coordinator holds, not from the slice that is
        # due: the two do not have to become due in the same cycle. A device
        # that sits unchanged while another is reclassified three cycles later
        # is absent from `due`, so its claim on a shared old shape would be
        # invisible - and the cleanup running for the other one would take its
        # entity down with nothing left to warn it off. Migrating only what is
        # due stays right; deciding what is contested from that slice is not.
        contested = _ids_claimed_by_more_than_one_reading(config_entry, everything)
        for device_id, fresh in due.items():
            if not fresh:
                continue
            if _migrate_device_unique_ids(
                registry, config_entry, device_id, fresh, contested
            ):
                change = True
            # After the id migration, not before: that step may still move an
            # old entry onto the current unique_id, and removing it first
            # would throw away the history it exists to preserve.
            _remove_entities_from_a_previous_platform(
                registry, config_entry, device_id, fresh, contested
            )
        # Entities an in-session merge retired: the value read drops the api
        # row under its own key, and the add-only builder never takes the
        # entity it already made down, so it shows unknown for good. From
        # EVERYTHING, not the due slice - a merge established cycles ago leaves
        # an orphan that never becomes due again.
        api = coordinator.api
        for device_id in everything:
            _remove_orphaned_by_a_merge(
                registry,
                config_entry,
                device_id,
                _orphaned_by_a_merge(
                    device_id,
                    api.scraping_mapper,
                    api.modules,
                    everything.get(device_id) or {},
                ),
            )
        return change

    change = _migrate_the_readings_not_seen_yet()
    # Registered here rather than after the platforms are forwarded, so this
    # runs BEFORE the listener that builds the entities: an entity added
    # first would be registered under the current unique_id, and the old
    # entry the migration exists to rename would be the one left over.
    config_entry.async_on_unload(
        coordinator.async_add_listener(_migrate_the_readings_not_seen_yet)
    )

    if change:
        # A debounced refresh is enough to update the migrated entities.
        # async_config_entry_first_refresh() here ran a SECOND full portal
        # cycle right after the initial one (and is meant for setup only) -
        # needless extra requests against the portal's rate limit.
        await coordinator.async_request_refresh()
