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
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


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


async def test_expert_service_raises_on_write_failure(hass, monkeypatch):
    """A failed write must surface as an exception to the caller, so an
    automation can tell whether the parameter was actually set. The old
    fire-and-forget handler always reported success."""
    await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))

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
    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))

    lock: threading.Lock = hass.data[DOMAIN][entry.entry_id]["expert_lock"]
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

    # Patch at the SOURCE: __init__.py imports async_call_later inside the
    # function, so patching the module attribute would have no effect.
    import homeassistant.helpers.event as ha_event

    monkeypatch.setattr(ha_event, "async_call_later", fake_call_later)

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
    store = hass.data[DOMAIN][entry.entry_id]
    assert store.get("expert_poll_started"), "auto-poll never started"
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
