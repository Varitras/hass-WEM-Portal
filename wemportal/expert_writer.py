"""Expert parameter access via the WEM Portal web frontend.

Standalone module, deliberately separate from scraper.py/wemportalapi.py:
it covers parameters that exist ONLY in the web Fachmann view and are not
exposed by the mobile API at all (e.g. the heat pump's "Leistungsbegrenzung").

Read and write happen on demand only (a few requests per invocation on a
short-lived session) - never periodically. Writing validates the new value
against the live option list from the freshly fetched edit form and
verifies the result by re-reading the form afterwards.
"""

import re
import time

from curl_cffi import requests
from lxml import html

from .exceptions import AuthError, ForbiddenError, ParameterWriteError
from .const import (
    _LOGGER,
    WEB_LOGIN_URL,
    SCRAPER_REQUEST_TIMEOUT_SECONDS,
)

# Edit dialog endpoint; entityvalue identifies device/module/parameter.
EXPERT_PARAMETER_URL = (
    "https://www.wemportal.com/Web/UControls/Weishaupt/DataDisplay/"
    "WwpsParameterDetails.aspx"
)

# Form field carrying the value in the edit dialog.
VALUE_FIELD_ID = "ctl00_DialogContent_ddlNewValue"


class ExpertParameterState:
    """Parsed state of one expert parameter's edit form."""

    def __init__(self, current, options, hidden_fields):
        self.current = current            # currently selected value (float)
        self.options = options            # all allowed values (list of float)
        self.min_value = min(options) if options else None
        self.max_value = max(options) if options else None
        # Hidden ASP.NET fields (VIEWSTATE etc.), kept for a later write step.
        self.hidden_fields = hidden_fields


class WemPortalExpertClient:
    """On-demand web client for reading expert parameters.

    Uses its own HTTP session, created per operation and closed afterwards -
    fully independent of the polling scraper/API paths.
    """

    def __init__(self, username, password, cooldown_check=None):
        self.username = username
        self.password = password
        # Optional callable raising ForbiddenError while a 403 cooldown is
        # active (shared protection with the rest of the integration).
        self._cooldown_check = cooldown_check
        self.session = None

    # ------------------------------------------------------------------
    def _check_cooldown(self):
        if self._cooldown_check is not None:
            self._cooldown_check()

    def _raise_if_forbidden(self, response):
        if response.status_code == 403:
            raise ForbiddenError(
                "WEM Portal web frontend returned 403 during expert parameter access."
            )

    # ------------------------------------------------------------------
    def _login(self):
        """Perform a fresh web login on a new session."""
        self.session = requests.Session(impersonate="chrome110")

        r1 = self.session.get(WEB_LOGIN_URL, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS)
        self._raise_if_forbidden(r1)
        tree = html.fromstring(r1.text)
        viewstate = tree.xpath("//*[@id='__VIEWSTATE']/@value")
        eventval = tree.xpath("//*[@id='__EVENTVALIDATION']/@value")
        if not viewstate or not eventval:
            raise AuthError("Expert client: login form fields not found.")

        login_data = {
            "__VIEWSTATE": viewstate[0],
            "__EVENTVALIDATION": eventval[0],
            "ctl00$content$tbxUserName": self.username,
            "ctl00$content$tbxPassword": self.password,
            "ctl00$content$btnLogin": "Anmelden",
        }
        r2 = self.session.post(
            WEB_LOGIN_URL, data=login_data, allow_redirects=True,
            timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
        )
        self._raise_if_forbidden(r2)
        # Redirect back to login page means the login did not succeed.
        if "AspxAutoDetectCookieSupport" in r2.url or WEB_LOGIN_URL.lower() in r2.url.lower():
            raise AuthError("Expert client: login failed.")

    def close(self):
        """Close the session; never raises."""
        if self.session is not None:
            try:
                self.session.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self.session = None

    # ------------------------------------------------------------------
    @staticmethod
    def parse_parameter_form(html_content) -> ExpertParameterState:
        """Parse current value, allowed options and hidden fields from the
        edit dialog HTML. Static so it can be tested against saved pages."""
        tree = html.fromstring(html_content)

        select = tree.xpath(f"//*[@id='{VALUE_FIELD_ID}']")
        if not select:
            raise ValueError("Expert parameter form: value field not found.")

        options = []
        current = None
        for opt in select[0].xpath(".//option"):
            raw = (opt.get("value") or "").strip()
            try:
                val = float(raw.replace(",", "."))
            except ValueError:
                continue
            options.append(val)
            if opt.get("selected") is not None:
                current = val

        if not options:
            raise ValueError("Expert parameter form: no numeric options found.")

        # Hidden ASP.NET fields, needed later for the (not yet built) write POST.
        hidden_fields = {}
        for inp in tree.xpath("//input[@type='hidden']"):
            name = inp.get("name")
            if name:
                hidden_fields[name] = inp.get("value", "")

        return ExpertParameterState(current, options, hidden_fields)

    # ------------------------------------------------------------------
    def read_parameter(self, entityvalue: str) -> ExpertParameterState:
        """Login, fetch one parameter's edit form, parse it, close session.

        Total server load: 3 requests (login page, login POST, form GET),
        only when explicitly invoked - never periodically.
        """
        self._validate_entityvalue(entityvalue)
        self._check_cooldown()
        try:
            self._login()
            return self._fetch_form(entityvalue)
        finally:
            self.close()

    def write_parameter(self, entityvalue: str, value) -> ExpertParameterState:
        """Login, set a new value via the edit form, verify, close session.

        Flow on one short-lived session (~5 requests total):
          1. GET the edit form (readdata=True) -> current state + hidden fields
          2. Validate `value` against the form's own option list (the
             device's real allowed range - never bypassed)
          3. POST the ASP.NET postback for the Senden button
          4. GET the form again and confirm the new value is now selected

        Returns the verified post-write state. Raises ParameterWriteError
        if the server did not accept the value.
        """
        self._validate_entityvalue(entityvalue)
        self._check_cooldown()
        try:
            self._login()
            state = self._fetch_form(entityvalue)

            # Validate against the live option list; option values are the
            # exact strings the server expects back.
            value_f = float(value)
            if value_f not in state.options:
                raise ValueError(
                    f"Value {value} not allowed; device accepts "
                    f"{state.min_value}..{state.max_value} "
                    f"({len(state.options)} discrete options)."
                )
            # Integer-like options are rendered without decimals ("30").
            value_str = str(int(value_f)) if value_f == int(value_f) else str(value_f)

            # The Senden button is type=button and submits via a JS
            # __doPostBack('ctl00$DialogContent$BtnSave', '') - replicate
            # that postback, carrying over all hidden ASP.NET fields.
            post_data = dict(state.hidden_fields)
            post_data["__EVENTTARGET"] = "ctl00$DialogContent$BtnSave"
            post_data["__EVENTARGUMENT"] = ""
            post_data["ctl00$DialogContent$ddlNewValue"] = value_str

            self._check_cooldown()
            resp = self.session.post(
                EXPERT_PARAMETER_URL,
                params={"entityvalue": entityvalue, "readdata": "True"},
                data=post_data,
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            )
            self._raise_if_forbidden(resp)

            # Verify by re-reading the form: the device/portal must now
            # report the new value as selected.
            verify = self._fetch_form(entityvalue)
            if verify.current != value_f:
                raise ParameterWriteError(
                    f"Write not confirmed: form still shows {verify.current}, "
                    f"expected {value_f}. The portal may have rejected the value."
                )
            _LOGGER.info(
                "Expert parameter %s written and verified: %s", entityvalue, value_f
            )
            return verify
        finally:
            self.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_entityvalue(entityvalue: str):
        if not re.fullmatch(r"[0-9A-Fa-f]+", entityvalue or ""):
            raise ValueError(f"Invalid entityvalue: {entityvalue!r}")

    def _fetch_form(self, entityvalue: str) -> ExpertParameterState:
        """GET + parse the edit form on the already logged-in session."""
        self._check_cooldown()
        resp = self.session.get(
            EXPERT_PARAMETER_URL,
            params={"entityvalue": entityvalue, "readdata": "True"},
            timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
        )
        self._raise_if_forbidden(resp)
        state = self.parse_parameter_form(resp.text)
        _LOGGER.debug(
            "Expert parameter %s: current=%s range=%s..%s",
            entityvalue, state.current, state.min_value, state.max_value,
        )
        return state


def create_expert_number_entities(config_entry):
    """Build the configured expert number entities (comfort layer on top
    of the write service). Imported lazily by number.py's setup so this
    module stays out of the load path while the option is disabled."""
    from .const import (
        CONF_EXPERT_WRITE,
        CONF_EXPERT_ENTITY_HEATING,
        CONF_EXPERT_ENTITY_COOLING,
    )

    if not config_entry.options.get(CONF_EXPERT_WRITE, False):
        return []

    entities = []
    for option_key, name in (
        (CONF_EXPERT_ENTITY_HEATING, "wp_leistungsbegrenzung_heizen"),
        (CONF_EXPERT_ENTITY_COOLING, "wp_leistungsbegrenzung_kuehlen"),
    ):
        entityvalue = (config_entry.options.get(option_key) or "").strip()
        if entityvalue:
            # Guard for non-HA contexts (e.g. unit tests without the HA
            # package): the entity class below only exists if the HA
            # imports succeeded.
            if "WemPortalExpertNumber" not in globals():
                _LOGGER.error("Expert number entities unavailable: HA imports missing.")
                return []
            entities.append(WemPortalExpertNumber(config_entry, name, entityvalue))
    return entities


# HA imports are only needed for the entity class below; kept at the end
# so plain use of the client (and its tests) needs no HA installed.
try:
    from homeassistant.components.number import RestoreNumber
    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
    from homeassistant.helpers.entity import DeviceInfo
    from .const import DOMAIN

    class WemPortalExpertNumber(RestoreNumber):
        """Writable expert parameter as a number entity.

        Value updates only on writes (the verified post-write state) or
        restore after restart - by design no periodic polling.
        """

        _attr_should_poll = False
        _attr_native_unit_of_measurement = "%"
        _attr_native_step = 1
        # Display bounds; the real device range is enforced live in
        # write_parameter() against the form's option list.
        _attr_native_min_value = 0
        _attr_native_max_value = 100
        _attr_icon = "mdi:speedometer"

        def __init__(self, config_entry, name, entityvalue):
            self._config_entry = config_entry
            self._entityvalue = entityvalue
            self._attr_name = name
            self._attr_unique_id = f"{config_entry.entry_id}:expert:{entityvalue}"
            self._attr_native_value = None

        async def async_added_to_hass(self):
            """Restore the last known value after a restart."""
            await super().async_added_to_hass()
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value

        async def async_set_native_value(self, value: float) -> None:
            def _do_write():
                client = WemPortalExpertClient(
                    self._config_entry.data.get(CONF_USERNAME),
                    self._config_entry.data.get(CONF_PASSWORD),
                    cooldown_check=self._cooldown_check(),
                )
                return client.write_parameter(self._entityvalue, value)

            state = await self.hass.async_add_executor_job(_do_write)
            # Verified value from the portal, plus the real device range.
            self._attr_native_value = state.current
            if state.min_value is not None:
                self._attr_native_min_value = state.min_value
            if state.max_value is not None:
                self._attr_native_max_value = state.max_value
            self.async_write_ha_state()

        def _cooldown_check(self):
            """Fetch the shared 403-cooldown check from the running api."""
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
            api = entry_data.get("api") if entry_data else None
            return api._check_cooldown if api is not None else None

        @property
        def device_info(self) -> DeviceInfo:
            return {
                "identifiers": {(DOMAIN, self._config_entry.entry_id)},
                "name": self._config_entry.title or "WEM Portal",
                "manufacturer": "Weishaupt",
            }

except ImportError:  # pragma: no cover - plain client use without HA
    pass
