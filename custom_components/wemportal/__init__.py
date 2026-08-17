"""
wemportal integration

Author: erikkastelec
https://github.com/erikkastelec/hass-WEM-Portal

"""

from typing import Final
import logging

from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry, issue_registry
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    CONF_EXPERT_NOTIFY_ON_SUCCESS,
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_WRITE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    DOMAIN,
    EXPERT_SLOT_COUNT,
    MIN_SCAN_INTERVAL_API_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    PLATFORMS,
)
from .coordinator import (
    WemPortalDataUpdateCoordinator,
    forget_auth_failures,
    get_modules_store,
    get_scraper_device_store,
)
from .exceptions import ExpertOperationAborted
from .models import (
    Reading,
    account_unique_id,
    WemPortalConfigEntry,
    WemPortalData,
    forget_account_state,
    is_still_serving,
)
from .utils import clamped_scan_interval, deserialize_modules
from .wemportalapi import WemPortalApi

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_EXPERT_PARAMETER: Final = "set_expert_parameter"


def get_wemportal_unique_id(config_entry_id: str, device_id: str, name: str):
    """Return unique ID for WEM Portal."""
    return f"{config_entry_id}:{device_id}:{name}"


# This integration is configured exclusively through the UI. Declaring that
# explicitly is what rejects a stray `wemportal:` block in configuration.yaml
# with a clear message instead of accepting it silently.
#
# It used to sit beside an async_setup whose whole body was
# `hass.data.setdefault(DOMAIN, {})`, and the comment justified the schema
# with that function's existence. Nothing has read hass.data since the
# rebuild moved the entry state to runtime_data (see models.py), so the
# function did nothing and the reason given for the schema was circular. The
# schema stands on its own; the function is gone.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


# Migrate values from previous versions
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


async def async_setup_entry(hass: HomeAssistant, entry: WemPortalConfigEntry) -> bool:
    """Set up the wemportal component."""
    # Set proper update_interval, based on selected mode. Clamped rather than
    # taken verbatim: the floors are enforced by the options-flow schema,
    # which only sees values entered now - an interval stored by an older
    # release keeps its old, possibly far too small value forever.
    scan_interval = clamped_scan_interval(
        entry.options,
        CONF_SCAN_INTERVAL,
        DEFAULT_CONF_SCAN_INTERVAL_VALUE,
        MIN_SCAN_INTERVAL_SECONDS,
    )
    scan_interval_api = clamped_scan_interval(
        entry.options,
        CONF_SCAN_INTERVAL_API,
        DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
        MIN_SCAN_INTERVAL_API_SECONDS,
    )
    if entry.options.get(CONF_MODE) == "web":
        update_interval = scan_interval
    elif entry.options.get(CONF_MODE) == "api":
        update_interval = scan_interval_api
    else:
        update_interval = min(scan_interval, scan_interval_api)

    _backfill_account_unique_id(hass, entry)

    registry = device_registry.async_get(hass)
    # DeviceEntry.config_entries is deprecated in Home Assistant 2026.8 and
    # goes in 2027.8; the registry's own index answers the same question, has
    # been there since well before the 2024.12 minimum, and does not scan
    # every device of every integration to do it.
    devices = registry.devices.get_devices_for_config_entry_id(entry.entry_id)
    device_ids = [device.name for device in devices]
    if not device_ids:
        _LOGGER.warning(
            "No devices found for %s. Starting first time initialization.", DOMAIN
        )
    else:
        _LOGGER.info("Found devices for %s: %s", DOMAIN, device_ids)

    # Load any previously persisted device/module/parameter metadata, so we
    # can skip the slow, rate-limited per-module discovery in
    # get_parameters() on this restart (see coordinator.py / wemportalapi.py
    # for where this cache is used and re-saved).
    cached_modules = None
    try:
        modules_store = get_modules_store(hass, entry.entry_id)
        cached_modules_raw = await modules_store.async_load()
        cached_modules = (
            deserialize_modules(cached_modules_raw) if cached_modules_raw else None
        )
    except Exception as exc:  # noqa: BLE001
        # A corrupted/unreadable cache file must never prevent the
        # integration from starting - worst case, we just lose the
        # startup-time optimization for this one restart and fall back to
        # a full discovery, exactly like a first-ever install.
        _LOGGER.warning(
            "Could not load cached WEM Portal module data, falling back to full "
            "discovery for this restart: %s",
            exc,
        )
    if cached_modules:
        _LOGGER.info(
            "Loaded cached module/parameter definitions for %s devices. "
            "Skipping full discovery for this restart.",
            len(cached_modules),
        )

    # Load the stable scraper device id (decided once, then persisted) so
    # scraped sensors keep a constant device id - and history - across mode
    # switches. None on a fresh install / first run after upgrade: the api
    # then decides it deterministically on the first scrape (preferring the
    # real API device id, else the placeholder) and the coordinator persists
    # it. Existing installs therefore lock in whatever id they already use,
    # so nobody loses history at upgrade.
    try:
        scraper_device_id = await get_scraper_device_store(
            hass, entry.entry_id
        ).async_load()
    except Exception as exc:  # noqa: BLE001
        # Not swallowed. Carrying on with None does not mean "no id yet" - it
        # means the api decides one again, and where that decision lands
        # somewhere else, every scraped sensor gets a new unique_id and its
        # history is orphaned. Refusing to set up is recoverable and Home
        # Assistant retries on its own; losing the history is not.
        raise ConfigEntryNotReady(
            "The stored scraper device id could not be read. Setting up "
            "without it would re-decide the id and move every scraped "
            f"sensor to a new one, so this account is not loaded: {exc}"
        ) from exc

    # Creating API object
    api = WemPortalApi(
        entry.data.get(CONF_USERNAME),
        entry.data.get(CONF_PASSWORD),
        config=entry.options,
        cached_modules=cached_modules,
        scraper_device_id=scraper_device_id,
    )
    # Create custom coordinator
    coordinator = WemPortalDataUpdateCoordinator(
        hass, api, entry, timedelta(seconds=update_interval)
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        # The api is not in hass.data yet, so async_unload_entry cannot close
        # it: a failed first refresh (portal down, 403, auth) would leak its
        # HTTP sessions, once more per setup retry.
        #
        # BaseException, not Exception: Home Assistant cancels a setup task on
        # shutdown and when it takes too long, and CancelledError is not an
        # Exception - so the one ending that leaves the most behind was the
        # one this never ran for. Re-raised immediately, so a cancellation
        # still cancels.
        await hass.async_add_executor_job(api.close_transport)
        raise

    # Is there an on_update function that we can add listener to?
    _LOGGER.info("Migrating entity names for wemportal")
    try:
        await migrate_unique_ids(hass, entry, coordinator)
    except Exception as exc:  # noqa: BLE001
        # Migration is a best-effort cleanup step (renames old unique_ids
        # to the new format). A failure here should never prevent the
        # integration from loading - worst case, some entities keep their
        # old unique_id until the next successful migration attempt.
        _LOGGER.warning("Unique_id migration failed, continuing without it: %s", exc)

    entry.runtime_data = WemPortalData(api=api, coordinator=coordinator)

    # Everything past this point runs with the store already PUBLISHED, so a
    # failure here cannot be left to async_unload_entry: Home Assistant only
    # calls that for an entry that finished setting up. Without this the
    # store and its two HTTP sessions were leaked - once more on every setup
    # retry, which is exactly when platform setup tends to fail.
    try:
        # Register the hub device so child devices can reference it via
        # via_device
        device_registry.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Weishaupt",
            name=entry.title or "WEM Portal",
            model="WEM Portal",
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # Deliberately NO update listener. Home Assistant deprecated combining one
        # with a reloading flow method in 2026.6 and rejects it from 2026.12, and
        # its check is literally `if entry.update_listeners`. Of the sanctioned
        # ways out, this is the one that holds in every case: the flows reload
        # explicitly.
        #
        # Relying on the listener instead looked equivalent and was not. It only
        # fires when the entry actually CHANGED, so re-authenticating with the
        # same password reloaded nothing while the flow still reported success -
        # and it is registered here, at the end of a successful setup, so a reauth
        # that fixes a failed setup had no listener to fire at all. That is the
        # case reauth exists for.

        # Expert write access (web): register the service only while the
        # option is enabled. Everything lives in expert_writer.py - the
        # polling paths (scraper/API/coordinator) are untouched.
        # Needs no option: it writes through the same mobile API the number,
        # select and switch entities already use. Registered BEFORE the
        # expert block, which is the part of this setup that can still fail -
        # so whatever fails below leaves a service registered that the
        # failure path has to take back, and the test for that covers both.
        #
        # Function-local like every other import of a sibling module here:
        # this file is where those modules get get_wemportal_unique_id from,
        # so importing them at the top would close the circle before that
        # name exists.
        from .holiday import async_register_holiday_service

        async_register_holiday_service(hass)

        if entry.options.get(CONF_EXPERT_WRITE, False):
            await _async_register_expert_service(hass)
            entry.runtime_data.expert.setup_auto_poll(hass, entry)
    except BaseException:
        # BaseException for the same reason as the block above: a setup task
        # cancelled on shutdown or on the setup timeout ends without an
        # Exception, and this is the half where everything that was already
        # published stays behind. Re-raised at the end, so a cancellation
        # still cancels.
        #
        # Take the platforms back down FIRST, while runtime_data is still
        # readable - their entities were built from it, and unloading them is
        # the only thing that can raise here, so it must not be starved of
        # what it needs.
        #
        # Home Assistant does not unload an entry whose setup failed, so
        # nothing else does this. Without it, everything forwarded before the
        # failure stayed registered: entities of an entry the user sees as
        # failed, unavailable and un-reloadable, one more set per setup retry.
        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception as unload_exc:  # noqa: BLE001
            # Best effort. The original failure is the one worth raising, and
            # losing it to a secondary error while cleaning up would hide why
            # the setup failed at all.
            _LOGGER.debug(
                "Could not unload the platforms after a failed setup: %s", unload_exc
            )
        # Home Assistant clears runtime_data only when a LOADED entry unloads,
        # so a setup that fails after publishing it has to clear it itself.
        if hasattr(entry, "runtime_data"):
            del entry.runtime_data
        await hass.async_add_executor_job(api.close_transport)
        # Home Assistant does not unload an entry whose setup failed, so
        # nothing else stops the coordinator - and by this point the
        # platforms have been forwarded, their entities have subscribed, and
        # its refresh timer is armed. It would keep polling the portal on its
        # interval for an entry the user sees as failed, against an account
        # that is blocked for 12 hours past 10,000 requests. Available since
        # well before the 2024.12 floor (checked in both).
        await coordinator.async_shutdown()
        # The service is registered one line before the auto-poll setup, so a
        # failure between the two left a domain service behind with no loaded
        # entry to serve it. Runs after runtime_data is gone, so this entry
        # already counts as not loaded.
        _async_release_expert_service(hass, entry)
        from .holiday import async_release_holiday_service

        async_release_holiday_service(hass, entry)
        raise

    return True


def _async_release_expert_service(hass: HomeAssistant, config_entry) -> None:
    """Drop the domain-wide expert service unless another entry still needs it.

    Called from two places, and the second one is why it is a function: an
    entry whose SETUP failed after the service was registered never reaches
    async_unload_entry, so the service used to stay behind with nothing loaded
    to serve it.
    """
    if not hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER):
        return
    still_enabled = any(
        other.entry_id != config_entry.entry_id
        and other.options.get(CONF_EXPERT_WRITE, False)
        and is_still_serving(other)
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not still_enabled:
        hass.services.async_remove(DOMAIN, SERVICE_SET_EXPERT_PARAMETER)


def _resolve_expert_entry(
    hass: HomeAssistant,
) -> tuple[WemPortalConfigEntry, WemPortalApi] | None:
    """Return (entry, api) of the single expert-write-enabled, loaded entry.

    Returns None if none - or MORE THAN ONE - entry currently has expert
    write enabled. The service is a single domain-wide registration; closing
    over one specific entry (the former behaviour) meant a second account
    could never be targeted and, worse, a write could hit the wrong account.
    Resolving at call time and refusing when ambiguous makes mis-addressing a
    heating parameter impossible rather than silent.
    """
    candidates = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not entry.options.get(CONF_EXPERT_WRITE, False):
            continue
        data = getattr(entry, "runtime_data", None)
        if data is not None:
            candidates.append((entry, data.api))
    return candidates[0] if len(candidates) == 1 else None


def _configured_expert_ids(config_entry) -> dict:
    """The ids the user put in slots, keyed by their canonical spelling.

    Both halves of what the service needs: the keys answer "may this be
    written at all", the values are the spelling to send - the one discovery
    produced and the portal has therefore accepted.
    """
    from .expert_options import canonical_entityvalue

    configured = {}
    for slot in range(1, EXPERT_SLOT_COUNT + 1):
        slot_id = config_entry.options.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot)
        if canonical_entityvalue(slot_id):
            configured[canonical_entityvalue(slot_id)] = slot_id
    return configured


def _load_expert_writer():
    """Import the expert client module. Runs in an executor - see the caller."""
    from . import expert_writer

    return expert_writer


async def _async_register_expert_service(hass: HomeAssistant) -> None:
    """Register wemportal.set_expert_parameter (idempotent).

    Takes no entry and no api on purpose: one global registration serves every
    configured account, so the handler resolves its target per call (see
    _resolve_expert_entry) and refuses when it cannot tell which is meant.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER):
        return

    # Deferred because the module pulls curl_cffi and lxml (~140 ms,
    # measured) - but deferring moved only WHEN, not onto which thread, so
    # those 140 ms were spent on the event loop. Below the idempotence check
    # as well, so a second account does not arrange for it again.
    expert_writer = await hass.async_add_import_executor_job(_load_expert_writer)
    expert_client = expert_writer.WemPortalExpertClient
    entityvalue_digest = expert_writer.entityvalue_digest
    short_entityvalue = expert_writer.short_entityvalue

    async def _handle_set_expert_parameter(call):
        # Strip once at the boundary: the validity check strips internally,
        # but the raw value is what ends up in the request URL - stray
        # whitespace from a copy/paste would otherwise travel along.
        entityvalue = call.data["entityvalue"].strip()
        value = call.data["value"]

        resolved = _resolve_expert_entry(hass)
        if resolved is None:
            raise HomeAssistantError(
                "WEM Portal expert write: could not determine the target account. "
                "Enable expert write on exactly one config entry (multiple "
                "expert-enabled entries are not yet supported for the service)."
            )
        target_entry, target_api = resolved

        # Only ids the user configured in a slot may be written. Without this
        # the service is a generic write primitive for ANY parameter of the
        # installation, including ones never surfaced in Home Assistant.
        # Compared in the canonical spelling: hex is case-insensitive, so a
        # caller passing the id in the other case names the same parameter
        # and was refused for a difference that means nothing.
        from .expert_options import canonical_entityvalue

        configured = _configured_expert_ids(target_entry)
        if canonical_entityvalue(entityvalue) not in configured:
            raise HomeAssistantError(
                f"WEM Portal expert write: {short_entityvalue(entityvalue)} is not one of "
                "the parameters configured in this integration's options. Add it "
                "to a slot first."
            )
        # From here on the CONFIGURED spelling, not the one that was typed.
        # The comparison above is case-insensitive because hex ids mean the
        # same parameter either way - and passing the caller's spelling on
        # from there sent the portal the one thing about this write nothing
        # had checked. The configured id came out of discovery, so the portal
        # has accepted it; that is what makes it the safe one to send under
        # exactly the uncertainty canonical_entityvalue's own comment names.
        entityvalue = configured[canonical_entityvalue(entityvalue)]

        data = getattr(target_entry, "runtime_data", None)
        if data is None:
            raise HomeAssistantError(
                "WEM Portal expert write: the integration is not loaded."
            )
        lock = data.expert.lock
        ev_short = short_entityvalue(entityvalue)

        def _raise_if_unloaded():
            """Abort gate for a write whose entry is going away.

            The write runs in an executor thread and cannot be cancelled, so
            the only way to stop it is to look before each step.

            Checking that the entry id is still present is NOT enough, in two
            different ways. The store is removed only after the platforms have
            been unloaded, so for the whole teardown the id is still there;
            and a reload puts a NEW store under the SAME id while this write
            still holds the old entry and api. Identity plus the unloading
            flag covers both: a different object means the configuration this
            write belongs to is gone, whatever its id says.

            The reason and nothing more: asked before every request, the
            confirming read included, it cannot say how far the write got.
            """
            reason = data.why_not_current(target_entry)
            if reason is not None:
                raise ExpertOperationAborted(reason)

        def _do_write():
            # Own short-lived session per write; honors the shared 403
            # cooldown (check) and ENGAGES it on a 403 (activate).
            from .expert_options import expert_client_options

            _raise_if_unloaded()
            client = expert_client(
                target_entry.data.get(CONF_USERNAME),
                target_entry.data.get(CONF_PASSWORD),
                cooldown_check=target_api.check_expert_cooldown,
                cooldown_activate=target_api.activate_expert_cooldown,
                cookie_jar=target_api.expert_cookies,
                abort_check=_raise_if_unloaded,
                **expert_client_options(target_entry.options),
            )
            return client.write_parameter(entityvalue, value)

        # Only one expert portal operation per account at a time (shared with
        # the entity writes and the auto-poll), so concurrent calls don't
        # collide on the same parameter or open parallel portal sessions.
        if lock is not None and not lock.acquire(blocking=False):
            raise HomeAssistantError(
                "WEM Portal expert write: another expert operation is already "
                "in progress for this account; try again shortly."
            )
        # Run synchronously and RAISE on failure so an automation calling this
        # action can tell whether the write actually succeeded (HA action-
        # exception guidance), instead of the old fire-and-forget that always
        # reported success. An expert write takes ~5-15s (login + Fachmann
        # navigation + write + verify) - acceptable for an explicit, on-demand
        # action. Only a SHORTENED entityvalue appears in any user-facing text.
        try:
            state = await hass.async_add_executor_job(_do_write)
        except ExpertOperationAborted as exc:
            # The configuration went away mid-write. Nothing reached the
            # portal - and the caller is still waiting on this call, so it
            # has to be told, in the kind of error Home Assistant surfaces.
            # It used to arrive here as an ordinary Exception and be wrapped
            # by the handler below; now that it travels as a control-flow
            # signal it needs saying explicitly, which is clearer anyway.
            _LOGGER.debug("Expert write for %s stopped: %s", ev_short, exc)
            raise HomeAssistantError(
                f"WEM Portal expert write for {ev_short} was stopped: {exc}"
            ) from exc
        except Exception as exc:
            _LOGGER.error("Expert write failed for %s: %s", ev_short, exc)
            raise HomeAssistantError(
                f"WEM Portal expert write for {ev_short} to {value} failed: {exc}"
            ) from exc
        finally:
            if lock is not None:
                lock.release()
        # The write verified itself against the portal; that answer is exactly
        # what the entity for this id should be showing.
        data.expert.apply_verified_write(entityvalue, state)
        # The dialog's own wording, which is the number for an ordinary
        # parameter and the only readable answer for one set beside the
        # scale - where `current` is empty by design.
        written = state.portal_text or state.current
        _LOGGER.info(
            "Expert parameter %s set to %s (allowed range %s..%s)",
            ev_short,
            written,
            state.min_value,
            state.max_value,
        )
        if target_entry.options.get(CONF_EXPERT_NOTIFY_ON_SUCCESS, False):
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "WEM Portal expert write",
                    "message": f"{ev_short} set to {written}.",
                    "notification_id": f"wemportal_expert_{entityvalue_digest(entityvalue)}",
                },
                blocking=False,
            )

    # Registered as an ADMIN service: it writes real settings on a heating
    # system. A plain async_register lets any authenticated user call it, and
    # nothing in the handler checked call.context.user_id - the opt-in option
    # and the installation-specific id are obscurity, not access control.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_EXPERT_PARAMETER,
        _handle_set_expert_parameter,
        schema=vol.Schema(
            {
                vol.Required("entityvalue"): cv.string,
                # A number, or the word the dialog shows for an option that
                # is not one ("Aus"). Coerce first, so "30" stays a number
                # and only what cannot be one travels on as text.
                vol.Required("value"): vol.Any(vol.Coerce(float), cv.string),
            }
        ),
    )


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle schema migrations."""
    # V1 -> V2 needs no data-schema change (entity-id migration happens
    # dynamically in async_setup_entry via migrate_unique_ids). But the entry
    # version must actually be bumped, otherwise HA keeps treating a V1 entry
    # as migration-pending and re-runs this on every startup.
    if config_entry.version < 2:
        hass.config_entries.async_update_entry(config_entry, version=2)
    return True


def _backfill_account_unique_id(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give an older entry the account unique_id it predates.

    Deliberately NOT in async_migrate_entry: Home Assistant only calls that
    when the entry version differs from the handler's, so every entry already
    at the current version - i.e. everyone who has run a recent release -
    would never be reached. This runs on every setup and does nothing once
    the id is in place.
    """
    if entry.unique_id is not None:
        return
    wanted = account_unique_id(entry.data.get(CONF_USERNAME))
    if not wanted:
        _LOGGER.debug(
            "No username on entry %s; leaving it without an id.", entry.entry_id
        )
        return
    taken = {
        other.unique_id
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    }
    if wanted in taken:
        # Two entries for one account - exactly what the unique_id prevents
        # from now on. Assigning it twice is not allowed, so this entry is
        # left as it is rather than failing to load.
        _LOGGER.warning(
            "Another config entry already covers this WEM Portal account, so "
            "this one keeps no account id. Consider removing the duplicate."
        )
        return
    hass.config_entries.async_update_entry(entry, unique_id=wanted)


async def async_unload_entry(
    hass: HomeAssistant, config_entry: WemPortalConfigEntry
) -> bool:
    """Handle removal of an entry."""
    # Flagged BEFORE the platforms come down, not when the store is finally
    # removed below: unloading the platforms is the slow part, and anything
    # already talking to the portal in a worker thread has to learn about the
    # teardown at its next gate rather than at the end of it.
    data = getattr(config_entry, "runtime_data", None)
    if data is not None:
        data.begin_unload()
    unload_ok = bool(
        await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    )
    if not unload_ok:
        # The entry stays loaded and keeps polling, so it must also stay
        # writeable. Leaving the flag set turned a refused unload into an
        # entry whose every write said "the integration is being unloaded"
        # until Home Assistant restarted.
        if data is not None:
            data.abort_unload()
        return False

    # Not unconditionally: the streak belongs to the ACCOUNT, and a legacy
    # duplicate entry of the same one is still allowed to load. Clearing it
    # here took the count out from under the entry that stays - and the
    # removal logic that protects the rest of the account state runs later, so
    # by the time it decides to keep it, it is already zero.
    if not _another_entry_shares_this_account(hass, config_entry):
        forget_auth_failures(config_entry)
    # An unloaded entry cannot re-check what its issues report, so they come
    # down with it; whatever still holds after a reload is re-raised within
    # a few cycles by the code that watches it.
    _async_delete_entry_issues(hass, config_entry.entry_id)
    # runtime_data is still readable here - Home Assistant drops it only
    # after this returns True. Close the API + scraper HTTP sessions so
    # they don't linger open after the entry is unloaded/reloaded.
    if data is not None:
        await hass.async_add_executor_job(data.api.close_transport)
    # The expert service is a single domain-wide registration shared by
    # all entries. Only remove it once NO remaining loaded entry still
    # has expert write enabled - previously unloading ANY entry removed
    # it globally, killing the service for other accounts.
    _async_release_expert_service(hass, config_entry)
    from .holiday import async_release_holiday_service

    async_release_holiday_service(hass, config_entry)

    return True


def _async_delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Drop every repair issue raised under this entry.

    Issue ids start with the entry id by contract (tests/test_repairs.py
    pins that), which is what makes deleting them by prefix possible.
    Called from unload AND removal: an entry whose setup failed never
    reaches async_unload_entry, but its first refresh can already have
    raised the rate-limit issue.
    """
    registry = issue_registry.async_get(hass)
    stale = [
        issue_id
        for domain, issue_id in registry.issues
        if domain == DOMAIN and issue_id.startswith(f"{entry_id}_")
    ]
    for issue_id in stale:
        issue_registry.async_delete_issue(hass, DOMAIN, issue_id)


async def async_remove_entry(
    hass: HomeAssistant, config_entry: WemPortalConfigEntry
) -> None:
    """Delete what a removed entry would otherwise leave behind for good.

    Home Assistant calls this only when the entry is removed, not on an
    unload or reload. Neither store was ever deleted before, so a removed
    entry left its module cache and scraper device id in .storage forever -
    and the account state kept remembering an account that no longer exists
    in this installation.
    """
    await get_modules_store(hass, config_entry.entry_id).async_remove()
    await get_scraper_device_store(hass, config_entry.entry_id).async_remove()
    _async_delete_entry_issues(hass, config_entry.entry_id)
    _forget_account_state_if_last_entry(hass, config_entry)


def _another_entry_shares_this_account(hass: HomeAssistant, config_entry) -> bool:
    """Whether a second entry of the same WEM account is configured.

    Asked by both halves of the teardown, which is why it is a function: the
    account state and the auth-failure streak live under the ACCOUNT, so
    neither of them belongs to the entry that is going away when a legacy
    duplicate of it is still there.
    """
    account = account_unique_id(config_entry.data.get(CONF_USERNAME))
    return any(
        other.entry_id != config_entry.entry_id
        and account_unique_id(other.data.get(CONF_USERNAME)) == account
        for other in hass.config_entries.async_entries(DOMAIN)
    )


def _forget_account_state_if_last_entry(hass: HomeAssistant, config_entry) -> None:
    """Drop the account memory only once no entry is left that shares it.

    The two stores above belong to one entry and go with it. This one does
    not: it is addressed by the normalised account, and a legacy duplicate
    entry of the same account is still allowed to load. Removing one of those
    used to take the 403 backoff, the auth-failure streak and the
    once-per-account warning markers away from the entry that stays, which
    then polled as though the portal had never refused anything.
    """
    username = config_entry.data.get(CONF_USERNAME)
    if _another_entry_shares_this_account(hass, config_entry):
        _LOGGER.debug(
            "Another entry still uses this account; keeping its remembered state."
        )
        return
    forget_account_state(username)
