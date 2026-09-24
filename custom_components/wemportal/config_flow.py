"""Config flow for wemportal integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Final
import logging

import voluptuous as vol
from homeassistant import exceptions
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OperationNotAllowed,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .models import account_unique_id
from .const import (
    CONF_EXPERT_MODULE_ARG,
    CONF_EXPERT_MODULE_LIST,
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DEFAULT_CONF_LANGUAGE_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    DEFAULT_MODE,
    DOMAIN,
    EXPERT_SLOT_COUNT,
)
from .exceptions import AuthError, ForbiddenError
from .coordinator import (
    forget_account_state_if_last_entry,
    forget_auth_failures,
    get_modules_store,
    get_scraper_device_store,
)
from .wemportalapi import WemPortalApi

_LOGGER = logging.getLogger(__name__)

AVAILABLE_MODES: Final = ["api", "web", "both"]

_RECONFIGURE_COMMIT_LOCK: Final = f"{DOMAIN}_reconfigure_commit_lock"

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


# account_unique_id moved to models.py: it is account vocabulary, and the
# per-account state registry keys on the same normalisation.


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
        await hass.async_add_executor_job(api.close_transport)

    return data


class RateLimited(exceptions.HomeAssistantError):
    """The portal is refusing this IP, not these credentials."""


class ExpertBusy(exceptions.HomeAssistantError):
    """Another expert operation holds the shared per-account lock."""


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""


def _without_the_installation(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options minus what names parts of one particular installation.

    The expert slots are the write action's allowlist: kept across a move to
    another login, a parameter chosen for one heating system stayed writable
    through the account of another. Preferences stay.
    """
    installation_bound = {CONF_EXPERT_MODULE_ARG, CONF_EXPERT_MODULE_LIST}
    for slot in range(1, EXPERT_SLOT_COUNT + 1):
        installation_bound.add(CONF_EXPERT_SLOT_ID_TEMPLATE % slot)
        installation_bound.add(CONF_EXPERT_SLOT_NAME_TEMPLATE % slot)
    return {
        key: value for key, value in options.items() if key not in installation_bound
    }


class WemPortalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for wemportal."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ):
        """Get the options flow for this handler."""
        from .options_flow import WemportalOptionsFlow

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
                if self._account_already_has_an_entry(account):
                    return self.async_abort(reason="already_configured")

                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(
                    title=info[CONF_USERNAME],
                    data=user_input,
                    options={
                        # The constants, not the numbers: the options flow
                        # (options_flow.py) edits the same defaults, so a new
                        # entry and the form that edits it disagreed the moment
                        # either default moved.
                        CONF_SCAN_INTERVAL: DEFAULT_CONF_SCAN_INTERVAL_VALUE,
                        CONF_SCAN_INTERVAL_API: DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
                        CONF_LANGUAGE: user_input.get(
                            CONF_LANGUAGE, DEFAULT_CONF_LANGUAGE_VALUE
                        ),
                        CONF_MODE: user_input.get(CONF_MODE, DEFAULT_MODE),
                    },
                )

            # skipcq: PYL-W0706 - shields the catch-all, not redundant
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

    def _account_already_has_an_entry(self, account) -> bool:
        """Whether some existing entry logs into the same portal account.

        The unique_id check above misses an entry created before unique_ids
        were used: it only gets one from _backfill_account_unique_id during
        setup, which a disabled or failing entry never reaches. Comparing the
        normalised usernames costs no network.
        """
        return any(
            account_unique_id(existing.data.get(CONF_USERNAME)) == account
            for existing in self._async_current_entries(include_ignore=False)
        )

    async def _credential_error(self, entry, new_data) -> str | None:
        """The error key these credentials produce, or None if they work.

        Split out because its four handlers sat three levels deep in the step
        below, where each one cost the reader two enclosing branches that have
        nothing to do with what the portal answered.
        """
        # Validate against the mode the entry actually runs in (options
        # override the value stored at initial setup).
        effective_mode = entry.options.get(
            CONF_MODE, new_data.get(CONF_MODE, DEFAULT_MODE)
        )
        try:
            await validate_input(self.hass, {**new_data, CONF_MODE: effective_mode})
        except RateLimited:
            return "rate_limited"
        except CannotConnect:
            return "cannot_connect"
        except InvalidAuth:
            return "invalid_auth"
        except Exception:
            _LOGGER.exception("Unexpected exception during reauth")
            return "unknown"
        return None

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
                failure = await self._credential_error(entry, new_data)
                if failure is None:
                    # The portal just accepted these credentials, so the
                    # failures that led here are answered. Nothing else does
                    # it: the count is dropped on unload, and an entry whose
                    # setup failed - which is how most reauth prompts arise -
                    # is not loaded, so its reload unloads nothing. Left
                    # standing, the next login page the portal hands out was
                    # the fourth in a row and asked for the same password
                    # again.
                    forget_auth_failures(entry.data.get(CONF_USERNAME))
                    # Reloads even when the entry is unchanged, which is the
                    # whole point here: someone re-entering the SAME password
                    # is telling us the portal rejected a login it should
                    # accept, and the entry is very likely sitting in a failed
                    # setup. Updating alone would change nothing and still
                    # report success. There is no update listener to collide
                    # with (see async_setup_entry).
                    return self.async_update_reload_and_abort(entry, data=new_data)
                errors["base"] = failure

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

    async def _forget_the_old_installation(self, entry) -> bool:
        """Drop what the entry kept on disk for the login it is leaving.

        The module list and the scraped readings' device id are per entry, but
        they describe the installation behind the old login. After a move to
        another installation, the new one would start on the old one's
        modules and, in web mode, file its scraped readings under the old
        one's device.

        Unloaded first: the unload waits for a store write in flight, so
        nothing can put the old data back between here and the fresh setup
        that follows. False, having forgotten nothing, when it would not
        unload: its entities still run for the old installation, and a move
        under them would have them write with the new login. The same when
        Home Assistant refuses to try - an entry still setting up, or left
        behind by an unload that failed - which it says by raising.
        """
        try:
            if not await self.hass.config_entries.async_unload(entry.entry_id):
                return False
        except OperationNotAllowed:
            return False
        await get_modules_store(self.hass, entry.entry_id).async_remove()
        await get_scraper_device_store(self.hass, entry.entry_id).async_remove()
        # Keyed by the account, not the entry: an older duplicate entry of
        # the account being left may still be loaded and still need it.
        forget_account_state_if_last_entry(self.hass, entry)
        return True

    async def _commit_reconfigure(self, entry, current, user_input):
        """Apply a login the portal accepted, to the entry as it is now."""
        # Removed meanwhile counts as changed: Home Assistant aborts a waiting
        # reauth when its entry goes, not a waiting reconfigure.
        entry_is_gone = self.hass.config_entries.async_get_entry(entry.entry_id) is None
        if entry_is_gone or account_unique_id(entry.data.get(CONF_USERNAME)) != current:
            return self.async_abort(reason="reconfigure_entry_changed")
        # The target needs no second look: the flow reserved it before
        # asking the portal, and every other flow for it aborts on that.
        account = account_unique_id(user_input[CONF_USERNAME])
        moves_to_another_login = account != current
        if moves_to_another_login and not (
            await self._forget_the_old_installation(entry)
        ):
            return self.async_abort(reason="reconfigure_unload_failed")
        # Same reason as after a reauth: the portal just accepted this login,
        # so the failures counted before are answered. That login's count,
        # not the entry's - on a move the old account was not asked. And
        # before the update: the reload it starts runs eagerly, and the new
        # coordinator copies the count when it is built.
        forget_auth_failures(user_input[CONF_USERNAME])
        title = entry.title
        options: Mapping[str, Any] = entry.options
        if moves_to_another_login:
            title = user_input[CONF_USERNAME]
            options = _without_the_installation(options)
        return self.async_update_reload_and_abort(
            entry,
            unique_id=account,
            title=title,
            data={
                **entry.data,
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            },
            options=options,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Change the login of an existing entry, the account included.

        Reauth re-authenticates the SAME account and refuses another username
        on purpose. This is the other case: the portal account's e-mail
        address changed, or the password is being updated before anything
        fails. A different login is allowed - that is what this step is for -
        but not one another entry already holds, which would leave two
        entries polling one installation under one identity.
        """
        entry = self._get_reconfigure_entry()
        current = account_unique_id(entry.data.get(CONF_USERNAME))
        errors = {}
        if user_input is not None:
            account = account_unique_id(user_input[CONF_USERNAME])
            moves_to_another_login = account != current
            # One check, not the user step's two: an entry's unique_id is its
            # normalised username, so comparing usernames finds every entry a
            # unique_id lookup would - and the ones from before unique_ids too.
            if moves_to_another_login and self._account_already_has_an_entry(account):
                return self.async_abort(reason="already_configured")
            if moves_to_another_login:
                # Claims the login for this flow before the portal is asked:
                # the scan above sees entries, not another dialog moving an
                # entry to the same login right now.
                await self.async_set_unique_id(account)
            checked = {**entry.data, **user_input}
            failure = await self._credential_error(entry, checked)
            if failure is None:
                # One dialog at a time from here, and the entry read again
                # inside: while this one waited on the portal, another could
                # move this entry, hand its login to another entry, or save
                # options - and a dialog committing what it read before the
                # wait wrote all of that back.
                lock = self.hass.data.setdefault(
                    _RECONFIGURE_COMMIT_LOCK, asyncio.Lock()
                )
                async with lock:
                    return await self._commit_reconfigure(entry, current, user_input)
            errors["base"] = failure

        return self.async_show_form(
            step_id="reconfigure",
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
