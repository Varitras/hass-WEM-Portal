"""End-to-end tests against a real Home Assistant instance.

These exercise the parts that only exist once Home Assistant itself is
driving the integration: entry setup/unload, the schema migration, the
config/options/reauth flows through the real flow manager, and the expert
service registration. Everything below the flow layer (portal HTTP) is
mocked - the point here is the Home Assistant contract, not the scraper.

Marked `e2e` because each test boots a full Home Assistant instance; the
everyday run deselects them (see pytest.ini), CI runs them with `-m ""`.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    STATE_UNAVAILABLE,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wemportal import expert_writer
from custom_components.wemportal.models import Reading
from custom_components.wemportal.const import (
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_WRITE,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DOMAIN,
    PLATFORMS,
)
from custom_components.wemportal.exceptions import (
    AuthError,
    ForbiddenError,
    ParameterWriteError,
)
from custom_components.wemportal.wemportalapi import WemPortalApi
from custom_components.wemportal import (
    SERVICE_SET_EXPERT_PARAMETER,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

# Placeholder entityvalue IDs. Real ones are installation-specific and must
# never appear in the repository - these only need to satisfy the format
# check (hex, >= MIN_EXPERT_ENTITYVALUE_LENGTH).
EV_A = "a" * 36
EV_B = "b" * 36

USER = "user@example.org"

BASE_DATA = {CONF_USERNAME: USER, CONF_PASSWORD: "secret"}
BASE_OPTIONS = {
    CONF_SCAN_INTERVAL: 1800,
    CONF_SCAN_INTERVAL_API: 300,
    CONF_LANGUAGE: "en",
    CONF_MODE: "api",
}

# One device with one sensor data point, in the shape fetch_data() returns.
FAKE_DATA = {
    "1234": {
        "Outside temperature": Reading(
            value=12.5,
            unit="°C",
            platform="sensor",
            friendly_name="Outside temperature",
            parameter_id="P1",
        )
    }
}


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Without this Home Assistant refuses to load a custom integration."""
    yield


@pytest.fixture(autouse=True)
def _mock_portal(monkeypatch):
    """Keep every test off the real portal.

    Patching the API's outward-facing methods (rather than replacing the
    whole class) keeps the real WemPortalApi object in play, so the
    coordinator, the entity platforms and the unload path all run against
    the production types.
    """
    monkeypatch.setattr(
        WemPortalApi, "fetch_data", lambda self, *_args, **_kwargs: FAKE_DATA
    )
    monkeypatch.setattr(WemPortalApi, "api_login", lambda self, *_args, **_kwargs: None)
    monkeypatch.setattr(WemPortalApi, "web_login", lambda self, *_args, **_kwargs: None)

    # The EXPERT client must be blocked too. It uses curl_cffi, which is not
    # covered by the socket guard, so an expert path reached during a test
    # really did contact wemportal.com - a failed login against a live
    # third-party service, on every run. Every network entry point is stubbed
    # here; tests that need specific behaviour override these.
    def _no_network(*_args, **_kwargs):
        raise AssertionError(
            "a test reached the real portal - stub the expert client instead"
        )

    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "_login", _no_network)
    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "_full_login", _no_network)
    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient, "read_many", lambda self, ids: {}
    )
    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient, "write_parameter", _no_network
    )
    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient, "list_modules", _no_network
    )
    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "discover", _no_network)


def _entry(hass, options=None, version=2):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USER,
        data=BASE_DATA,
        options={**BASE_OPTIONS, **(options or {})},
        version=version,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --- setup / unload ---------------------------------------------------


async def test_setup_creates_entities_and_devices(hass):
    """A full setup must produce a live sensor state and the hub + child
    device, i.e. the coordinator data actually reaches the entity layer."""
    entry = await _setup(hass, _entry(hass))

    assert entry.state is ConfigEntryState.LOADED

    states = [
        state
        for state in hass.states.async_all("sensor")
        if "outside_temperature" in state.entity_id
    ]
    assert states, "no sensor entity was created from the coordinator data"
    assert states[0].state == "12.5"

    from homeassistant.helpers import device_registry

    dr = device_registry.async_get(hass)
    identifiers = {
        ident for device in dr.devices.values() for ident in device.identifiers
    }
    assert (DOMAIN, entry.entry_id) in identifiers, "hub device missing"
    assert (DOMAIN, f"{entry.entry_id}:1234") in identifiers, "child device missing"


async def test_unload_cleans_up(hass):
    """Unload must release the entry store; a leftover would make a later
    reload operate on a stale api/coordinator."""
    entry = await _setup(hass, _entry(hass))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(entry, "runtime_data")


async def test_removing_the_entry_deletes_its_stores_and_account_memory(
    hass, hass_storage
):
    """Removal must take everything the entry left behind with it.

    Neither store was ever deleted, so a removed (or re-added) entry left
    its module cache and scraper device id in .storage forever; the account
    state kept the removed account's memory; and a repair issue raised for
    the entry stayed in the dashboard with no integration behind it.
    """
    from homeassistant.helpers import issue_registry

    from custom_components.wemportal.models import account_state

    entry = await _setup(hass, _entry(hass))
    modules_key = f"{DOMAIN}_{entry.entry_id}_modules"
    scraper_key = f"{DOMAIN}_{entry.entry_id}_scraper_device"
    hass_storage[modules_key] = {"version": 1, "key": modules_key, "data": {}}
    hass_storage[scraper_key] = {"version": 1, "key": scraper_key, "data": "1234"}
    # Remembered state that a plain unload deliberately KEEPS (unlike the
    # auth streak, which unload already clears - asserting on that would
    # pass without any removal logic at all).
    account_state(USER).duplicate_rows_reported.add("some row")
    issue_registry.async_create_issue(
        hass,
        DOMAIN,
        f"{entry.entry_id}_rate_limited",
        is_fixable=False,
        severity=issue_registry.IssueSeverity.WARNING,
        translation_key="rate_limited",
    )

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert modules_key not in hass_storage, "the module cache survived removal"
    assert scraper_key not in hass_storage, "the scraper device id survived removal"
    assert "some row" not in account_state(USER).duplicate_rows_reported, (
        "the removed account's memory was kept"
    )
    assert (
        DOMAIN,
        f"{entry.entry_id}_rate_limited",
    ) not in issue_registry.async_get(hass).issues, (
        "a repair issue outlived the entry it belongs to"
    )


async def test_a_reload_drops_expert_entities_whose_slot_is_gone(hass):
    """Clearing a slot must clear its registry entry on the next (re)load.

    The unique_id of a cleared slot was never offered again, so its registry
    entry sat in the dashboard as a permanently unavailable number - one more
    per cleared slot.
    """
    from homeassistant.helpers import entity_registry

    digest_kept = expert_writer.entityvalue_digest(EV_A)
    digest_gone = expert_writer.entityvalue_digest(EV_B)
    entry = _entry(
        hass,
        {CONF_EXPERT_WRITE: True, CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A},
    )
    registry = entity_registry.async_get(hass)
    registry.async_get_or_create(
        "number",
        DOMAIN,
        f"{entry.entry_id}:expert:{digest_gone}",
        config_entry=entry,
    )

    await _setup(hass, entry)

    assert (
        registry.async_get_entity_id(
            "number", DOMAIN, f"{entry.entry_id}:expert:{digest_gone}"
        )
        is None
    ), "the cleared slot's entity stayed registered"
    assert (
        registry.async_get_entity_id(
            "number", DOMAIN, f"{entry.entry_id}:expert:{digest_kept}"
        )
        is not None
    ), "the configured slot's entity was removed with the ghost"


async def test_disabling_expert_write_drops_its_registry_entries(hass):
    """With the option off there are no expert entities, so entries under the
    expert unique_id prefix are ghosts - and ONLY those may go: an entity of
    another platform under this entry must stay untouched."""
    from homeassistant.helpers import entity_registry

    entry = _entry(hass)
    registry = entity_registry.async_get(hass)
    registry.async_get_or_create(
        "number",
        DOMAIN,
        f"{entry.entry_id}:expert:{expert_writer.entityvalue_digest(EV_A)}",
        config_entry=entry,
    )
    bystander = registry.async_get_or_create(
        "number",
        DOMAIN,
        f"{entry.entry_id}:1234:Some plain number",
        config_entry=entry,
    )

    await _setup(hass, entry)

    assert (
        registry.async_get_entity_id(
            "number",
            DOMAIN,
            f"{entry.entry_id}:expert:{expert_writer.entityvalue_digest(EV_A)}",
        )
        is None
    ), "a ghost expert entity survived disabling the option"
    assert registry.async_get_entity_id("number", DOMAIN, bystander.unique_id), (
        "the cleanup removed an entity outside the expert prefix"
    )


async def test_a_rate_limit_becomes_a_repair_issue_and_success_clears_it(
    hass, monkeypatch
):
    """A 403 cooldown pauses ALL polling for a long time - the one state the
    user WILL notice and cannot see the reason for anywhere but the log."""
    from homeassistant.helpers import issue_registry

    entry = await _setup(hass, _entry(hass))
    issue_id = f"{entry.entry_id}_rate_limited"
    registry = issue_registry.async_get(hass)

    def refuse(self, *_args, **_kwargs):
        raise ForbiddenError("rate limited")

    monkeypatch.setattr(WemPortalApi, "fetch_data", refuse)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (DOMAIN, issue_id) in registry.issues, (
        "a rate-limited poll raised no repair issue"
    )

    monkeypatch.setattr(
        WemPortalApi, "fetch_data", lambda self, *_args, **_kwargs: FAKE_DATA
    )
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (DOMAIN, issue_id) not in registry.issues, (
        "the repair issue survived the successful poll that ends the story"
    )


async def test_an_ordinary_failure_does_not_claim_a_rate_limit(hass, monkeypatch):
    """Only a ForbiddenError is evidence of a rate limit. Raising the issue
    for every failed poll would tell the user to wait out a block that does
    not exist - while the real cause goes uninvestigated."""
    from homeassistant.helpers import issue_registry

    from custom_components.wemportal.exceptions import WemPortalError

    entry = await _setup(hass, _entry(hass))

    def broken(self, *_args, **_kwargs):
        raise WemPortalError("portal answered garbage")

    monkeypatch.setattr(WemPortalApi, "fetch_data", broken)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        DOMAIN,
        f"{entry.entry_id}_rate_limited",
    ) not in issue_registry.async_get(hass).issues, (
        "an ordinary failure was reported as a rate limit"
    )


async def test_unloading_takes_the_entry_issues_down(hass):
    """An unloaded entry cannot re-check what its issues report, so they
    must come down with it; a reloaded entry re-raises what still holds
    within a few cycles."""
    from homeassistant.helpers import issue_registry

    entry = await _setup(hass, _entry(hass))
    issue_registry.async_create_issue(
        hass,
        DOMAIN,
        f"{entry.entry_id}_rate_limited",
        is_fixable=False,
        severity=issue_registry.IssueSeverity.WARNING,
        translation_key="rate_limited",
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        DOMAIN,
        f"{entry.entry_id}_rate_limited",
    ) not in issue_registry.async_get(hass).issues, (
        "an issue kept reporting for an entry that can no longer check it"
    )


async def test_removing_a_never_loaded_entry_still_clears_its_issues(hass, monkeypatch):
    """A setup that fails on its first refresh has already raised the
    rate-limit issue - but a failed entry never reaches async_unload_entry,
    so removal is the only cleanup it gets."""
    from homeassistant.helpers import issue_registry

    def refuse(self, *_args, **_kwargs):
        raise ForbiddenError("rate limited")

    monkeypatch.setattr(WemPortalApi, "fetch_data", refuse)
    entry = _entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        DOMAIN,
        f"{entry.entry_id}_rate_limited",
    ) in issue_registry.async_get(hass).issues, (
        "precondition: the failed first refresh raised the issue"
    )

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        DOMAIN,
        f"{entry.entry_id}_rate_limited",
    ) not in issue_registry.async_get(hass).issues, (
        "the issue outlived the entry that raised it"
    )


async def test_migrate_entry_bumps_version(hass):
    """A V1 entry must end up at V2. The bump was missing, so Home Assistant
    treated the entry as migration-pending and re-ran the migration on every
    single startup."""
    entry = _entry(hass, version=1)
    assert entry.version == 1

    await _setup(hass, entry)

    assert entry.version == 2
    assert entry.state is ConfigEntryState.LOADED


# --- unique_id migration ----------------------------------------------


def _sensor(name="Outside temperature"):
    return Reading(
        value=12.5, unit="°C", platform="sensor", friendly_name=name, parameter_id="P1"
    )


async def test_unique_ids_are_migrated_for_every_device(hass, monkeypatch):
    """Old entities must be migrated on ALL devices, not just the first.

    The unique_id carries the entity's history. Migrating only the first
    device silently left every further device's entities behind under their
    old id - they would be re-created empty and their history orphaned.
    """
    two_devices = {
        "1234": {"Outside temperature": _sensor()},
        "5678": {"Outside temperature": _sensor()},
    }
    monkeypatch.setattr(
        WemPortalApi, "fetch_data", lambda self, *_args, **_kwargs: two_devices
    )

    entry = _entry(hass)

    # Pre-register both devices' entities under an OLD unique_id format
    # ("<device_id>-<key>"), as an installation upgrading from an older
    # release would have them in .storage/core.entity_registry.
    from homeassistant.helpers import entity_registry

    er = entity_registry.async_get(hass)
    old_entity_ids = {}
    for device_id in two_devices:
        old_unique_id = f"{device_id}-Outside temperature"
        registered = er.async_get_or_create(
            "sensor", DOMAIN, old_unique_id, config_entry=entry
        )
        old_entity_ids[device_id] = registered.entity_id

    await _setup(hass, entry)

    for device_id in two_devices:
        new_unique_id = f"{entry.entry_id}:{device_id}:Outside temperature"
        migrated = er.async_get_entity_id("sensor", DOMAIN, new_unique_id)

        assert migrated is not None, f"device {device_id} was not migrated"
        # Same registry entry, only re-keyed: that is what preserves history.
        assert migrated == old_entity_ids[device_id]
        assert (
            er.async_get_entity_id("sensor", DOMAIN, f"{device_id}-Outside temperature")
            is None
        )


async def test_migration_is_skipped_when_no_data_arrived(hass, monkeypatch):
    """An empty first refresh must not abort setup - the migration simply
    has nothing to do."""
    monkeypatch.setattr(WemPortalApi, "fetch_data", lambda self, *_args, **_kwargs: {})

    entry = await _setup(hass, _entry(hass))

    assert entry.state is ConfigEntryState.LOADED


# --- expert service ---------------------------------------------------


def _expert_options():
    """Expert write enabled AND the parameter configured in a slot.

    Both are required: the service only accepts ids the user actually put in
    a slot, so a test that omits the slot no longer exercises what it claims.
    """
    return {
        CONF_EXPERT_WRITE: True,
        CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
    }


async def test_expert_service_registered_only_while_enabled(hass):
    """The service exists exactly as long as an expert-enabled entry is
    loaded - and disappears again on unload."""
    plain = await _setup(hass, _entry(hass))
    assert not hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER)

    await hass.config_entries.async_unload(plain.entry_id)
    await hass.async_block_till_done()

    expert = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    assert hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER)

    assert await hass.config_entries.async_unload(expert.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER)


async def test_the_holiday_service_follows_the_loaded_entries(hass):
    """It needs no option - it writes through the same mobile API the number,
    select and switch entities already use - but it must still disappear when
    nothing is loaded to serve it."""
    from custom_components.wemportal.holiday import SERVICE_SET_HOLIDAY

    entry = await _setup(hass, _entry(hass))
    assert hass.services.has_service(DOMAIN, SERVICE_SET_HOLIDAY)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SERVICE_SET_HOLIDAY)


async def test_the_holiday_service_refuses_a_non_admin(hass, hass_read_only_user):
    """It writes a real setting on a heating system, same as the expert one.

    The registration is admin-only and always has been, but nothing checked
    it from the outside: every other test here calls the private handler
    directly, which skips the permission layer entirely. Swapping
    async_register_admin_service for a plain registration would have kept
    them all green while opening the service to any authenticated user.

    Rejected before the schema is applied, so the payload only has to be
    shaped right, not point at anything real.
    """
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    from custom_components.wemportal.holiday import SERVICE_SET_HOLIDAY

    await _setup(hass, _entry(hass))

    read_only = Context(user_id=hass_read_only_user.id)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOLIDAY,
            {
                "begin_entity": "date.holiday_begin",
                "begin": "2026-12-24",
                "end_entity": "date.holiday_end",
                "end": "2026-12-31",
            },
            blocking=True,
            context=read_only,
        )


async def test_the_expert_module_is_only_loaded_when_it_is_enabled(hass, monkeypatch):
    """The expert module pulls curl_cffi and lxml at import time (~140 ms,
    measured), on the event loop, during platform setup.

    number.py used to import it unconditionally while three comments claimed
    it stayed out of the load path unless the option was on. Asserting on the
    CALL rather than on sys.modules is deliberate: the test session has the
    module imported long before this runs, so sys.modules proves nothing.
    """
    calls = []
    monkeypatch.setattr(
        expert_writer,
        "create_expert_number_entities",
        lambda entry: calls.append(entry) or [],
    )

    plain = await _setup(hass, _entry(hass))
    assert calls == [], "the expert module was loaded although the option is off"

    await hass.config_entries.async_unload(plain.entry_id)
    await hass.async_block_till_done()

    await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    assert len(calls) == 1, "the expert module was not loaded although it is on"


async def test_expert_service_raises_on_write_failure(hass, monkeypatch):
    """A failed write must surface as an exception to the caller, so an
    automation can tell whether the parameter was actually set. The old
    fire-and-forget handler always reported success."""
    await _setup(hass, _entry(hass, _expert_options()))

    def boom(self, *_args, **_kwargs):
        raise ParameterWriteError("portal said no")

    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "write_parameter", boom)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_A, "value": 30},
            blocking=True,
        )


async def test_a_service_write_reaches_the_entity_that_shows_the_parameter(
    hass, monkeypatch
):
    """The service verifies its write and then threw the answer away.

    Both ways of setting the same parameter end in a portal read-back, and
    the entity write applies it. The service did not, so the number entity
    kept showing the value from before the write - until the next auto-poll,
    which is off by default, or a restart. The README puts the two side by
    side as the same operation with different permissions.
    """
    await _setup(hass, _entry(hass, _expert_options()))

    def written(self, entityvalue, value, **_kwargs):
        return expert_writer.ExpertParameterState(value, [10.0, 20.0, 30.0], {})

    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "write_parameter", written)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_EXPERT_PARAMETER,
        {"entityvalue": EV_A, "value": 30},
        blocking=True,
    )
    await hass.async_block_till_done()

    shown = [
        state
        for state in hass.states.async_all("number")
        if "expert_parameter_1" in state.entity_id
    ]
    assert shown, "no expert number entity was created for the configured slot"
    assert shown[0].state == "30.0", (
        "the service wrote the parameter but the entity still shows the old value"
    )


async def test_the_service_can_set_the_option_that_is_not_a_number(hass, monkeypatch):
    """The whole way in for a value that sits beside the scale.

    The schema coerced everything to a float, so the word never reached the
    write - and the number entity cannot carry it either, which left "Aus" a
    setting the portal offers and nothing here could set.
    """
    await _setup(hass, _entry(hass, _expert_options()))

    written = {}

    def write(self, entityvalue, value, **_kwargs):
        written["value"] = value
        return expert_writer.ExpertParameterState(
            None, [20.0, 68.0], {}, portal_text="Aus"
        )

    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "write_parameter", write)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_EXPERT_PARAMETER,
        {"entityvalue": EV_A, "value": "Aus"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert written["value"] == "Aus", (
        "the word was coerced to a number before it ever reached the write"
    )
    shown = [
        state
        for state in hass.states.async_all("number")
        if "expert_parameter_1" in state.entity_id
    ]
    assert shown[0].state == "unknown", "a word was published as a number"
    assert shown[0].attributes["portal_value"] == "Aus", (
        "nothing on the entity says which setting it is on"
    )


def _writeable_rows(number_row=None, select_row=None):
    """FAKE_DATA plus one writeable row, in the mapper's own shape."""
    import copy

    data = copy.deepcopy(FAKE_DATA)
    if number_row is not None:
        data["1234"]["Heat pump-Komfort"] = number_row
    if select_row is not None:
        data["1234"]["Heat pump-Betriebsart"] = select_row
    return data


def _number_row(value, min_value, max_value, step):
    return Reading(
        friendly_name="Heat pump Komfort",
        parameter_id="Komfort",
        unit="°C",
        value=value,
        data_type=3,
        module_index=0,
        module_type=1,
        platform="number",
        min_value=min_value,
        max_value=max_value,
        step=step,
    )


def _select_row(value, options, options_names):
    return Reading(
        friendly_name="Heat pump Betriebsart",
        parameter_id="Betriebsart",
        unit=None,
        value=value,
        data_type=1,
        module_index=0,
        module_type=1,
        platform="select",
        options=options,
        options_names=options_names,
    )


async def test_fresh_bounds_from_the_portal_reach_a_running_number(hass, monkeypatch):
    """Rediscovery delivers new bounds; the entity published its
    construction-time ones forever - so a value the device now accepts was
    refused by Home Assistant before this integration was ever asked."""
    monkeypatch.setattr(
        WemPortalApi,
        "fetch_data",
        lambda self, *_args, **_kwargs: _writeable_rows(
            number_row=_number_row(21.0, 0.0, 100.0, 1)
        ),
    )
    entry = await _setup(hass, _entry(hass))
    komfort = next(
        state
        for state in hass.states.async_all("number")
        if "komfort" in state.entity_id
    )
    assert komfort.attributes["min"] == 0.0
    assert komfort.attributes["max"] == 100.0

    entry.runtime_data.coordinator.async_set_updated_data(
        _writeable_rows(number_row=_number_row(22.0, 5.0, 35.0, 0.5))
    )
    await hass.async_block_till_done()

    komfort = hass.states.get(komfort.entity_id)
    assert komfort.state == "22.0"
    assert komfort.attributes["min"] == 5.0, (
        "the new lower bound never reached the running entity"
    )
    assert komfort.attributes["max"] == 35.0
    assert komfort.attributes["step"] == 0.5


async def test_fresh_options_from_the_portal_reach_a_running_select(hass, monkeypatch):
    """The counterpart for selects: an option added by rediscovery was
    missing from the entity, and a device already ON that option read as
    unknown - indistinguishable from a failure."""
    monkeypatch.setattr(
        WemPortalApi,
        "fetch_data",
        lambda self, *_args, **_kwargs: _writeable_rows(
            select_row=_select_row("0", ["0", "1"], ["Aus", "Ein"])
        ),
    )
    entry = await _setup(hass, _entry(hass))
    betriebsart = next(
        state
        for state in hass.states.async_all("select")
        if "betriebsart" in state.entity_id
    )
    assert betriebsart.state == "Aus"

    entry.runtime_data.coordinator.async_set_updated_data(
        _writeable_rows(
            select_row=_select_row("2", ["0", "1", "2"], ["Aus", "Ein", "Party"])
        )
    )
    await hass.async_block_till_done()

    betriebsart = hass.states.get(betriebsart.entity_id)
    assert betriebsart.state == "Party", (
        "the device is on the new option and the entity cannot say so"
    )
    assert "Party" in betriebsart.attributes["options"]


async def test_diagnostics_carry_no_credentials_and_no_installation_ids(hass):
    """The diagnostics download is written to be attached to a public issue.

    Whatever else it contains, three things must not leave the house: the
    login (username, password), the configured expert ids, and the device
    ids - the latter are dict KEYS, which the redaction helper cannot touch,
    so they are replaced by positional aliases.
    """
    import json as json_module

    from custom_components.wemportal.const import CONF_EXPERT_MODULE_LIST
    from custom_components.wemportal.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    # The discovered-module cache lives in the options and carries an
    # entityvalue per entry - under the key "value", which is far too
    # generic to redact by name (every reading has one).
    options = {
        **_expert_options(),
        CONF_EXPERT_MODULE_LIST: [{"value": EV_B, "label": "Heating"}],
    }
    entry = await _setup(hass, _entry(hass, options))
    # A row keyed the way production keys its status rows. FAKE_DATA has no
    # such key, so without this the report simply never contains one - and
    # the assertion below would pass while the aliasing did nothing. The
    # mutation run is what said so: breaking the row-key aliasing left the
    # suite green.
    entry.runtime_data.coordinator.data["1234"]["1234-ConnectionStatus"] = Reading(
        value="online",
        friendly_name="Connection Status",
        parameter_id="ConnectionStatus",
        platform="sensor",
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    dump = json_module.dumps(result, default=str)
    assert "secret" not in dump, "the password is in the report"
    assert USER not in dump, "the username is in the report"
    assert EV_A not in dump, "a configured expert id is in the report"
    assert EV_B not in dump, "a cached expert id from discovery is in the report"
    # No quotes: the device id also sits INSIDE the row keys
    # ("1234-ConnectionStatus"), where the outer aliasing does not reach it.
    assert "1234" not in dump, "a device id survived somewhere in the report"
    assert '"device_1"' in dump, "the aliased device data is missing entirely"
    assert "outside temperature" in dump.lower(), (
        "the readings are gone - a report without data helps nobody"
    )


async def test_expert_service_refuses_while_another_operation_runs(hass):
    """The shared per-account lock must reject a second concurrent expert
    operation instead of opening a parallel portal session."""
    entry = await _setup(hass, _entry(hass, _expert_options()))

    lock: threading.Lock = entry.runtime_data.expert.lock
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(HomeAssistantError, match="in progress"):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_EXPERT_PARAMETER,
                {"entityvalue": EV_A, "value": 30},
                blocking=True,
            )
    finally:
        lock.release()


# --- config flow ------------------------------------------------------


async def test_config_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USER,
            CONF_PASSWORD: "secret",
            CONF_LANGUAGE: "en",
            CONF_MODE: "api",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USER
    assert result["data"][CONF_USERNAME] == USER


async def test_config_flow_rejects_second_entry_for_same_account(hass):
    _entry(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USER,
            CONF_PASSWORD: "secret",
            CONF_LANGUAGE: "en",
            CONF_MODE: "api",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _blocked_ip(monkeypatch):
    """A portal that is refusing this IP, on whichever login is tried."""

    def refuse(self, *_args, **_kwargs):
        raise ForbiddenError("Rate limited")

    monkeypatch.setattr(WemPortalApi, "api_login", refuse)
    monkeypatch.setattr(WemPortalApi, "web_login", refuse)


async def test_setup_names_a_blocked_ip_as_one(hass, monkeypatch):
    """Reported as "cannot connect", a rate limit reads like a network fault
    and invites an immediate retry - against an IP the portal is refusing for
    twelve hours, where every attempt makes it last longer.

    Driven through the real flow on purpose. The check that existed counted
    occurrences of the error key in the SOURCE and verified the translations,
    which stays green while the handler that produces it is unreachable -
    and a reordered pair of except clauses makes it exactly that.
    """
    _blocked_ip(monkeypatch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USER,
            CONF_PASSWORD: "secret",
            CONF_LANGUAGE: "en",
            CONF_MODE: "api",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "rate_limited"}


async def test_reauthentication_names_a_blocked_ip_as_one(hass, monkeypatch):
    """The same, for the step somebody reaches after the entry has already
    failed - which is where a blocked IP sends them."""
    entry = await _setup(hass, _entry(hass))
    _blocked_ip(monkeypatch)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USER, CONF_PASSWORD: "secret"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "rate_limited"}


async def test_a_genuine_connection_problem_is_still_reported_as_one(hass, monkeypatch):
    """The counter-test: naming everything a rate limit would pass the two
    above and tell users to wait twelve hours for a DNS failure."""

    def unreachable(self, *_args, **_kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(WemPortalApi, "api_login", unreachable)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USER,
            CONF_PASSWORD: "secret",
            CONF_LANGUAGE: "en",
            CONF_MODE: "api",
        },
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_refuses_a_different_account(hass):
    """Reauth must re-authenticate the SAME account: the username field is
    editable, and silently repointing an entry at another login would move
    every entity to a different installation."""
    entry = await _setup(hass, _entry(hass))

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "someone-else@example.org", CONF_PASSWORD: "new"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data[CONF_PASSWORD] == "secret", "password must not change"


# --- options flow -----------------------------------------------------


async def _open_options(hass, entry, step):
    """Open the options flow and pick one of the menu entries."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


def _configure_input(**overrides):
    """A complete, valid payload for the `configure` step."""
    data = {
        CONF_SCAN_INTERVAL: 1800,
        CONF_SCAN_INTERVAL_API: 300,
        CONF_LANGUAGE: "en",
        CONF_MODE: "api",
    }
    data.update(overrides)
    return data


async def test_switching_mode_checks_the_transport_it_switches_to(hass, monkeypatch):
    """The two logins are separate, so one working says nothing about the
    other. Setup validates exactly the transport the mode will use; switching
    later did not, so an entry could be moved to a connection its credentials
    do not work on - the dialog reporting success and every update failing.
    """
    entry = await _setup(hass, _entry(hass))

    def refuse_web(self, *_args, **_kwargs):
        raise AuthError("no web login for this account")

    monkeypatch.setattr(WemPortalApi, "web_login", refuse_web)

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _configure_input(**{CONF_MODE: "web"})
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MODE: "invalid_auth"}
    assert entry.options[CONF_MODE] == "api", "the broken mode was saved anyway"


async def test_saving_without_touching_the_mode_costs_no_login(hass, monkeypatch):
    """A save that leaves the mode alone must not spend a portal request -
    least of all the one the portal is most likely to refuse."""
    entry = await _setup(hass, _entry(hass))

    def no_login(self, *_args, **_kwargs):
        raise AssertionError("an unchanged mode was validated against the portal")

    monkeypatch.setattr(WemPortalApi, "api_login", no_login)
    monkeypatch.setattr(WemPortalApi, "web_login", no_login)

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _configure_input(**{CONF_SCAN_INTERVAL: 900})
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_flow_saves_expert_slots(hass):
    entry = await _setup(hass, _entry(hass))

    result = await _open_options(hass, entry, "configure")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "configure"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _configure_input(
            **{
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Power limit",
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
            }
        ),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EXPERT_SLOT_ID_TEMPLATE % 1] == EV_A
    assert entry.options[CONF_EXPERT_WRITE] is True


async def test_options_flow_rejects_duplicate_entityvalue(hass):
    """The same parameter in two slots would create two entities writing the
    same value - the dropdown is meant to prevent it, so the save must too."""
    entry = await _setup(hass, _entry(hass))

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _configure_input(
            **{
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
                CONF_EXPERT_SLOT_ID_TEMPLATE % 2: EV_A,
            }
        ),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_EXPERT_SLOT_ID_TEMPLATE % 1] == "duplicate_entityvalue"
    assert result["errors"][CONF_EXPERT_SLOT_ID_TEMPLATE % 2] == "duplicate_entityvalue"


async def test_options_flow_rejects_malformed_entityvalue(hass):
    entry = await _setup(hass, _entry(hass))

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _configure_input(**{CONF_EXPERT_SLOT_ID_TEMPLATE % 1: "nothex!"}),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_EXPERT_SLOT_ID_TEMPLATE % 1] == "invalid_entityvalue"


async def test_options_flow_without_changes_does_not_reload(hass):
    """Saving an unchanged form must abort instead of writing new options:
    a write reloads the integration and triggers a fresh portal login, which
    counts against the portal's rate limit for nothing.

    The first save is a real change (a fresh entry's options hold only the
    four setup keys, while the form also submits every expert default), so
    the no-op case is the SECOND, identical save.
    """
    entry = await _setup(hass, _entry(hass))

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _configure_input()
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _configure_input()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_changes"


async def test_the_expert_client_is_actually_buildable(hass, monkeypatch):
    """The one test that runs _expert_client itself.

    Every discovery test replaces the method with a stub, so its body was
    never executed - including the function-local import that keeps
    curl_cffi out of a normal entry setup. Deleting that import left the
    whole suite green and would have raised NameError on a real user's first
    discovery run.
    """
    from custom_components.wemportal.config_flow import WemportalOptionsFlow

    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    flow = WemportalOptionsFlow()
    monkeypatch.setattr(type(flow), "config_entry", property(lambda self: entry))

    client = flow._expert_client()

    assert type(client).__name__ == "WemPortalExpertClient"


async def test_an_aborted_discovery_ends_the_flow_instead_of_escaping(
    hass, monkeypatch
):
    """Stopping the worker is half of it; the flow has to end too.

    ExpertOperationAborted is a BaseException, so the `except Exception`
    around both discovery calls does not see it, and Home Assistant's flow
    manager translates only AbortFlow. The teardown therefore stopped the
    portal work and left the flow to die of an uncaught exception - a
    traceback where the user should get a sentence.

    Driven through _run_expert, which both discovery calls go through, rather
    than through the gate: the gate was already covered and is not where this
    went wrong.
    """
    from homeassistant.data_entry_flow import AbortFlow

    from custom_components.wemportal.config_flow import WemportalOptionsFlow
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    flow = WemportalOptionsFlow()
    flow.hass = hass
    monkeypatch.setattr(type(flow), "config_entry", property(lambda self: entry))

    def the_entry_went_away():
        raise ExpertOperationAborted("the integration is being unloaded")

    with pytest.raises(AbortFlow):
        await flow._run_expert(the_entry_went_away)


@pytest.mark.parametrize(
    "step, user_input",
    [
        # A refresh is what makes the module step fetch the list at all; the
        # discovery step calls the portal on any input.
        ("async_step_discover_modules", {"refresh": True}),
        ("async_step_run_discovery", {"modules": ["1"]}),
    ],
)
async def test_the_abort_survives_the_step_that_calls_it(
    hass, monkeypatch, step, user_input
):
    """The translation above is only half the journey - it has to arrive.

    Driven through the FLOW STEP, not through _run_expert: both callers wrap
    it in `except Exception`, and AbortFlow reaches Exception through
    FlowError and HomeAssistantError. So the deliberate stop was caught one
    frame above where it was raised and shown as "discovery_failed" - the
    exact wording the translation exists to avoid. The test that called
    _run_expert directly could not see that, because the swallowing happens
    in the caller.
    """
    from homeassistant.data_entry_flow import AbortFlow

    from custom_components.wemportal import config_flow as flow_module
    from custom_components.wemportal.config_flow import WemportalOptionsFlow
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    flow = WemportalOptionsFlow()
    flow.hass = hass
    monkeypatch.setattr(type(flow), "config_entry", property(lambda self: entry))
    flow._modules = ["1"]
    flow._selected_modules = ["1"]

    class _AbortingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getattr__(self, _name):
            def stop(*_args, **_kwargs):
                raise ExpertOperationAborted("the integration is being unloaded")

            return stop

    monkeypatch.setattr(
        flow_module, "WemPortalExpertClient", _AbortingClient, raising=False
    )
    monkeypatch.setattr(flow, "_expert_client", lambda: _AbortingClient())

    run_step = getattr(flow, step)

    with pytest.raises(AbortFlow):
        await run_step(user_input)


async def test_discovery_stops_when_its_entry_goes_away(hass, monkeypatch):
    """Discovery was the one expert client built without a way to stop.

    It is also the longest sequence there is - a login, the module navigation
    and a form read per module - so an entry unloaded or reloaded while it ran
    had it navigate the portal to the end on credentials and options that were
    no longer current. Every other expert caller had this gate.
    """
    from custom_components.wemportal.config_flow import WemportalOptionsFlow
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    flow = WemportalOptionsFlow()
    monkeypatch.setattr(type(flow), "config_entry", property(lambda self: entry))
    client = flow._expert_client()

    client._check_gates()  # still loaded: the gate has to let this through

    entry.runtime_data.begin_unload()

    with pytest.raises(ExpertOperationAborted):
        client._check_gates()


def test_the_auto_poll_stops_at_the_START_of_an_unload():
    """The two teardown flags sit at opposite ends of the unload.

    `unloading` is set before the platforms come down; `stop()` runs last,
    through async_on_unload. Reading only the second let a poll that began
    just before an unload keep navigating the portal through the whole slow
    part in between - which is the window begin_unload() exists to close, and
    which every other write gate already respects.
    """
    import types

    from custom_components.wemportal.exceptions import ExpertOperationAborted
    from custom_components.wemportal.expert_controller import ExpertController

    controller = ExpertController()
    store = types.SimpleNamespace(unloading=False)
    controller.bind(store)

    controller._raise_if_stopped()  # nothing announced yet

    store.unloading = True

    with pytest.raises(ExpertOperationAborted):
        controller._raise_if_stopped()


def test_two_options_flows_do_not_share_their_discovery():
    """Each flow gets its own lists, because one of them holds entityvalues.

    They used to be class attributes - one list object for every options flow
    in the process. Nothing mutated them, so nothing leaked, but the fix is
    cheaper than the failure: on a Home Assistant running two WEM Portal
    accounts, the first `.append()` anyone wrote would have offered one
    account's installation-specific parameter ids in the other's dropdown.
    """
    from custom_components.wemportal.config_flow import WemportalOptionsFlow

    first = WemportalOptionsFlow()
    second = WemportalOptionsFlow()

    first._discovered.append({"entityvalue": "AAAA"})
    first._selected_modules.append({"index": 1})

    assert second._discovered == [], "one flow's discovery reached another"
    assert second._selected_modules == [], "one flow's selection reached another"


async def test_options_flow_discovery_fills_slot_dropdown(hass, monkeypatch):
    """The discovery path: pick modules, run discovery, and land back on the
    configure form with the found parameters offered in the slot dropdowns."""
    entry = await _setup(hass, _entry(hass))

    modules = [{"index": 6, "value": "m6", "label": "Heat pump"}]
    discovered = [
        {
            "entityvalue": EV_B,
            "name": "Power limit",
            "group": "Heating",
            "value": "30 %",
        }
    ]

    class _StubClient:
        def list_modules(self):
            return modules

        def discover(self, selected):
            assert selected == modules, "only the picked module may be fetched"
            return discovered

    monkeypatch.setattr(
        "custom_components.wemportal.config_flow.WemportalOptionsFlow._expert_client",
        lambda self: _StubClient(),
    )

    result = await _open_options(hass, entry, "discover_modules")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover_modules"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"modules": ["6"], "refresh": False}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "configure"

    # The discovered parameter must be offered as a dropdown option on the
    # slot id fields, labelled "group / name (value)".
    schema = result["data_schema"].schema
    slot_key = next(
        key for key in schema if str(key) == CONF_EXPERT_SLOT_ID_TEMPLATE % 1
    )
    options = schema[slot_key].config["options"]
    assert {"value": EV_B, "label": "Heating / Power limit (30 %)"} in options
    assert not result["errors"], "a successful discovery must not report an error"


async def _run_discovery_with(hass, entry, monkeypatch, discover):
    """Drive the discovery path with a stubbed client's discover()."""
    modules = [{"index": 6, "value": "m6", "label": "Heat pump"}]

    class _StubClient:
        def list_modules(self):
            return modules

        discover = None  # replaced below

    _StubClient.discover = lambda self, selected: discover(selected)
    monkeypatch.setattr(
        "custom_components.wemportal.config_flow.WemportalOptionsFlow._expert_client",
        lambda self: _StubClient(),
    )

    result = await _open_options(hass, entry, "discover_modules")
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"modules": ["6"], "refresh": False}
    )


def _forbidden_expert_client(monkeypatch):
    """Stub whose every portal call fails the test if it is reached."""

    class _StubClient:
        def list_modules(self):
            raise AssertionError("the portal was contacted while the lock was held")

        def discover(self, _selected):
            raise AssertionError("the portal was contacted while the lock was held")

    monkeypatch.setattr(
        "custom_components.wemportal.config_flow.WemportalOptionsFlow._expert_client",
        lambda self: _StubClient(),
    )


async def test_reading_the_module_list_waits_for_the_shared_expert_lock(
    hass, monkeypatch
):
    """One expert operation per account at a time. The entity write and the
    auto-poll both take the lock; discovery - the heaviest of the three, and
    the only one a user starts by hand - did not, so it could open a second
    portal session beside a running poll or write."""
    entry = await _setup(hass, _entry(hass))
    _forbidden_expert_client(monkeypatch)

    assert entry.runtime_data.expert.lock.acquire(blocking=False)
    try:
        result = await _open_options(hass, entry, "discover_modules")
    finally:
        entry.runtime_data.expert.lock.release()

    assert result["errors"] == {"base": "discovery_busy"}


async def test_the_parameter_search_waits_for_the_shared_expert_lock(hass, monkeypatch):
    """The second of the two portal calls in this flow, with the module list
    already stored so only the search itself is exercised."""
    from custom_components.wemportal.const import CONF_EXPERT_MODULE_LIST

    modules = [{"index": 6, "value": "m6", "label": "Heat pump"}]
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_MODULE_LIST: modules}))
    _forbidden_expert_client(monkeypatch)

    result = await _open_options(hass, entry, "discover_modules")
    assert entry.runtime_data.expert.lock.acquire(blocking=False)
    try:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"modules": ["6"], "refresh": False}
        )
    finally:
        entry.runtime_data.expert.lock.release()

    assert result["errors"] == {"base": "discovery_busy"}


async def test_discovery_blocked_by_cooldown_is_reported(hass, monkeypatch):
    """A 403 cooldown aborts discovery BEFORE any request is sent. Silently
    showing an empty dropdown made that indistinguishable from "the portal
    has no parameters" - the user must be told the search never ran."""
    entry = await _setup(hass, _entry(hass))

    def blocked(_selected):
        raise ForbiddenError("backing off (~4 min remaining)")

    result = await _run_discovery_with(hass, entry, monkeypatch, blocked)

    assert result["step_id"] == "configure"
    assert result["errors"] == {"base": "discovery_blocked"}
    # The specifics (remaining time, which request was rejected) reach the
    # form, so the user does not have to read the log to find out.
    assert "4 min remaining" in result["description_placeholders"]["status"]


async def test_discovery_status_is_not_carried_into_the_next_form(hass, monkeypatch):
    """A stale error from a previous run must not reappear later."""
    entry = await _setup(hass, _entry(hass))

    def blocked(_selected):
        raise ForbiddenError("backing off")

    await _run_discovery_with(hass, entry, monkeypatch, blocked)

    result = await _open_options(hass, entry, "configure")

    assert not result["errors"]
    assert result["description_placeholders"]["status"] == ""


async def test_discovery_failure_is_reported(hass, monkeypatch):
    entry = await _setup(hass, _entry(hass))

    def boom(_selected):
        raise RuntimeError("parsing went wrong")

    result = await _run_discovery_with(hass, entry, monkeypatch, boom)

    assert result["errors"] == {"base": "discovery_failed"}


async def test_discovery_without_results_is_reported(hass, monkeypatch):
    """The search ran but found nothing - a distinct case from a failure,
    and the one that tells us the module page parsing needs work."""
    entry = await _setup(hass, _entry(hass))

    result = await _run_discovery_with(hass, entry, monkeypatch, lambda _s: [])

    assert result["errors"] == {"base": "discovery_empty"}


async def test_first_refresh_does_not_filter_devices_away(hass, monkeypatch):
    """On the first refresh nothing is known yet, so no device filter may be
    sent.

    The coordinator builds the "enabled devices" list from `api.data`, which
    is empty until `get_devices()` runs INSIDE the fetch. Passing that empty
    list as a filter means "poll nothing", so discovery never runs and no
    entity is ever created - permanently, because platform setup does not
    run again.
    """
    seen = []

    def record(self, enabled_devices=None):
        seen.append(enabled_devices)
        return FAKE_DATA

    monkeypatch.setattr(WemPortalApi, "fetch_data", record)

    await _setup(hass, _entry(hass))

    assert seen, "the coordinator never fetched"
    assert seen[0] is None, (
        "an empty device list was passed as a filter on the first refresh; "
        "None means 'no filter', [] means 'poll nothing'"
    )
    assert hass.states.async_all("sensor"), "no entities were created"


async def test_a_disabled_device_is_filtered_out_on_the_first_cycle(hass, monkeypatch):
    """Restarting must not buy a disabled device one more poll.

    `api.data` is empty until get_devices() runs inside the fetch, so keying
    "do we know any devices?" off it answered "no" after every restart - and
    a disabled device was polled once per restart, forever. The persisted
    module cache knows the same devices and survives the restart.
    """
    from homeassistant.helpers import device_registry

    from custom_components.wemportal.utils import device_identifier

    entry = await _setup(hass, _entry(hass))
    registry = device_registry.async_get(hass)
    device = registry.async_get_device(
        identifiers={device_identifier(entry.entry_id, "1234")}
    )
    assert device is not None, "setup did not register the device to disable"
    registry.async_update_device(
        device.id, disabled_by=device_registry.DeviceEntryDisabler.USER
    )
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    # What a restart looks like: readings gone, the module cache still there.
    coordinator.api.data = {}
    coordinator.api.modules = {"1234": {(0, 1): {"Index": 0, "Type": 1}}}

    seen = []

    def record(self, enabled_devices=None):
        seen.append(enabled_devices)
        return FAKE_DATA

    monkeypatch.setattr(WemPortalApi, "fetch_data", record)
    await coordinator._async_update_data()

    assert seen == [[]], (
        f"the disabled device was polled anyway (filter was {seen}); "
        "[] means 'poll nothing', None means 'no filter'"
    )


async def test_saving_options_keeps_options_that_are_not_form_fields(hass):
    """Home Assistant REPLACES the options dict with what the flow returns.

    Passing only the form fields silently dropped the cached module list, so
    the next discovery had to read it from the portal again - an extra login,
    which is the request the portal is most likely to reject.
    """
    from custom_components.wemportal.const import CONF_EXPERT_MODULE_LIST

    modules = [{"index": 6, "value": "m6", "label": "Heat pump"}]
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_MODULE_LIST: modules}))

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _configure_input(**{CONF_SCAN_INTERVAL: 900})
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 900
    assert entry.options[CONF_EXPERT_MODULE_LIST] == modules


async def test_submitting_no_module_is_reported(hass, monkeypatch):
    """Distinct from "searched and found nothing": here the search never ran.

    Submitting with nothing ticked used to fall through silently to an empty
    dropdown. Note the existing "found nothing" test ticks a module, so it
    exercises a DIFFERENT branch that happens to set the same error key.
    """
    entry = await _setup(hass, _entry(hass))

    class _StubClient:
        def list_modules(self):
            return [{"index": 6, "value": "m6", "label": "Heat pump"}]

        def discover(self, _selected):
            raise AssertionError("nothing was selected, so nothing may be searched")

    monkeypatch.setattr(
        "custom_components.wemportal.config_flow.WemportalOptionsFlow._expert_client",
        lambda self: _StubClient(),
    )

    result = await _open_options(hass, entry, "discover_modules")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"modules": [], "refresh": False}
    )

    assert result["step_id"] == "configure"
    assert result["errors"] == {"base": "discovery_empty"}


async def test_failed_first_refresh_closes_its_sessions(hass, monkeypatch):
    """The api is not in hass.data yet when the first refresh fails, so the
    normal unload path cannot close it - every setup retry leaked another."""
    import custom_components.wemportal as wemportal_init

    closed = []
    monkeypatch.setattr(
        wemportal_init, "close_api_sessions", lambda api: closed.append(api)
    )

    def boom(self, *_args, **_kwargs):
        raise ConnectionError("portal unreachable")

    monkeypatch.setattr(WemPortalApi, "fetch_data", boom)

    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is not ConfigEntryState.LOADED
    assert closed, "a failed first refresh leaked its HTTP sessions"


async def test_unloaded_entry_does_not_rearm_the_auto_poll(hass, monkeypatch):
    """A poll in flight during unload must not schedule a successor.

    `_poll` reschedules in a `finally`, which also runs on cancellation - and
    it wrote the new timer into the store `_cancel` had already emptied, so
    nothing could cancel it. One immortal chain per reload.
    """
    from custom_components.wemportal.const import CONF_EXPERT_AUTO_POLL

    scheduled = []

    def fake_call_later(_hass, _delay, action):
        scheduled.append(action)
        return lambda: None

    # Patched where it is USED: the controller imports async_call_later at
    # module level, so that is the name the auto-poll actually calls.
    from custom_components.wemportal import expert_controller

    monkeypatch.setattr(expert_controller, "async_call_later", fake_call_later)

    # A configured slot is required: without an expert entity there is
    # nothing to poll, so the timer chain is never armed in the first place.
    entry = await _setup(
        hass,
        _entry(
            hass,
            {
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_AUTO_POLL: True,
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
            },
        ),
    )
    data = entry.runtime_data
    assert data.expert._started, "auto-poll never started"
    assert scheduled, "no poll was ever scheduled"
    poll = scheduled[-1]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Assert the BEHAVIOUR, not just that a flag was set: run the poll that
    # was already in flight when the entry went away. Its `finally` must not
    # arm a successor. (Asserting only the flag passed even with the guard
    # removed - the flag was still being set, just ignored.)
    before = len(scheduled)
    await poll(None)

    assert len(scheduled) == before, "an unloaded entry re-armed the auto-poll timer"


async def _auto_poll_entry(hass, monkeypatch, read_many, entityvalues=None):
    """An entry with the auto-poll armed, plus the list of scheduled polls.

    Returns (entry, scheduled, raised_issues). `read_many` stands in for the
    portal round trip and may raise. `entityvalues` configures more than one
    parameter, which is what the "one bad batch" rule needs to be visible at
    all - with a single id there is nothing to compare it against.

    `raised_issues` records every async_create_issue CALL, not the registry's
    end state: the registry de-duplicates by issue_id, so a controller that
    re-raises the same issue every cycle still ends at one entry - only the
    call count can see the difference the once-per-streak rule makes.
    """
    from custom_components.wemportal import expert_controller
    from custom_components.wemportal.const import CONF_EXPERT_AUTO_POLL

    scheduled = []
    # Patched where it is USED, not where it is defined: the controller
    # imports async_call_later at module level, so the name it calls is
    # the one bound here.
    monkeypatch.setattr(
        expert_controller,
        "async_call_later",
        lambda _hass, _delay, action: scheduled.append(action) or (lambda: None),
    )
    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient,
        "read_many",
        lambda self, ids: read_many(ids),
    )

    raised_issues = []
    real_create_issue = expert_controller.async_create_issue

    def record(hass_argument, domain, issue_id, **kwargs):
        raised_issues.append({"issue_id": issue_id, **kwargs})
        return real_create_issue(hass_argument, domain, issue_id, **kwargs)

    monkeypatch.setattr(expert_controller, "async_create_issue", record)

    entry = await _setup(
        hass,
        _entry(
            hass,
            {
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_AUTO_POLL: True,
                **{
                    CONF_EXPERT_SLOT_ID_TEMPLATE % (slot + 1): ev
                    for slot, ev in enumerate(entityvalues or [EV_A])
                },
            },
        ),
    )
    await hass.async_block_till_done()
    # Setup fires an initial poll of its own. Reset to a known point so the
    # counts a test asserts are the ones it caused, not one more.
    entry.runtime_data.expert.fail_counts.clear()
    entry.runtime_data.expert.fail_notified.clear()
    raised_issues.clear()
    return entry, scheduled, raised_issues


async def test_a_failed_read_does_not_count_as_a_broken_parameter(hass, monkeypatch):
    """The distinction a decomposition is most likely to invert.

    Per-id failure counting exists to spot a typo'd entityvalue: after three
    consecutive failures the user is told to check the ID. But that only
    applies when the batch SUCCEEDED and the portal returned nothing for that
    one id. A failed read - portal down, 403, timeout - returns early and
    counts nothing, because it says nothing about any individual id.

    The obvious clean shape ("read returns {} on failure, then always apply
    the results") inverts exactly this: after three outages every configured
    parameter would raise "Check the configured entityvalue ID". Wrong, and
    alarming on a heating system.
    """

    def always_fails(_ids):
        raise RuntimeError("portal unavailable")

    entry, scheduled, raised_issues = await _auto_poll_entry(
        hass, monkeypatch, always_fails
    )
    poll = scheduled[-1]

    for _ in range(4):
        await poll(None)
        await hass.async_block_till_done()

    assert entry.runtime_data.expert.fail_counts == {}, (
        "an outage was counted against the individual parameters"
    )
    assert raised_issues == [], "an outage produced a 'check your ID' issue"


async def test_a_parameter_the_portal_keeps_omitting_is_reported_once(
    hass, monkeypatch
):
    """The case the counting DOES exist for: the batch works, one id never
    comes back. After three of those the user hears about it - once."""
    from homeassistant.helpers import issue_registry

    entry, scheduled, raised_issues = await _auto_poll_entry(
        hass,
        monkeypatch,
        lambda ids: {},
    )
    poll = scheduled[-1]

    for _ in range(5):
        await poll(None)
        await hass.async_block_till_done()

    assert entry.runtime_data.expert.fail_counts[EV_A] == 5
    assert len(raised_issues) == 1, (
        f"{len(raised_issues)} repair issues for one persistent failure"
    )
    # An id that is not in the result at all was never requested - read_many
    # rejects one it cannot read before sending anything - so the issue may
    # point at the configuration (the unreadable-id wording does exactly that).
    assert raised_issues[0]["translation_key"] == "expert_poll_unreadable_id"
    assert (DOMAIN, raised_issues[0]["issue_id"]) in issue_registry.async_get(
        hass
    ).issues, "the recorded call never reached the real issue registry"


async def test_a_recovered_parameter_clears_its_failure_streak(hass, monkeypatch):
    """Otherwise a parameter that failed once could never notify again, and
    a recurring problem would go quiet after its first streak - and the
    repair issue would outlive the problem it reports."""
    from homeassistant.helpers import issue_registry

    state = {"fail": True}

    def sometimes(_ids):
        return (
            {}
            if state["fail"]
            else {EV_A: expert_writer.ExpertParameterState(21.0, [0.0, 100.0], {})}
        )

    entry, scheduled, raised_issues = await _auto_poll_entry(
        hass, monkeypatch, sometimes
    )
    poll = scheduled[-1]

    for _ in range(3):
        await poll(None)
        await hass.async_block_till_done()
    assert entry.runtime_data.expert.fail_counts[EV_A] == 3
    assert len(raised_issues) == 1, "three consecutive misses raised no issue"

    state["fail"] = False
    await poll(None)
    await hass.async_block_till_done()

    assert EV_A not in entry.runtime_data.expert.fail_counts
    assert EV_A not in entry.runtime_data.expert.fail_notified
    assert (DOMAIN, raised_issues[0]["issue_id"]) not in issue_registry.async_get(
        hass
    ).issues, "the repair issue survived the recovery it reports on"


async def test_a_failed_poll_still_arms_the_next_one(hass, monkeypatch):
    """A transient error must not end the chain - that would silently stop
    the feature until the next restart."""

    def always_fails(_ids):
        raise RuntimeError("portal unavailable")

    _entry_obj, scheduled, _ = await _auto_poll_entry(hass, monkeypatch, always_fails)
    before = len(scheduled)

    await scheduled[-1](None)
    await hass.async_block_till_done()

    assert len(scheduled) == before + 1, "the poll chain died on one failure"


async def test_a_poll_does_not_touch_the_portal_while_a_write_runs(hass, monkeypatch):
    """Reading in parallel with a write can push the pre-write value back
    into the entity right after the write was verified."""
    reads = []

    entry, scheduled, _ = await _auto_poll_entry(
        hass,
        monkeypatch,
        lambda ids: reads.append(ids) or {},
    )
    reads.clear()  # the initial poll already ran during setup
    for entity in entry.runtime_data.expert.entities:
        entity._write_in_progress = True

    await scheduled[-1](None)
    await hass.async_block_till_done()

    assert reads == [], "the auto-poll read while a write was in flight"


async def test_a_poll_skips_when_another_expert_operation_holds_the_lock(
    hass, monkeypatch
):
    """One portal session per account. Skipping also must not release a lock
    this cycle never acquired."""
    reads = []

    entry, scheduled, _ = await _auto_poll_entry(
        hass,
        monkeypatch,
        lambda ids: reads.append(ids) or {},
    )
    reads.clear()  # the initial poll already ran during setup
    lock = entry.runtime_data.expert.lock
    assert lock.acquire(blocking=False), "lock was already held"

    await scheduled[-1](None)
    await hass.async_block_till_done()

    assert reads == [], "the auto-poll opened a second portal session"
    assert not lock.acquire(blocking=False), (
        "the skipped cycle released a lock it never took"
    )
    lock.release()


async def _submit_options(hass, entry, changes, omit=()):
    """Submit the configure form with `changes` applied to what is stored.

    `omit` drops keys entirely, which is what a browser does for an empty
    optional field - the distinction the slot handling depends on, and one a
    payload built from defaults would never produce.
    """
    result = await _open_options(hass, entry, "configure")
    schema_keys = {str(marker) for marker in result["data_schema"].schema}
    payload = {key: value for key, value in entry.options.items() if key in schema_keys}
    payload.update(changes)
    for key in omit:
        payload.pop(key, None)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], payload
    )
    await hass.async_block_till_done()
    return result


async def test_clearing_a_slot_id_actually_clears_it(hass):
    """The form's slot fields are Optional with no default, so a field the
    browser leaves empty is absent from user_input entirely.

    The step materialises them anyway, and that is load-bearing: the options
    are stored as {**current, **user_input}, so without the materialisation
    the STORED id survives the merge and clearing a slot silently does
    nothing. That was a real bug once (1.8.3); this is what keeps it fixed.
    """
    entry = await _setup(
        hass,
        _entry(
            hass,
            {
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
                CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Slot one",
            },
        ),
    )

    await _submit_options(hass, entry, {}, omit=[CONF_EXPERT_SLOT_ID_TEMPLATE % 1])

    assert entry.options[CONF_EXPERT_SLOT_ID_TEMPLATE % 1] == "", (
        "the cleared slot kept its stored id"
    )


async def test_a_cleared_slot_name_is_actually_cleared(hass):
    """The same trap as the slot id, one field over.

    An emptied optional field is absent from the form data, and
    _save_configure merges over the stored options - so without writing the
    name back explicitly the old one came straight back, and a name could be
    replaced but never removed.
    """
    entry = await _setup(
        hass,
        _entry(
            hass,
            {
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
                CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Slot one",
            },
        ),
    )

    await _submit_options(hass, entry, {}, omit=[CONF_EXPERT_SLOT_NAME_TEMPLATE % 1])

    assert entry.options[CONF_EXPERT_SLOT_NAME_TEMPLATE % 1] == "", (
        "the cleared slot name came back from the stored options"
    )
    assert entry.options[CONF_EXPERT_SLOT_ID_TEMPLATE % 1] == EV_A, (
        "clearing the name took the id with it"
    )


async def test_a_slot_name_is_stored_stripped(hass):
    """Whitespace around a name would travel into the entity name."""
    entry = await _setup(
        hass,
        _entry(
            hass,
            {
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
            },
        ),
    )

    await _submit_options(
        hass, entry, {CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "  Living room  "}
    )

    assert entry.options[CONF_EXPERT_SLOT_NAME_TEMPLATE % 1] == "Living room"


async def test_a_slot_id_is_stored_stripped(hass):
    """Whitespace from a copy/paste would otherwise travel into the request
    URL."""
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))

    await _submit_options(
        hass, entry, {CONF_EXPERT_SLOT_ID_TEMPLATE % 1: f"  {EV_A}  "}
    )

    assert entry.options[CONF_EXPERT_SLOT_ID_TEMPLATE % 1] == EV_A


async def test_an_invalid_slot_id_is_rejected_and_nothing_is_stored(hass):
    """A short or non-hex entry is a typo, not an id, and would only produce
    a failing portal request later."""
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    before = dict(entry.options)

    result = await _submit_options(
        hass, entry, {CONF_EXPERT_SLOT_ID_TEMPLATE % 1: "abc"}
    )

    assert result["type"] is FlowResultType.FORM
    assert (
        result["errors"].get(CONF_EXPERT_SLOT_ID_TEMPLATE % 1) == "invalid_entityvalue"
    )
    assert entry.options == before, "a rejected form still changed the options"


async def test_a_rejected_form_shows_what_the_user_typed(hass):
    """The redisplay prefills from user_input, not from the stored options -
    otherwise a validation error would throw away everything else the user
    had just entered."""
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))

    result = await _submit_options(
        hass,
        entry,
        {
            CONF_EXPERT_SLOT_ID_TEMPLATE % 1: "abc",
            CONF_EXPERT_SLOT_NAME_TEMPLATE % 2: "typed but not saved",
        },
    )

    suggestions = {
        str(marker): (marker.description or {}).get("suggested_value")
        for marker in result["data_schema"].schema
    }
    assert suggestions[CONF_EXPERT_SLOT_NAME_TEMPLATE % 2] == "typed but not saved"


async def test_saving_keeps_options_that_are_not_form_fields(hass):
    """The options dict is REPLACED with what the step returns, so anything
    not on the form has to be merged back in. The cached module list is the
    one that matters: losing it costs another portal login on the next
    discovery."""
    from custom_components.wemportal.const import CONF_EXPERT_MODULE_LIST

    cached = [{"index": "6", "label": "Heat pump"}]
    entry = await _setup(
        hass, _entry(hass, {CONF_EXPERT_WRITE: True, CONF_EXPERT_MODULE_LIST: cached})
    )

    await _submit_options(hass, entry, {CONF_SCAN_INTERVAL_API: 600})

    assert entry.options[CONF_EXPERT_MODULE_LIST] == cached
    assert entry.options[CONF_SCAN_INTERVAL_API] == 600


async def test_saving_without_a_change_is_not_a_write(hass):
    """Every save reloads the integration - a full login and scrape. A form
    submitted unchanged must not pay that."""
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    # The first save is NOT a no-op: it materialises the ten slot keys that
    # the form is the sole producer of. From then on an unchanged form must
    # change nothing.
    await _submit_options(hass, entry, {})

    result = await _submit_options(hass, entry, {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_changes"


async def test_update_timeout_is_counted_and_reported(hass, monkeypatch):
    """asyncio.timeout CANCELS the task; CancelledError is a BaseException,
    so the coordinator's `except Exception` never saw it and num_failed was
    never incremented - the extra backoff stayed off for the one failure mode
    where waiting longer matters most.
    """
    import asyncio as _asyncio

    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal import coordinator as coord_mod

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    before = coordinator.num_failed

    monkeypatch.setattr(coord_mod, "DEFAULT_TIMEOUT", 0.05)

    async def never_returns(*_args, **_kwargs):
        await _asyncio.sleep(5)

    monkeypatch.setattr(hass, "async_add_executor_job", never_returns)

    with pytest.raises(UpdateFailed, match="[Tt]imed out"):
        await coordinator._async_update_data()

    assert coordinator.num_failed == before + 1, (
        "a timeout did not count as a failure, so the backoff never engages"
    )


async def test_a_busy_api_does_not_trigger_the_recovery_swap(hass, monkeypatch):
    """A lock timeout means "still busy", not "session corrupted".

    Treated as the latter, the coordinator re-instantiated the api - closing
    the HTTP sessions the still-running thread was using and handing the next
    poll a FRESH lock, so the two ran concurrently against a portal that was
    already too slow.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal.exceptions import ApiBusyError

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    api_before = coordinator.api
    coordinator.num_failed = 1  # one more failure would trip the swap

    def busy(self, *_args, **_kwargs):
        raise ApiBusyError("Timed out waiting for the connection to become free")

    monkeypatch.setattr(WemPortalApi, "fetch_data", busy)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.api is api_before, "a busy api was replaced as if broken"


async def test_a_busy_api_counts_neither_for_nor_against_the_credentials(
    hass, monkeypatch
):
    """A cycle that never took the lock asked the portal nothing.

    Both counters answer questions this cycle has no evidence about.
    num_failed drives the backoff and, since the tolerance, whether the
    entities stay visible - and nothing is broken, so raising it would make a
    busy moment look like an outage. num_auth_failed is reset by any cycle
    that reached the portal WITHOUT an auth failure, because that is evidence
    the credentials are fine; a cycle that sent no request is not.

    Written down as a test because the code says only why this clause comes
    before the WemPortalError one, and an audit read the two silences as
    oversights.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal.exceptions import ApiBusyError

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    failures_before = coordinator.num_failed
    coordinator.num_auth_failed = 2  # a streak that must neither grow nor end

    def busy(self, *_args, **_kwargs):
        raise ApiBusyError("Timed out waiting for the connection to become free")

    monkeypatch.setattr(WemPortalApi, "fetch_data", busy)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.num_failed == failures_before, (
        "a cycle that sent no request was counted as a failed one, which "
        "backs the portal off and can take the entities off the dashboard"
    )
    assert coordinator.num_auth_failed == 2, (
        "a cycle that never asked the portal was taken as proof the "
        "credentials are fine"
    )


async def test_a_cycle_that_ran_out_of_time_keeps_its_connection(hass, monkeypatch):
    """A worker that stopped on its own deadline says "too slow", not
    "session broken".

    Handled by the generic WemPortalError branch it would trip the transport
    reset on the second failure - throwing away a warm session, and making
    the next cycle open a fresh login at the very portal that was already
    answering too slowly to finish in time.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal.exceptions import PollDeadlineExceeded

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    coordinator.num_failed = 1  # one more failure would trip the reset

    resets = []
    monkeypatch.setattr(
        WemPortalApi, "reset_transport", lambda self: resets.append(True)
    )

    def out_of_time(self, *_args, **_kwargs):
        raise PollDeadlineExceeded("passed its 330s budget and stopped")

    monkeypatch.setattr(WemPortalApi, "fetch_data", out_of_time)

    with pytest.raises(UpdateFailed, match="budget"):
        await coordinator._async_update_data()

    assert resets == [], "a slow cycle had its connection reset as if broken"


async def test_a_cycle_that_ran_out_of_time_still_counts_as_a_failure(
    hass, monkeypatch
):
    """Unlike a busy api, this cycle really did fail to deliver readings.

    The backoff that gives a struggling portal more room is exactly what is
    wanted here, so the failure has to be counted - just not as an AUTH
    failure, which would march towards a reauth prompt for credentials that
    were never in question.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal.exceptions import PollDeadlineExceeded

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    failures_before = coordinator.num_failed
    coordinator.num_auth_failed = 2

    def out_of_time(self, *_args, **_kwargs):
        raise PollDeadlineExceeded("passed its 330s budget and stopped")

    monkeypatch.setattr(WemPortalApi, "fetch_data", out_of_time)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.num_failed == failures_before + 1, (
        "the cycle delivered nothing but did not count as a failure, so the "
        "backoff never engages"
    )
    assert coordinator.num_auth_failed == 0, (
        "a slow portal moved the integration towards a reauth prompt"
    )


async def test_a_good_cycle_between_two_bad_ones_keeps_the_entities(hass, monkeypatch):
    """The tolerated failure is a CONSECUTIVE one, so a success must clear it.

    Every branch that counts a failure was tested; the success that resets
    the count was not. Left standing, the counter only climbs, and the first
    hiccup after weeks of clean polling arrives as the second failure - so
    the tolerance expires on exactly the outage it was written for, and the
    dashboard empties anyway.
    """
    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator

    cycles = iter([ForbiddenError("blocked"), FAKE_DATA, ForbiddenError("blocked")])

    def next_cycle(self, *_args, **_kwargs):
        outcome = next(cycles)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(WemPortalApi, "fetch_data", next_cycle)

    for _ in range(3):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.num_failed == 1, (
        "the successful cycle in between did not clear the failure count"
    )
    outside = next(
        state
        for state in hass.states.async_all("sensor")
        if "outside_temperature" in state.entity_id
    )
    assert outside.state != STATE_UNAVAILABLE, (
        "one failed cycle emptied the dashboard, though it is the one case "
        "the tolerance exists for"
    )


async def test_expert_service_refuses_an_unconfigured_parameter(hass):
    """Without this the service is a generic write primitive for ANY
    parameter of the installation, including ones never surfaced in Home
    Assistant - the opt-in option and a hard-to-guess id are obscurity,
    not access control."""
    await _setup(hass, _entry(hass, _expert_options()))

    with pytest.raises(HomeAssistantError, match="not one of"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_B, "value": 30},
            blocking=True,
        )


async def test_expert_service_refuses_a_non_admin(hass, hass_read_only_user):
    """It writes real settings on a heating system, so it is an admin
    service. A plain registration lets any authenticated user call it."""
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    await _setup(hass, _entry(hass, _expert_options()))

    read_only = Context(user_id=hass_read_only_user.id)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_A, "value": 30},
            blocking=True,
            context=read_only,
        )


async def test_auth_failures_survive_setup_retries(hass, monkeypatch):
    """A failed first refresh makes Home Assistant retry the whole setup, and
    every retry builds a fresh coordinator. With the counter living on the
    coordinator it restarted at zero each time, so the reauth threshold was
    unreachable during startup - a password changed while Home Assistant was
    off left the entry retrying forever instead of asking for new credentials.
    """
    from homeassistant.exceptions import ConfigEntryAuthFailed

    from custom_components.wemportal import coordinator as coord_mod
    from custom_components.wemportal.const import AUTH_ERROR_ESCALATION_THRESHOLD

    entry = _entry(hass)

    def bad_credentials(self, *_args, **_kwargs):
        raise AuthError("Login failed: Invalid username or password.")

    monkeypatch.setattr(WemPortalApi, "fetch_data", bad_credentials)

    raised = None
    for _ in range(AUTH_ERROR_ESCALATION_THRESHOLD):
        # A fresh coordinator per attempt, exactly like a setup retry.
        coordinator = coord_mod.WemPortalDataUpdateCoordinator(
            hass, WemPortalApi(USER, "secret"), entry, timedelta(seconds=300)
        )
        try:
            await coordinator._async_update_data()
        except ConfigEntryAuthFailed as exc:
            raised = exc
            break
        except Exception:  # noqa: BLE001, S112
            # Every failure except the one under test is uninteresting here:
            # what is being counted is how many attempts it takes to escalate.
            continue

    assert raised is not None, (
        "the reauth threshold was never reached across setup retries"
    )
    coord_mod.forget_auth_failures(entry)


async def test_a_successful_cycle_clears_the_auth_failure_count(hass):
    """A transient login hiccup must not accumulate towards reauth forever."""
    from custom_components.wemportal.models import account_state

    entry = await _setup(hass, _entry(hass))
    account_state(USER).auth_failures = 2

    await entry.runtime_data.coordinator._async_update_data()

    assert account_state(USER).auth_failures == 0


async def test_entities_of_an_offline_device_go_unavailable(hass, monkeypatch):
    """One device offline among several must not keep serving its last
    readings as current - while the healthy device stays untouched."""
    two_devices = {
        "1234": {
            "1234-ConnectionStatus": Reading(
                value="online",
                unit=None,
                platform="sensor",
                friendly_name="Connection Status",
                parameter_id="ConnectionStatus",
            ),
            "Outside temperature": _sensor(),
        },
        "5678": {
            "5678-ConnectionStatus": Reading(
                value="offline",
                unit=None,
                platform="sensor",
                friendly_name="Connection Status",
                parameter_id="ConnectionStatus",
            ),
            "Outside temperature": _sensor(),
        },
    }
    monkeypatch.setattr(
        WemPortalApi, "fetch_data", lambda self, *_args, **_kwargs: two_devices
    )

    await _setup(hass, _entry(hass))

    def _state(entity_id_part, device):
        return next(
            s
            for s in hass.states.async_all("sensor")
            if entity_id_part in s.entity_id and device in s.entity_id
        )

    assert _state("outside_temperature", "1234").state == "12.5"
    assert _state("outside_temperature", "5678").state == "unavailable"
    # The diagnostic sensor must survive - it is what explains the rest.
    assert _state("connection_status", "5678").state == "offline"


async def test_a_differently_capitalised_account_is_still_a_duplicate(hass):
    """Portal usernames are email addresses, so casing is not meaningful -
    but the check compared them verbatim, so the same account added with a
    different capitalisation became a second entry polling the same
    installation twice."""
    _entry(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: "  USER@Example.ORG ",
            CONF_PASSWORD: "secret",
            CONF_LANGUAGE: "en",
            CONF_MODE: "api",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_setup_backfills_the_account_unique_id(hass):
    """Entries created before unique_ids were used have none, so the
    duplicate check cannot see them at all."""
    entry = _entry(hass)
    assert entry.unique_id is None

    await _setup(hass, entry)

    assert entry.unique_id == USER


async def test_a_duplicate_account_is_not_given_a_second_unique_id(hass):
    """Two entries for one account is exactly what the unique_id prevents
    from now on - but an installation that already has both must still load
    rather than fail on a duplicate id."""
    first = _entry(hass)
    second = _entry(hass)

    # Setting up the component sets up EVERY entry of the domain, so one
    # call covers both - and which of them is reached first is not defined.
    await _setup(hass, first)

    ids = sorted(
        (e.unique_id for e in (first, second)), key=lambda unique_id: unique_id or ""
    )
    assert ids == [None, USER], "the account id must be claimed exactly once"
    assert first.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED, (
        "a pre-existing duplicate must still load, not fail on a clashing id"
    )


async def test_an_unload_stops_an_expert_write_before_it_reaches_the_portal(hass):
    """A write in flight when the entry goes away must not reach the portal.

    Nothing here can cancel it: the portal call runs in an executor thread,
    and a thread cannot be killed from outside. What stops it is the abort
    gate the client is handed and checks after the login and again directly
    before the writing request. This is that gate, seen from the outside -
    the entity was removed, so the next check must refuse.

    Used to be phrased as "the background task is cancelled". The write is
    awaited by its caller now, so there is no task to cancel and never was
    the thing that stopped it.
    """
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    entry = await _setup(hass, _entry(hass, _expert_options()))
    entities = entry.runtime_data.expert.entities
    assert entities, "no expert entity was created"
    entity = entities[0]

    entity._raise_if_removed()  # not removed yet: the gate must let this pass

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ExpertOperationAborted):
        entity._raise_if_removed()


async def test_a_non_auth_failure_breaks_the_auth_streak(hass, monkeypatch):
    """The threshold is documented as CONSECUTIVE auth failures.

    The counter only ever went up, though: a timeout, a maintenance window or
    a 403 in between left it standing, so auth failures spread over hours - a
    portal that hands out the odd login page - still added up to a reauth
    prompt for credentials that were correct the whole time.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal.exceptions import WemPortalError

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator

    failures = []

    def flaky(self, *_args, **_kwargs):
        raise failures.pop(0)

    monkeypatch.setattr(WemPortalApi, "fetch_data", flaky)

    # Two auth failures, then something entirely unrelated.
    failures.extend(
        [
            AuthError("login page"),
            AuthError("login page"),
            WemPortalError("portal unreachable"),
        ]
    )
    # UpdateFailed, not a bare Exception: catching anything would pass just as
    # happily on a TypeError from a mistyped test double.
    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.num_auth_failed == 0, "the streak was not broken"
    from custom_components.wemportal.models import account_state

    assert account_state(USER).auth_failures == 0


async def test_reauth_reloads_the_entry_exactly_once(hass, monkeypatch):
    """Updating the entry already fires the update listener, which reloads.

    Reloading from the flow as well is the double reload (and race) Home
    Assistant deprecated in 2026.6 and rejects from 2026.12.
    """
    entry = await _setup(hass, _entry(hass))
    reloads = []
    original = hass.config_entries.async_reload

    async def counting_reload(entry_id):
        reloads.append(entry_id)
        return await original(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", counting_reload)
    monkeypatch.setattr(WemPortalApi, "api_login", lambda self: None)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USER, CONF_PASSWORD: "new-secret"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    assert reloads == [entry.entry_id], f"reloaded {len(reloads)} times"


async def test_adding_a_configured_account_says_so(hass):
    """AbortFlow is how Home Assistant ENDS a flow, not an error in it.

    Caught by the step's catch-all, the abort raised by
    _abort_if_unique_id_configured turned into a bare "unknown" - the user
    was told something went wrong instead of that the account is already set
    up.
    """
    await _setup(hass, _entry(hass))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USER.upper(), CONF_PASSWORD: "secret", CONF_MODE: "api"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_with_the_same_password_still_reloads(hass, monkeypatch):
    """The case reauth exists for.

    Relying on the update listener looked equivalent to reloading and was
    not: Home Assistant only fires it when the entry actually CHANGED, so
    re-entering the same password reloaded nothing while the flow still
    reported success. Someone whose portal had been rejecting a correct
    password was left with a dead entry and a green confirmation.
    """
    entry = await _setup(hass, _entry(hass))
    reloads = []
    original = hass.config_entries.async_reload

    async def counting_reload(entry_id):
        reloads.append(entry_id)
        return await original(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", counting_reload)
    monkeypatch.setattr(WemPortalApi, "api_login", lambda self: None)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        # Deliberately unchanged - this is the whole point.
        {CONF_USERNAME: USER, CONF_PASSWORD: entry.data[CONF_PASSWORD]},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert reloads == [entry.entry_id], f"reloaded {len(reloads)} times"


async def test_the_entry_carries_no_update_listener(hass):
    """Home Assistant's own deprecation check is `if entry.update_listeners`.

    Keeping one while the flows reload is what breaks in 2026.12, so this
    pins the decision rather than the symptom - a listener added back later
    would reintroduce the double reload silently.
    """
    entry = await _setup(hass, _entry(hass))

    assert not entry.update_listeners


async def test_saving_options_reloads_the_entry(hass, monkeypatch):
    """With no listener doing it implicitly, the options flow has to.

    Every option is read during setup - scan intervals, mode, expert access -
    so without a reload the form would appear to save and change nothing.
    """
    entry = await _setup(hass, _entry(hass))
    reloads = []
    original = hass.config_entries.async_reload

    async def counting_reload(entry_id):
        reloads.append(entry_id)
        return await original(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", counting_reload)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "configure"}
    )
    # Submit what the form itself declares, so this does not have to track
    # every field the options step happens to offer.
    schema_keys = {str(marker) for marker in result["data_schema"].schema}
    payload = {key: value for key, value in entry.options.items() if key in schema_keys}
    # The API interval, because this entry runs in `api` mode - so the value
    # is observable in the coordinator afterwards.
    payload[CONF_SCAN_INTERVAL_API] = 600
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], payload
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL_API] == 600
    assert reloads == [entry.entry_id], f"reloaded {len(reloads)} times"
    # The reload has to see the NEW options. Asserting only that a reload
    # happened would pass just as well on one scheduled early enough to read
    # the old ones - which is the actual risk with a scheduled task.
    coordinator = entry.runtime_data.coordinator
    assert coordinator.update_interval == timedelta(seconds=600)


async def test_recovery_resets_the_connection_and_keeps_everything_else(
    hass, monkeypatch
):
    """After repeated errors the integration recovers by dropping its HTTP
    state - not by rebuilding the api object.

    Rebuilding meant copying nine pieces of state across by hand, so every new
    field was a new chance to forget one, and two were forgotten in practice.
    It also reset things nobody intended: the statistics and circuit-times
    timestamps are portal RATE LIMITS, and a fresh object handed out a fresh
    lock while a poll thread still held the old one.

    So this pins both halves: the transport is gone, everything else is not.
    """

    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal.exceptions import WemPortalError

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    api = coordinator.api

    # Aware, like the value the api itself stores: a naive one would raise
    # TypeError the moment _scrape_is_due subtracts it.
    stamp = dt_util.as_local(datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC))
    api.spider_wait_interval, api.spider_retry_count = 3, 3
    api.last_scraping_update = stamp
    api.last_statistics_fetch = 111.0
    api._last_circuit_times_fetch = {"1234-x": 222.0}
    api._blocked_until, api._expert_blocked_until = 333.0, 444.0
    api.expert_cookies = {"cookies": {"ASP.NET_SessionId": "abc"}}
    api.device_types = {"1234": 2}
    api.scraper_device_id = "1234"
    api.modules = {"1234": {}}
    api.valid_login = True
    lock_before = api._api_lock

    # Rebinding a field is not the same as releasing the socket behind it.
    closed_transports = []

    class _ClosingSession:
        def close(self):
            closed_transports.append("session")

    class _ClosingScraper:
        def close(self):
            closed_transports.append("scraper")

    api.session = _ClosingSession()
    api._scraper = _ClosingScraper()

    monkeypatch.setattr(
        WemPortalApi,
        "fetch_data",
        lambda self, *_args, **_kwargs: (_ for _ in ()).throw(
            WemPortalError("portal broken")
        ),
    )

    # The recovery runs from the second consecutive failure onwards.
    for _ in range(2):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    # The transport is what recovery is for.
    assert api.valid_login is False, "the login was not invalidated"
    assert api.session is None
    assert api._scraper is None
    # An attribute check alone passes for a recovery that only drops the
    # reference and leaves both connections open to the collector.
    assert closed_transports == ["session", "scraper"], closed_transports

    # Everything else has to survive, or the recovery becomes the problem.
    assert (api.spider_wait_interval, api.spider_retry_count) == (3, 3)
    assert api.last_scraping_update == stamp
    assert api.last_statistics_fetch == 111.0, "an hourly rate limit was reset"
    assert api._last_circuit_times_fetch == {"1234-x": 222.0}, "rate limit reset"
    assert (api._blocked_until, api._expert_blocked_until) == (333.0, 444.0)
    assert api.expert_cookies["cookies"]["ASP.NET_SessionId"] == "abc"
    assert api.device_types == {"1234": 2}
    assert api.scraper_device_id == "1234"
    assert api.modules == {"1234": {}}
    assert api._api_lock is lock_before, "a running poll would hold the old lock"

    # And the object everyone else refers to is still the one in use.
    assert coordinator.api is api
    assert entry.runtime_data.api is api


# What this integration STILL uses of the three that Home Assistant 2026.8
# announced. DeviceEntry.config_entries and async_get_device() are gone from
# it now - the registry's own index answers the first on every supported
# version, and the second goes through coordinator.device_by_identifier,
# which uses the entry-aware lookup where there is one. Listing an API nobody
# calls any more only invites a false alarm on somebody else's warning.
DEPRECATED_DEVICE_REGISTRY_APIS = ("via_device",)


def _deprecation_reports(records):
    """The device-registry deprecations among captured log records.

    Home Assistant announces these through helpers.frame.report_usage, and
    that writes to a LOGGER - `warnings` is not imported in that module at
    all. This test used to watch warnings.catch_warnings(), which meant it
    could not fail: the channel it listened on never carries the message.
    """
    import logging

    hits = []
    for record in records:
        if record.levelno < logging.WARNING:
            continue
        message = record.getMessage()
        # Both signals, not just the API name: "config_entries" appears in
        # every log line that quotes a path through config_entries.py, and
        # asyncio's slow-task warning does exactly that during setup. Asking
        # for the name alone made this fire on it.
        if "deprecat" not in message.lower():
            continue
        if any(name in message for name in DEPRECATED_DEVICE_REGISTRY_APIS):
            hits.append(message)
    return hits


async def test_the_device_registry_apis_are_not_deprecated_yet(hass, caplog):
    """A tripwire for the one that is left.

    Three device-registry APIs were announced as deprecated in Home Assistant
    2026.8, for removal in 2027.8. Two of them are no longer used here:
    DeviceEntry.config_entries gave way to the registry's own index, which
    exists on every supported version, and async_get_device() to the
    entry-aware lookup where the installed Home Assistant has one - see
    coordinator.device_by_identifier.

    DeviceInfo.via_device is still in use. Its replacement, via_device_id,
    wants the hub's registry id, which build_device_info does not have and
    cannot look up: it is called from an entity property, synchronously, on
    every state write. Changing that is a design question rather than a
    substitution, so it waits - and measured against the installed 2026.8,
    Home Assistant does not report our use of it yet.

    So this fails on the day it does, and points at the call. Guessing a
    replacement is how the last two wrong claims in this repository were
    made; feature detection over a version comparison, since 2024.12 stays
    supported.

    Two things had to be true for it to be able to fail at all, and neither
    was. It listened on `warnings`, while report_usage writes to a logger.
    And report_usage remembers what it has already said in a module-level
    set, so the first test in the session to touch the same API consumes the
    only report there will be - which is why that set is cleared here.
    """
    import logging

    from homeassistant.helpers import frame

    frame._REPORTED_INTEGRATIONS.clear()

    with caplog.at_level(logging.WARNING):
        entry = await _setup(hass, _entry(hass))
        await hass.async_block_till_done()

    hits = _deprecation_reports(caplog.records)

    assert not hits, (
        "Home Assistant now deprecates a device-registry API this integration "
        f"uses: {hits}. Write the feature-detected adapters now - the "
        "replacement is finally readable, and there is one release cycle "
        "before removal."
    )
    assert entry.state is ConfigEntryState.LOADED


def test_the_tripwire_watches_the_channel_the_report_arrives_on():
    """Guards the guard, and this one had actually failed silently.

    A tripwire that watches the wrong channel passes forever and reads as
    "not deprecated yet". The point is not that the collector can match a
    string - it is that it matches a LOG record, which is what
    helpers.frame.report_usage produces.
    """
    import logging

    record = logging.LogRecord(
        "homeassistant.helpers.frame",
        logging.WARNING,
        __file__,
        0,
        "Detected that custom integration 'wemportal' accesses via_device, "
        "which is deprecated and will stop working in HA Core 2027.8",
        None,
        None,
    )

    assert _deprecation_reports([record])

    def _record(message):
        return logging.LogRecord("x", logging.WARNING, __file__, 0, message, None, None)

    assert not _deprecation_reports([_record("something else")])

    # Naming a watched API is not enough - the line has to announce a
    # deprecation as well. The real case that produced this: asyncio's
    # slow-task warning quotes a code path, and back when config_entries was
    # on the list, "homeassistant/config_entries.py:951" in that warning fired
    # the tripwire.
    #
    # Driven off the tuple rather than off that one message, because the
    # message stopped mattering the moment config_entries left the list -
    # and this assertion silently stopped testing anything. A mutation
    # removing the deprecation filter went unnoticed until it was written
    # this way.
    for api_name in DEPRECATED_DEVICE_REGISTRY_APIS:
        assert not _deprecation_reports(
            [_record(f"Executing <Task ... {api_name} ...> took 0.2 seconds")]
        ), f"a line merely naming {api_name} was reported as a deprecation"


async def test_a_setup_that_fails_late_leaves_no_service_behind(hass, monkeypatch):
    """The service is registered one line before the auto-poll is set up.

    A failure between the two never reaches async_unload_entry - Home
    Assistant only unloads entries that finished setting up - so the domain
    service stayed registered with nothing loaded to serve it. The next call
    to it then failed somewhere deeper instead of simply not existing.
    """
    from custom_components.wemportal.coordinator import (
        WemPortalDataUpdateCoordinator,
    )
    from custom_components.wemportal.expert_controller import ExpertController

    monkeypatch.setattr(
        ExpertController,
        "setup_auto_poll",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("poll setup broke")
        ),
    )

    coordinators = []
    original = WemPortalDataUpdateCoordinator.__init__

    def _remember(self, *args, **kwargs):
        original(self, *args, **kwargs)
        coordinators.append(self)

    monkeypatch.setattr(WemPortalDataUpdateCoordinator, "__init__", _remember)

    entry = _entry(hass, {CONF_EXPERT_WRITE: True})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert coordinators, "no coordinator was built, so this proves nothing"
    coordinator = coordinators[-1]

    from custom_components.wemportal.holiday import SERVICE_SET_HOLIDAY

    assert not hass.services.has_service(DOMAIN, SERVICE_SET_EXPERT_PARAMETER), (
        "a failed setup left its expert service registered"
    )
    assert not hass.services.has_service(DOMAIN, SERVICE_SET_HOLIDAY), (
        "a failed setup left its holiday service registered"
    )

    # And nothing may keep polling for it. Home Assistant does not unload an
    # entry whose setup failed, so by this point the platforms are forwarded,
    # their entities have subscribed, and the coordinator's refresh timer is
    # armed - it would go on asking the portal on its interval for an entry
    # the user sees as failed, against an account blocked for 12 hours past
    # 10,000 requests.
    #
    # This assertion is load-bearing on the MINIMUM supported version only:
    # HA 2024.12 leaves the timer running (its harness reported it as a
    # lingering timer, which is how this was found), while 2026.7.2 already
    # tears it down itself. That is also why there is no mutation for it -
    # the mutation harness runs one version, and this one would survive
    # there while failing the version that needs it. The CI matrix is what
    # covers it.
    assert coordinator._unsub_refresh is None, (
        "the coordinator of a failed entry is still scheduled to poll"
    )


async def test_the_recovery_runs_off_the_event_loop(hass, monkeypatch):
    """The reset takes the shared api lock, so it must not run on the loop.

    Waiting for a threading lock on the event loop stalls everything Home
    Assistant does for as long as the write it waits for takes - which is
    precisely the situation the lock exists for. Asserting on the THREAD
    rather than on the call: patching async_add_executor_job away would make
    a direct call look identical.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal.exceptions import WemPortalError

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator

    loop_thread = threading.get_ident()
    seen = []
    monkeypatch.setattr(
        WemPortalApi,
        "reset_transport",
        lambda self: seen.append(threading.get_ident()),
    )
    monkeypatch.setattr(
        WemPortalApi,
        "fetch_data",
        lambda self, *_args, **_kwargs: (_ for _ in ()).throw(
            WemPortalError("portal broken")
        ),
    )

    for _ in range(2):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert seen, "the recovery never ran"
    assert loop_thread not in seen, "the recovery ran on the event loop"


async def test_a_failed_platform_setup_does_not_leak_the_store(hass, monkeypatch):
    """Home Assistant only calls async_unload_entry for an entry that
    finished setting up.

    Everything after the store is published therefore has to clean up after
    itself; without that the store and its two HTTP sessions were left
    behind - once more on every setup retry, which is exactly when platform
    setup tends to fail.
    """
    entry = _entry(hass)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("platform setup exploded")

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", boom)

    # Home Assistant catches the failure and marks the entry as errored
    # rather than propagating, which is exactly why the cleanup has to happen
    # inside async_setup_entry.
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not hasattr(entry, "runtime_data")


async def test_a_write_is_abandoned_when_its_entry_is_reloaded(hass, monkeypatch):
    """Checking that the entry id is still present is not enough.

    A reload puts a NEW store under the SAME id while the running write still
    holds the old entry and api, and the store is removed only after the
    platforms are down - so for the whole teardown the id is still there too.
    """
    entry = await _setup(
        hass,
        _entry(
            hass,
            options={
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
                CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Slot",
            },
        ),
    )

    def reload_midway(self, *_args, **_kwargs):
        # The reload happens WHILE the write runs, which is the whole point:
        # the store the handler captured is replaced by an equivalent one
        # under the same id. Swapping it before the call would simply hand
        # the handler the new store and prove nothing.
        import dataclasses

        entry.runtime_data = dataclasses.replace(entry.runtime_data)
        self._abort_check()
        raise AssertionError("the write continued after the reload")

    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient, "write_parameter", reload_midway
    )

    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_A, "value": 21.0},
            blocking=True,
        )

    assert "reloaded" in str(excinfo.value)


async def test_a_write_is_abandoned_while_the_entry_is_unloading(hass, monkeypatch):
    """The store survives until the platforms are down, so its presence says
    nothing during a teardown. The flag is set before that starts."""
    entry = await _setup(
        hass,
        _entry(
            hass,
            options={
                CONF_EXPERT_WRITE: True,
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
                CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Slot",
            },
        ),
    )
    # Exactly what async_unload_entry sets before unloading the platforms.
    entry.runtime_data.unloading = True

    def never(self, *_args, **_kwargs):
        raise AssertionError("the write continued during the unload")

    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "write_parameter", never)

    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_A, "value": 21.0},
            blocking=True,
        )

    assert "unloaded" in str(excinfo.value)


async def test_unloading_is_flagged_before_the_platforms_come_down(hass, monkeypatch):
    """Ordering is the whole point, so the flag is checked at the moment the
    platforms start unloading.

    Unloading the platforms is the slow part, and the store is only removed
    afterwards - so anything already talking to the portal in a worker thread
    has to learn about the teardown at its next gate, not at the end of it.
    A test that sets the flag itself would pass with the production code
    removed; this one drives the real unload.
    """
    entry = await _setup(hass, _entry(hass))
    # Held by reference: Home Assistant drops runtime_data once the unload
    # finishes, but this object is what a running write is looking at.
    data = entry.runtime_data
    seen = {}
    original = hass.config_entries.async_unload_platforms

    async def check_when_platforms_unload(entry_arg, platforms):
        seen["flagged"] = data.unloading
        return await original(entry_arg, platforms)

    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", check_when_platforms_unload
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert seen["flagged"] is True, "the teardown was only announced afterwards"


async def test_a_refused_unload_leaves_the_entry_writeable(hass, monkeypatch):
    """A platform may refuse to unload, and Home Assistant then leaves the
    entry loaded and polling. It has to stay writeable too.

    The teardown is announced before the platforms come down, which is right -
    but the announcement was never taken back. A refused unload therefore left
    an entry that kept working in every respect except that every write, from
    an entity or from the service, answered "the integration is being
    unloaded" until Home Assistant restarted.
    """
    entry = await _setup(hass, _entry(hass))
    data = entry.runtime_data

    async def refuse(*_args, **_kwargs):
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", refuse)

    assert await hass.config_entries.async_unload(entry.entry_id) is False
    await hass.async_block_till_done()

    assert data.unloading is False, (
        "the entry stayed flagged as unloading and can no longer be written to"
    )
    assert data.why_not_current(entry) is None

    # The refusal left the entry running with its refresh timer armed - which
    # is exactly why the flag has to be taken back. Home Assistant now holds
    # the entry in FAILED_UNLOAD and refuses to unload it again, so the only
    # way to leave a clean event loop behind is to stop the coordinator
    # directly.
    await data.coordinator.async_shutdown()
    await hass.async_block_till_done()


async def test_a_setup_that_fails_after_forwarding_takes_the_platforms_back_down(
    hass, monkeypatch
):
    """Home Assistant does not unload an entry whose setup failed.

    Everything forwarded before the failure therefore stayed registered:
    entities belonging to an entry the user sees as failed, unavailable and
    un-reloadable, and one more set of them on every setup retry. The failure
    is injected at the same place as the service test above - the only window
    in which the platforms are up and the setup can still fail.
    """
    from custom_components.wemportal.expert_controller import ExpertController

    monkeypatch.setattr(
        ExpertController,
        "setup_auto_poll",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("poll setup broke")
        ),
    )

    unloaded = []
    original = hass.config_entries.async_unload_platforms

    async def record(entry_arg, platforms):
        unloaded.append(list(platforms))
        return await original(entry_arg, platforms)

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", record)

    entry = _entry(hass, {CONF_EXPERT_WRITE: True})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert unloaded, "the platforms were left registered on a failed setup"
    assert set(unloaded[0]) == set(PLATFORMS)


async def test_one_bad_batch_is_not_blamed_on_every_configured_id(hass, monkeypatch):
    """Two ids requested, both failed: that is one bad batch, not two bad ids.

    Counting it per id told the user to go and fix settings that were fine -
    a persistent notification per parameter, on a portal hiccup.
    """
    entry, scheduled, raised_issues = await _auto_poll_entry(
        hass,
        monkeypatch,
        lambda ids: {entityvalue: None for entityvalue in ids},
        entityvalues=[EV_A, EV_B],
    )
    poll = scheduled[-1]

    for _ in range(5):
        await poll(None)
        await hass.async_block_till_done()

    assert not entry.runtime_data.expert.fail_counts, (
        "a failed batch was counted against the ids it consists of"
    )
    assert raised_issues == []


async def test_a_single_configured_id_is_still_reported(hass, monkeypatch):
    """The rule above needs a second id to mean anything.

    With one configured parameter "all of them failed" is true every time it
    fails, so applying the batch rule there would silence the notification for
    exactly the installation that has the least other evidence - the same trap
    as refusing a read that named no JobID.
    """
    entry, scheduled, raised_issues = await _auto_poll_entry(
        hass,
        monkeypatch,
        lambda ids: {entityvalue: None for entityvalue in ids},
    )
    poll = scheduled[-1]

    for _ in range(4):
        await poll(None)
        await hass.async_block_till_done()

    assert entry.runtime_data.expert.fail_counts[EV_A] == 4
    assert len(raised_issues) == 1
    # It was requested and failed, so the portal is a candidate too - the
    # read-failures wording must not assert the configuration is wrong.
    assert raised_issues[0]["translation_key"] == "expert_poll_read_failures"


async def test_the_rescan_option_marks_the_cached_lists_as_due(hass):
    """The button is for the moment right after something changed in the
    portal, when waiting a day for the interval is the wrong answer.

    It does no portal work of its own: setting the timestamps back is enough,
    and the next update cycle re-reads through the normal path, with the
    normal rate limiting and the normal "keep what we have if the re-read
    fails" rule.
    """
    entry = await _setup(hass, _entry(hass))
    api = entry.runtime_data.api
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {"P": {}},
                "parameters_fetched_at": 9999.0,
            },
            (0, 7): {"Index": 0, "Type": 7, "Name": "Boiler"},
        }
    }

    result = await _open_options(hass, entry, "rescan_parameters")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "configure", (
        "the re-scan should hand the user back to the settings form"
    )
    assert api.modules["1234"][(0, 1)]["parameters_fetched_at"] == 0, (
        "the cached list was not marked for a re-read"
    )
    # A module with no discovered list needs no marking - it is read anyway.
    assert "parameters_fetched_at" not in api.modules["1234"][(0, 7)]


async def test_the_rescan_option_also_marks_a_refused_module(hass):
    """The module this button exists for is the one with an EMPTY list.

    A module the portal refused keeps `parameters: {}` plus its timestamp, so
    it is retried once a day rather than never. Testing the stored list for
    truthiness instead of presence skipped exactly those - the button did
    nothing for the only case where waiting a day is the wrong answer.
    """
    entry = await _setup(hass, _entry(hass))
    api = entry.runtime_data.api
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {},
                "parameters_fetched_at": 9999.0,
            }
        }
    }

    await _open_options(hass, entry, "rescan_parameters")

    assert api.modules["1234"][(0, 1)]["parameters_fetched_at"] == 0


async def test_the_rescan_option_makes_no_portal_requests(hass):
    """Doing the reads here would put a multi-second round trip inside a
    dialog and duplicate the rate limiting the normal path already has."""
    entry = await _setup(hass, _entry(hass))
    api = entry.runtime_data.api
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {"P": {}},
                "parameters_fetched_at": 1.0,
            }
        }
    }
    calls = []
    api.make_api_call = lambda url, **_kwargs: calls.append(url)

    await _open_options(hass, entry, "rescan_parameters")

    assert calls == []
