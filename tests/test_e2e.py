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
import types
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wemportal import expert_writer
from custom_components.wemportal.exceptions import ForbiddenError, ParameterWriteError
from custom_components.wemportal.const import (
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_WRITE,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DOMAIN,
    SERVICE_SET_EXPERT_PARAMETER,
)
from custom_components.wemportal.wemportalapi import WemPortalApi

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
        "Outside temperature": {
            "value": 12.5,
            "unit": "°C",
            "platform": "sensor",
            "friendlyName": "Outside temperature",
            "ParameterID": "P1",
        }
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
    monkeypatch.setattr(WemPortalApi, "fetch_data", lambda self, *a, **k: FAKE_DATA)
    monkeypatch.setattr(WemPortalApi, "api_login", lambda self, *a, **k: None)
    monkeypatch.setattr(WemPortalApi, "web_login", lambda self, *a, **k: None)

    # The EXPERT client must be blocked too. It uses curl_cffi, which is not
    # covered by the socket guard, so an expert path reached during a test
    # really did contact wemportal.com - a failed login against a live
    # third-party service, on every run. Every network entry point is stubbed
    # here; tests that need specific behaviour override these.
    def _no_network(*_a, **_k):
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
    monkeypatch.setattr(expert_writer.WemPortalExpertClient, "list_modules", _no_network)
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
    return {
        "value": 12.5,
        "unit": "°C",
        "platform": "sensor",
        "friendlyName": name,
        "ParameterID": "P1",
    }


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
    monkeypatch.setattr(WemPortalApi, "fetch_data", lambda self, *a, **k: two_devices)

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
        assert er.async_get_entity_id("sensor", DOMAIN, f"{device_id}-Outside temperature") is None


async def test_migration_is_skipped_when_no_data_arrived(hass, monkeypatch):
    """An empty first refresh must not abort setup - the migration simply
    has nothing to do."""
    monkeypatch.setattr(WemPortalApi, "fetch_data", lambda self, *a, **k: {})

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
        expert_writer, "create_expert_number_entities",
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

    def boom(self, *_a, **_k):
        raise ParameterWriteError("portal said no")

    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient, "write_parameter", boom
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_A, "value": 30},
            blocking=True,
        )


async def test_expert_service_refuses_while_another_operation_runs(hass):
    """The shared per-account lock must reject a second concurrent expert
    operation instead of opening a parallel portal session."""
    entry = await _setup(hass, _entry(hass, _expert_options()))

    lock: threading.Lock = entry.runtime_data.expert_lock
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
        key
        for key in schema
        if str(key) == CONF_EXPERT_SLOT_ID_TEMPLATE % 1
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

    def boom(self, *_a, **_k):
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
    from custom_components.wemportal.const import (
        CONF_EXPERT_AUTO_POLL,
        CONF_EXPERT_WRITE,
    )

    scheduled = []

    def fake_call_later(_hass, _delay, action):
        scheduled.append(action)
        return lambda: None

    # Patched where it is USED: __init__.py imports async_call_later at
    # module level, so that is the name the auto-poll actually calls.
    import custom_components.wemportal as wemportal

    monkeypatch.setattr(wemportal, "async_call_later", fake_call_later)

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
    assert data.expert_poll_started, "auto-poll never started"
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

    assert len(scheduled) == before, (
        "an unloaded entry re-armed the auto-poll timer"
    )


async def _auto_poll_entry(hass, monkeypatch, read_many):
    """An entry with the auto-poll armed, plus the list of scheduled polls.

    Returns (entry, scheduled, notifications). `read_many` stands in for the
    portal round trip and may raise.
    """
    from custom_components.wemportal.const import (
        CONF_EXPERT_AUTO_POLL,
        CONF_EXPERT_WRITE,
    )
    import custom_components.wemportal as wemportal

    scheduled = []
    # Patched where it is USED, not where it is defined: the integration
    # imports async_call_later at module level, so the name it calls is the
    # one bound here.
    monkeypatch.setattr(
        wemportal, "async_call_later",
        lambda _hass, _delay, action: scheduled.append(action) or (lambda: None),
    )
    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient, "read_many",
        lambda self, ids: read_many(ids),
    )

    # Register our own handler rather than patching the registry (async_call
    # is read-only): this is also the path a real notification takes.
    notifications = []

    async def record(call):
        notifications.append(call.data)

    hass.services.async_register("persistent_notification", "create", record)

    entry = await _setup(
        hass,
        _entry(hass, {
            CONF_EXPERT_WRITE: True,
            CONF_EXPERT_AUTO_POLL: True,
            CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
        }),
    )
    await hass.async_block_till_done()
    # Setup fires an initial poll of its own. Reset to a known point so the
    # counts a test asserts are the ones it caused, not one more.
    entry.runtime_data.expert_poll_fail_counts.clear()
    entry.runtime_data.expert_poll_fail_notified.clear()
    notifications.clear()
    return entry, scheduled, notifications


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

    entry, scheduled, notifications = await _auto_poll_entry(
        hass, monkeypatch, always_fails
    )
    poll = scheduled[-1]

    for _ in range(4):
        await poll(None)
        await hass.async_block_till_done()

    assert entry.runtime_data.expert_poll_fail_counts == {}, (
        "an outage was counted against the individual parameters"
    )
    assert notifications == [], "an outage produced a 'check your ID' notice"


async def test_a_parameter_the_portal_keeps_omitting_is_reported_once(hass, monkeypatch):
    """The case the counting DOES exist for: the batch works, one id never
    comes back. After three of those the user hears about it - once."""
    entry, scheduled, notifications = await _auto_poll_entry(
        hass, monkeypatch, lambda ids: {},
    )
    poll = scheduled[-1]

    for _ in range(5):
        await poll(None)
        await hass.async_block_till_done()

    assert entry.runtime_data.expert_poll_fail_counts[EV_A] == 5
    assert len(notifications) == 1, (
        f"{len(notifications)} notifications for one persistent failure"
    )
    assert "entityvalue" in notifications[0]["message"]


async def test_a_recovered_parameter_clears_its_failure_streak(hass, monkeypatch):
    """Otherwise a parameter that failed once could never notify again, and
    a recurring problem would go quiet after its first streak."""
    state = {"fail": True}

    def sometimes(_ids):
        return {} if state["fail"] else {EV_A: types.SimpleNamespace(
            current=21.0, min_value=0.0, max_value=100.0)}

    entry, scheduled, _ = await _auto_poll_entry(hass, monkeypatch, sometimes)
    poll = scheduled[-1]

    for _ in range(2):
        await poll(None)
        await hass.async_block_till_done()
    assert entry.runtime_data.expert_poll_fail_counts[EV_A] == 2

    state["fail"] = False
    await poll(None)
    await hass.async_block_till_done()

    assert EV_A not in entry.runtime_data.expert_poll_fail_counts
    assert EV_A not in entry.runtime_data.expert_poll_fail_notified


async def test_a_failed_poll_still_arms_the_next_one(hass, monkeypatch):
    """A transient error must not end the chain - that would silently stop
    the feature until the next restart."""
    def always_fails(_ids):
        raise RuntimeError("portal unavailable")

    _entry_obj, scheduled, _ = await _auto_poll_entry(
        hass, monkeypatch, always_fails
    )
    before = len(scheduled)

    await scheduled[-1](None)
    await hass.async_block_till_done()

    assert len(scheduled) == before + 1, "the poll chain died on one failure"


async def test_a_poll_does_not_touch_the_portal_while_a_write_runs(hass, monkeypatch):
    """Reading in parallel with a write can push the pre-write value back
    into the entity right after the write was verified."""
    reads = []

    entry, scheduled, _ = await _auto_poll_entry(
        hass, monkeypatch, lambda ids: reads.append(ids) or {},
    )
    reads.clear()   # the initial poll already ran during setup
    for entity in entry.runtime_data.expert_entities:
        entity._write_in_progress = True

    await scheduled[-1](None)
    await hass.async_block_till_done()

    assert reads == [], "the auto-poll read while a write was in flight"


async def test_a_poll_skips_when_another_expert_operation_holds_the_lock(hass, monkeypatch):
    """One portal session per account. Skipping also must not release a lock
    this cycle never acquired."""
    reads = []

    entry, scheduled, _ = await _auto_poll_entry(
        hass, monkeypatch, lambda ids: reads.append(ids) or {},
    )
    reads.clear()   # the initial poll already ran during setup
    lock = entry.runtime_data.expert_lock
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
    payload = {k: v for k, v in entry.options.items() if k in schema_keys}
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
        _entry(hass, {
            CONF_EXPERT_WRITE: True,
            CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
            CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Slot one",
        }),
    )

    await _submit_options(hass, entry, {}, omit=[CONF_EXPERT_SLOT_ID_TEMPLATE % 1])

    assert entry.options[CONF_EXPERT_SLOT_ID_TEMPLATE % 1] == "", (
        "the cleared slot kept its stored id"
    )


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
    assert result["errors"].get(CONF_EXPERT_SLOT_ID_TEMPLATE % 1) == "invalid_entityvalue"
    assert entry.options == before, "a rejected form still changed the options"


async def test_a_rejected_form_shows_what_the_user_typed(hass):
    """The redisplay prefills from user_input, not from the stored options -
    otherwise a validation error would throw away everything else the user
    had just entered."""
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))

    result = await _submit_options(
        hass, entry, {
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

    from custom_components.wemportal import coordinator as coord_mod
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    before = coordinator.num_failed

    monkeypatch.setattr(coord_mod, "DEFAULT_TIMEOUT", 0.05)

    async def never_returns(*_a, **_k):
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
    from custom_components.wemportal.exceptions import ApiBusyError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    api_before = coordinator.api
    coordinator.num_failed = 1  # one more failure would trip the swap

    def busy(self, *_a, **_k):
        raise ApiBusyError("Timed out waiting for the connection to become free")

    monkeypatch.setattr(WemPortalApi, "fetch_data", busy)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.api is api_before, "a busy api was replaced as if broken"


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

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_A, "value": 30},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
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
    from custom_components.wemportal.exceptions import AuthError

    entry = _entry(hass)

    def bad_credentials(self, *_a, **_k):
        raise AuthError("Login failed: Invalid username or password.")

    monkeypatch.setattr(WemPortalApi, "fetch_data", bad_credentials)

    raised = None
    for _ in range(AUTH_ERROR_ESCALATION_THRESHOLD):
        # A fresh coordinator per attempt, exactly like a setup retry.
        c = coord_mod.WemPortalDataUpdateCoordinator(
            hass, WemPortalApi(USER, "secret"), entry, timedelta(seconds=300)
        )
        try:
            await c._async_update_data()
        except ConfigEntryAuthFailed as exc:
            raised = exc
            break
        except Exception:
            continue

    assert raised is not None, (
        "the reauth threshold was never reached across setup retries"
    )
    coord_mod.forget_auth_failures(entry.entry_id)


async def test_a_successful_cycle_clears_the_auth_failure_count(hass):
    """A transient login hiccup must not accumulate towards reauth forever."""
    from custom_components.wemportal import coordinator as coord_mod

    entry = await _setup(hass, _entry(hass))
    coord_mod._AUTH_FAILURES[entry.entry_id] = 2

    await entry.runtime_data.coordinator._async_update_data()

    assert entry.entry_id not in coord_mod._AUTH_FAILURES


async def test_entities_of_an_offline_device_go_unavailable(hass, monkeypatch):
    """One device offline among several must not keep serving its last
    readings as current - while the healthy device stays untouched."""
    two_devices = {
        "1234": {
            "1234-ConnectionStatus": {
                "value": "online", "unit": None, "platform": "sensor",
                "friendlyName": "Connection Status", "ParameterID": "ConnectionStatus",
            },
            "Outside temperature": _sensor(),
        },
        "5678": {
            "5678-ConnectionStatus": {
                "value": "offline", "unit": None, "platform": "sensor",
                "friendlyName": "Connection Status", "ParameterID": "ConnectionStatus",
            },
            "Outside temperature": _sensor(),
        },
    }
    monkeypatch.setattr(WemPortalApi, "fetch_data", lambda self, *a, **k: two_devices)

    await _setup(hass, _entry(hass))

    def _state(entity_id_part, device):
        return next(
            s for s in hass.states.async_all("sensor")
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

    ids = sorted((e.unique_id for e in (first, second)), key=lambda v: v or "")
    assert ids == [None, USER], "the account id must be claimed exactly once"
    assert first.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED, (
        "a pre-existing duplicate must still load, not fail on a clashing id"
    )


async def test_an_in_flight_expert_write_is_cancelled_on_unload(hass, monkeypatch):
    """The write runs as a background task so the UI does not block for the
    5-15s it takes. Untracked, it kept running against the portal after the
    entry was unloaded - using the credentials and options of a
    configuration that no longer exists."""
    import asyncio

    entry = await _setup(hass, _entry(hass, _expert_options()))
    entities = entry.runtime_data.expert_entities
    assert entities, "no expert entity was created"
    entity = entities[0]

    started = asyncio.Event()

    async def never_finishes(_value):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(entity, "_async_write_in_background", never_finishes)

    await entity.async_set_native_value(42)
    await started.wait()
    assert not entity._write_task.done()

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entity._write_task.cancelled() or entity._write_task.done(), (
        "an unloaded entry left a write running against the portal"
    )


async def test_a_non_auth_failure_breaks_the_auth_streak(hass, monkeypatch):
    """The threshold is documented as CONSECUTIVE auth failures.

    The counter only ever went up, though: a timeout, a maintenance window or
    a 403 in between left it standing, so auth failures spread over hours - a
    portal that hands out the odd login page - still added up to a reauth
    prompt for credentials that were correct the whole time.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wemportal import coordinator as coord_mod
    from custom_components.wemportal.exceptions import AuthError, WemPortalError

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator

    failures = []

    def flaky(self, *_a, **_k):
        raise failures.pop(0)

    monkeypatch.setattr(WemPortalApi, "fetch_data", flaky)

    # Two auth failures, then something entirely unrelated.
    failures.extend([AuthError("login page"), AuthError("login page"),
                     WemPortalError("portal unreachable")])
    # UpdateFailed, not a bare Exception: catching anything would pass just as
    # happily on a TypeError from a mistyped test double.
    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.num_auth_failed == 0, "the streak was not broken"
    assert entry.entry_id not in coord_mod._AUTH_FAILURES


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
    payload = {k: v for k, v in entry.options.items() if k in schema_keys}
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


async def test_recovery_resets_the_connection_and_keeps_everything_else(hass, monkeypatch):
    """After repeated errors the integration recovers by dropping its HTTP
    state - not by rebuilding the api object.

    Rebuilding meant copying nine pieces of state across by hand, so every new
    field was a new chance to forget one, and two were forgotten in practice.
    It also reset things nobody intended: the statistics and circuit-times
    timestamps are portal RATE LIMITS, and a fresh object handed out a fresh
    lock while a poll thread still held the old one.

    So this pins both halves: the transport is gone, everything else is not.
    """
    from datetime import datetime

    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.wemportal.exceptions import WemPortalError

    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator
    api = coordinator.api

    stamp = datetime(2026, 8, 2, 12, 0, 0)
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

    monkeypatch.setattr(
        WemPortalApi, "fetch_data",
        lambda self, *a, **k: (_ for _ in ()).throw(WemPortalError("portal broken")),
    )

    # The recovery runs from the second consecutive failure onwards.
    for _ in range(2):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    # The transport is what recovery is for.
    assert api.valid_login is False, "the login was not invalidated"
    assert api.session is None
    assert api._scraper is None

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


async def test_a_failed_platform_setup_does_not_leak_the_store(hass, monkeypatch):
    """Home Assistant only calls async_unload_entry for an entry that
    finished setting up.

    Everything after the store is published therefore has to clean up after
    itself; without that the store and its two HTTP sessions were left
    behind - once more on every setup retry, which is exactly when platform
    setup tends to fail.
    """
    entry = _entry(hass)

    async def boom(*_a, **_k):
        raise RuntimeError("platform setup exploded")

    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", boom
    )

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
    from custom_components.wemportal import expert_writer

    entry = await _setup(
        hass,
        _entry(hass, options={
            CONF_EXPERT_WRITE: True,
            CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
            CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Slot",
        }),
    )
    def reload_midway(self, *_a, **_k):
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
            DOMAIN, SERVICE_SET_EXPERT_PARAMETER,
            {"entityvalue": EV_A, "value": 21.0},
            blocking=True,
        )

    assert "reloaded" in str(excinfo.value)


async def test_a_write_is_abandoned_while_the_entry_is_unloading(hass, monkeypatch):
    """The store survives until the platforms are down, so its presence says
    nothing during a teardown. The flag is set before that starts."""
    from custom_components.wemportal import expert_writer

    entry = await _setup(
        hass,
        _entry(hass, options={
            CONF_EXPERT_WRITE: True,
            CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A,
            CONF_EXPERT_SLOT_NAME_TEMPLATE % 1: "Slot",
        }),
    )
    # Exactly what async_unload_entry sets before unloading the platforms.
    entry.runtime_data.unloading = True

    def never(self, *_a, **_k):
        raise AssertionError("the write continued during the unload")

    monkeypatch.setattr(
        expert_writer.WemPortalExpertClient, "write_parameter", never
    )

    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_EXPERT_PARAMETER,
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
