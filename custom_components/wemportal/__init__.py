"""
wemportal integration

Author: erikkastelec
https://github.com/erikkastelec/hass-WEM-Portal

"""
from datetime import timedelta

import homeassistant.helpers.config_validation as config_validation
import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from .const import (
    CONF_LANGUAGE,
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
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
    CONF_EXPERT_MODULE_ARG,
    SERVICE_SET_EXPERT_PARAMETER,
)
from .coordinator import WemPortalDataUpdateCoordinator, get_modules_store
from .wemportalapi import WemPortalApi
from .utils import deserialize_modules
import homeassistant.helpers.entity_registry as entity_registry
from homeassistant.helpers import device_registry as device_registry

def get_wemportal_unique_id(config_entry_id: str, device_id: str, name: str):
    """Return unique ID for WEM Portal."""
    return f"{config_entry_id}:{device_id}:{name}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the wemportal component."""
    hass.data.setdefault(DOMAIN, {})
    return True


# Migrate values from previous versions
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
    # Do migration for first device if we have multiple
    device_id = list(coordinator.data.keys())[0]
    data = coordinator.data[device_id]

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

    if change:
        await coordinator.async_config_entry_first_refresh()


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

    # Currently we only support one device so we will take first device id
    device_id = "0000"
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

    # Creating API object
    api = WemPortalApi(
        entry.data.get(CONF_USERNAME),
        entry.data.get(CONF_PASSWORD),
        config=entry.options,
        cached_modules=cached_modules,
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


def _async_register_expert_service(hass: HomeAssistant, entry: ConfigEntry, api) -> None:
    """Register wemportal.set_expert_parameter (idempotent)."""
    from .expert_writer import WemPortalExpertClient

    if hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER):
        return

    async def _handle_set_expert_parameter(call):
        entityvalue = call.data["entityvalue"]
        value = call.data["value"]

        def _do_write():
            # Own short-lived session per write; honors the global 403
            # cooldown via the api object's check.
            from .expert_writer import expert_client_options
            client = WemPortalExpertClient(
                entry.data.get(CONF_USERNAME),
                entry.data.get(CONF_PASSWORD),
                cooldown_check=api._check_cooldown,
                **expert_client_options(entry.options),
            )
            return client.write_parameter(entityvalue, value)

        async def _run_in_background():
            # An expert write takes ~60-80s (full Fachmann navigation),
            # longer than a service call will wait, so run it detached and
            # report the outcome via a persistent notification + the log.
            try:
                state = await hass.async_add_executor_job(_do_write)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.error("Expert write failed for %s: %s", entityvalue, exc)
                await hass.services.async_call(
                    "persistent_notification", "create",
                    {
                        "title": "WEM Portal expert write failed",
                        "message": f"Setting {entityvalue} to {value} failed: {exc}",
                        "notification_id": f"wemportal_expert_{entityvalue}",
                    },
                    blocking=False,
                )
                return
            _LOGGER.info(
                "Expert parameter %s set to %s (allowed range %s..%s)",
                entityvalue, state.current, state.min_value, state.max_value,
            )
            await hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "WEM Portal expert write",
                    "message": f"{entityvalue} set to {state.current}.",
                    "notification_id": f"wemportal_expert_{entityvalue}",
                },
                blocking=False,
            )

        # Return immediately; the write continues in the background.
        hass.async_create_background_task(
            _run_in_background(), name=f"wemportal_expert_write_{entityvalue}"
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EXPERT_PARAMETER,
        _handle_set_expert_parameter,
        schema=vol.Schema(
            {
                vol.Required("entityvalue"): config_validation.string,
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
    import random
    from homeassistant.helpers.event import async_call_later
    from .expert_writer import WemPortalExpertClient

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
            def _do_read():
                from .expert_writer import expert_client_options
                client = WemPortalExpertClient(
                    entry.data.get(CONF_USERNAME),
                    entry.data.get(CONF_PASSWORD),
                    cooldown_check=api._check_cooldown,
                    **expert_client_options(entry.options),
                )
                return client.read_many(entityvalues)

            try:
                results = await hass.async_add_executor_job(_do_read)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning("Expert auto-poll read failed: %s", exc)
                return
            for entity in entities:
                entity.apply_read_state(results.get(entity.entityvalue))
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
        hass.async_create_background_task(_poll(), name="wemportal_expert_initial_poll")

    # If the entities already exist, start now; otherwise number.py will call
    # this once it has created them.
    store["start_expert_auto_poll"] = _start
    if store.get("expert_entities"):
        _start()


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle schema migrations."""
    # V1 to V2 migration is a no-op for the data schema, as entity ID migration 
    # is handled dynamically inside async_setup_entry via migrate_unique_ids.
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
        hass.data.get(DOMAIN, {}).pop(config_entry.entry_id, None)
        # Remove the expert service (if registered) so a reload with the
        # option disabled doesn't leave a stale service behind.
        if hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER):
            hass.services.async_remove(DOMAIN, SERVICE_SET_EXPERT_PARAMETER)

    return unload_ok
