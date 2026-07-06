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
import random

from curl_cffi import requests
from lxml import html

from .exceptions import AuthError, ForbiddenError, ParameterWriteError
from .const import (
    _LOGGER,
    WEB_LOGIN_URL,
    WEB_MAIN_URL,
    WEB_DEFAULT_URL,
    WEB_CODE_EXPERTS_URL,
    EXPERT_VIEWSTATE_FIELDS,
    EXPERT_ASYNCPOST_FIELD,
    EXPERT_SKIP_MODULE_NAV,
    SCRAPER_REQUEST_TIMEOUT_SECONDS,
    EXPERT_SUBMENU_TARGET,
    EXPERT_SUBMENU_ARG,
    EXPERT_DIALOG_SAVE_TARGET,
    EXPERT_SECURITY_CODE_FIELD,
    EXPERT_SECURITY_CODE,
    EXPERT_DIALOG_RADAJAX_ID,
    EXPERT_DIALOG_TSM_FIELD,
    EXPERT_DIALOG_TSM_VALUE,
    EXPERT_DIALOG_RTS_STATE_FIELD,
    EXPERT_DIALOG_RTS_STATE_VALUE,
    EXPERT_PAGE_TSM_FIELD,
    EXPERT_PAGE_TSM_ID_FIELD,
    EXPERT_PAGE_TSM_VALUE,
    EXPERT_PAGE_TSM_PANEL_BY_TARGET,
    EXPERT_RAM_MASTER_TARGET,
    EXPERT_RAM_MASTER_RADAJAX_ID,
    EXPERT_RAM_MASTER_TSM_VALUE,
    EXPERT_RAM_MASTER_UNLOCK_ARGUMENT,
    EXPERT_MODULE_ICONMENU_STATE_FIELD,
    EXPERT_MODULE_ICONMENU_STATE_TEMPLATE,
    EXPERT_MODULE_MENU_TARGET,
    EXPERT_MODULE_ARG_HEATPUMP,
    EXPERT_TIMER_TARGET,
    EXPERT_TIMER_MAX_POLLS,
    EXPERT_TIMER_DELAY_SECONDS,
    EXPERT_TIMER_SETTLE_SECONDS,
    EXPERT_FORM_MAX_ATTEMPTS,
    EXPERT_FORM_RETRY_DELAY_SECONDS,
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

    def __init__(self, username, password, cooldown_check=None, module_arg=None):
        self.username = username
        self.password = password
        # Optional callable raising ForbiddenError while a 403 cooldown is
        # active (shared protection with the rest of the integration).
        self._cooldown_check = cooldown_check
        # Icon-menu argument selecting the target module; defaults to the
        # heat pump index of the reference installation but is overridable
        # for other module layouts.
        self._module_arg = module_arg or EXPERT_MODULE_ARG_HEATPUMP
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

        self._establish_context()

    def _establish_context(self):
        """Reproduce the browser navigation that unlocks the Fachmann view.

        Reconstructed from a real browser HAR capture. A fresh login only
        reaches the user level; the Fachmann parameters (e.g.
        Leistungsbegrenzung) require, in order:
          1. load the portal main page (Default.aspx),
          2. unlock the Fachmann level via a security-code dialog (code
             "11", publicly known),
          3. select the target device module (heat pump),
          4. poll the live-value timer a few times until values arrive.
        Only after this does the parameter edit dialog return a populated
        value dropdown. This is inherently heavier than the API path and
        runs solely on explicit, on-demand write operations.
        """
        # Step 1: main page (also captures the base VIEWSTATE we need).
        r_main = self.session.get(
            WEB_MAIN_URL, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS
        )
        self._raise_if_forbidden(r_main)
        if WEB_LOGIN_URL.lower() in r_main.url.lower():
            raise AuthError("Expert client: session not accepted by portal main page.")
        current_html = r_main.text
        _LOGGER.debug(
            "Expert navigation step 1 (main page): %d bytes, pagestate=%s",
            len(current_html), self._has_viewstate(self._hidden_fields(current_html)),
        )

        # Step 2: unlock Fachmann level. The submenu is a classic full
        # postback (302 -> reloaded Default.aspx), not an async one; then
        # the security-code dialog appears and posting "11" unlocks it.
        current_html = self._postback(
            WEB_DEFAULT_URL, current_html,
            event_target=EXPERT_SUBMENU_TARGET, event_argument=EXPERT_SUBMENU_ARG,
            async_postback=False,
        )
        self._submit_security_code()
        # The real browser does NOT reload the main page here (confirmed via
        # HAR: no GET Default.aspx appears at all between the security-code
        # POST and the module select). Instead, the closing dialog fires a
        # RadAjaxManager client callback on the PARENT page
        # (__EVENTTARGET=ctl00$RAMMasterPage, Function="columns") - this is
        # what actually registers the unlock server-side; a plain reload
        # carries no such signal and leaves the unlock inert (which is why
        # the previous approach never got past an empty parameter dropdown).
        # The dialog runs in its own independent ViewState/ScriptManager
        # context (plain __VIEWSTATE, "TSMeControlNetDialog"), so this
        # callback must carry forward the PARENT page's own prior state
        # (from the submenu postback above), not the dialog's response.
        current_html = self._postback(
            WEB_DEFAULT_URL, current_html,
            event_target=EXPERT_RAM_MASTER_TARGET,
            event_argument=EXPERT_RAM_MASTER_UNLOCK_ARGUMENT,
            extra_fields={
                "RadAJAXControlID": EXPERT_RAM_MASTER_RADAJAX_ID,
                EXPERT_PAGE_TSM_FIELD: EXPERT_RAM_MASTER_TSM_VALUE,
                EXPERT_PAGE_TSM_ID_FIELD: EXPERT_PAGE_TSM_VALUE,
            },
        )
        _LOGGER.debug(
            "Expert navigation step 2 (Fachmann unlock) done via RAMMasterPage "
            "callback: %d bytes, pagestate=%s",
            len(current_html), self._has_viewstate(self._hidden_fields(current_html)),
        )

        if EXPERT_SKIP_MODULE_NAV:
            # Hybrid path under test: the Fachmann unlock alone may be
            # enough for the parameter dialog to return populated. Skip the
            # module-select + timer-poll postbacks (which need many
            # JS-generated _ClientState fields) and let _fetch_form() below
            # (with its retries) fetch the dialog directly.
            _LOGGER.debug(
                "Expert navigation: skipping module/timer postbacks "
                "(EXPERT_SKIP_MODULE_NAV); fetching dialog directly."
            )
            time.sleep(EXPERT_TIMER_SETTLE_SECONDS)
            return

        # Step 3: select the target module via its icon-menu async postback.
        # Besides the postback event itself, the icon-menu control's own
        # client state must reflect the selection - otherwise the server
        # accepts the postback (real response, valid page state) but
        # doesn't register "module N selected" for the session, leaving
        # the parameter dialog empty afterwards.
        icon_menu_state = EXPERT_MODULE_ICONMENU_STATE_TEMPLATE % self._module_arg
        current_html = self._postback(
            WEB_DEFAULT_URL, current_html,
            event_target=EXPERT_MODULE_MENU_TARGET,
            event_argument=self._module_arg,
            extra_fields={EXPERT_MODULE_ICONMENU_STATE_FIELD: icon_menu_state},
        )
        _LOGGER.debug("Expert navigation step 3 (module select, arg=%s) done.", self._module_arg)

        # Step 4: poll the live-value timer. The browser polls repeatedly
        # with no explicit "done" signal, so we poll generously. We favor
        # reliability over speed here (see const.py): the real early-exit
        # is that _fetch_form() retries until the dropdown is populated.
        for poll in range(EXPERT_TIMER_MAX_POLLS):
            time.sleep(EXPERT_TIMER_DELAY_SECONDS)
            self._check_cooldown()
            current_html = self._postback(
                WEB_DEFAULT_URL, current_html,
                event_target=EXPERT_TIMER_TARGET, event_argument="",
            )
            _LOGGER.debug("Expert navigation step 4: timer poll %d/%d done.",
                          poll + 1, EXPERT_TIMER_MAX_POLLS)
        # Extra settle pause before the first dialog fetch, giving the
        # server a moment to finish applying the freshly polled values.
        time.sleep(EXPERT_TIMER_SETTLE_SECONDS)

    def _submit_security_code(self):
        """Post the Fachmann security code to the code-experts dialog.

        This is a Telerik RadAjax async postback (confirmed via HAR): it
        needs __ASYNCPOST=true plus the RadAjax control id, the
        ScriptManager target and the dialog's RadTabStrip client state, on
        top of the page's hidden fields (VIEWSTATE/EVENTVALIDATION etc.).
        """
        # The dialog is a RadWindow served from its own URL; fetch it to
        # get its VIEWSTATE, then post the code via the dialog's save button.
        dialog_url = f"{WEB_CODE_EXPERTS_URL}?rwndrnd={random.random()}"
        r = self.session.get(dialog_url, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS)
        self._raise_if_forbidden(r)
        fields = self._hidden_fields(r.text)
        _LOGGER.debug(
            "Expert navigation: security-code dialog fetched, %d hidden fields, pagestate=%s",
            len(fields), self._has_viewstate(fields),
        )
        fields[EXPERT_SECURITY_CODE_FIELD] = EXPERT_SECURITY_CODE
        fields["__EVENTTARGET"] = EXPERT_DIALOG_SAVE_TARGET
        fields["__EVENTARGUMENT"] = ""
        # RadAjax async-postback fields the server requires for this dialog.
        fields[EXPERT_ASYNCPOST_FIELD] = "true"
        fields["RadAJAXControlID"] = EXPERT_DIALOG_RADAJAX_ID
        fields[EXPERT_DIALOG_TSM_FIELD] = EXPERT_DIALOG_TSM_VALUE
        fields[EXPERT_DIALOG_RTS_STATE_FIELD] = EXPERT_DIALOG_RTS_STATE_VALUE
        self._check_cooldown()
        r2 = self.session.post(
            dialog_url, data=fields, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers={"X-MicrosoftAjax": "Delta=true"},
        )
        self._raise_if_forbidden(r2)
        _LOGGER.debug(
            "Expert navigation: security-code POST -> %d bytes, delta=%s",
            len(r2.text), "|hiddenField|" in r2.text,
        )

    # --- ASP.NET postback helpers ------------------------------------
    @staticmethod
    def _has_viewstate(fields) -> bool:
        """True if the field dict carries a non-empty page state field.

        The portal's main pages use __ECNPAGEVIEWSTATE, dialog pages use
        __VIEWSTATE - accept either as the state field.
        """
        return any(fields.get(name) for name in EXPERT_VIEWSTATE_FIELDS)

    @staticmethod
    def _hidden_fields(content) -> dict:
        """Extract hidden fields (VIEWSTATE, EVENTVALIDATION, ...).

        Handles both response shapes:
        - a normal HTML page (input[type=hidden]), and
        - a Telerik/MS-Ajax async-postback delta response, which is NOT
          HTML but a pipe-delimited stream containing e.g.
          `...|hiddenField|__VIEWSTATE|<value>|...`. A plain HTML parser
          finds nothing there, which would silently forward an empty
          VIEWSTATE and break the navigation chain.
        """
        fields = {}
        # Delta response: pipe-delimited, carries hiddenField segments.
        if "|hiddenField|" in content:
            parts = content.split("|")
            for i, token in enumerate(parts):
                if token == "hiddenField" and i + 2 < len(parts):
                    fields[parts[i + 1]] = parts[i + 2]
            if fields:
                return fields
        # Otherwise parse as HTML.
        try:
            tree = html.fromstring(content)
            for inp in tree.xpath("//input[@type='hidden']"):
                name = inp.get("name")
                if name:
                    fields[name] = inp.get("value", "")
        except Exception:  # pylint: disable=broad-except
            pass
        return fields

    def _postback(self, url, current_html, event_target, event_argument="",
                  async_postback=True, extra_fields=None):
        """Perform one ASP.NET postback, carrying over the current page's
        hidden fields, and return the resulting page HTML for the next step.

        Two shapes exist in this portal's navigation (confirmed via HAR):
        - async_postback=True: a Telerik RadAjax async postback. Sends
          __ASYNCPOST=true in the body plus the X-MicrosoftAjax:Delta=true
          header; the response is a delta stream. Used for module select
          and the timer polls.
        - async_postback=False: a classic full postback that ends in a 302
          redirect to the reloaded page. No async field, no async header,
          follow the redirect. Used for the submenu (Fachmann) unlock.

        extra_fields lets a caller add postback-specific fields (e.g. a
        control's own client state) on top of the standard ones.
        """
        fields = self._hidden_fields(current_html)
        # Diagnostics: if the carried-over page state is missing/empty the
        # server won't advance the session state, and the chain fails
        # silently. Surface that instead.
        if not self._has_viewstate(fields):
            _LOGGER.debug(
                "Expert navigation: no page state field to carry into postback %s "
                "(previous response had %d hidden fields).",
                event_target, len(fields),
            )
        fields["__EVENTTARGET"] = event_target
        fields["__EVENTARGUMENT"] = event_argument
        if extra_fields:
            fields.update(extra_fields)

        self._check_cooldown()
        if async_postback:
            # Telerik async postback: marker field + header, response is a
            # delta stream we keep parsing for the next state.
            fields[EXPERT_ASYNCPOST_FIELD] = "true"
            # Main-page async postbacks (module select, timer polls) also
            # need the ScriptManager field identifying which panel posted
            # back, plus its static TSM version blob. Only add this for
            # known targets - the dialog postbacks use a different
            # ScriptManager field (see _submit_security_code) and don't
            # need this one.
            panel = EXPERT_PAGE_TSM_PANEL_BY_TARGET.get(event_target)
            if panel is not None:
                fields[EXPERT_PAGE_TSM_FIELD] = f"{panel}|{event_target}"
                fields[EXPERT_PAGE_TSM_ID_FIELD] = EXPERT_PAGE_TSM_VALUE
            resp = self.session.post(
                url, data=fields, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                headers={"X-MicrosoftAjax": "Delta=true"},
            )
        else:
            # Full postback ending in a 302 -> follow it to the reloaded
            # page, whose HTML carries the fresh state for the next step.
            resp = self.session.post(
                url, data=fields, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
        self._raise_if_forbidden(resp)
        if WEB_LOGIN_URL.lower() in resp.url.lower():
            raise AuthError("Expert client: session expired during navigation.")
        _LOGGER.debug(
            "Expert navigation: postback %s (async=%s) -> %d bytes, delta=%s, pagestate=%s",
            event_target, async_postback, len(resp.text),
            "|hiddenField|" in resp.text,
            self._has_viewstate(self._hidden_fields(resp.text)),
        )
        return resp.text

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
            # Distinguish "got the login page instead" from a genuinely
            # changed/unknown dialog structure.
            if "tbxUserName" in html_content or "Login.aspx" in html_content:
                raise AuthError(
                    "Expert parameter form: portal returned the login page - "
                    "session was not authenticated."
                )
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
            # Dropdown present but empty: the session has no active
            # installation context (see _establish_context) or the
            # parameter could not be resolved for this entityvalue.
            _LOGGER.debug(
                "Expert parameter form with empty dropdown, response snippet: %.500s",
                html_content,
            )
            raise ValueError(
                "Expert parameter form: value dropdown is empty - the portal "
                "session has no active installation context, or the "
                "entityvalue does not match a readable parameter."
            )

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
            # that postback, carrying over all hidden ASP.NET fields. The
            # portal sends this as a Telerik async postback (see the
            # X-MicrosoftAjax header and rwndrnd cache-buster in the HAR).
            post_data = dict(state.hidden_fields)
            post_data["__EVENTTARGET"] = "ctl00$DialogContent$BtnSave"
            post_data["__EVENTARGUMENT"] = ""
            post_data["ctl00$DialogContent$ddlNewValue"] = value_str

            self._check_cooldown()
            resp = self.session.post(
                EXPERT_PARAMETER_URL,
                params={"entityvalue": entityvalue, "readdata": "True",
                        "rwndrnd": str(random.random())},
                data=post_data,
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                headers={"X-MicrosoftAjax": "Delta=true"},
            )
            self._raise_if_forbidden(resp)

            # Verify by re-reading the form: the device/portal must now
            # report the new value as selected. The value is applied
            # immediately, so a short retry budget is enough here (unlike
            # the initial read, where live values may still be loading).
            verify = self._fetch_form(entityvalue, max_attempts=2)
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

    def _fetch_form(self, entityvalue: str, max_attempts: int = None) -> ExpertParameterState:
        """GET + parse the edit form on the already logged-in session.

        Retries on an empty dropdown: after selecting the module the live
        values can still be trickling in, so a first fetch may legitimately
        come back empty. Rather than failing immediately we retry a few
        times with a pause, favoring reliability over speed (these are
        rare, on-demand operations). Only after all attempts still yield an
        empty dropdown do we raise.

        max_attempts defaults to EXPERT_FORM_MAX_ATTEMPTS (initial read,
        where values may still be loading). The post-write verify passes a
        smaller value, since the value is applied immediately and no long
        wait is warranted there.
        """
        if max_attempts is None:
            max_attempts = EXPERT_FORM_MAX_ATTEMPTS
        last_error = None
        for attempt in range(max_attempts):
            self._check_cooldown()
            resp = self.session.get(
                EXPERT_PARAMETER_URL,
                params={"entityvalue": entityvalue, "readdata": "True",
                        "rwndrnd": str(random.random())},
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            )
            self._raise_if_forbidden(resp)
            if WEB_LOGIN_URL.lower() in resp.url.lower():
                raise AuthError("Expert client: redirected to login when fetching the form.")
            try:
                state = self.parse_parameter_form(resp.text)
                _LOGGER.debug(
                    "Expert parameter %s: current=%s range=%s..%s (attempt %d)",
                    entityvalue, state.current, state.min_value, state.max_value,
                    attempt + 1,
                )
                return state
            except ValueError as exc:
                # Empty dropdown / values not ready yet - wait and retry.
                # (parse_parameter_form raises AuthError for a login page,
                # which we deliberately do NOT swallow here.)
                last_error = exc
                _LOGGER.debug(
                    "Expert parameter %s not ready on attempt %d/%d: %s",
                    entityvalue, attempt + 1, max_attempts, exc,
                )
                if attempt < max_attempts - 1:
                    time.sleep(EXPERT_FORM_RETRY_DELAY_SECONDS)
        raise last_error


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
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.entity import DeviceInfo
    from .const import DOMAIN

    class WemPortalExpertNumber(RestoreNumber):
        """Writable expert parameter as a number entity.

        Value updates only on writes (the verified post-write state) or
        restore after restart - by design no periodic polling.
        """

        _attr_should_poll = False
        _attr_has_entity_name = True
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
            # `name` is the technical slug (e.g. wp_leistungsbegrenzung_heizen);
            # keep it as the stable object_id source but show a readable
            # friendly name, consistent with has_entity_name on the other
            # platforms.
            self._attr_name = name.replace("_", " ").title()
            self._attr_translation_key = name
            self._attr_unique_id = f"{config_entry.entry_id}:expert:{entityvalue}"
            self._attr_native_value = None
            # Guards against starting a second write while one is still
            # running in the background (the write takes ~60-80s).
            self._write_in_progress = False

        async def async_added_to_hass(self):
            """Restore the last known value after a restart."""
            await super().async_added_to_hass()
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value

        async def async_set_native_value(self, value: float) -> None:
            """Start the write in the background and return immediately.

            An expert write reproduces the full Fachmann navigation and
            takes ~60-80s - far longer than a frontend service call will
            wait. Running it as a background task lets the call return at
            once; the outcome is reported via a persistent notification
            and the log. The entity value updates once the write is
            verified.
            """
            if self._write_in_progress:
                raise HomeAssistantError(
                    f"{self._attr_name}: a write is already in progress, please wait."
                )
            self._write_in_progress = True
            self.hass.async_create_background_task(
                self._async_write_in_background(value),
                name=f"wemportal_expert_write_{self._attr_unique_id}",
            )

        async def _async_write_in_background(self, value: float) -> None:
            """Perform the actual (slow) write off the service-call path."""
            from .const import CONF_EXPERT_MODULE_ARG

            module_arg = (self._config_entry.options.get(CONF_EXPERT_MODULE_ARG) or "").strip() or None

            def _do_write():
                client = WemPortalExpertClient(
                    self._config_entry.data.get(CONF_USERNAME),
                    self._config_entry.data.get(CONF_PASSWORD),
                    cooldown_check=self._cooldown_check(),
                    module_arg=module_arg,
                )
                return client.write_parameter(self._entityvalue, value)

            try:
                state = await self.hass.async_add_executor_job(_do_write)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.error("Expert write failed for %s: %s", self._attr_name, exc)
                self._notify(
                    f"Setting {self._attr_name} to {value} failed: {exc}",
                    success=False,
                )
                return
            finally:
                self._write_in_progress = False

            # Verified value from the portal, plus the real device range.
            self._attr_native_value = state.current
            if state.min_value is not None:
                self._attr_native_min_value = state.min_value
            if state.max_value is not None:
                self._attr_native_max_value = state.max_value
            self.async_write_ha_state()
            _LOGGER.info(
                "Expert parameter %s set and verified: %s", self._attr_name, state.current
            )
            self._notify(f"{self._attr_name} set to {state.current}.", success=True)

        def _notify(self, message: str, success: bool) -> None:
            """Report the background write outcome via a persistent notification."""
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "WEM Portal expert write"
                        + ("" if success else " failed"),
                        "message": message,
                        "notification_id": f"wemportal_expert_{self._attr_unique_id}",
                    },
                    blocking=False,
                )
            )

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
