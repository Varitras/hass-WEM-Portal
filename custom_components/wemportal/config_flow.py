"""Config flow for wemportal integration."""
from __future__ import annotations

import logging
import re

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)

from homeassistant import exceptions
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback, HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from .wemportalapi import WemPortalApi
from .const import (
    DOMAIN,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DEFAULT_MODE,
    AVAILABLE_MODES,
    DEFAULT_CONF_LANGUAGE_VALUE,
    CONF_EXPERT_WRITE,
    EXPERT_SLOT_COUNT,
    MIN_EXPERT_ENTITYVALUE_LENGTH,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_AUTO_POLL,
    CONF_EXPERT_POLL_INTERVAL,
    CONF_EXPERT_NOTIFY_ON_SUCCESS,
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
    CONF_EXPERT_ENABLE_MODULE_NAV,
    CONF_EXPERT_ENABLE_SECURITY_CODE,
    CONF_EXPERT_MODULE_ARG,
    MIN_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_API_SECONDS,
)
from .exceptions import AuthError

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
        vol.Required(CONF_LANGUAGE, default=DEFAULT_CONF_LANGUAGE_VALUE): vol.In(["en", "de"]),
        vol.Optional(CONF_MODE, default=DEFAULT_MODE): vol.In(AVAILABLE_MODES),
    }
)


async def validate_input(hass: HomeAssistant, data):
    """Validate the user input allows us to connect."""
    # Create API object for authentication check
    api = WemPortalApi(data[CONF_USERNAME], data[CONF_PASSWORD])

    try:
        if data[CONF_MODE] in ("api", "both"):
            try:
                await hass.async_add_executor_job(api.api_login)
            except AuthError:
                _LOGGER.warning("Mobile API login failed, trying web login...")
                await hass.async_add_executor_job(api.web_login)
        elif data[CONF_MODE] == "web":
            await hass.async_add_executor_job(api.web_login)
    except AuthError as exc:
        raise InvalidAuth from exc
    except Exception as exc:
        raise CannotConnect from exc

    return data

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
                info = await validate_input(self.hass, user_input)
                for existing_entry in self._async_current_entries(include_ignore=False):
                    if existing_entry.data[CONF_USERNAME] == user_input[CONF_USERNAME]:
                        return self.async_abort(reason="already_configured")

                return self.async_create_entry(
                    title=info[CONF_USERNAME], data=user_input, options={
                        CONF_SCAN_INTERVAL: 1800,
                        CONF_SCAN_INTERVAL_API: 300,
                        CONF_LANGUAGE: user_input.get(CONF_LANGUAGE, DEFAULT_CONF_LANGUAGE_VALUE),
                        CONF_MODE: user_input.get(CONF_MODE, DEFAULT_MODE)
                        }
                )

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
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
            new_data = {**entry.data, **user_input}
            # Validate against the mode the entry actually runs in (options
            # override the value stored at initial setup).
            effective_mode = entry.options.get(
                CONF_MODE, new_data.get(CONF_MODE, DEFAULT_MODE)
            )
            try:
                await validate_input(
                    self.hass, {**new_data, CONF_MODE: effective_mode}
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

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

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}
        if user_input is not None:
            # Validate the ten expert slot IDs on save: an entityvalue must
            # be a plain hex string of a plausible length. Real entityvalues
            # are long (the known ones are 36 hex chars); a short entry like
            # "0" or "abc" is a stray value/typo, not a real ID, and would
            # only cause a pointless failing portal request later. We require
            # hex AND a minimum length, kept well below the observed 36 so a
            # slightly different length on another installation still passes.
            # Whitespace is stripped; empty stays allowed (slot unused).
            min_len = MIN_EXPERT_ENTITYVALUE_LENGTH
            for i in range(1, EXPERT_SLOT_COUNT + 1):
                id_key = CONF_EXPERT_SLOT_ID_TEMPLATE % i
                raw = (user_input.get(id_key) or "").strip()
                user_input[id_key] = raw  # persist the stripped value
                if raw and (
                    not re.fullmatch(r"[0-9A-Fa-f]+", raw) or len(raw) < min_len
                ):
                    errors[id_key] = "invalid_entityvalue"
            # The module menu index feeds an ASP.NET postback argument and a
            # ClientState JSON template verbatim - restrict it to digits so
            # a typo (or stray JSON) is caught in the form instead of being
            # sent to the portal. Empty stays allowed (= use the default).
            module_arg = (user_input.get(CONF_EXPERT_MODULE_ARG) or "").strip()
            user_input[CONF_EXPERT_MODULE_ARG] = module_arg
            if module_arg and not module_arg.isdigit():
                errors[CONF_EXPERT_MODULE_ARG] = "invalid_module_arg"
            if not errors:
                # No-op guard: writing a new options entry always triggers a
                # full integration reload (and a fresh portal login). If the
                # normalized input is identical to the stored options -
                # e.g. the user opened the dialog and saved without changes,
                # or only typed whitespace into an already-empty ID field -
                # skip the write so we don't reload for nothing. Reloading
                # needlessly also risks the portal's 403 rate limit.
                current = dict(self.config_entry.options)
                merged = {**current, **user_input}
                if merged == current:
                    return self.async_abort(reason="no_changes")
                return self.async_create_entry(title="", data=user_input)

        # On an error redisplay, prefill the form with what the user just
        # typed (so nothing has to be re-entered); otherwise with the
        # stored options.
        source = user_input if user_input is not None else self.config_entry.options

        def opt(key, fallback):
            # Local helper (previously a lambda stored on self): read an
            # option value with a fallback - from the just-submitted input on
            # an error redisplay, or the stored options otherwise.
            return source.get(key, fallback)

        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    # Both scan intervals are clamped to a lower bound (like
                    # the expert poll interval below): a stray tiny value
                    # such as "1" second would poll the portal continuously
                    # and reliably trigger the IP-wide 403 rate limit.
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=opt(CONF_SCAN_INTERVAL, 1800),
                    ): vol.All(
                        cv.positive_int,
                        vol.Clamp(min=MIN_SCAN_INTERVAL_SECONDS),
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL_API,
                        default=opt(CONF_SCAN_INTERVAL_API, 300
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
                        default=opt(CONF_LANGUAGE, "en"),
                    ): vol.In(["en", "de"]),

                    vol.Optional(
                        CONF_MODE, default=opt(CONF_MODE, DEFAULT_MODE)
                        ): vol.In(AVAILABLE_MODES),
                    # Expert write access (web) - off by default. Entities/
                    # service only exist while this is enabled.
                    vol.Optional(
                        CONF_EXPERT_WRITE,
                        default=opt(CONF_EXPERT_WRITE, False),
                    ): cv.boolean,
                    # Post a persistent notification after a SUCCESSFUL expert
                    # write. OFF by default (noisy when setting several
                    # values); failures always notify regardless.
                    vol.Optional(
                        CONF_EXPERT_NOTIFY_ON_SUCCESS,
                        default=opt(CONF_EXPERT_NOTIFY_ON_SUCCESS, False
                        ),
                    ): cv.boolean,
                    # Optional periodic read-back of the configured expert
                    # parameters - OFF by default (each read is a full
                    # Fachmann navigation; frequent polling risks a 403 IP
                    # block). The interval is in minutes and floored at
                    # MIN_EXPERT_POLL_INTERVAL_MINUTES.
                    vol.Optional(
                        CONF_EXPERT_AUTO_POLL,
                        default=opt(CONF_EXPERT_AUTO_POLL, False),
                    ): cv.boolean,
                    vol.Optional(
                        CONF_EXPERT_POLL_INTERVAL,
                        default=opt(CONF_EXPERT_POLL_INTERVAL, DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
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
                        default=opt(CONF_EXPERT_ENABLE_MODULE_NAV, False
                        ),
                    ): cv.boolean,
                    # Module menu index used ONLY when module select is
                    # enabled above. Empty default; "6" = heat pump on the
                    # reference install.
                    vol.Optional(
                        CONF_EXPERT_MODULE_ARG,
                        default=opt(CONF_EXPERT_MODULE_ARG, ""
                        ),
                    ): cv.string,
                    vol.Optional(
                        CONF_EXPERT_ENABLE_SECURITY_CODE,
                        default=opt(CONF_EXPERT_ENABLE_SECURITY_CODE, False
                        ),
                    ): cv.boolean,
                    # Ten generic expert-parameter slots (name + entityvalue
                    # hex ID). Added programmatically below so the block stays
                    # compact. Empty slots are ignored.
                    **self._expert_slot_schema(opt),
                }
            ),
        )

    def _expert_slot_schema(self, opt):
        """Build the vol schema fields for the ten generic expert slots.

        Each slot is a name field and an entityvalue-id field, both optional.
        Prefilled values use `suggested_value` (via description), NOT
        `default`: with `default`, a field the user clears on save falls
        back to the stored value, making it impossible to delete a value
        (e.g. a stray "0" left in a slot). `suggested_value` shows the
        current value but lets an emptied field stay empty. The suggested
        value prefers just-submitted input on an error redisplay so a
        validation error never wipes what the user typed.
        """
        fields = {}
        for i in range(1, EXPERT_SLOT_COUNT + 1):
            name_key = CONF_EXPERT_SLOT_NAME_TEMPLATE % i
            id_key = CONF_EXPERT_SLOT_ID_TEMPLATE % i
            fields[
                vol.Optional(
                    name_key,
                    description={"suggested_value": opt(name_key, "")},
                )
            ] = cv.string
            fields[
                vol.Optional(
                    id_key,
                    description={"suggested_value": opt(id_key, "")},
                )
            ] = cv.string
        return fields

