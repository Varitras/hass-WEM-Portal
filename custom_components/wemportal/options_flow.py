"""Options flow for wemportal integration.

Split from config_flow.py so the multi-step options flow (menu, module
discovery, configure, re-scan) sits apart from the initial setup flow and
config_flow drops back under the size budget. It imports the shared credential
validation and its exceptions back from config_flow - a one-way dependency,
and no cycle, because config_flow reaches this class only through a
function-local import in async_get_options_flow. expert_writer stays behind the
same function-local import as before (see _expert_client), which is what
tests/test_security.py enforces.
"""

from __future__ import annotations

import logging
import re

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import (
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_EXPERT_AUTO_POLL,
    CONF_EXPERT_ENABLE_MODULE_NAV,
    CONF_EXPERT_ENABLE_SECURITY_CODE,
    CONF_EXPERT_MODULE_ARG,
    CONF_EXPERT_MODULE_LIST,
    CONF_EXPERT_NOTIFY_ON_SUCCESS,
    CONF_EXPERT_POLL_INTERVAL,
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_WRITE,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DEFAULT_CONF_LANGUAGE_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    DEFAULT_MODE,
    EXPERT_SLOT_COUNT,
    MIN_EXPERT_ENTITYVALUE_LENGTH,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_API_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)
from .exceptions import ExpertOperationAborted, ForbiddenError
from .expert_options import (
    discovery_option_list,
    canonical_entityvalue,
    duplicate_entityvalues,
    expert_client_options,
)

from .config_flow import (
    AVAILABLE_MODES,
    CannotConnect,
    ExpertBusy,
    InvalidAuth,
    RateLimited,
    validate_input,
)

_LOGGER = logging.getLogger(__name__)


class WemportalOptionsFlow(OptionsFlow):
    """Handle options."""

    # Error key from the last discovery run, shown on the configure form.
    # Without this a failed discovery silently produced an EMPTY dropdown,
    # which is indistinguishable from "the portal has no parameters" - the
    # user had no way to tell that the search never actually ran.
    _discovery_error: str | None = None
    # Human-readable detail for the configure form's description (e.g. the
    # exact remaining backoff time). The error strings themselves are static
    # translations, so the specifics go here instead of leaving the user to
    # guess how long "try again later" means.
    _discovery_detail: str = ""
    # Module list fetched during THIS flow. Persisted only by the final save,
    # so discovery never triggers an integration reload mid-flow.
    _module_list: list | None = None

    def __init__(self) -> None:
        # Per-flow, not per-class. Discovery result and the modules the user
        # picked, held across the multi-step options flow (menu -> module
        # select -> discovery -> configure). Not persisted; re-opening the
        # flow re-discovers on demand.
        #
        # As class attributes these were ONE list shared by every options flow
        # in the process. Nothing leaks today - both are only ever reassigned,
        # never appended to - but `_discovered` holds installation-specific
        # entityvalues, so on a Home Assistant running two WEM Portal accounts
        # the first `.append()` anyone writes here would show one account's
        # parameter ids in the other account's dropdown. The others above stay
        # class attributes: they are immutable defaults, which cannot be
        # shared by accident.
        self._discovered: list = []
        self._selected_modules: list = []

    async def async_step_init(self, user_input=None):
        """Options menu: configure, discover expert parameters, or re-scan."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["configure", "discover_modules", "rescan_parameters"],
        )

    async def async_step_rescan_parameters(self, user_input=None):
        """Mark the cached API parameter lists as due for a re-read.

        The lists are refreshed on their own once a day. This is for the
        moment right after something changed in the portal - activating an
        input or output on a module the integration already knows - when
        waiting for the interval is the wrong answer.

        It does no portal work itself. Setting the timestamps back is enough:
        the next update cycle reads the modules again through the normal path,
        with the normal rate limiting and the normal "keep what we have if the
        re-read fails" rule. Doing the requests here would put a multi-second
        portal round trip inside a dialog and duplicate all of that.
        """
        data = getattr(self.config_entry, "runtime_data", None)
        api = getattr(data, "api", None) if data is not None else None
        modules = getattr(api, "modules", None) if api is not None else None

        marked = 0
        for device_modules in (modules or {}).values():
            for module in device_modules.values():
                # Presence, not truthiness. A module the portal refused keeps
                # an EMPTY parameter list plus its timestamp, so a truthiness
                # test skipped exactly the modules this button exists for -
                # the ones whose list is due for another attempt.
                if "parameters" in module:
                    module["parameters_fetched_at"] = 0
                    marked += 1

        if marked:
            # To disk as well, not only to the api object: saving the form
            # this step returns to schedules a reload, and a reload rebuilds
            # that object from the persisted cache. Written through the
            # coordinator so it shares the store lock the unload waits on and
            # the same gate - opening the store here wrote outside them, where
            # a removal could re-create it or a stale cycle save overwrite it.
            # A failure is not a slower next start like the cycle's own save -
            # it is the request itself going missing, so it is not swallowed.
            await data.coordinator.async_persist_rescan()
            _LOGGER.info(
                "Options: marked the parameter list of %d module(s) for a "
                "re-read on the next update.",
                marked,
            )
        else:
            # Nothing loaded, or nothing discovered yet - in both cases the
            # next cycle discovers anyway, so this is not an error.
            _LOGGER.debug("Options: no cached parameter lists to mark for a re-read.")
        return await self.async_step_configure()

    async def async_step_configure(self, user_input=None):
        """Manage the options."""
        errors = {}
        # Surface a failed discovery here (the step the user is sent to
        # afterwards), then clear it so it doesn't stick to a later save.
        detail = ""
        if self._discovery_error and user_input is None:
            errors["base"] = self._discovery_error
            self._discovery_error = None
            detail = self._discovery_detail
            self._discovery_detail = ""
        if user_input is not None:
            errors.update(self._validate_configure_input(user_input))
            if not errors:
                errors.update(await self._validate_mode_change(user_input))
            if not errors:
                return self._save_configure(user_input)

        # On an error redisplay, prefill the form with what the user just
        # typed (so nothing has to be re-entered); otherwise with the
        # stored options.
        source = user_input if user_input is not None else self.config_entry.options

        def prefill(key, fallback):
            # Local helper (previously a lambda stored on self): read an
            # option value with a fallback - from the just-submitted input on
            # an error redisplay, or the stored options otherwise.
            return source.get(key, fallback)

        # Slot id dropdown options: discovered parameters (if a discovery ran
        # this session) plus any already-configured ids, so a stored
        # selection stays selectable even without a fresh discovery.
        current_ids = [
            self.config_entry.options.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot, "")
            for slot in range(1, EXPERT_SLOT_COUNT + 1)
        ]
        id_options = discovery_option_list(self._discovered, current_ids)

        return self.async_show_form(
            step_id="configure",
            errors=errors,
            description_placeholders={"status": detail},
            data_schema=self._configure_schema(prefill, id_options),
        )

    async def _validate_mode_change(self, user_input) -> dict:
        """Check the credentials against the transport the new mode needs.

        The initial setup deliberately validates exactly the transport the
        chosen mode will use at runtime, because the two logins are separate
        and one working says nothing about the other. Switching mode later
        skipped that check entirely, so an entry validated with a web login
        could be moved to `api` and then fail every single update - with the
        options dialog having reported success.

        Only on an actual change: a save that leaves the mode alone must not
        cost a portal login, least of all one the portal might refuse.
        """
        new_mode = user_input.get(CONF_MODE)
        if new_mode == self.config_entry.options.get(CONF_MODE, DEFAULT_MODE):
            return {}
        try:
            await validate_input(
                self.hass, {**self.config_entry.data, CONF_MODE: new_mode}
            )
        except RateLimited:
            return {"base": "rate_limited"}
        except InvalidAuth:
            return {CONF_MODE: "invalid_auth"}
        except CannotConnect:
            return {"base": "cannot_connect"}
        return {}

    def _validate_configure_input(self, user_input) -> dict:
        """Check the submitted options and return the per-field errors.

        NORMALISES `user_input` IN PLACE, and that is load-bearing rather
        than cosmetic: the stripped slot ids written back here are the only
        source of the ten `expert_slot_id_N` keys. Without them a cleared
        field is simply absent from the form data, the merge in
        _save_configure keeps the STORED id, and "empty a slot" quietly stops
        working - the bug 1.8.3 shipped.

        The slot NAMES need the same treatment for the same reason, and used
        not to get it: clearing a name left the field out of the form data,
        the merge brought the old one back, and the name could only be
        replaced, never removed.
        """
        errors = {}
        # Validate the ten expert slot IDs on save: an entityvalue must
        # be a plain hex string of a plausible length. Real entityvalues
        # are long (the known ones are 36 hex chars); a short entry like
        # "0" or "abc" is a stray value/typo, not a real ID, and would
        # only cause a pointless failing portal request later. We require
        # hex AND a minimum length, kept well below the observed 36 so a
        # slightly different length on another installation still passes.
        # Whitespace is stripped; empty stays allowed (slot unused).
        min_len = MIN_EXPERT_ENTITYVALUE_LENGTH
        for slot in range(1, EXPERT_SLOT_COUNT + 1):
            id_key = CONF_EXPERT_SLOT_ID_TEMPLATE % slot
            raw = (user_input.get(id_key) or "").strip()
            user_input[id_key] = raw  # persist the stripped value
            if raw and (not re.fullmatch(r"[0-9A-Fa-f]+", raw) or len(raw) < min_len):
                errors[id_key] = "invalid_entityvalue"
            # Materialised for the same reason, not validated: any name is
            # allowed, including none.
            name_key = CONF_EXPERT_SLOT_NAME_TEMPLATE % slot
            user_input[name_key] = (user_input.get(name_key) or "").strip()
        # The module menu index feeds an ASP.NET postback argument and a
        # ClientState JSON template verbatim - restrict it to digits so
        # a typo (or stray JSON) is caught in the form instead of being
        # sent to the portal. Empty stays allowed (= use the default).
        module_arg = (user_input.get(CONF_EXPERT_MODULE_ARG) or "").strip()
        user_input[CONF_EXPERT_MODULE_ARG] = module_arg
        if module_arg and not module_arg.isdigit():
            errors[CONF_EXPERT_MODULE_ARG] = "invalid_module_arg"
        # De-dup: the same entityvalue must not be selected in two slots.
        slot_ids = [
            (user_input.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot) or "").strip()
            for slot in range(1, EXPERT_SLOT_COUNT + 1)
        ]
        duplicates = duplicate_entityvalues(slot_ids)
        if duplicates:
            for slot in range(1, EXPERT_SLOT_COUNT + 1):
                # Canonical on BOTH sides: the set is built that way, so a raw
                # comparison marked a slot only where the spellings happened to
                # match, and two that both differ from it went through as new.
                slot_id = user_input.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot)
                if canonical_entityvalue(slot_id) in duplicates:
                    errors[CONF_EXPERT_SLOT_ID_TEMPLATE % slot] = (
                        "duplicate_entityvalue"
                    )
        return errors

    def _save_configure(self, user_input):
        """Persist the validated options and reload the entry."""
        # No-op guard: writing a new options entry always triggers a
        # full integration reload (and a fresh portal login). If the
        # normalized input is identical to the stored options -
        # e.g. the user opened the dialog and saved without changes,
        # or only typed whitespace into an already-empty ID field -
        # skip the write so we don't reload for nothing. Reloading
        # needlessly also risks the portal's 403 rate limit.
        current = dict(self.config_entry.options)
        merged = {**current, **user_input}
        # A module list fetched during this flow is persisted here -
        # the only place options are written, so no mid-flow reload.
        if self._module_list is not None:
            merged[CONF_EXPERT_MODULE_LIST] = self._module_list
        if merged == current:
            return self.async_abort(reason="no_changes")
        # Write the MERGED options, not just the form fields: Home
        # Assistant REPLACES the options dict with what is passed
        # here. Passing only `user_input` silently dropped every
        # option that is not a form field - notably the cached module
        # list - so each save cost another portal login on the next
        # discovery. It also makes the no-op comparison above and the
        # value actually written agree on the same dict.
        # Options only take effect on a reload: scan intervals, mode
        # and expert access are all read during setup. With no update
        # listener doing that implicitly, the flow has to.
        #
        # The options are written HERE, before the reload is
        # scheduled, and only then handed to the flow manager. Order
        # matters and is easy to get wrong: the manager writes them
        # after this step returns, so scheduling a reload from here
        # without writing first queued a reload that read the OLD
        # values - the form saved and nothing changed until the next
        # restart. The manager's own write below then finds them
        # already in place and is a no-op.
        #
        # Home Assistant offers OptionsFlowWithReload for exactly
        # this, but only from 2025.8 - later than the 2024.12 this
        # integration supports, and it is selected by isinstance, not
        # by an attribute, so it cannot be adopted conditionally
        # without a second code path.
        self.hass.config_entries.async_update_entry(self.config_entry, options=merged)
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=merged)

    def _configure_schema(self, prefill, id_options):
        """The options form itself. `prefill` reads a field's stored or
        just-submitted value, `id_options` is the slot-id dropdown content."""
        return vol.Schema(
            {
                # Both scan intervals are clamped to a lower bound (like
                # the expert poll interval below): a stray tiny value
                # such as "1" second would poll the portal continuously
                # and reliably trigger the IP-wide 403 rate limit.
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=prefill(
                        CONF_SCAN_INTERVAL, DEFAULT_CONF_SCAN_INTERVAL_VALUE
                    ),
                ): vol.All(
                    cv.positive_int,
                    vol.Clamp(min=MIN_SCAN_INTERVAL_SECONDS),
                ),
                vol.Optional(
                    CONF_SCAN_INTERVAL_API,
                    default=prefill(
                        CONF_SCAN_INTERVAL_API, DEFAULT_CONF_SCAN_INTERVAL_API_VALUE
                    ),
                ): vol.All(
                    cv.positive_int,
                    vol.Clamp(min=MIN_SCAN_INTERVAL_API_SECONDS),
                ),
                # Same closed choice as the initial setup form -
                # previously a free string here allowed saving an
                # unsupported language code.
                vol.Optional(
                    CONF_LANGUAGE,
                    default=prefill(CONF_LANGUAGE, DEFAULT_CONF_LANGUAGE_VALUE),
                ): vol.In(["en", "de"]),
                vol.Optional(
                    CONF_MODE, default=prefill(CONF_MODE, DEFAULT_MODE)
                ): vol.In(AVAILABLE_MODES),
                # Expert write access (web) - off by default. Entities/
                # service only exist while this is enabled.
                vol.Optional(
                    CONF_EXPERT_WRITE,
                    default=prefill(CONF_EXPERT_WRITE, False),
                ): cv.boolean,
                # Post a persistent notification after a SUCCESSFUL expert
                # write. OFF by default (noisy when setting several
                # values); failures always notify regardless.
                vol.Optional(
                    CONF_EXPERT_NOTIFY_ON_SUCCESS,
                    default=prefill(CONF_EXPERT_NOTIFY_ON_SUCCESS, False),
                ): cv.boolean,
                # Optional periodic read-back of the configured expert
                # parameters - OFF by default (each read is a full
                # Fachmann navigation; frequent polling risks a 403 IP
                # block). The interval is in minutes and floored at
                # MIN_EXPERT_POLL_INTERVAL_MINUTES.
                vol.Optional(
                    CONF_EXPERT_AUTO_POLL,
                    default=prefill(CONF_EXPERT_AUTO_POLL, False),
                ): cv.boolean,
                vol.Optional(
                    CONF_EXPERT_POLL_INTERVAL,
                    default=prefill(
                        CONF_EXPERT_POLL_INTERVAL, DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
                    ),
                ): vol.All(
                    cv.positive_int,
                    vol.Clamp(min=MIN_EXPERT_POLL_INTERVAL_MINUTES),
                ),
                # --- Advanced expert options (only if you know what you
                # are doing) --------------------------------------------
                # Both navigation steps below are skipped by default
                # because they were proven unnecessary on the reference
                # installation. Re-enable only for an unusual portal or
                # module layout where reads/writes otherwise fail.
                vol.Optional(
                    CONF_EXPERT_ENABLE_MODULE_NAV,
                    default=prefill(CONF_EXPERT_ENABLE_MODULE_NAV, False),
                ): cv.boolean,
                # Module menu index used ONLY when module select is
                # enabled above. Empty default; "6" = heat pump on the
                # reference install.
                vol.Optional(
                    CONF_EXPERT_MODULE_ARG,
                    default=prefill(CONF_EXPERT_MODULE_ARG, ""),
                ): cv.string,
                vol.Optional(
                    CONF_EXPERT_ENABLE_SECURITY_CODE,
                    default=prefill(CONF_EXPERT_ENABLE_SECURITY_CODE, False),
                ): cv.boolean,
                # Ten generic expert-parameter slots (name + entityvalue
                # hex ID). Added programmatically below so the block stays
                # compact. Empty slots are ignored.
                **self._expert_slot_schema(prefill, id_options),
            }
        )

    def _expert_slot_schema(self, prefill, id_options):
        """Build the vol schema fields for the ten generic expert slots.

        Each slot is a free-text name field and an entityvalue-id field. The
        id field is a dropdown of discovered parameters (`id_options`:
        [{value, label}]) with `custom_value=True`, so a discovered id can be
        picked OR a raw id typed manually (fallback if discovery failed or a
        field is missing). Prefilled values use `suggested_value` (via
        description), NOT `default`: with `default`, a field the user clears
        on save falls back to the stored value, making it impossible to
        delete a value. `suggested_value` shows the current value but lets an
        emptied field stay empty, and prefers just-submitted input on an
        error redisplay so a validation error never wipes what the user typed.
        """
        id_selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=option["value"], label=option["label"])
                    for option in id_options
                ],
                mode=SelectSelectorMode.DROPDOWN,
                custom_value=True,
                sort=False,
            )
        )
        fields = {}
        for slot in range(1, EXPERT_SLOT_COUNT + 1):
            name_key = CONF_EXPERT_SLOT_NAME_TEMPLATE % slot
            id_key = CONF_EXPERT_SLOT_ID_TEMPLATE % slot
            fields[
                vol.Optional(
                    name_key,
                    description={"suggested_value": prefill(name_key, "")},
                )
            ] = cv.string
            fields[
                vol.Optional(
                    id_key,
                    description={"suggested_value": prefill(id_key, "")},
                )
            ] = id_selector
        return fields

    def _known_modules(self) -> list:
        """Module list for this flow: freshly fetched one first, else stored."""
        if self._module_list is not None:
            return self._module_list
        return self.config_entry.options.get(CONF_EXPERT_MODULE_LIST) or []

    # --- Expert parameter discovery -----------------------------------
    def _expert_client(self):
        """Build a WemPortalExpertClient from the entry's credentials.

        Imported here, not at module level: Home Assistant loads this file
        during a normal entry setup, and expert_writer pulls curl_cffi and
        lxml (~140 ms, measured). Only discovery ever needs the client.
        """
        from .expert_writer import WemPortalExpertClient

        entry = self.config_entry
        client_options = expert_client_options(entry.options)
        data = getattr(entry, "runtime_data", None)
        api = data.api if data is not None else None

        def abort_if_the_entry_is_gone():
            """The gate every other expert caller already had.

            Discovery is the longest expert sequence there is - a login, the
            module navigation and a form read per module - and it was the one
            client built without a way to stop. An entry unloaded or reloaded
            while it ran had it navigate the portal to the end on credentials
            and options that were no longer current.
            """
            if data is None:
                return
            reason = data.why_not_current(entry)
            if reason is not None:
                raise ExpertOperationAborted(f"expert discovery: {reason}")

        return WemPortalExpertClient(
            entry.data.get(CONF_USERNAME),
            entry.data.get(CONF_PASSWORD),
            cooldown_check=api.check_expert_cooldown if api is not None else None,
            cooldown_activate=api.activate_expert_cooldown if api is not None else None,
            cookie_jar=api.expert_cookies if api is not None else None,
            abort_check=abort_if_the_entry_is_gone,
            **client_options,
        )

    async def _run_expert(self, work, *arguments):
        """Run one blocking expert operation under the shared per-account lock.

        The entity write and the auto-poll take the same lock, so only one
        expert portal operation runs per account at a time. Discovery is the
        heaviest of the three and the only one a user starts by hand.

        Taken INSIDE the executor job, like the write path does it: waiting for
        a threading lock on the event loop would stall Home Assistant for as
        long as the other operation runs.
        """
        data = getattr(self.config_entry, "runtime_data", None)
        controller = getattr(data, "expert", None) if data is not None else None
        lock = getattr(controller, "lock", None) if controller is not None else None

        def run_locked():
            if lock is not None and not lock.acquire(blocking=False):
                raise ExpertBusy("another expert operation is running for this account")
            try:
                return work(*arguments)
            finally:
                if lock is not None:
                    lock.release()

        try:
            return await self.hass.async_add_executor_job(run_locked)
        except ExpertOperationAborted as exc:
            # The one boundary both discovery calls cross, which is why the
            # translation happens here rather than at each of them.
            #
            # ExpertOperationAborted is a BaseException, so the `except
            # Exception` around those calls does not see it, and the flow
            # manager translates only AbortFlow. Left to travel, it stopped
            # the portal work correctly and then killed the flow with a
            # traceback where the user should get a sentence. There is
            # nothing to fall back to either: the configuration this flow
            # edits is being torn down.
            _LOGGER.debug("Expert discovery stopped: %s", exc)
            raise AbortFlow("discovery_aborted") from exc

    async def async_step_discover_modules(self, user_input=None):
        """Pick which modules to search. Module list is cached in options."""
        errors = {}
        modules = self._known_modules()
        if user_input is not None and not user_input.get("refresh"):
            selected = user_input.get("modules", [])
            self._selected_modules = [
                module for module in modules if str(module["index"]) in selected
            ]
            return await self.async_step_run_discovery()

        # (Re)fetch the module list if missing or a refresh was requested.
        if not modules or (user_input is not None and user_input.get("refresh")):
            client = self._expert_client()
            try:
                modules = await self._run_expert(client.list_modules)
            # Before the catch-all below: AbortFlow reaches Exception through
            # FlowError and HomeAssistantError, so the broad clause turned the
            # deliberate stop back into "discovery_failed" - the exact wording
            # _run_expert translates it away from.
            except AbortFlow:
                raise
            except ExpertBusy:
                _LOGGER.debug("Expert discovery: another operation holds the lock.")
                errors["base"] = "discovery_busy"
                modules = self._known_modules()
            except ForbiddenError:
                _LOGGER.warning(
                    "Expert discovery: module list not read - portal access is "
                    "in the 403 cooldown."
                )
                errors["base"] = "discovery_blocked"
                modules = self._known_modules()
            except Exception:
                _LOGGER.exception("Expert discovery: reading module list failed")
                errors["base"] = "discovery_failed"
                modules = self._known_modules()
            else:
                # Hold the list on the flow and let the final save persist it.
                # Writing it here called async_update_entry, which back then
                # fired the update listener -> async_reload: a full
                # integration reload (fresh login + full scrape) in the middle
                # of the flow. That reload also RESET the 403 backoff and
                # discarded the cached session, so one click on "discover"
                # cost two logins - the
                # exact load this feature exists to avoid.
                self._module_list = modules

        return self.async_show_form(
            step_id="discover_modules",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional("modules", default=[]): cv.multi_select(
                        {str(module["index"]): module["label"] for module in modules}
                    ),
                    vol.Optional("refresh", default=False): cv.boolean,
                }
            ),
        )

    async def async_step_run_discovery(self, user_input=None):
        """Run discovery over the selected modules, then go to configure.

        Any outcome other than "found something" is reported on the configure
        form. Previously every failure was only logged, so the user was sent
        on to an empty dropdown with no indication that the search had been
        blocked (or had never run at all).
        """
        modules = self._selected_modules
        if not modules:
            # Submitting without ticking a module used to fall through
            # silently to an empty dropdown - the very "user cannot tell the
            # search never ran" case the error keys were added for.
            _LOGGER.debug("Expert discovery: no module selected, nothing to do.")
            self._discovery_error = "discovery_empty"
        if modules:
            client = self._expert_client()
            try:
                self._discovered = await self._run_expert(client.discover, modules)
            # See the module list above: the catch-all swallows AbortFlow.
            except AbortFlow:
                raise
            except ExpertBusy:
                _LOGGER.debug("Expert discovery: another operation holds the lock.")
                self._discovery_error = "discovery_busy"
            except ForbiddenError as exc:
                # Either the expert path is backing off from an earlier 403,
                # or the portal rejected a request just now. The exception
                # text says which, and names the request - pass it through to
                # the form instead of making the user read the log.
                _LOGGER.warning("Expert discovery: not run - %s", exc)
                self._discovery_error = "discovery_blocked"
                self._discovery_detail = str(exc)
            except Exception:
                _LOGGER.exception("Expert discovery: parameter discovery failed")
                self._discovery_error = "discovery_failed"
            else:
                if not self._discovered:
                    _LOGGER.warning(
                        "Expert discovery: the selected module(s) returned no "
                        "readable parameters."
                    )
                    self._discovery_error = "discovery_empty"
        return await self.async_step_configure()
