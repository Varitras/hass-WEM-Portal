"""Config flow for wemportal integration."""

from __future__ import annotations

import logging
import re

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import exceptions
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    AVAILABLE_MODES,
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
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    DEFAULT_MODE,
    DOMAIN,
    EXPERT_SLOT_COUNT,
    MIN_EXPERT_ENTITYVALUE_LENGTH,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_API_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)
from .exceptions import AuthError, ForbiddenError
from .expert_options import (
    discovery_option_list,
    duplicate_entityvalues,
    expert_client_options,
)
from .utils import close_api_sessions
from .wemportalapi import WemPortalApi

_LOGGER = logging.getLogger(__name__)

# Password uses a proper password-type selector so the browser masks the
# input (a plain `str` field renders as clear text - shoulder-surfing /
# screen-sharing exposure while typing).
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Required(CONF_LANGUAGE, default=DEFAULT_CONF_LANGUAGE_VALUE): vol.In(
            ["en", "de"]
        ),
        vol.Optional(CONF_MODE, default=DEFAULT_MODE): vol.In(AVAILABLE_MODES),
    }
)


def account_unique_id(username) -> str:
    """Normalised account id used as the config entry's unique_id.

    Portal usernames are email addresses, so casing and stray whitespace are
    not meaningful - but a raw comparison treated "Max@example.org" and
    "max@example.org" as two accounts, which meant two entries polling the
    same installation twice.
    """
    return (username or "").strip().lower()


async def validate_input(hass: HomeAssistant, data):
    """Validate the user input allows us to connect."""
    # Create API object for authentication check
    api = WemPortalApi(data[CONF_USERNAME], data[CONF_PASSWORD])

    try:
        # Validate exactly what the mode will require at runtime. `api` and
        # `both` both call api_login() on every cycle (see
        # WemPortalApi._fetch_data), so falling back to a web login here
        # accepted a configuration that could never poll: setup succeeded,
        # then every update failed with the API login the user was never
        # told about. Only `web` may be validated with a web login.
        if data[CONF_MODE] in ("api", "both"):
            await hass.async_add_executor_job(api.api_login)
        elif data[CONF_MODE] == "web":
            await hass.async_add_executor_job(api.web_login)
    except AuthError as exc:
        raise InvalidAuth from exc
    except ForbiddenError as exc:
        # Caught BEFORE the broad handler below, which would report it as
        # "cannot connect". That reads like a network problem and invites an
        # immediate retry - against an IP the portal is refusing right now,
        # and refuses per IP for twelve hours past its request limit. Every
        # retry makes the situation it describes last longer. The options
        # flow has said this properly for a while; the setup flow, which is
        # where somebody lands after deleting and re-adding the integration
        # to "fix" the blockade, did not.
        raise RateLimited from exc
    except Exception as exc:
        # Broad on purpose: this runs during the config flow, where any
        # failure that is not an auth rejection has to reach the user as
        # "cannot connect" rather than as an unhandled flow error.
        raise CannotConnect from exc
    finally:
        # Close the throwaway validation session(s); config-flow validation
        # otherwise left an open connection behind on every setup attempt.
        await hass.async_add_executor_job(close_api_sessions, api)

    return data


class RateLimited(exceptions.HomeAssistantError):
    """The portal is refusing this IP, not these credentials."""


class ExpertBusy(exceptions.HomeAssistantError):
    """Another expert operation holds the shared per-account lock."""


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""


class WemPortalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for wemportal."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ):
        """Get the options flow for this handler."""
        return WemportalOptionsFlow()

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                # Claimed BEFORE validating: the previous manual scan ran
                # after a full portal login, so re-adding an existing account
                # cost a needless request - the one the portal is most likely
                # to reject. It also compared usernames verbatim, so a
                # different capitalisation slipped through as a second entry,
                # and a hand-rolled loop takes no lock, so two flows opened at
                # once could both pass it.
                account = account_unique_id(user_input[CONF_USERNAME])
                await self.async_set_unique_id(account)
                # reload_on_update=False: the default also OVERWRITES the
                # existing entry's data with what was typed here and reloads
                # it. Someone re-adding an account by mistake would silently
                # replace the stored password of a working entry. Aborting is
                # the whole answer; nothing about the existing entry changes.
                self._abort_if_unique_id_configured(reload_on_update=False)
                # Belt and braces: an entry created before unique_ids were
                # used only gets one from _backfill_account_unique_id in
                # async_setup_entry, which needs the entry to have been set
                # up at least once - a disabled or failing entry never is.
                # Until then the check above cannot see it, so compare the
                # normalised usernames as well. Costs nothing - no network.
                for existing in self._async_current_entries(include_ignore=False):
                    if account_unique_id(existing.data.get(CONF_USERNAME)) == account:
                        return self.async_abort(reason="already_configured")

                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(
                    title=info[CONF_USERNAME],
                    data=user_input,
                    options={
                        CONF_SCAN_INTERVAL: 1800,
                        CONF_SCAN_INTERVAL_API: 300,
                        CONF_LANGUAGE: user_input.get(
                            CONF_LANGUAGE, DEFAULT_CONF_LANGUAGE_VALUE
                        ),
                        CONF_MODE: user_input.get(CONF_MODE, DEFAULT_MODE),
                    },
                )

            except AbortFlow:
                # How Home Assistant ENDS a flow, not an error in it:
                # _abort_if_unique_id_configured and async_set_unique_id both
                # raise it. Swallowed by the catch-all below, the user got a
                # bare "unknown" instead of "already_configured" /
                # "already_in_progress".
                raise
            except RateLimited:
                errors["base"] = "rate_limited"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data):
        """Start reauthentication (triggered by ConfigEntryAuthFailed).

        Without this step, the reauth flow Home Assistant starts after a
        ConfigEntryAuthFailed would fail with an unknown-step error, and
        changed portal credentials could only be fixed by deleting and
        re-adding the integration.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for fresh credentials, validate them, update and reload."""
        entry = getattr(self, "_reauth_entry", None)
        if entry is None:
            return self.async_abort(reason="unknown")

        errors = {}
        if user_input is not None:
            # Reauth must re-authenticate the SAME account. The username field
            # is editable, so guard against silently switching the entry to a
            # different login: require it to match the entry's existing
            # account (case-insensitive, as portal usernames are emails).
            # Only the password is actually updated.
            original = account_unique_id(entry.data.get(CONF_USERNAME))
            entered = account_unique_id(user_input.get(CONF_USERNAME))
            if entered != original:
                errors["base"] = "wrong_account"
            else:
                new_data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                # Validate against the mode the entry actually runs in (options
                # override the value stored at initial setup).
                effective_mode = entry.options.get(
                    CONF_MODE, new_data.get(CONF_MODE, DEFAULT_MODE)
                )
                try:
                    await validate_input(
                        self.hass, {**new_data, CONF_MODE: effective_mode}
                    )
                except RateLimited:
                    errors["base"] = "rate_limited"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected exception during reauth")
                    errors["base"] = "unknown"
                else:
                    # Reloads even when the entry is unchanged, which is the
                    # whole point here: someone re-entering the SAME password
                    # is telling us the portal rejected a login it should
                    # accept, and the entry is very likely sitting in a failed
                    # setup. Updating alone would change nothing and still
                    # report success. There is no update listener to collide
                    # with (see async_setup_entry).
                    return self.async_update_reload_and_abort(entry, data=new_data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )


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
                if (
                    user_input.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot) or ""
                ).strip() in duplicates:
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
                    default=prefill(CONF_SCAN_INTERVAL, 1800),
                ): vol.All(
                    cv.positive_int,
                    vol.Clamp(min=MIN_SCAN_INTERVAL_SECONDS),
                ),
                vol.Optional(
                    CONF_SCAN_INTERVAL_API,
                    default=prefill(CONF_SCAN_INTERVAL_API, 300),
                ): vol.All(
                    cv.positive_int,
                    vol.Clamp(min=MIN_SCAN_INTERVAL_API_SECONDS),
                ),
                # Same closed choice as the initial setup form -
                # previously a free string here allowed saving an
                # unsupported language code.
                vol.Optional(
                    CONF_LANGUAGE,
                    default=prefill(CONF_LANGUAGE, "en"),
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
        return WemPortalExpertClient(
            entry.data.get(CONF_USERNAME),
            entry.data.get(CONF_PASSWORD),
            cooldown_check=api.check_expert_cooldown if api is not None else None,
            cooldown_activate=api.activate_expert_cooldown if api is not None else None,
            cookie_jar=api.expert_cookies if api is not None else None,
            **client_options,
        )

    async def _run_expert(self, work, *arguments):
        """Run one blocking expert operation under the shared per-account lock.

        The entity write and the auto-poll both take it, so only one expert
        portal operation runs at a time - discovery, the heaviest of the three
        and the only one a user starts by hand, did not. It could open a second
        portal session on the same account beside a running poll or write.

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

        return await self.hass.async_add_executor_job(run_locked)

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
