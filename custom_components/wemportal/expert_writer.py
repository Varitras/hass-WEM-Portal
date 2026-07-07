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
    WEB_PORTAL_ORIGIN,
    WEB_ACCEPT_LANGUAGE,
    WEB_ACCEPT_NAV,
    WEB_ACCEPT_AJAX,
    WEB_DEFAULT_URL,
    WEB_CODE_EXPERTS_URL,
    EXPERT_VIEWSTATE_FIELDS,
    EXPERT_ASYNCPOST_FIELD,
    EXPERT_SKIP_MODULE_NAV,
    EXPERT_SKIP_SECURITY_CODE,
    SCRAPER_REQUEST_TIMEOUT_SECONDS,
    EXPERT_SUBMENU_TARGET,
    EXPERT_SUBMENU_ARG,
    EXPERT_SUBMENU_CLIENTSTATE_FIELD,
    EXPERT_SUBMENU_CLIENTSTATE_VALUE,
    EXPERT_DIALOG_SAVE_TARGET,
    EXPERT_SECURITY_CODE_FIELD,
    EXPERT_SECURITY_CODE,
    EXPERT_DIALOG_RADAJAX_ID,
    EXPERT_DIALOG_TSM_FIELD,
    EXPERT_DIALOG_TSM_VALUE,
    EXPERT_DIALOG_TSM_ID_FIELD,
    EXPERT_DIALOG_TSM_ID_VALUE,
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
    EXPERT_RAM_MASTER_REFRESH_BUTTON_FIELD,
    EXPERT_RAM_MASTER_REFRESH_BUTTON_VALUE,
    EXPERT_MODULE_ICONMENU_STATE_FIELD,
    EXPERT_MODULE_ICONMENU_STATE_TEMPLATE,
    EXPERT_MODULE_MENU_TARGET,
    EXPERT_MODULE_ARG_HEATPUMP,
    EXPERT_TIMER_TARGET,
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


def _short_ev(entityvalue: str) -> str:
    """Shortened entityvalue for user-visible log/notification text.

    entityvalues are installation-specific and shouldn't end up verbatim in
    text people copy into issues/forums. Debug-level logs keep the full id
    (needed for troubleshooting); info/warning/error and notifications use
    this shortened form.
    """
    ev = entityvalue or ""
    return f"{ev[:6]}…" if len(ev) > 6 else ev


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

    def __init__(self, username, password, cooldown_check=None, module_arg=None,
                 enable_module_nav=None, enable_security_code=None):
        self.username = username
        self.password = password
        # Optional callable raising ForbiddenError while a 403 cooldown is
        # active (shared protection with the rest of the integration).
        self._cooldown_check = cooldown_check
        # Icon-menu argument selecting the target module; defaults to the
        # heat pump index of the reference installation but is overridable
        # for other module layouts.
        self._module_arg = module_arg or EXPERT_MODULE_ARG_HEATPUMP
        # Per-instance overrides for the two navigation steps that are
        # skipped by default. None -> fall back to the module constants
        # (EXPERT_SKIP_MODULE_NAV / EXPERT_SKIP_SECURITY_CODE). A concrete
        # bool (from the options UI) wins over the constant, so a user can
        # re-enable either step for an unusual portal/module layout. Stored
        # as "do the step?" for readability (inverse of the SKIP_ constants).
        self._do_module_nav = (
            (not EXPERT_SKIP_MODULE_NAV) if enable_module_nav is None
            else bool(enable_module_nav)
        )
        self._do_security_code = (
            (not EXPERT_SKIP_SECURITY_CODE) if enable_security_code is None
            else bool(enable_security_code)
        )
        self.session = None
        # URL the last successfully fetched parameter dialog was served at
        # (including its real rwndrnd) - used as Referer for the following
        # write POST, matching the HAR's "same-page form submit" pattern.
        self._last_dialog_url = None
        # Main-page HTML state left by _establish_context (after module
        # select). _fetch_form uses it to fire an on-demand live-value timer
        # postback only when the dialog still comes back empty - replacing
        # the old fixed pre-poll loop with a demand-driven one (early exit
        # as soon as the dropdown is populated). Updated as polls advance.
        self._nav_html = None
        # Optional hook for standalone debugging (set by wem_debug.py, never
        # used by the real integration): if set, called as
        # hook(step_name, url, fields, session) right before every POST,
        # letting an external tool export the exact computed field values
        # for a manual replay test. Never invoked/needed in normal operation.
        self._export_hook = None

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
        self.session = requests.Session(impersonate="chrome146")

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
        """Reproduce the browser navigation that reaches the Fachmann view.

        Reconstructed from a real browser HAR capture. A fresh login only
        reaches the user level; the Fachmann parameters (e.g.
        Leistungsbegrenzung) require, in order:
          1. load the portal main page (Default.aspx),
          2. switch to the Fachmann submenu - the decisive step is the
             submenu RadMenu ClientState selecting "Fachmann" (index 3);
             this alone puts the session on the Fachmann level (verified by
             a live read AND write). A separate security-code ("11") dialog
             exists and is reproduced by an optional, disabled-by-default
             sub-sequence kept as a safety net (see EXPERT_SKIP_SECURITY_CODE),
             but is not needed while the account's Fachmann access is active,
          3. select the target device module (heat pump),
          4. poll the live-value timer a few times until values arrive.
        Only after this does the parameter edit dialog return a populated
        value dropdown. This is inherently heavier than the API path and
        runs solely on explicit, on-demand read/write operations.
        """
        # Step 1: main page (also captures the base VIEWSTATE we need).
        r_main = self.session.get(
            WEB_MAIN_URL, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers={"Accept": WEB_ACCEPT_NAV, "Accept-Language": WEB_ACCEPT_LANGUAGE},
        )
        self._raise_if_forbidden(r_main)
        if WEB_LOGIN_URL.lower() in r_main.url.lower():
            raise AuthError("Expert client: session not accepted by portal main page.")
        current_html = r_main.text
        _LOGGER.debug(
            "Expert navigation step 1 (main page): %d bytes, pagestate=%s",
            len(current_html), self._has_viewstate(self._hidden_fields(current_html)),
        )

        # Step 2: switch to the Fachmann submenu. Classic full postback
        # (302 -> reloaded Default.aspx), not an async one. The submenu's
        # RadMenu client state must be supplied so the server knows the
        # "Fachmann" item (index 3) is the one being selected - it is a
        # JS-generated field, not a server-rendered hidden input, so
        # _postback's hidden-field carry-over never includes it. Without it
        # the reload returns the plain user level and every later step
        # operates on a non-Fachmann page (confirmed via HAR).
        current_html = self._postback(
            WEB_DEFAULT_URL, current_html,
            event_target=EXPERT_SUBMENU_TARGET, event_argument=EXPERT_SUBMENU_ARG,
            async_postback=False,
            extra_fields={
                EXPERT_SUBMENU_CLIENTSTATE_FIELD: EXPERT_SUBMENU_CLIENTSTATE_VALUE,
            },
        )
        # --- Fachmann security-code sub-sequence (retained safety net) ---
        # DISABLED by default (EXPERT_SKIP_SECURITY_CODE=True in const.py).
        # Proven unnecessary for both read and write: the submenu ClientState
        # alone puts the session on the Fachmann level (same as the scraper,
        # which reads the expert view with no code at all). The full block
        # below is kept, not deleted, so it can be re-enabled instantly if
        # Weishaupt ever makes the code mandatory again (e.g. a per-session
        # unlock). See the constant's comment in const.py for the full
        # rationale. When active, it fires: timer postback -> security-code
        # dialog+POST -> RAMMasterPage unlock callback.
        if self._do_security_code:
            # HAR-confirmed: after the security-code dialog opens and before
            # the code is posted, the browser fires exactly ONE main-page
            # timer postback (timerUpdateData). Its response carries the
            # fresh main-page state (__ECNPAGEVIEWSTATE/__EVENTVALIDATION)
            # that the subsequent RAMMasterPage unlock callback must
            # reference - byte-for-byte identical to the callback body in the
            # capture. Omitting it left the unlock callback carrying the stale
            # submenu-reload state, so the server accepted the callback
            # without error but never materialised the unlock (empty
            # parameter dropdown afterwards). A single postback, NOT the
            # generic poll loop, keeps the added server load minimal.
            self._check_cooldown()
            current_html = self._postback(
                WEB_DEFAULT_URL, current_html,
                event_target=EXPERT_TIMER_TARGET, event_argument="",
            )
            self._submit_security_code()
            # The real browser does NOT reload the main page here (confirmed
            # via HAR: no GET Default.aspx appears at all between the
            # security-code POST and the module select). Instead, the closing
            # dialog fires a RadAjaxManager client callback on the PARENT page
            # (__EVENTTARGET=ctl00$RAMMasterPage, Function="columns") - this
            # is what actually registers the unlock server-side; a plain
            # reload carries no such signal and leaves the unlock inert (which
            # is why the previous approach never got past an empty parameter
            # dropdown). The dialog runs in its own independent
            # ViewState/ScriptManager context (plain __VIEWSTATE,
            # "TSMeControlNetDialog"), so this callback must carry forward the
            # PARENT page's own prior state - and specifically the state from
            # the timer postback just above (the last main-page response), not
            # the earlier submenu reload, since the timer postback is what the
            # real callback's state matches in the capture.
            current_html = self._postback(
                WEB_DEFAULT_URL, current_html,
                event_target=EXPERT_RAM_MASTER_TARGET,
                event_argument=EXPERT_RAM_MASTER_UNLOCK_ARGUMENT,
                extra_fields={
                    "RadAJAXControlID": EXPERT_RAM_MASTER_RADAJAX_ID,
                    EXPERT_PAGE_TSM_FIELD: EXPERT_RAM_MASTER_TSM_VALUE,
                    EXPERT_PAGE_TSM_ID_FIELD: EXPERT_PAGE_TSM_VALUE,
                    EXPERT_RAM_MASTER_REFRESH_BUTTON_FIELD: EXPERT_RAM_MASTER_REFRESH_BUTTON_VALUE,
                },
            )
            _LOGGER.debug(
                "Expert navigation step 2 (Fachmann unlock) done via "
                "RAMMasterPage callback: %d bytes, pagestate=%s",
                len(current_html), self._has_viewstate(self._hidden_fields(current_html)),
            )
        else:
            _LOGGER.debug(
                "Expert navigation: security-code sub-sequence disabled - "
                "Fachmann level reached via the submenu ClientState alone; "
                "code not required for read/write on the reference install."
            )

        if not self._do_module_nav:
            # DEFAULT PATH: skip the module-select postback. A live read
            # proved the parameter dialog comes back fully populated without
            # selecting a module first (the heat pump is the 7th menu entry,
            # not the first, so this is not a default-module coincidence) -
            # the entityvalue in the dialog URL addresses device/module/
            # parameter completely. _fetch_form fetches the dialog directly
            # and still polls live values on demand if it ever comes back
            # empty (using the page state handed over here). The module-
            # select code below is kept as a safety net for other module
            # layouts and can be re-enabled from the options UI.
            _LOGGER.debug(
                "Expert navigation: skipping module-select postback "
                "(default); fetching dialog directly."
            )
            self._nav_html = current_html
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

        # After the module select the live values may still be trickling in.
        # Instead of firing a fixed batch of timer postbacks up front (which
        # always cost their full wait even when the dialog is already ready),
        # we hand the current page state to _fetch_form and let it poll the
        # live-value timer ON DEMAND - one postback at a time, only while the
        # dialog still comes back empty, stopping the instant it is populated.
        # This early-exit is both faster in the common case and no worse than
        # the old loop in the worst case (same max poll budget).
        self._nav_html = current_html

    def _poll_live_values_once(self):
        """Fire one live-value timer postback on the main page.

        Used by _fetch_form to advance the server's live-value loading when
        the parameter dialog still comes back empty, replacing the former
        fixed pre-poll loop in _establish_context. Safe no-op if navigation
        state is unavailable (e.g. the module-nav skip path).
        """
        if not self._nav_html:
            return
        self._check_cooldown()
        self._nav_html = self._postback(
            WEB_DEFAULT_URL, self._nav_html,
            event_target=EXPERT_TIMER_TARGET, event_argument="",
        )
        _LOGGER.debug("Expert navigation: on-demand live-value timer poll done.")

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
        r = self.session.get(
            dialog_url, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers={
                "Referer": WEB_MAIN_URL,
                "Accept": WEB_ACCEPT_NAV,
                "Accept-Language": WEB_ACCEPT_LANGUAGE,
            },
        )
        self._raise_if_forbidden(r)
        fields = self._hidden_fields(r.text)
        _LOGGER.debug(
            "Expert navigation: security-code dialog fetched, %d hidden fields, "
            "pagestate=%s, __VIEWSTATE len=%d, __EVENTVALIDATION len=%d",
            len(fields), self._has_viewstate(fields),
            len(fields.get("__VIEWSTATE", "")),
            len(fields.get("__EVENTVALIDATION", "")),
        )
        fields[EXPERT_SECURITY_CODE_FIELD] = EXPERT_SECURITY_CODE
        fields["__EVENTTARGET"] = EXPERT_DIALOG_SAVE_TARGET
        fields["__EVENTARGUMENT"] = ""
        # RadAjax async-postback fields the server requires for this dialog.
        fields[EXPERT_ASYNCPOST_FIELD] = "true"
        fields["RadAJAXControlID"] = EXPERT_DIALOG_RADAJAX_ID
        fields[EXPERT_DIALOG_TSM_FIELD] = EXPERT_DIALOG_TSM_VALUE
        fields[EXPERT_DIALOG_TSM_ID_FIELD] = EXPERT_DIALOG_TSM_ID_VALUE
        fields[EXPERT_DIALOG_RTS_STATE_FIELD] = EXPERT_DIALOG_RTS_STATE_VALUE
        self._check_cooldown()
        sec_headers = {
            "X-MicrosoftAjax": "Delta=true",
            "Referer": dialog_url,
            "Origin": WEB_PORTAL_ORIGIN,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": WEB_ACCEPT_AJAX,
            "Accept-Language": WEB_ACCEPT_LANGUAGE,
        }
        if self._export_hook is not None:
            self._export_hook("security_code", dialog_url, dict(fields), dict(sec_headers), self.session)
        r2 = self.session.post(
            dialog_url, data=fields, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers=sec_headers,
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
        # The main page's ScriptManager TSM version-blob field is sent on
        # EVERY postback once its scripts are loaded (confirmed via a
        # structural field comparison against a real browser's subMenu
        # postback - a FULL, non-async postback that still carries this
        # field) - not just async ones as previously assumed. The
        # $-prefixed panel-target field remains async/known-panel-only,
        # since it identifies which UpdatePanel triggered THIS specific
        # async postback, which doesn't apply to a full postback.
        fields[EXPERT_PAGE_TSM_ID_FIELD] = EXPERT_PAGE_TSM_VALUE

        self._check_cooldown()
        if async_postback:
            # Telerik async postback: marker field + header, response is a
            # delta stream we keep parsing for the next state.
            fields[EXPERT_ASYNCPOST_FIELD] = "true"
            # Main-page async postbacks (module select, timer polls) also
            # need the ScriptManager field identifying which panel posted
            # back. Only add this for known targets - the dialog postbacks
            # use a different ScriptManager field (see
            # _submit_security_code) and don't need this one.
            panel = EXPERT_PAGE_TSM_PANEL_BY_TARGET.get(event_target)
            if panel is not None:
                fields[EXPERT_PAGE_TSM_FIELD] = f"{panel}|{event_target}"
            headers = {
                "X-MicrosoftAjax": "Delta=true",
                "Referer": WEB_MAIN_URL,
                "Origin": WEB_PORTAL_ORIGIN,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": WEB_ACCEPT_AJAX,
                "Accept-Language": WEB_ACCEPT_LANGUAGE,
            }
        else:
            headers = {
                "Referer": WEB_MAIN_URL,
                "Origin": WEB_PORTAL_ORIGIN,
                "Accept": WEB_ACCEPT_NAV,
                "Accept-Language": WEB_ACCEPT_LANGUAGE,
            }
        if self._export_hook is not None:
            self._export_hook(event_target, url, dict(fields), dict(headers), self.session)
        if async_postback:
            resp = self.session.post(
                url, data=fields, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                headers=headers,
            )
        else:
            # Full postback ending in a 302 -> follow it to the reloaded
            # page, whose HTML carries the fresh state for the next step.
            resp = self.session.post(
                url, data=fields, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True, headers=headers,
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

    def read_many(self, entityvalues) -> dict:
        """Read several parameters on ONE shared session.

        Logs in and navigates to the Fachmann level once, then fetches each
        parameter dialog in turn - far cheaper than one login per id, which
        matters for the periodic auto-poll. Returns {entityvalue: state}; an
        id that fails to read maps to None instead of aborting the batch, so
        one bad id doesn't lose the others. A ForbiddenError (403) is NOT
        swallowed - it propagates so the shared cooldown engages.
        """
        result = {}
        ids = [e for e in (entityvalues or []) if e]
        for entityvalue in ids:
            self._validate_entityvalue(entityvalue)
        if not ids:
            return result
        self._check_cooldown()
        try:
            self._login()
            for entityvalue in ids:
                try:
                    result[entityvalue] = self._fetch_form(entityvalue)
                except ForbiddenError:
                    raise
                except Exception as exc:  # pylint: disable=broad-except
                    _LOGGER.warning(
                        "Expert auto-poll: reading %s failed: %s", _short_ev(entityvalue), exc
                    )
                    result[entityvalue] = None
        finally:
            self.close()
        return result

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
                headers={
                    "X-MicrosoftAjax": "Delta=true",
                    "Referer": self._last_dialog_url or WEB_MAIN_URL,
                    "Origin": WEB_PORTAL_ORIGIN,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": WEB_ACCEPT_AJAX,
                    "Accept-Language": WEB_ACCEPT_LANGUAGE,
                },
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
                "Expert parameter %s written and verified: %s", _short_ev(entityvalue), value_f
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

        Demand-driven live-value loading: after selecting the module the
        values can still be trickling in, so a first fetch may legitimately
        come back empty. On an empty dropdown we fire ONE live-value timer
        postback, pause, and retry - stopping the moment the dropdown is
        populated (early exit). This replaces the former fixed pre-poll loop
        in _establish_context: in the common case the dialog is ready on the
        first try and no timer postbacks are sent at all; in the worst case
        it polls up to the same budget as before. Favors reliability over
        speed (rare, on-demand operations). Only after all attempts still
        yield an empty dropdown do we raise.

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
                headers={
                    "Referer": WEB_MAIN_URL,
                    "Accept": WEB_ACCEPT_NAV,
                    "Accept-Language": WEB_ACCEPT_LANGUAGE,
                },
            )
            self._raise_if_forbidden(resp)
            if WEB_LOGIN_URL.lower() in resp.url.lower():
                raise AuthError("Expert client: redirected to login when fetching the form.")
            # Remember the exact URL this form was served at, so a
            # following write POST can reference it as Referer (confirmed
            # via HAR: the write's Referer is the same URL - including
            # rwndrnd - that rendered the form being submitted).
            self._last_dialog_url = resp.url
            try:
                state = self.parse_parameter_form(resp.text)
                _LOGGER.debug(
                    "Expert parameter %s: current=%s range=%s..%s (attempt %d)",
                    entityvalue, state.current, state.min_value, state.max_value,
                    attempt + 1,
                )
                return state
            except ValueError as exc:
                # Empty dropdown / values not ready yet. (parse_parameter_form
                # raises AuthError for a login page, which we deliberately do
                # NOT swallow here.) Nudge the server with one live-value
                # timer postback, wait, then retry.
                last_error = exc
                _LOGGER.debug(
                    "Expert parameter %s not ready on attempt %d/%d: %s",
                    entityvalue, attempt + 1, max_attempts, exc,
                )
                if attempt < max_attempts - 1:
                    self._poll_live_values_once()
                    time.sleep(EXPERT_FORM_RETRY_DELAY_SECONDS)
        raise last_error


def expert_client_options(options):
    """Return the WemPortalExpertClient kwargs derived from entry options.

    Centralises reading the module argument and the two advanced navigation
    toggles (module select / security code) so every client instantiation -
    write service, entity background write, and auto-poll - stays consistent.
    Both toggles default to OFF (i.e. the steps stay skipped) unless the user
    enabled them in the options UI.
    """
    from .const import (
        CONF_EXPERT_MODULE_ARG,
        CONF_EXPERT_ENABLE_MODULE_NAV,
        CONF_EXPERT_ENABLE_SECURITY_CODE,
    )
    module_arg = (options.get(CONF_EXPERT_MODULE_ARG) or "").strip() or None
    return {
        "module_arg": module_arg,
        "enable_module_nav": bool(options.get(CONF_EXPERT_ENABLE_MODULE_NAV, False)),
        "enable_security_code": bool(options.get(CONF_EXPERT_ENABLE_SECURITY_CODE, False)),
    }


def create_expert_number_entities(config_entry):
    """Build the configured expert number entities (comfort layer on top
    of the write service). Imported lazily by number.py's setup so this
    module stays out of the load path while the option is disabled.

    Entities are built from the ten generic slots (name + entityvalue id).
    Empty slots are skipped; duplicate entityvalues are de-duplicated.
    """
    from .const import (
        CONF_EXPERT_WRITE,
        EXPERT_SLOT_COUNT,
        CONF_EXPERT_SLOT_NAME_TEMPLATE,
        CONF_EXPERT_SLOT_ID_TEMPLATE,
    )

    if not config_entry.options.get(CONF_EXPERT_WRITE, False):
        return []

    if "WemPortalExpertNumber" not in globals():
        _LOGGER.error("Expert number entities unavailable: HA imports missing.")
        return []

    opts = config_entry.options
    # Collect (name, entityvalue) pairs from the generic slots. A slot with
    # an id but no name gets a default name.
    specs = []
    for i in range(1, EXPERT_SLOT_COUNT + 1):
        entityvalue = (opts.get(CONF_EXPERT_SLOT_ID_TEMPLATE % i) or "").strip()
        if not entityvalue:
            continue
        name = (opts.get(CONF_EXPERT_SLOT_NAME_TEMPLATE % i) or "").strip()
        specs.append((name or f"expert_parameter_{i}", entityvalue))

    entities = []
    seen = set()
    for name, entityvalue in specs:
        if entityvalue in seen:
            continue
        seen.add(entityvalue)
        entities.append(WemPortalExpertNumber(config_entry, name, entityvalue))
    return entities


# HA imports are only needed for the entity class below; kept at the end
# so plain use of the client (and its tests) needs no HA installed.
try:
    from homeassistant.components.number import RestoreNumber
    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
    from homeassistant.core import callback
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.device_registry import DeviceInfo
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
            # `name` comes from the slot's name field (or a default like
            # "expert_parameter_3"); use it as the stable object_id source
            # but show a readable friendly name, consistent with
            # has_entity_name on the other platforms.
            self._attr_name = name.replace("_", " ").title()
            # No translation_key: slot names are free text with no matching
            # translation entry, so the friendly name above is used directly
            # (setting an unresolvable translation_key would only log warnings).
            self._attr_unique_id = f"{config_entry.entry_id}:expert:{entityvalue}"
            self._attr_native_value = None
            # Guards against starting a second write while one is still
            # running in the background (the write takes roughly 5-15s).
            self._write_in_progress = False

        async def async_added_to_hass(self):
            """Restore the last known value after a restart."""
            await super().async_added_to_hass()
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value

        @property
        def entityvalue(self):
            """The portal entityvalue hex ID this entity reads/writes."""
            return self._entityvalue

        @callback
        def apply_read_state(self, state):
            """Update this entity from a periodic read result (ExpertParameterState).

            Called by the hourly auto-poll after reading the parameter in a
            shared session. Updates the value and the live device range, then
            writes HA state. A no-op if state is None (read failed for this
            id) or while a write is in flight - a poll that started before
            the write carries the pre-write value, and applying it would
            briefly overwrite the freshly verified one.
            """
            if state is None:
                return
            if self._write_in_progress:
                _LOGGER.debug(
                    "Discarding poll result for %s: a write is in progress.",
                    self._attr_name,
                )
                return
            self._attr_native_value = state.current
            if state.min_value is not None:
                self._attr_native_min_value = state.min_value
            if state.max_value is not None:
                self._attr_native_max_value = state.max_value
            self.async_write_ha_state()

        async def async_set_native_value(self, value: float) -> None:
            """Start the write in the background and return immediately.

            An expert write logs in, does the minimal Fachmann navigation,
            writes and verifies - roughly 5-15s, still longer than a
            frontend service call comfortably waits. Running it as a
            background task lets the call return at once; the outcome is
            reported via a persistent notification and the log. The entity
            value updates once the write is verified.
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
            client_opts = expert_client_options(self._config_entry.options)

            def _do_write():
                client = WemPortalExpertClient(
                    self._config_entry.data.get(CONF_USERNAME),
                    self._config_entry.data.get(CONF_PASSWORD),
                    cooldown_check=self._cooldown_check(),
                    **client_opts,
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
            """Report the background write outcome via a persistent notification.

            Failures always notify. Success only notifies when the user
            enabled CONF_EXPERT_NOTIFY_ON_SUCCESS (off by default) - the
            success is logged regardless.
            """
            if success:
                from .const import CONF_EXPERT_NOTIFY_ON_SUCCESS
                if not self._config_entry.options.get(CONF_EXPERT_NOTIFY_ON_SUCCESS, False):
                    return
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
