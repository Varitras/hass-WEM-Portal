"""Config flow for wemportal integration."""

from __future__ import annotations

from typing import Final
import logging

import voluptuous as vol
from homeassistant import exceptions
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
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
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DEFAULT_CONF_LANGUAGE_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    DEFAULT_MODE,
    DOMAIN,
)
from .exceptions import AuthError, ForbiddenError
from .coordinator import forget_auth_failures
from .wemportalapi import WemPortalApi

_LOGGER = logging.getLogger(__name__)

AVAILABLE_MODES: Final = ["api", "web", "both"]

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
                    forget_auth_failures(entry)
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
