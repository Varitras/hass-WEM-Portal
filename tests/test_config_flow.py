"""The config, reauth and options flows through the real flow manager.

Everything a user ever types at this integration arrives here: the account
in the config flow, a new password in reauth, and every setting - scan
intervals, mode, the expert slots and their discovery - in the options flow.
They are tested against a real Home Assistant so the flow manager itself is
part of the test: which step comes next, what a form reports back, when an
entry is written and when it is reloaded are its rules, not ours.

Split out of test_e2e.py, where they had grown to a third of the file. The
harness they share with the rest of the end-to-end suite - a mock entry, a
finished setup - still lives there and is imported.

Marked `e2e` for the same reason as its neighbour: each test boots a full
Home Assistant instance; the everyday run deselects them (see pytest.ini),
CI runs them with `-m ""`.
"""

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wemportal.const import (
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_WRITE,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DOMAIN,
)
from custom_components.wemportal.exceptions import (
    AuthError,
    ForbiddenError,
)
from custom_components.wemportal.wemportalapi import WemPortalApi

# The end-to-end harness these flows are driven through. The two fixtures
# are autouse where they are defined and stay autouse here - importing a
# fixture is how pytest registers it in a second module - which is why they
# look unused to a linter and are not.
from .test_e2e import (  # noqa: F401
    EV_A,
    EV_B,
    USER,
    _enable_custom_integrations,
    _entry,
    _mock_portal,
    _open_options,
    _setup,
    _submit_options,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]


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


async def test_options_flow_rejects_two_spellings_of_one_entityvalue(hass):
    """Neither slot has to be spelled the canonical way for it to be a duplicate.

    The duplicate set is built canonically - two spellings collapse to one
    entry - but the loop that marks the offending slots compared the raw
    value against that set. Both slots below differ from the canonical
    spelling, so neither matched, no error was set, and the save went
    through with one parameter in two slots. A single UPPERCASE slot beside
    a lowercase one happened to work, because the lowercase one IS the
    canonical spelling and matched - which is why this went unnoticed.
    """
    entry = await _setup(hass, _entry(hass))

    result = await _open_options(hass, entry, "configure")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _configure_input(
            **{
                CONF_EXPERT_SLOT_ID_TEMPLATE % 1: EV_A.upper(),
                CONF_EXPERT_SLOT_ID_TEMPLATE % 2: "aA" * 18,
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
    from custom_components.wemportal.options_flow import WemportalOptionsFlow

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

    from custom_components.wemportal.options_flow import WemportalOptionsFlow
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
    from custom_components.wemportal.options_flow import WemportalOptionsFlow
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
    from custom_components.wemportal.options_flow import WemportalOptionsFlow
    from custom_components.wemportal.exceptions import ExpertOperationAborted

    entry = await _setup(hass, _entry(hass, {CONF_EXPERT_WRITE: True}))
    flow = WemportalOptionsFlow()
    monkeypatch.setattr(type(flow), "config_entry", property(lambda self: entry))
    client = flow._expert_client()

    client._check_gates()  # still loaded: the gate has to let this through

    entry.runtime_data.begin_unload()

    with pytest.raises(ExpertOperationAborted):
        client._check_gates()


def test_two_options_flows_do_not_share_their_discovery():
    """Each flow gets its own lists, because one of them holds entityvalues.

    They used to be class attributes - one list object for every options flow
    in the process. Nothing mutated them, so nothing leaked, but the fix is
    cheaper than the failure: on a Home Assistant running two WEM Portal
    accounts, the first `.append()` anyone wrote would have offered one
    account's installation-specific parameter ids in the other's dropdown.
    """
    from custom_components.wemportal.options_flow import WemportalOptionsFlow

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
        "custom_components.wemportal.options_flow.WemportalOptionsFlow._expert_client",
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
        "custom_components.wemportal.options_flow.WemportalOptionsFlow._expert_client",
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
        "custom_components.wemportal.options_flow.WemportalOptionsFlow._expert_client",
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
        "custom_components.wemportal.options_flow.WemportalOptionsFlow._expert_client",
        lambda self: _StubClient(),
    )

    result = await _open_options(hass, entry, "discover_modules")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"modules": [], "refresh": False}
    )

    assert result["step_id"] == "configure"
    assert result["errors"] == {"base": "discovery_empty"}


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


async def test_a_successful_reauth_clears_the_auth_failure_streak(hass, monkeypatch):
    """The streak outlives a reload on purpose - and that is what bit here.

    It is kept on the account rather than the coordinator because a failed
    SETUP triggers a reload that would otherwise reset it, so the escalation
    to a reauth prompt could never be reached. Nothing then cleared it when
    that prompt was answered CORRECTLY: the count is dropped on unload, and
    an entry whose setup failed is not loaded, so its reload unloads nothing.

    A cycle that succeeds afterwards does clear it, which is why this drives
    the case where the next one does NOT: the portal hands out one more login
    page. That single failure arrived on top of three the new credentials had
    already answered, so it escalated straight back to a reauth prompt - for
    a password the portal had just accepted.
    """
    from custom_components.wemportal.models import account_state

    def refusing_portal(self, *_args, **_kwargs):
        raise AuthError("Login failed")

    monkeypatch.setattr(WemPortalApi, "fetch_data", refusing_portal)
    entry = _entry(hass)
    # Seeded past the escalation threshold, so the first cycle raises
    # ConfigEntryAuthFailed and the entry ends up NOT loaded - which is the
    # state this is about: nothing unloads, so nothing clears the count.
    account_state(USER).auth_failures = 3
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is not ConfigEntryState.LOADED, (
        "the entry loaded, so the reload would clear the count on its own"
    )

    # The credentials check passes - fetch_data keeps failing, so the reload
    # after the reauth runs into one more login page.
    monkeypatch.setattr(WemPortalApi, "api_login", lambda self: None)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USER, CONF_PASSWORD: "new-secret"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert account_state(USER).auth_failures == 1, (
        f"the accepted credentials started at {account_state(USER).auth_failures} "
        "failed logins, so one more asks for them again"
    )


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
