"""Config flow for wemportal integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)

from homeassistant import exceptions
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback, HomeAssistant
import homeassistant.helpers.config_validation as config_validation
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
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_AUTO_POLL,
    CONF_EXPERT_POLL_INTERVAL,
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
    CONF_EXPERT_ENABLE_MODULE_NAV,
    CONF_EXPERT_ENABLE_SECURITY_CODE,
    CONF_EXPERT_MODULE_ARG,
    EXPERT_MODULE_ARG_HEATPUMP,
)
from .exceptions import AuthError

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
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


class WemportalOptionsFlow(OptionsFlow):
    """Handle options."""

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(CONF_SCAN_INTERVAL, 1800),
                    ): config_validation.positive_int,
                    vol.Optional(
                        CONF_SCAN_INTERVAL_API,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL_API, 300
                        ),
                    ): config_validation.positive_int,
                    vol.Optional(
                        CONF_LANGUAGE,
                        default=self.config_entry.options.get(CONF_LANGUAGE, "en"),
                    ): config_validation.string,
                    
                    vol.Optional(
                        CONF_MODE, default=self.config_entry.options.get(CONF_MODE, DEFAULT_MODE)
                        ): vol.In(AVAILABLE_MODES),
                    # Expert write access (web) - off by default. Entities/
                    # service only exist while this is enabled.
                    vol.Optional(
                        CONF_EXPERT_WRITE,
                        default=self.config_entry.options.get(CONF_EXPERT_WRITE, False),
                    ): config_validation.boolean,
                    # Optional periodic read-back of the configured expert
                    # parameters - OFF by default (each read is a full
                    # Fachmann navigation; frequent polling risks a 403 IP
                    # block). The interval is in minutes and floored at
                    # MIN_EXPERT_POLL_INTERVAL_MINUTES.
                    vol.Optional(
                        CONF_EXPERT_AUTO_POLL,
                        default=self.config_entry.options.get(CONF_EXPERT_AUTO_POLL, False),
                    ): config_validation.boolean,
                    vol.Optional(
                        CONF_EXPERT_POLL_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_EXPERT_POLL_INTERVAL, DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
                        ),
                    ): vol.All(
                        config_validation.positive_int,
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
                        default=self.config_entry.options.get(
                            CONF_EXPERT_ENABLE_MODULE_NAV, False
                        ),
                    ): config_validation.boolean,
                    # Module menu index used ONLY when module select is
                    # enabled above. Empty default; "6" = heat pump on the
                    # reference install.
                    vol.Optional(
                        CONF_EXPERT_MODULE_ARG,
                        default=self.config_entry.options.get(
                            CONF_EXPERT_MODULE_ARG, ""
                        ),
                    ): config_validation.string,
                    vol.Optional(
                        CONF_EXPERT_ENABLE_SECURITY_CODE,
                        default=self.config_entry.options.get(
                            CONF_EXPERT_ENABLE_SECURITY_CODE, False
                        ),
                    ): config_validation.boolean,
                    # Ten generic expert-parameter slots (name + entityvalue
                    # hex ID). Added programmatically below so the block stays
                    # compact. Empty slots are ignored.
                    **self._expert_slot_schema(),
                }
            ),
        )

    def _expert_slot_schema(self):
        """Build the vol schema fields for the ten generic expert slots.

        Each slot is a name field and an entityvalue-id field, both optional
        and defaulting to any previously stored value. Kept as a helper so
        the ten slots don't bloat the main schema literal.
        """
        opts = self.config_entry.options
        fields = {}
        for i in range(1, EXPERT_SLOT_COUNT + 1):
            name_key = CONF_EXPERT_SLOT_NAME_TEMPLATE % i
            id_key = CONF_EXPERT_SLOT_ID_TEMPLATE % i
            fields[
                vol.Optional(name_key, default=opts.get(name_key, ""))
            ] = config_validation.string
            fields[
                vol.Optional(id_key, default=opts.get(id_key, ""))
            ] = config_validation.string
        return fields

