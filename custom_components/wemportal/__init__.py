"""
wemportal integration

Author: erikkastelec
https://github.com/erikkastelec/hass-WEM-Portal

"""
from datetime import timedelta
import random
import threading

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from .const import (
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DOMAIN,
    PLATFORMS,
    _LOGGER,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    CONF_EXPERT_WRITE,
    CONF_EXPERT_AUTO_POLL,
    CONF_EXPERT_POLL_INTERVAL,
    CONF_EXPERT_NOTIFY_ON_SUCCESS,
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
    SERVICE_SET_EXPERT_PARAMETER,
)
from .coordinator import (
    WemPortalDataUpdateCoordinator,
    get_modules_store,
    get_scraper_device_store,
)
from .wemportalapi import WemPortalApi
from .utils import deserialize_modules, close_api_sessions
from homeassistant.helpers import device_registry, entity_registry

def get_wemportal_unique_id(config_entry_id: str, device_id: str, name: str):
    """Return unique ID for WEM Portal."""
    return f"{config_entry_id}:{device_id}:{name}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the wemportal component."""
    hass.data.setdefault(DOMAIN, {})
    return True


# Migrate values from previous versions
def _migrate_device_unique_ids(er, config_entry, device_id, data) -> bool:
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
            possible_old_ids.append(get_wemportal_unique_id(config_entry.entry_id, device_id, friendly_name))

        parameter_id = values.get("ParameterID")
        if parameter_id:
            possible_old_ids.append(parameter_id)
            possible_old_ids.append(f"{device_id}-{parameter_id}")
            possible_old_ids.append(get_wemportal_unique_id(config_entry.entry_id, device_id, parameter_id))

        # Try to find an entity under any of these old ids
        for old_id in possible_old_ids:
            if not old_id:
                continue
            name_id = er.async_get_entity_id(platform, DOMAIN, old_id)
            if name_id is not None:
                new_entity_id = er.async_get_entity_id(platform, DOMAIN, new_id)
                if new_entity_id is not None and new_entity_id != name_id:
                    _LOGGER.info(
                        "Found entity with old id and an entity with a new unique_id. Preserving old entity..."
                    )
                    er.async_remove(new_entity_id)

                if old_id != new_id:
                    _LOGGER.info(
                        "Migrating entity %s from old id %s to new unique_id %s",
                        name_id,
                        old_id,
                        new_id,
                    )
                    er.async_update_entity(
                        name_id,
                        new_unique_id=new_id,
                    )
                    change = True
                break
    return change


async def migrate_unique_ids(
    hass: HomeAssistant, config_entry: ConfigEntry, coordinator
):
    er = entity_registry.async_get(hass)
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
        if _migrate_device_unique_ids(er, config_entry, device_id, coordinator.data[device_id]):
            change = True

    if change:
        # A debounced refresh is enough to update the migrated entities.
        # async_config_entry_first_refresh() here ran a SECOND full portal
        # cycle right after the initial one (and is meant for setup only) -
        # needless extra requests against the portal's rate limit.
        await coordinator.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the wemportal component."""
    # Set proper update_interval, based on selected mode
    if entry.options.get(CONF_MODE) == "web":
        update_interval = entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_CONF_SCAN_INTERVAL_VALUE
        )

    elif entry.options.get(CONF_MODE) == "api":
        update_interval = entry.options.get(
            CONF_SCAN_INTERVAL_API, DEFAULT_CONF_SCAN_INTERVAL_API_VALUE
        )
    else:
        update_interval = min(
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_CONF_SCAN_INTERVAL_VALUE),
            entry.options.get(
                CONF_SCAN_INTERVAL_API, DEFAULT_CONF_SCAN_INTERVAL_API_VALUE
            ),
        )

    dr = device_registry.async_get(hass)
    devices = [
        device
        for device in dr.devices.values()
        if entry.entry_id in device.config_entries
    ]
    device_ids = [device.name for device in devices]
    if not device_ids:
        _LOGGER.warning("No devices found for %s. Starting first time initialization.", DOMAIN)
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
        cached_modules = deserialize_modules(cached_modules_raw) if cached_modules_raw else None
    except Exception as exc:  # pylint: disable=broad-except
        # A corrupted/unreadable cache file must never prevent the
        # integration from starting - worst case, we just lose the
        # startup-time optimization for this one restart and fall back to
        # a full discovery, exactly like a first-ever install.
        _LOGGER.warning(
            "Could not load cached WEM Portal module data, falling back to full "
            "discovery for this restart: %s", exc
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
        scraper_device_id = await get_scraper_device_store(hass, entry.entry_id).async_load()
    except Exception as exc:  # pylint: disable=broad-except
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

    await coordinator.async_config_entry_first_refresh()

    # Is there an on_update function that we can add listener to?
    _LOGGER.info("Migrating entity names for wemportal")
    try:
        await migrate_unique_ids(hass, entry, coordinator)
    except Exception as exc:  # pylint: disable=broad-except
        # Migration is a best-effort cleanup step (renames old unique_ids
        # to the new format). A failure here should never prevent the
        # integration from loading - worst case, some entities keep their
        # old unique_id until the next successful migration attempt.
        _LOGGER.warning("Unique_id migration failed, continuing without it: %s", exc)

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        # "config": entry.data,
        "coordinator": coordinator,
        # Shared per-account lock: only one expert portal operation (a number
        # entity write, the service, or the auto-poll read) may run at a time,
        # so they don't collide on the same parameter or open parallel portal
        # sessions. Acquired non-blocking by each expert path.
        "expert_lock": threading.Lock(),
    }

    # Register the hub device so child devices can reference it via via_device
    device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Weishaupt",
        name=entry.title or "WEM Portal",
        model="WEM Portal",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    # Expert write access (web): register the service only while the
    # option is enabled. Everything lives in expert_writer.py - the
    # polling paths (scraper/API/coordinator) are untouched.
    if entry.options.get(CONF_EXPERT_WRITE, False):
        _async_register_expert_service(hass, entry, api)
        _async_setup_expert_auto_poll(hass, entry, api)

    return True


def _resolve_expert_entry(hass: HomeAssistant):
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
        store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        api = store.get("api") if store else None
        if api is not None:
            candidates.append((entry, api))
    return candidates[0] if len(candidates) == 1 else None


def _async_register_expert_service(hass: HomeAssistant, entry: ConfigEntry, api) -> None:
    """Register wemportal.set_expert_parameter (idempotent).

    The handler does NOT close over `entry`/`api`; it resolves the target
    account on each call (see _resolve_expert_entry), so the single global
    service addresses the correct account and refuses when it can't tell.
    """
    from .expert_writer import WemPortalExpertClient, ev_digest, short_ev

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

        store = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {})
        lock = store.get("expert_lock")
        ev_short = short_ev(entityvalue)

        def _do_write():
            # Own short-lived session per write; honors the shared 403
            # cooldown (check) and ENGAGES it on a 403 (activate).
            from .expert_writer import expert_client_options
            client = WemPortalExpertClient(
                target_entry.data.get(CONF_USERNAME),
                target_entry.data.get(CONF_PASSWORD),
                cooldown_check=target_api.check_expert_cooldown,
                cooldown_activate=target_api.activate_expert_cooldown,
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
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error("Expert write failed for %s: %s", ev_short, exc)
            raise HomeAssistantError(
                f"WEM Portal expert write for {ev_short} to {value} failed: {exc}"
            ) from exc
        finally:
            if lock is not None:
                lock.release()
        _LOGGER.info(
            "Expert parameter %s set to %s (allowed range %s..%s)",
            ev_short, state.current, state.min_value, state.max_value,
        )
        if target_entry.options.get(CONF_EXPERT_NOTIFY_ON_SUCCESS, False):
            await hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "WEM Portal expert write",
                    "message": f"{ev_short} set to {state.current}.",
                    "notification_id": f"wemportal_expert_{ev_digest(entityvalue)}",
                },
                blocking=False,
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EXPERT_PARAMETER,
        _handle_set_expert_parameter,
        schema=vol.Schema(
            {
                vol.Required("entityvalue"): cv.string,
                vol.Required("value"): vol.Coerce(float),
            }
        ),
    )


def _async_setup_expert_auto_poll(hass: HomeAssistant, entry: ConfigEntry, api) -> None:
    """Optionally read the configured expert parameters on a timer.

    OFF unless CONF_EXPERT_AUTO_POLL is enabled. Each read is a full
    Fachmann navigation, so this is deliberately infrequent (default 60 min,
    floored at MIN_EXPERT_POLL_INTERVAL_MINUTES) and reads ALL configured ids
    in ONE shared session to minimise load. A 403 engages the shared cooldown
    via the client's cooldown_check, and this poll then skips until it clears.

    The entities are created by number.py's platform setup, which may run
    after this. So we expose a `start_expert_auto_poll` callback in the entry
    store; whichever of the two runs last actually starts the timer.
    """
    from homeassistant.helpers.event import async_call_later
    from .expert_writer import WemPortalExpertClient, ev_digest

    if not entry.options.get(CONF_EXPERT_AUTO_POLL, False):
        return

    interval_min = entry.options.get(
        CONF_EXPERT_POLL_INTERVAL, DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
    )
    try:
        interval_min = max(int(interval_min), MIN_EXPERT_POLL_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        interval_min = DEFAULT_EXPERT_POLL_INTERVAL_MINUTES

    # Fraction of extra, random delay added on top of the configured interval
    # each cycle (0..20%). Jitter is added ONLY upwards, so the effective
    # interval is always >= the user's setting (and thus never below the
    # 15-min floor) - the poll pattern is less regular without ever hitting
    # the portal more often than configured.
    EXPERT_POLL_JITTER_FRACTION = 0.20

    store = hass.data[DOMAIN][entry.entry_id]

    def _next_delay_seconds():
        base = interval_min * 60
        return base + random.uniform(0, base * EXPERT_POLL_JITTER_FRACTION)

    async def _poll(_now=None):
        try:
            entities = store.get("expert_entities") or []
            entityvalues = [e.entityvalue for e in entities]
            if not entityvalues:
                return
            # Collision guard: if any entity write is in flight, skip this
            # cycle instead of opening a second concurrent portal session.
            # Reading in parallel could also briefly write a pre-write
            # (stale) value back into an entity right after its verified
            # write. The next scheduled poll picks things up again.
            if any(getattr(e, "_write_in_progress", False) for e in entities):
                _LOGGER.debug(
                    "Expert auto-poll: a write is in progress, skipping this cycle."
                )
                return
            # Use the CURRENT api from the store (the coordinator swaps it on
            # recovery); the closed-over `api` may be a discarded instance
            # with a stale cooldown state.
            current_api = store.get("api", api)
            # Shared per-account lock: skip this cycle if a write (entity or
            # service) is already using the portal for this account.
            lock = store.get("expert_lock")
            if lock is not None and not lock.acquire(blocking=False):
                _LOGGER.debug(
                    "Expert auto-poll: another expert operation in progress, "
                    "skipping this cycle."
                )
                return

            def _do_read():
                from .expert_writer import expert_client_options
                client = WemPortalExpertClient(
                    entry.data.get(CONF_USERNAME),
                    entry.data.get(CONF_PASSWORD),
                    cooldown_check=current_api.check_expert_cooldown,
                    cooldown_activate=current_api.activate_expert_cooldown,
                    **expert_client_options(entry.options),
                )
                return client.read_many(entityvalues)

            try:
                results = await hass.async_add_executor_job(_do_read)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning("Expert auto-poll read failed: %s", exc)
                return
            finally:
                if lock is not None:
                    lock.release()
            # Per-id consecutive-failure tracking: a persistently failing id
            # (usually a typo'd entityvalue) would otherwise only produce an
            # hourly debug/warning nobody sees. After 3 consecutive failures
            # raise ONE notification per id; reset on the next success so a
            # recurring problem re-notifies at most once per streak.
            fail_counts = store.setdefault("expert_poll_fail_counts", {})
            notified = store.setdefault("expert_poll_fail_notified", set())
            for entity in entities:
                state = results.get(entity.entityvalue)
                ev = entity.entityvalue
                if state is None:
                    fail_counts[ev] = fail_counts.get(ev, 0) + 1
                    if fail_counts[ev] >= 3 and ev not in notified:
                        notified.add(ev)
                        hass.async_create_task(
                            hass.services.async_call(
                                "persistent_notification", "create",
                                {
                                    "title": "WEM Portal expert auto-poll",
                                    "message": (
                                        f"Reading '{entity.name}' has failed "
                                        f"{fail_counts[ev]} times in a row. "
                                        "Check the configured entityvalue ID "
                                        "in the integration options."
                                    ),
                                    "notification_id": f"wemportal_poll_fail_{ev_digest(ev)}",
                                },
                                blocking=False,
                            )
                        )
                else:
                    fail_counts.pop(ev, None)
                    notified.discard(ev)
                entity.apply_read_state(state)
        finally:
            # Always reschedule the next run (with fresh jitter), even if this
            # cycle failed - a transient error must not stop future polls.
            _schedule_next()

    def _schedule_next():
        delay = _next_delay_seconds()
        unsub = async_call_later(hass, delay, _poll)
        store["expert_poll_unsub"] = unsub
        _LOGGER.debug(
            "Expert auto-poll: next read in %.1f min (base %d min + jitter).",
            delay / 60, interval_min,
        )

    def _cancel():
        unsub = store.pop("expert_poll_unsub", None)
        if unsub is not None:
            unsub()
        # Also cancel the initial poll if it is still running: it is a
        # background task that would otherwise keep going after the entry is
        # unloaded (only the scheduled timer was cancelled before).
        task = store.pop("expert_poll_initial_task", None)
        if task is not None and not task.done():
            task.cancel()

    def _start():
        # Idempotent: only one timer chain per entry.
        if store.get("expert_poll_started"):
            return
        store["expert_poll_started"] = True
        entry.async_on_unload(_cancel)
        _LOGGER.info(
            "Expert auto-poll enabled: reading configured parameters about "
            "every %d min (with up to +%d%% random jitter).",
            interval_min, int(EXPERT_POLL_JITTER_FRACTION * 100),
        )
        # Initial read shortly after startup; it reschedules itself afterwards.
        # Tracked so _cancel() can stop it if the entry is unloaded mid-run.
        store["expert_poll_initial_task"] = hass.async_create_background_task(
            _poll(), name="wemportal_expert_initial_poll"
        )

    # If the entities already exist, start now; otherwise number.py will call
    # this once it has created them.
    store["start_expert_auto_poll"] = _start
    if store.get("expert_entities"):
        _start()


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle schema migrations."""
    # V1 -> V2 needs no data-schema change (entity-id migration happens
    # dynamically in async_setup_entry via migrate_unique_ids). But the entry
    # version must actually be bumped, otherwise HA keeps treating a V1 entry
    # as migration-pending and re-runs this on every startup.
    if config_entry.version < 2:
        hass.config_entries.async_update_entry(config_entry, version=2)
    return True


async def _async_entry_updated(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle entry updates."""
    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if entry_data is None or "coordinator" not in entry_data:
        _LOGGER.debug("No coordinator found for %s during entry update; skipping migration.", config_entry.entry_id)
    else:
        _LOGGER.info("Migrating entity names for wemportal because of config entry update")
        try:
            await migrate_unique_ids(hass, config_entry, entry_data["coordinator"])
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("Unique_id migration failed, continuing without it: %s", exc)
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unload_ok = bool(
        await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    )
    if unload_ok:
        store = hass.data.get(DOMAIN, {}).pop(config_entry.entry_id, None)
        # Close the API + scraper HTTP sessions so they don't linger with an
        # open connection after the entry is unloaded/reloaded.
        api = store.get("api") if store else None
        if api is not None:
            await hass.async_add_executor_job(close_api_sessions, api)
        # The expert service is a single domain-wide registration shared by
        # all entries. Only remove it once NO remaining loaded entry still
        # has expert write enabled - previously unloading ANY entry removed
        # it globally, killing the service for other accounts.
        if hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER):
            still_enabled = any(
                other.entry_id != config_entry.entry_id
                and other.options.get(CONF_EXPERT_WRITE, False)
                and hass.data.get(DOMAIN, {}).get(other.entry_id) is not None
                for other in hass.config_entries.async_entries(DOMAIN)
            )
            if not still_enabled:
                hass.services.async_remove(DOMAIN, SERVICE_SET_EXPERT_PARAMETER)

    return unload_ok
