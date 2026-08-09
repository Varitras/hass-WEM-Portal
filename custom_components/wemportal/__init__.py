"""
wemportal integration

Author: erikkastelec
https://github.com/erikkastelec/hass-WEM-Portal

"""

from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import (
    _LOGGER,
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
    SERVICE_SET_EXPERT_PARAMETER,
)
from .coordinator import (
    WemPortalDataUpdateCoordinator,
    forget_auth_failures,
    get_modules_store,
    get_scraper_device_store,
)
from .exceptions import ExpertOperationAborted
from .models import WemPortalConfigEntry, WemPortalData
from .utils import clamped_scan_interval, close_api_sessions, deserialize_modules
from .wemportalapi import WemPortalApi


def get_wemportal_unique_id(config_entry_id: str, device_id: str, name: str):
    """Return unique ID for WEM Portal."""
    return f"{config_entry_id}:{device_id}:{name}"


# This integration is configured exclusively through the UI; async_setup only
# prepares hass.data. Declaring that explicitly is what hassfest asks for -
# without it, every run warns that async_setup exists without a CONFIG_SCHEMA,
# and a stray `wemportal:` block in configuration.yaml would be accepted
# silently instead of being rejected with a clear message.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the wemportal component."""
    hass.data.setdefault(DOMAIN, {})
    return True


# Migrate values from previous versions
def _migrate_device_unique_ids(registry, config_entry, device_id, data) -> bool:
    """Migrate one device's entities from old unique_id formats to the current
    one. Returns True if any entity was updated. Factored out so migration can
    run for EVERY device, not just the first."""
    change = False
    for unique_id, values in data.items():
        if isinstance(values, int):
            continue

        new_id = get_wemportal_unique_id(config_entry.entry_id, device_id, unique_id)

        # Build a list of possible old unique_ids
        friendly_name = values.get("friendlyName", "")
        platform = values.get("platform", "sensor")

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

        parameter_id = values.get("ParameterID")
        if parameter_id:
            possible_old_ids.append(parameter_id)
            possible_old_ids.append(f"{device_id}-{parameter_id}")
            possible_old_ids.append(
                get_wemportal_unique_id(config_entry.entry_id, device_id, parameter_id)
            )

        # Try to find an entity under any of these old ids
        for old_id in possible_old_ids:
            if not old_id:
                continue
            name_id = registry.async_get_entity_id(platform, DOMAIN, old_id)
            if name_id is not None:
                new_entity_id = registry.async_get_entity_id(platform, DOMAIN, new_id)
                if new_entity_id is not None and new_entity_id != name_id:
                    _LOGGER.info(
                        "Found entity with old id and an entity with a new unique_id. Preserving old entity..."
                    )
                    registry.async_remove(new_entity_id)

                if old_id != new_id:
                    _LOGGER.info(
                        "Migrating entity %s from old id %s to new unique_id %s",
                        name_id,
                        old_id,
                        new_id,
                    )
                    registry.async_update_entity(
                        name_id,
                        new_unique_id=new_id,
                    )
                    change = True
                break
    return change


def _remove_entities_from_a_previous_platform(
    registry, config_entry, device_id, data
) -> None:
    """Drop registry entries this integration no longer provides.

    A parameter can change platform between releases when we learn what it
    actually is. Holiday begin and end were switches until the portal's own
    parameter list showed them to be dates - and because our unique_id does
    not carry the platform, the old switch entry survives the change and sits
    in the registry unavailable, next to the working date entity.

    Only entries under OUR unique_id and OUR own platforms are touched, and
    only the ones the current data says belong to a different platform now.
    """
    for unique_id, values in data.items():
        if isinstance(values, int):
            continue
        current = values.get("platform", "sensor")
        entity_unique_id = get_wemportal_unique_id(
            config_entry.entry_id, device_id, unique_id
        )
        for platform in PLATFORMS:
            if platform == current:
                continue
            stale = registry.async_get_entity_id(platform, DOMAIN, entity_unique_id)
            if stale is None:
                continue
            _LOGGER.info(
                "%s is a %s now, not a %s - removing the entity it left behind.",
                stale,
                current,
                platform,
            )
            registry.async_remove(stale)


async def migrate_unique_ids(
    hass: HomeAssistant, config_entry: ConfigEntry, coordinator
):
    registry = entity_registry.async_get(hass)
    # Nothing to migrate yet if the first refresh came back empty (e.g. no
    # devices found, or every device failed this cycle) - guard against
    # this instead of crashing with an IndexError on an empty keys() list,
    # which would otherwise abort the entire integration setup.
    if not coordinator.data:
        _LOGGER.debug("Skipping unique_id migration: coordinator has no data yet.")
        return
    # Migrate EVERY device, not just the first: with multiple devices the
    # others' old unique_ids (and their history) were previously left behind.
    change = False
    for device_id in coordinator.data:
        if _migrate_device_unique_ids(
            registry, config_entry, device_id, coordinator.data[device_id]
        ):
            change = True
        # After the id migration, not before: that step may still move an old
        # entry onto the current unique_id, and removing it first would throw
        # away the history it exists to preserve.
        _remove_entities_from_a_previous_platform(
            registry, config_entry, device_id, coordinator.data[device_id]
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
    scraper_device_id = None
    try:
        scraper_device_id = await get_scraper_device_store(
            hass, entry.entry_id
        ).async_load()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not load stored scraper device id: %s", exc)

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
    except Exception:
        # The api is not in hass.data yet, so async_unload_entry cannot close
        # it: a failed first refresh (portal down, 403, auth) would leak its
        # HTTP sessions, once more per setup retry.
        await hass.async_add_executor_job(close_api_sessions, api)
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
            _async_register_expert_service(hass)
            entry.runtime_data.expert.setup_auto_poll(hass, entry)
    except Exception:
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
        await hass.async_add_executor_job(close_api_sessions, api)
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
        and getattr(other, "runtime_data", None) is not None
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


def _async_register_expert_service(hass: HomeAssistant) -> None:
    """Register wemportal.set_expert_parameter (idempotent).

    Takes no entry and no api on purpose: one global registration serves every
    configured account, so the handler resolves its target per call (see
    _resolve_expert_entry) and refuses when it cannot tell which is meant.
    """
    # Function-local, like every other expert_writer import in this file:
    # the module pulls curl_cffi and lxml (~140 ms, measured) and this
    # file is imported whenever Home Assistant loads the integration.
    from .expert_writer import (
        WemPortalExpertClient,
        entityvalue_digest,
        short_entityvalue,
    )

    if hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER):
        return

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
        allowed = {
            (
                target_entry.options.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot) or ""
            ).strip()
            for slot in range(1, EXPERT_SLOT_COUNT + 1)
        }
        allowed.discard("")
        if entityvalue not in allowed:
            raise HomeAssistantError(
                f"WEM Portal expert write: {short_entityvalue(entityvalue)} is not one of "
                "the parameters configured in this integration's options. Add it "
                "to a slot first."
            )

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
            """
            reason = data.why_not_current(target_entry)
            if reason is not None:
                raise ExpertOperationAborted(
                    f"{reason} before the write reached the portal"
                )

        def _do_write():
            # Own short-lived session per write; honors the shared 403
            # cooldown (check) and ENGAGES it on a 403 (activate).
            from .expert_options import expert_client_options

            _raise_if_unloaded()
            client = WemPortalExpertClient(
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
    # Function-local: config_flow imports expert_writer at module level,
    # so a top-level import here would pull curl_cffi into every setup.
    from .config_flow import account_unique_id

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

    forget_auth_failures(config_entry.entry_id)
    # runtime_data is still readable here - Home Assistant drops it only
    # after this returns True. Close the API + scraper HTTP sessions so
    # they don't linger open after the entry is unloaded/reloaded.
    if data is not None:
        await hass.async_add_executor_job(close_api_sessions, data.api)
    # The expert service is a single domain-wide registration shared by
    # all entries. Only remove it once NO remaining loaded entry still
    # has expert write enabled - previously unloading ANY entry removed
    # it globally, killing the service for other accounts.
    _async_release_expert_service(hass, config_entry)
    from .holiday import async_release_holiday_service

    async_release_holiday_service(hass, config_entry)

    return True
