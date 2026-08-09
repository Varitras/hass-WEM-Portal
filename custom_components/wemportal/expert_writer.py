"""Expert parameter access via the WEM Portal web frontend.

Standalone module, deliberately separate from scraper.py/wemportalapi.py:
it covers parameters that exist ONLY in the web Fachmann view and are not
exposed by the mobile API at all (e.g. the heat pump's "Leistungsbegrenzung").

Read and write happen on demand only (a few requests per invocation on a
short-lived session) - never periodically. Writing validates the new value
against the live option list from the freshly fetched edit form and
verifies the result by re-reading the form afterwards.
"""

import hashlib
import random
import re
import time
from urllib.parse import urlsplit

from curl_cffi import requests
from lxml import html

from .const import (
    _LOGGER,
    CONF_EXPERT_NOTIFY_ON_SUCCESS,
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_WRITE,
    EXPERT_ASYNCPOST_FIELD,
    EXPERT_DIALOG_RADAJAX_ID,
    EXPERT_DIALOG_RTS_STATE_FIELD,
    EXPERT_DIALOG_RTS_STATE_VALUE,
    EXPERT_DIALOG_SAVE_TARGET,
    EXPERT_DIALOG_TSM_FIELD,
    EXPERT_DIALOG_TSM_ID_FIELD,
    EXPERT_DIALOG_TSM_ID_VALUE,
    EXPERT_DIALOG_TSM_VALUE,
    EXPERT_FORM_MAX_ATTEMPTS,
    EXPERT_FORM_RETRY_DELAY_SECONDS,
    EXPERT_MODULE_ARG_HEATPUMP,
    EXPERT_MODULE_ICONMENU_STATE_FIELD,
    EXPERT_MODULE_ICONMENU_STATE_TEMPLATE,
    EXPERT_MODULE_MENU_TARGET,
    EXPERT_PAGE_TSM_FIELD,
    EXPERT_PAGE_TSM_ID_FIELD,
    EXPERT_PAGE_TSM_PANEL_BY_TARGET,
    EXPERT_PAGE_TSM_VALUE,
    EXPERT_RAM_MASTER_RADAJAX_ID,
    EXPERT_RAM_MASTER_REFRESH_BUTTON_FIELD,
    EXPERT_RAM_MASTER_REFRESH_BUTTON_VALUE,
    EXPERT_RAM_MASTER_TARGET,
    EXPERT_RAM_MASTER_TSM_VALUE,
    EXPERT_RAM_MASTER_UNLOCK_ARGUMENT,
    EXPERT_SECURITY_CODE,
    EXPERT_SECURITY_CODE_FIELD,
    EXPERT_SESSION_MAX_AGE_SECONDS,
    EXPERT_SKIP_MODULE_NAV,
    EXPERT_SKIP_SECURITY_CODE,
    EXPERT_SLOT_COUNT,
    EXPERT_SUBMENU_ARG,
    EXPERT_SUBMENU_CLIENTSTATE_FIELD,
    EXPERT_SUBMENU_CLIENTSTATE_VALUE,
    EXPERT_SUBMENU_TARGET,
    EXPERT_TIMER_TARGET,
    EXPERT_VIEWSTATE_FIELDS,
    MIN_EXPERT_ENTITYVALUE_LENGTH,
    SCRAPER_REQUEST_TIMEOUT_SECONDS,
    WEB_ACCEPT_AJAX,
    WEB_ACCEPT_LANGUAGE,
    WEB_ACCEPT_NAV,
    WEB_CODE_EXPERTS_URL,
    WEB_LOGGED_IN_MARKER,
    WEB_LOGIN_FORM_MARKER,
    WEB_LOGIN_URL,
    WEB_MAIN_URL,
    WEB_PORTAL_ORIGIN,
)
from .exceptions import (
    AuthError,
    ExpertOperationAborted,
    ForbiddenError,
    ParameterWriteError,
    PortalMaintenanceError,
    ServerError,
)
from .utils import (
    maintenance_notice,
    report_unexpected_maintenance_marker,
)

# Edit dialog endpoint; entityvalue identifies device/module/parameter.
EXPERT_PARAMETER_URL = (
    "https://www.wemportal.com/Web/UControls/Weishaupt/DataDisplay/"
    "WwpsParameterDetails.aspx"
)

# Form field carrying the value in the edit dialog.
VALUE_FIELD_ID = "ctl00_DialogContent_ddlNewValue"

# A Telerik/MS-Ajax async postback answers with a pipe-delimited delta stream
# instead of HTML and labels each hidden field with this token. The marker
# form is also how a delta response is told apart from a full page, so both
# spellings come from one place - they are the same portal concept.
_HIDDEN_FIELD_TOKEN = "hiddenField"
_HIDDEN_FIELD_MARKER = f"|{_HIDDEN_FIELD_TOKEN}|"


# ASP.NET embeds a cookieless session id in the PATH - credential-equivalent,
# so it must never reach a log or a user-facing string. The documented form is
# /(S(<id>))/, but the token letter is not case-sensitive and several tokens
# can share one segment (/(A(..)S(..)F(..))/), so match the general shape
# rather than the single upper-case example.
_COOKIELESS_SESSION_RE = re.compile(r"/\((?:[A-Za-z]\([^)]*\))+\)")

# What redact_url returns when there is no endpoint left to name. A fixed
# string rather than an empty one: it goes into log lines that ask "which
# request did the portal reject?", where a blank reads as a formatting bug.
_UNKNOWN_URL = "unknown URL"


def redact_url(url) -> str:
    """Return only the endpoint of a portal URL, stripped of identifying data.

    A 403 has to stay diagnosable ("which request did the portal reject?")
    without publishing anything installation-specific. Two parts have to go:
    the QUERY, which carries the full entityvalue on the parameter-dialog
    requests, and a cookieless ASP.NET session id in the PATH. The endpoint
    alone answers the diagnostic question.
    """
    if not url:
        return _UNKNOWN_URL
    try:
        parts = urlsplit(str(url))
        path = _COOKIELESS_SESSION_RE.sub("", parts.path)
        if parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{path}"
        return path or _UNKNOWN_URL
    except Exception:  # noqa: BLE001
        # Redaction must never be the thing that breaks error handling.
        return _UNKNOWN_URL


def short_entityvalue(entityvalue: str) -> str:
    """Shortened entityvalue for user-visible log/notification text.

    entityvalues are installation-specific and shouldn't end up verbatim in
    text people copy into issues/forums. Debug-level logs keep the full id
    (needed for troubleshooting); info/warning/error and notifications use
    this shortened form.
    """
    text = entityvalue or ""
    return f"{text[:6]}…" if len(text) > 6 else text


def entityvalue_digest(entityvalue: str) -> str:
    """Short, stable digest of an entityvalue for use in internal IDs.

    Used wherever an id derived from the entityvalue must be unique and
    stable but ends up in persisted/inspectable places (entity-registry
    unique_ids, persistent-notification ids, task names). The raw
    entityvalue is installation-specific and shouldn't appear there
    verbatim - someone sharing their .storage files or diagnostic dumps
    would otherwise leak it. SHA-256 (truncated) keeps the mapping
    deterministic without being reversible.
    """
    cleaned = (entityvalue or "").strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def _is_valid_entityvalue(entityvalue) -> bool:
    """True if the entityvalue looks like a real ID: hex and long enough.

    Real entityvalues are long hex strings (the known ones are 36 chars).
    A short/stray value like "0" or "abc" - typically left over from a typo
    or a pre-1.8.1 config where the length check didn't exist yet - is not a
    readable parameter and only produces an empty dialog. Callers use this
    to skip such values instead of firing a pointless portal request.
    """
    cleaned = (entityvalue or "").strip()
    return (
        bool(re.fullmatch(r"[0-9A-Fa-f]+", cleaned))
        and len(cleaned) >= MIN_EXPERT_ENTITYVALUE_LENGTH
    )


def _ajax_headers(referer: str) -> dict[str, str]:
    """The headers a Telerik async postback is answered with a delta for.

    Three call sites sent the same six and differed in the referer alone.
    Without the first two the portal replies with a whole page instead of the
    delta the parser expects, so they belong together rather than next to
    whichever request happens to need them.

    Navigation requests deliberately send a SMALLER set - no postback headers
    and WEB_ACCEPT_NAV - because they do want a whole page back.
    """
    return {
        "X-MicrosoftAjax": "Delta=true",
        "Referer": referer,
        "Origin": WEB_PORTAL_ORIGIN,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": WEB_ACCEPT_AJAX,
        "Accept-Language": WEB_ACCEPT_LANGUAGE,
    }


# Matches the edit-icon onclick that opens the parameter dialog. lxml returns
# the attribute with the entity decoded, so it reads `&readdata`, not
# `&amp;readdata`.
_EDIT_LINK_RE = re.compile(r"entityvalue=([0-9A-Fa-f]+)&readdata=(?:True|False)")

# Module id values in the icon-menu RadMenu init are long hex strings; the
# top-menu RadMenu uses short numeric values ("100"), so a length floor tells
# them apart when reading the icon menu's own $create block.
_MODULE_VALUE_RE = re.compile(r'"value":"([0-9A-Fa-f]{20,})"')


def parse_parameter_list(html_content) -> list:
    """Parse a Fachmann module overview page into a list of parameters.

    Returns one dict per editable row: {group, name, entityvalue, value}.
    Static so it can be tested against saved pages.

    BOTH values of the link's `readdata` flag are taken. Measured at the
    portal: it is False on a parameter that stands alone in its section and
    True where several share one - Betriebsart, Party/Pause, Heizkennlinie,
    So/Wi Umschaltung and Reset are each alone in theirs. It says how the
    PORTAL opens the dialog, not whether there is one.

    Reading it as "aggregate entries with no value dialog" cost the discovery
    exactly those parameters, which are among the ones most worth having. They
    open the same dialog as any other, and fetching one with readdata=True -
    which is what this integration does for every parameter - answers with the
    same dropdown, current value and factory default.
    """
    results: list[dict] = []
    try:
        tree = html.fromstring(html_content)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not parse parameter list page: %s", exc)
        return results
    for panel in tree.xpath("//div[contains(@class, 'RadPanelBar')]"):
        header = panel.xpath(".//span[contains(@id, '_HeaderTemplate_lblHeaderText')]")
        group = header[0].text_content().strip() if header else ""
        for icon in panel.xpath(".//input[contains(@class, 'EditIcon')]"):
            match = _EDIT_LINK_RE.search(icon.get("onclick") or "")
            if not match:
                continue
            row = icon.xpath("./ancestor::tr[1]")
            if not row:
                continue
            name = row[0].xpath(".//span[contains(@class, 'simpleDataName')]")
            value = row[0].xpath(".//span[contains(@class, 'simpleDataValue')]")
            results.append(
                {
                    "group": group,
                    "name": name[0].text_content().strip() if name else "",
                    "entityvalue": match.group(1),
                    "value": value[0].text_content().strip() if value else "",
                }
            )
    return results


def parse_module_list(html_content) -> list:
    """Parse the Fachmann icon menu into a list of selectable modules.

    Visible labels (li > a > span.rmText, document order) are zipped with the
    module id values from the icon menu's RadMenu client-init block (same
    order). Returns [{index, value, label}]; index is the menu position used
    as the module-select postback argument. Static so it can be tested
    against saved pages.
    """
    try:
        tree = html.fromstring(html_content)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not parse module list page: %s", exc)
        return []
    menu = tree.xpath("//div[contains(@class, 'IconMenuControl')]")
    labels = (
        [
            span.text_content().strip()
            for span in menu[0].xpath(".//a//span[contains(@class, 'rmText')]")
        ]
        if menu
        else []
    )
    values = []
    for script in tree.xpath("//script"):
        text = script.text or ""
        if "iconMenu_rmMenuLayer_ClientState" in text and "itemData" in text:
            values = _MODULE_VALUE_RE.findall(text)
            break
    return [
        {"index": index, "value": value, "label": label}
        # strict=False on purpose: if labels and values ever differ in count
        # (unexpected portal change), pair up to the shorter instead of raising.
        for index, (label, value) in enumerate(zip(labels, values, strict=False))
    ]


def _as_number(text):
    """The number a dropdown label carries, or None if it carries none.

    Labels are decimal ("1.5", "1,5") on a scaled parameter and plain on the
    rest; an enum reads "Aus" and has no number at all.
    """
    try:
        return float((text or "").strip().replace(",", "."))
    except ValueError:
        return None


def _smallest_gap(options):
    """The distance between the two closest allowed values, or None.

    The portal's edit form is a dropdown of the values it accepts, so the
    spacing is stated rather than declared: halves for a temperature, whole
    numbers for a percentage, tens for a delay. The smallest gap rather than
    the first one because the list need not be evenly spaced, and a step
    larger than the true one makes values unreachable.

    No guard for a short list: one option produces no pair to subtract, so
    the comprehension is empty and the answer is already None. The guard that
    stood here said the same thing twice, which a mutation demonstrated by
    removing it with nothing changing.
    """
    ordered = sorted(options or [])
    gaps = [
        later - earlier
        for earlier, later in zip(ordered, ordered[1:])
        if later > earlier
    ]
    return min(gaps) if gaps else None


class ExpertParameterState:
    """Parsed state of one expert parameter's edit form."""

    def __init__(
        self,
        current,
        options,
        hidden_fields,
        post_values=None,
        portal_text=None,
        factory_default=None,
    ):
        self.current = current  # currently selected value (float), or None
        # What the portal displays for the current selection, and what it says
        # the parameter left the factory with. Both are text: a scaled
        # parameter reads "0.55" but the same dialog reads "Aus" where a
        # special value is set, and a number cannot carry that.
        self.portal_text = portal_text
        self.factory_default = factory_default
        self.options = options  # all allowed values (list of float)
        # What the form has to be given back for each of those values. Kept
        # apart from the value itself because the portal scales some
        # parameters: 1.5 is offered as the string "15". Defaults to the value
        # written out, which is what every unscaled parameter needs.
        self.post_values = post_values or {}
        self.min_value = min(options) if options else None
        self.max_value = max(options) if options else None
        # The gap between two neighbouring options, which is what the entity
        # needs as its step. Alongside min/max because it comes from the same
        # place and answers the same kind of question. None when the list is
        # too short to measure one - a single option says nothing about
        # spacing, and assuming from it would be guessing in a new place.
        self.step = _smallest_gap(options)
        # Hidden ASP.NET fields (VIEWSTATE etc.), kept for a later write step.
        self.hidden_fields = hidden_fields

    def post_value_for(self, value):
        """The exact string the form expects back for `value`.

        The portal's own option string rather than one rebuilt from the float,
        so a scaled parameter posts "15" for 1.5 and an integer-like one posts
        "30" rather than "30.0". Falls back to writing the number out for a
        state assembled without the mapping - the tests do that, and every
        parameter whose label equals its value attribute is unaffected either
        way.
        """
        attribute = self.post_values.get(value)
        if attribute is not None:
            return attribute
        return str(int(value)) if value == int(value) else str(value)


class WemPortalExpertClient:
    """On-demand web client for reading expert parameters.

    Uses its own HTTP session, created per operation and closed afterwards -
    fully independent of the polling scraper/API paths.
    """

    def __init__(
        self,
        username,
        password,
        cooldown_check=None,
        cooldown_activate=None,
        module_arg=None,
        enable_module_nav=None,
        enable_security_code=None,
        cookie_jar=None,
        abort_check=None,
    ):
        self.username = username
        self.password = password
        # Shared, in-memory cookie cache for session reuse across operations
        # (a plain dict owned by the WemPortalApi, passed by reference, so
        # every short-lived client instance sees the same one). Structure:
        # {"cookies": {...}, "saved_at": monotonic}. Never persisted - a live
        # session cookie is credential-equivalent.
        self._cookie_jar = cookie_jar if cookie_jar is not None else {}
        # Optional callable raising ForbiddenError while a 403 cooldown is
        # active (shared protection with the rest of the integration).
        self._cooldown_check = cooldown_check
        # Optional callable raising ExpertOperationAborted when the entry this
        # operation belongs to is being torn down. Checked at the same points
        # as the cooldown, and once more directly before the request that
        # WRITES - Python cannot cancel the executor thread this runs in, so
        # the only way to stop a write is to look before making it.
        self._abort_check = abort_check
        # Optional callable that ENGAGES the shared 403 cooldown. On a 403
        # here the whole integration should back off, not just this expert
        # operation; without this the API/scraper paths kept hitting a portal
        # that had just rate-limited us (the check-only callback could never
        # trip because nothing set the cooldown from the expert path).
        self._cooldown_activate = cooldown_activate
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
            (not EXPERT_SKIP_MODULE_NAV)
            if enable_module_nav is None
            else bool(enable_module_nav)
        )
        self._do_security_code = (
            (not EXPERT_SKIP_SECURITY_CODE)
            if enable_security_code is None
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

    # ------------------------------------------------------------------
    def _check_gates(self):
        """Both reasons not to make the next portal request, in one place.

        They are asked at the same moments and mean the same thing to the
        caller - do not send this. Kept apart, they drifted: the cooldown was
        checked at thirteen points and the abort at five, so a teardown that
        landed inside the four form attempts went unnoticed until the whole
        sequence had run, and the executor kept navigating the portal with
        the credentials of an entry that was gone.

        Abort before cooldown: an operation whose configuration has been torn
        down should say that, not report a portal refusal - the latter starts
        a cooldown over a request nobody was going to make.

        Both are no-ops when their callable was not supplied.
        """
        self._check_abort()
        self._check_cooldown()

    def _check_cooldown(self):
        if self._cooldown_check is not None:
            self._cooldown_check()

    def _check_abort(self):
        """Stop if the configuration this operation belongs to is gone."""
        if self._abort_check is not None:
            self._abort_check()

    def _check_response(self, response, what, check_maintenance=False):
        """The single gate every portal response passes through.

        This exists because the alternative kept failing. Each request site
        used to decide for itself what to validate, and the result was ten
        sites checking for a 403 while three checked the status code - so an
        error page was parsed as a portal answer, and three separate audit
        rounds each found the next unguarded request. A per-site decision is
        a per-site chance to forget; one gate cannot be forgotten.

        `check_maintenance` is option-in rather than universal on purpose. The
        marker is a container class in the page, and whether it can appear on
        a HEALTHY portal page has not been established - enabling it
        everywhere would trade a known gap for an unknown false positive. It
        is therefore switched on only where a real maintenance page was
        observed and is covered by tests.
        """
        self._raise_if_forbidden(response)
        status = getattr(response, "status_code", 200)
        if status != 200:
            # Not `>= 400`: every request in this module asks for an HTML
            # page or posts a form to one, so 200 is the only answer that
            # carries something to parse. A 204, or a redirect that was not
            # followed, used to reach the parser and surface as a missing
            # viewstate - and one step later as an authentication error,
            # which is a wrong and very misleading diagnosis.
            raise ServerError(f"WEM Portal returned {status} for the {what}.")
        notice = maintenance_notice(getattr(response, "text", "") or "")
        if notice:
            if check_maintenance:
                raise PortalMaintenanceError(notice)
            # Not acted on here - but worth knowing about, because it is the
            # open question that keeps the check from being universal.
            report_unexpected_maintenance_marker(notice, what)

    def _raise_if_forbidden(self, response):
        if response.status_code == 403:
            # Name the offending request. A generic "403 during expert access"
            # left more than a dozen possible call sites (login, postbacks,
            # module GET) indistinguishable, which made every diagnosis
            # guesswork. The URL comes straight off the response, so no call
            # site has to pass anything.
            # REDACTED: the raw url carries the full entityvalue on the
            # parameter-dialog requests (params={"entityvalue": ...}).
            where = redact_url(getattr(response, "url", None))
            _LOGGER.warning(
                "Expert path: the portal rejected a request with 403. "
                "Request: %s. Note that this does not necessarily mean a rate "
                "limit - it can equally mean the portal did not accept this "
                "particular request.",
                where,
            )
            # Backs off the EXPERT path only (see activate_expert_cooldown in
            # wemportalapi.py); sensor polling keeps running.
            if self._cooldown_activate is not None:
                self._cooldown_activate()
            raise ForbiddenError(
                f"WEM Portal returned 403 for an expert request ({where})."
            )

    # ------------------------------------------------------------------
    def _login(self):
        """Reach the Fachmann context, reusing a cached session if possible.

        A full login is what the portal rejects most readily (observed: 403 on
        Login.aspx while the same portal answered a browser normally, and while
        the scraper - which reuses its cookies - kept working). Every expert
        operation used to log in from scratch, so this path did far more logins
        than any other part of the integration.

        Reuse is attempted only while the cached session is young enough
        (EXPERT_SESSION_MAX_AGE_SECONDS), because a failed attempt costs two
        requests before falling back. A dead session falls back to a full
        login; a 403 is NOT swallowed - it must reach the caller so the
        backoff engages instead of us immediately retrying with a login.
        """
        if self._try_cached_session():
            return
        self._full_login()

    def _try_cached_session(self) -> bool:
        """Try to continue with the cached cookies. True if we got there."""
        cookies = self._cookie_jar.get("cookies")
        saved_at = self._cookie_jar.get("saved_at")
        if not cookies or saved_at is None:
            return False
        age = time.monotonic() - saved_at
        if age > EXPERT_SESSION_MAX_AGE_SECONDS:
            _LOGGER.debug(
                "Expert session cache is %.0fs old (max %ds), logging in fresh.",
                age,
                EXPERT_SESSION_MAX_AGE_SECONDS,
            )
            return False

        self.session = requests.Session(impersonate="chrome146")
        try:
            self.session.cookies.update(cookies)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not restore cached expert cookies: %s", exc)
            return False

        try:
            self._establish_context()
        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except (ForbiddenError, PortalMaintenanceError, ServerError):
            # All three are ANSWERS, not signs that the cached session went
            # stale, so none of them is a reason to log in again. Falling
            # through would send the two requests of a full login handshake
            # immediately after the portal said "rate limited", "we are down
            # for maintenance" or "something broke" - the opposite of backing
            # off, and against an IP the portal blocks past 10,000 requests
            # per 12 hours.
            #
            # Only ForbiddenError was re-raised here. The web scraper has
            # covered all three since the same defect was found there; this
            # is that fix in the module next door, which is where this
            # codebase keeps finding the other half of a fix.
            raise
        except Exception as exc:  # noqa: BLE001
            # Broad, but the three answers that must NOT be retried are
            # re-raised above. What is left really is a stale session -
            # AuthError, a dropped connection - and a fresh login is the
            # right answer to those.
            _LOGGER.debug(
                "Cached expert session no longer usable (%s), logging in fresh.", exc
            )
            self.close()
            return False

        self._save_session()
        _LOGGER.debug("Expert path reused the cached session (no login needed).")
        return True

    def _save_session(self):
        """Remember the current cookies for the next operation."""
        try:
            self._cookie_jar["cookies"] = dict(self.session.cookies)
            self._cookie_jar["saved_at"] = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            # Caching is an optimisation; failing to cache only means the
            # next operation logs in again.
            _LOGGER.debug("Could not cache expert session cookies: %s", exc)

    def _full_login(self):
        """Perform a fresh web login on a new session."""
        self.session = requests.Session(impersonate="chrome146")

        login_page = self.session.get(
            WEB_LOGIN_URL, timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS
        )
        self._check_response(login_page, "login page", check_maintenance=True)
        tree = html.fromstring(login_page.text)
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
        # A login is two requests, and the gate was only asked before the
        # first. An unload arriving while that one was in flight went
        # unnoticed until the whole sequence had run, so the credentials went
        # to the portal for a configuration that no longer existed.
        self._check_gates()
        login_response = self.session.post(
            WEB_LOGIN_URL,
            data=login_data,
            allow_redirects=True,
            timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
        )
        # check_maintenance, as on the GET above: what comes back is the login
        # page or the main page, and both carry the notice. Without it,
        # announced downtime arrives on the login URL - which is exactly the
        # test for "these credentials were rejected" just below - and was
        # reported as a wrong password. Same gap the scraper had.
        self._check_response(login_response, "login POST", check_maintenance=True)
        # Three outcomes, not two, same as the scraper and web_login: staying
        # on the login URL was the whole test, and a portal error or
        # interstitial does that too while answering 200. Only the login FORM
        # is evidence that credentials were seen and refused.
        logged_in = WEB_LOGGED_IN_MARKER in login_response.text
        if not logged_in and WEB_LOGIN_FORM_MARKER in login_response.text:
            raise AuthError("Expert client: invalid username or password.")
        if not logged_in:
            raise ServerError(
                "The WEM Portal answered the expert login with a page that is "
                "neither a session nor the login form. This can also mean the "
                f"portal did not accept our cookies. URL: {login_response.url}"
            )

        self._establish_context()
        # Only cache once the full navigation succeeded: cookies from a
        # session that never reached the Fachmann context are worthless and
        # would just cost a failed reuse attempt next time.
        self._save_session()

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
        # Gated like every other request: this runs straight after the login
        # POST, so a teardown arriving during that one would otherwise be
        # answered with another authenticated request.
        self._check_gates()
        main_page = self.session.get(
            WEB_MAIN_URL,
            timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers={"Accept": WEB_ACCEPT_NAV, "Accept-Language": WEB_ACCEPT_LANGUAGE},
        )
        self._check_response(main_page, "main page", check_maintenance=True)
        if WEB_LOGIN_URL.lower() in main_page.url.lower():
            raise AuthError("Expert client: session not accepted by portal main page.")
        current_html = main_page.text
        _LOGGER.debug(
            "Expert navigation step 1 (main page): %d bytes, pagestate=%s",
            len(current_html),
            self._has_viewstate(self._hidden_fields(current_html)),
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
            WEB_MAIN_URL,
            current_html,
            event_target=EXPERT_SUBMENU_TARGET,
            event_argument=EXPERT_SUBMENU_ARG,
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
            self._check_gates()
            current_html = self._postback(
                WEB_MAIN_URL,
                current_html,
                event_target=EXPERT_TIMER_TARGET,
                event_argument="",
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
                WEB_MAIN_URL,
                current_html,
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
                len(current_html),
                self._has_viewstate(self._hidden_fields(current_html)),
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
            WEB_MAIN_URL,
            current_html,
            event_target=EXPERT_MODULE_MENU_TARGET,
            event_argument=self._module_arg,
            extra_fields={EXPERT_MODULE_ICONMENU_STATE_FIELD: icon_menu_state},
        )
        _LOGGER.debug(
            "Expert navigation step 3 (module select, arg=%s) done.", self._module_arg
        )

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
        self._check_gates()
        self._nav_html = self._postback(
            WEB_MAIN_URL,
            self._nav_html,
            event_target=EXPERT_TIMER_TARGET,
            event_argument="",
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
        self._check_gates()
        dialog_url = f"{WEB_CODE_EXPERTS_URL}?rwndrnd={random.random()}"
        dialog_page = self.session.get(
            dialog_url,
            timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers={
                "Referer": WEB_MAIN_URL,
                "Accept": WEB_ACCEPT_NAV,
                "Accept-Language": WEB_ACCEPT_LANGUAGE,
            },
        )
        self._check_response(dialog_page, "security-code dialog")
        fields = self._hidden_fields(dialog_page.text)
        _LOGGER.debug(
            "Expert navigation: security-code dialog fetched, %d hidden fields, "
            "pagestate=%s, __VIEWSTATE len=%d, __EVENTVALIDATION len=%d",
            len(fields),
            self._has_viewstate(fields),
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
        self._check_gates()
        security_headers = _ajax_headers(dialog_url)
        code_response = self.session.post(
            dialog_url,
            data=fields,
            timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers=security_headers,
        )
        self._check_response(code_response, "security-code POST")
        _LOGGER.debug(
            "Expert navigation: security-code POST -> %d bytes, delta=%s",
            len(code_response.text),
            _HIDDEN_FIELD_MARKER in code_response.text,
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
        if _HIDDEN_FIELD_MARKER in content:
            parts = content.split("|")
            for position, token in enumerate(parts):
                if token == _HIDDEN_FIELD_TOKEN and position + 2 < len(parts):
                    fields[parts[position + 1]] = parts[position + 2]
            if fields:
                return fields
        # Otherwise parse as HTML.
        try:
            tree = html.fromstring(content)
            for hidden_input in tree.xpath("//input[@type='hidden']"):
                name = hidden_input.get("name")
                if name:
                    fields[name] = hidden_input.get("value", "")
        except Exception as exc:  # noqa: BLE001
            # Malformed/unparseable response: return whatever was collected
            # so the caller degrades gracefully instead of crashing. Logged
            # so a parsing regression (e.g. a portal format change) is visible.
            _LOGGER.debug("Could not parse hidden fields from response: %s", exc)
        return fields

    def _postback(
        self,
        url,
        current_html,
        event_target,
        event_argument="",
        async_postback=True,
        extra_fields=None,
    ):
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
                event_target,
                len(fields),
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

        self._check_gates()
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
            headers = _ajax_headers(WEB_MAIN_URL)
        else:
            headers = {
                "Referer": WEB_MAIN_URL,
                "Origin": WEB_PORTAL_ORIGIN,
                "Accept": WEB_ACCEPT_NAV,
                "Accept-Language": WEB_ACCEPT_LANGUAGE,
            }
        if async_postback:
            response = self.session.post(
                url,
                data=fields,
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                headers=headers,
            )
        else:
            # Full postback ending in a 302 -> follow it to the reloaded
            # page, whose HTML carries the fresh state for the next step.
            response = self.session.post(
                url,
                data=fields,
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
                headers=headers,
            )
        self._check_response(response, "navigation postback")
        if WEB_LOGIN_URL.lower() in response.url.lower():
            raise AuthError("Expert client: session expired during navigation.")
        _LOGGER.debug(
            "Expert navigation: postback %s (async=%s) -> %d bytes, delta=%s, pagestate=%s",
            event_target,
            async_postback,
            len(response.text),
            _HIDDEN_FIELD_MARKER in response.text,
            self._has_viewstate(self._hidden_fields(response.text)),
        )
        return response.text

    def close(self):
        """Close the session; never raises."""
        if self.session is not None:
            try:
                self.session.close()
            except Exception as exc:  # noqa: BLE001
                # Closing is best-effort; the session is being discarded anyway.
                _LOGGER.debug("Ignoring error while closing expert session: %s", exc)
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

        # Two numbers per option, and they are not always the same one. The
        # label is what the parameter IS; the value attribute is what the form
        # posts back. They part company where the portal encodes fractions as
        # whole numbers: a range of 1.0 to 30.0 in halves arrives as 10 to 300
        # in fives. Reading only the attribute published that scaled number as
        # the parameter's range, so a value copied from the portal was written
        # ten times too small - and the same installation's parameter list,
        # which reads the label, disagreed with the entity about the same
        # parameter.
        pairs = []
        for option in select[0].xpath(".//option"):
            attribute = (option.get("value") or "").strip()
            try:
                posted = float(attribute.replace(",", "."))
            except ValueError:
                continue
            label = (option.text or "").strip()
            pairs.append(
                (
                    _as_number(label),
                    posted,
                    attribute,
                    option.get("selected") is not None,
                    label,
                )
            )

        # A parameter offering ANY numeric label is measured in those labels,
        # and the odd word among them is a special value beside that scale
        # rather than a point on it: "Aus" is offered as 0 on the heating
        # curve but as -32768 on the frost protection, so it cannot be placed
        # on the scale by dividing - and taking the attributes instead would
        # publish -32768 as the minimum of a range that runs -20.0 to 17.5.
        #
        # Only a dropdown with no numeric label at all is read by its
        # attributes: on an enum ("Automatik", "Party 2.5 h") they are the
        # only number there is, and they are an index rather than a scale.
        by_label = any(shown is not None for shown, *_ in pairs)

        options = []
        current = None
        selected_text = None
        post_values = {}
        for shown, posted, attribute, selected, label in pairs:
            on_scale = shown is not None or not by_label
            value = (shown if by_label else posted) if on_scale else None
            if on_scale:
                options.append(value)
                post_values[value] = attribute
            if selected:
                # None where the portal has a special value selected: a number
                # entity cannot show "Aus", and the attribute below says so.
                current = value
                selected_text = label or attribute

        if not options:
            # Dropdown present but empty: the session has no active
            # installation context (see _establish_context) or the
            # parameter could not be resolved for this entityvalue.
            # Keep the snippet short: it is the dialog HTML (may include bulky
            # ASP.NET __VIEWSTATE state) and only needs to reveal a format
            # change, not the whole page.
            _LOGGER.debug(
                "Expert parameter form with empty dropdown, response snippet: %.200s",
                html_content,
            )
            raise ValueError(
                "Expert parameter form: value dropdown is empty - the portal "
                "session has no active installation context, or the "
                "entityvalue does not match a readable parameter."
            )

        # The value the parameter left the factory with, which the dialog
        # shows beside the dropdown. Kept as the portal's own text: it reads
        # "0.75" on a scaled parameter but "Aus" or "Mittel" on others, and
        # rewriting either into a number would lose one of them.
        delivered = tree.xpath("//*[contains(@id, 'ltDeliveryStatusData')]")
        factory_default = delivered[0].text_content().strip() if delivered else None

        # Hidden ASP.NET fields, needed later for the (not yet built) write POST.
        hidden_fields = {}
        for hidden_input in tree.xpath("//input[@type='hidden']"):
            name = hidden_input.get("name")
            if name:
                hidden_fields[name] = hidden_input.get("value", "")

        return ExpertParameterState(
            current,
            options,
            hidden_fields,
            post_values,
            portal_text=selected_text,
            factory_default=factory_default or None,
        )

    # ------------------------------------------------------------------
    def read_parameter(self, entityvalue: str) -> ExpertParameterState:
        """Login, fetch one parameter's edit form, parse it, close session.

        Total server load: 3 requests (login page, login POST, form GET),
        only when explicitly invoked - never periodically.
        """
        self._validate_entityvalue(entityvalue)
        self._check_gates()
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
        result: dict[str, ExpertParameterState | None] = {}
        ids = [candidate for candidate in (entityvalues or []) if candidate]
        # Skip entityvalues that can't be a real ID (too short / non-hex) -
        # e.g. a stale "0" from a pre-1.8.1 config. Polling them would only
        # hit an empty dialog and log a misleading failure every cycle.
        skipped = [
            candidate for candidate in ids if not _is_valid_entityvalue(candidate)
        ]
        for candidate in skipped:
            _LOGGER.debug(
                "Expert auto-poll: skipping invalid entityvalue %s "
                "(not a readable ID); fix or clear it in the options.",
                short_entityvalue(candidate),
            )
        ids = [candidate for candidate in ids if _is_valid_entityvalue(candidate)]
        if not ids:
            return result
        # Same cooperative stop the write path has. Cancelling the auto-poll
        # cancels the AWAIT, not this thread, so an unload halfway through a
        # batch kept navigating the portal with the credentials of an entry
        # being torn down - and the longer the batch, the longer that lasted.
        # Outside the per-id try below, which turns an exception into "this
        # id could not be read" and would swallow the stop.
        self._check_gates()
        try:
            self._login()
            for entityvalue in ids:
                self._check_gates()
                try:
                    result[entityvalue] = self._fetch_form(entityvalue)
                # A 403 is about the connection, not this id: it has to reach
                # the caller so the shared cooldown engages.
                # skipcq: PYL-W0706 - shields the catch-all, not redundant
                except ForbiddenError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Expert auto-poll: reading %s failed: %s",
                        short_entityvalue(entityvalue),
                        exc,
                    )
                    result[entityvalue] = None
        finally:
            self.close()
        return result

    # ------------------------------------------------------------------
    def list_modules(self) -> list:
        """Login, read the Fachmann icon menu, return selectable modules.

        Returns [{index, value, label}]; on-demand only (a single short
        session). _establish_context leaves the Fachmann main page in
        self._nav_html, which carries the icon menu we parse.
        """
        self._check_gates()
        try:
            self._login()
            return parse_module_list(self._nav_html or "")
        finally:
            self.close()

    def discover(self, modules) -> list:
        """Login once, fetch each module's overview, return its parameters.

        `modules` are dicts from list_modules(). Returns the concatenated
        parse_parameter_list() results (readable rows only), de-duplicated by
        entityvalue. A ForbiddenError (403) propagates so the shared cooldown
        engages; other per-module errors are logged and skipped so one bad
        module doesn't lose the rest.
        """
        result = []
        seen = set()
        self._check_gates()
        try:
            self._login()
            for module in modules or []:
                try:
                    html_text = self._fetch_module_page(module)
                # A 403 is about the connection, not this module: it has to
                # reach the caller so the shared cooldown engages.
                # skipcq: PYL-W0706 - shields the catch-all, not redundant
                except ForbiddenError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Expert discovery: module %s failed: %s",
                        module.get("label"),
                        exc,
                    )
                    continue
                for parameter in parse_parameter_list(html_text):
                    entityvalue = parameter["entityvalue"]
                    if entityvalue in seen:
                        continue
                    seen.add(entityvalue)
                    result.append(parameter)
        finally:
            self.close()
        return result

    def _fetch_module_page(self, module) -> str:
        """Select a module via the icon-menu postback and return its HTML.

        The module select is a Telerik async postback, so its response is a
        delta stream that ALREADY carries the re-rendered module panel. That
        is preferred: it is the server's answer to "show me this module", it
        needs no second request, and it cannot be a stale default page.

        The original implementation discarded that response and issued a plain
        `GET Default.aspx` instead (spec option (a)). Live-testing showed that
        path returning no readable parameters, so the GET is now only the
        fallback for the case where the delta carries no rows.
        """
        index = str(module.get("index"))
        icon_menu_state = EXPERT_MODULE_ICONMENU_STATE_TEMPLATE % index
        self._nav_html = self._postback(
            WEB_MAIN_URL,
            self._nav_html,
            event_target=EXPERT_MODULE_MENU_TARGET,
            event_argument=index,
            extra_fields={EXPERT_MODULE_ICONMENU_STATE_FIELD: icon_menu_state},
        )
        delta_rows = len(parse_parameter_list(self._nav_html))
        if delta_rows:
            _LOGGER.debug(
                "Expert discovery: module %s -> %d parameter(s) from the "
                "postback response; no extra request needed.",
                module.get("label"),
                delta_rows,
            )
            return self._nav_html

        self._check_gates()
        response = self.session.get(
            WEB_MAIN_URL,
            timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            headers={
                "Referer": WEB_MAIN_URL,
                "Accept": WEB_ACCEPT_NAV,
                "Accept-Language": WEB_ACCEPT_LANGUAGE,
            },
        )
        self._check_response(response, "module page")
        # Log both sources' yield: if discovery still comes up empty, this
        # says immediately whether the postback or the follow-up GET is the
        # one that fails to deliver the module - no guesswork needed.
        _LOGGER.debug(
            "Expert discovery: module %s -> postback response had no editable "
            "rows (%d bytes); GET Default.aspx yielded %d parameter(s) "
            "(%d bytes).",
            module.get("label"),
            len(self._nav_html or ""),
            len(parse_parameter_list(response.text)),
            len(response.text),
        )
        return response.text

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
        self._check_gates()
        try:
            self._login()
            # Again after the login: it is the slow part (several requests),
            # and an unload during it used to be noticed only after the write
            # had already happened.
            self._check_gates()
            state = self._fetch_form(entityvalue)

            # Validate against the live option list; option values are the
            # exact strings the server expects back.
            value_f = float(value)
            if value_f not in state.options:
                # Carries the state: a caller whose idea of the range is out
                # of date is precisely the caller that lands here.
                raise ParameterWriteError(
                    f"Value {value} not allowed; device accepts "
                    f"{state.min_value}..{state.max_value} "
                    f"({len(state.options)} discrete options).",
                    state=state,
                )
            value_str = state.post_value_for(value_f)

            # The Senden button is type=button and submits via a JS
            # __doPostBack('ctl00$DialogContent$BtnSave', '') - replicate
            # that postback, carrying over all hidden ASP.NET fields. The
            # portal sends this as a Telerik async postback (see the
            # X-MicrosoftAjax header and rwndrnd cache-buster in the HAR).
            post_data = dict(state.hidden_fields)
            post_data["__EVENTTARGET"] = "ctl00$DialogContent$BtnSave"
            post_data["__EVENTARGUMENT"] = ""
            post_data["ctl00$DialogContent$ddlNewValue"] = value_str

            # The last gate before the request that actually CHANGES a
            # heating parameter. Everything up to here is reads.
            self._check_gates()
            response = self.session.post(
                EXPERT_PARAMETER_URL,
                params={
                    "entityvalue": entityvalue,
                    "readdata": "True",
                    "rwndrnd": str(random.random()),
                },
                data=post_data,
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                headers=_ajax_headers(self._last_dialog_url or WEB_MAIN_URL),
            )
            self._check_response(response, "parameter write")

            # Verify by re-reading the form: the device/portal must now
            # report the new value as selected. The value is applied
            # immediately, so a short retry budget is enough here (unlike
            # the initial read, where live values may still be loading).
            verify = self._fetch_form(entityvalue, max_attempts=2)
            if verify.current != value_f:
                raise ParameterWriteError(
                    f"Write not confirmed: form still shows {verify.current}, "
                    f"expected {value_f}. The portal may have rejected the value.",
                    state=verify,
                )
            _LOGGER.info(
                "Expert parameter %s written and verified: %s",
                short_entityvalue(entityvalue),
                value_f,
            )
            return verify
        finally:
            self.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_entityvalue(entityvalue: str):
        # Active read/write of a single parameter: reject anything that
        # isn't a plausible ID (hex + minimum length) with a clear error,
        # so a user writing to a mistyped/too-short id gets feedback rather
        # than a confusing empty-dialog failure. Only the SHORTENED id goes
        # into the message: it propagates into error logs and persistent
        # notifications (texts people copy into issues/forums), and a
        # nearly-correct id would otherwise appear there almost in full.
        if not _is_valid_entityvalue(entityvalue):
            raise ValueError(
                f"Invalid entityvalue: {short_entityvalue((entityvalue or '').strip())!r} "
                "(must be a long hex string; check the configured ID)"
            )

    def _fetch_form(
        self, entityvalue: str, max_attempts: int | None = None
    ) -> ExpertParameterState:
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
            self._check_gates()
            response = self.session.get(
                EXPERT_PARAMETER_URL,
                params={
                    "entityvalue": entityvalue,
                    "readdata": "True",
                    "rwndrnd": str(random.random()),
                },
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
                headers={
                    "Referer": WEB_MAIN_URL,
                    "Accept": WEB_ACCEPT_NAV,
                    "Accept-Language": WEB_ACCEPT_LANGUAGE,
                },
            )
            self._check_response(response, "parameter dialog")
            if WEB_LOGIN_URL.lower() in response.url.lower():
                raise AuthError(
                    "Expert client: redirected to login when fetching the form."
                )
            # Remember the exact URL this form was served at, so a
            # following write POST can reference it as Referer (confirmed
            # via HAR: the write's Referer is the same URL - including
            # rwndrnd - that rendered the form being submitted).
            self._last_dialog_url = response.url
            try:
                state = self.parse_parameter_form(response.text)
                _LOGGER.debug(
                    "Expert parameter %s: current=%s range=%s..%s (attempt %d)",
                    short_entityvalue(entityvalue),
                    state.current,
                    state.min_value,
                    state.max_value,
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
                    short_entityvalue(entityvalue),
                    attempt + 1,
                    max_attempts,
                    exc,
                )
                if attempt < max_attempts - 1:
                    self._poll_live_values_once()
                    time.sleep(EXPERT_FORM_RETRY_DELAY_SECONDS)
        raise last_error


def create_expert_number_entities(config_entry):
    """Build the configured expert number entities (comfort layer on top
    of the write service).

    number.py imports this module only when CONF_EXPERT_WRITE is on, which is
    what actually keeps it - and curl_cffi with it - out of the load path
    while the option is disabled. The check below is kept as the authoritative
    one for any other caller.

    Entities are built from the ten generic slots (name + entityvalue id).
    Empty slots are skipped; duplicate entityvalues are de-duplicated.
    """

    if not config_entry.options.get(CONF_EXPERT_WRITE, False):
        return []

    if "WemPortalExpertNumber" not in globals():
        _LOGGER.error("Expert number entities unavailable: HA imports missing.")
        return []

    options = config_entry.options
    # Collect (name, entityvalue) pairs from the generic slots. A slot with
    # an id but no name gets a default name.
    configured_slots = []
    for slot in range(1, EXPERT_SLOT_COUNT + 1):
        entityvalue = (options.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot) or "").strip()
        if not entityvalue:
            continue
        name = (options.get(CONF_EXPERT_SLOT_NAME_TEMPLATE % slot) or "").strip()
        configured_slots.append((name or f"expert_parameter_{slot}", entityvalue))

    entities = []
    seen = set()
    for name, entityvalue in configured_slots:
        if entityvalue in seen:
            continue
        seen.add(entityvalue)
        entities.append(WemPortalExpertNumber(config_entry, name, entityvalue))
    return entities


# HA imports are only needed for the entity class below; kept at the end
# so plain use of the client (and its tests) needs no HA installed.
# Placeholders for "the portal has not told us yet", not estimates of what a
# parameter looks like. Wide enough and fine enough that nothing a portal
# might offer is excluded before it has been asked - see the entity class for
# why excluding anything here is a lock rather than a label.
EXPERT_UNKNOWN_BOUND = 100000.0
# 0.01 rather than the 0.5 this started as: the heating curve is offered in
# 0.05 steps, so half-value granularity locked out four values in five. Being
# finer than any parameter needs costs nothing - while the bounds are
# placeholders the entity is a box, where the arrows are useless anyway and
# the value is typed - whereas being too coarse is a lock on the very write
# that would fetch the real step.
EXPERT_UNKNOWN_STEP = 0.01

# What every slot claimed before the placeholders existed. Kept only to
# recognise such a record on the first start after an upgrade - see
# WemPortalExpertNumber._is_the_old_assumption.
LEGACY_ASSUMED_MIN = 0
LEGACY_ASSUMED_MAX = 100
LEGACY_ASSUMED_STEP = 1

try:
    from homeassistant.components.number import NumberMode, RestoreNumber
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
        # No unit. The portal's edit form carries a list of allowed values and
        # nothing else - it never says what they measure. Every slot used to
        # be published as a percentage, so a flow temperature, a curve slope
        # and a delay all read as `%` and were recorded under that unit.
        #
        # The three below hold only until the parameter has been read,
        # written or restored once; all three are then replaced by what the
        # portal actually offers.
        #
        # Until then they must EXCLUDE NOTHING, which is why they are absurd
        # rather than plausible. Home Assistant validates a call against the
        # published range before the integration is asked, so a slot claiming
        # 0-100 refuses a valid 350 - and the write that would have fetched
        # the real range is precisely what it refuses. With the hourly read
        # off by default and nothing to restore on a fresh install, that lock
        # never opens. A step of 1 does the same to every half-value.
        #
        # Nothing is lost by being permissive here: what the portal will not
        # accept is caught where it is known, in write_parameter, against the
        # form's own option list.
        _attr_native_step = EXPERT_UNKNOWN_STEP
        _attr_native_min_value = -EXPERT_UNKNOWN_BOUND
        _attr_native_max_value = EXPERT_UNKNOWN_BOUND
        _attr_icon = "mdi:speedometer"

        def __init__(self, config_entry, name, entityvalue):
            self._config_entry = config_entry
            self._entityvalue = entityvalue
            # Both are only known once the portal has been read; neither is
            # restored, because a stored copy would outlive the reading that
            # produced it and there is nothing to check it against.
            self._portal_text = None
            self._factory_default = None
            # `name` comes from the slot's name field (or a default like
            # "expert_parameter_3"); use it as the stable object_id source
            # but show a readable friendly name, consistent with
            # has_entity_name on the other platforms.
            self._attr_name = name.replace("_", " ").title()
            # No translation_key: slot names are free text with no matching
            # translation entry, so the friendly name above is used directly
            # (setting an unresolvable translation_key would only log warnings).
            # unique_id carries a DIGEST of the entityvalue, not the raw id:
            # unique_ids are persisted in .storage/core.entity_registry, and
            # the installation-specific raw id shouldn't leak into files
            # people share for debugging. number.py migrates entities from
            # the old raw-id format on setup, preserving entity_id/history.
            self._attr_unique_id = (
                f"{config_entry.entry_id}:expert:{entityvalue_digest(entityvalue)}"
            )
            self._attr_native_value = None
            # Guards against a second write starting while one is still
            # running - the frontend can send two in quick succession.
            self._write_in_progress = False
            # Set once the entity is on its way out, so a queued write can
            # bail out before it opens a portal session. See
            # async_will_remove_from_hass for why cancelling is not enough.
            self._removed = False

        async def async_added_to_hass(self):
            """Restore what the last run knew about this parameter."""
            await super().async_added_to_hass()
            last = await self.async_get_last_number_data()
            if last is not None:
                self._restore_from(last)

        def _apply_state(self, state):
            """Take value, range and step from a form the portal just showed.

            Both callers land here - the periodic read and the verify after a
            write - and each carried its own copy of this before. That is how
            the step came to be updated by neither: it was added to the class
            as a fixed 1 and there were two places to remember it in.
            """
            self._attr_native_value = state.current
            if state.min_value is not None:
                self._attr_native_min_value = state.min_value
            if state.max_value is not None:
                self._attr_native_max_value = state.max_value
            if state.step is not None:
                self._attr_native_step = state.step
            if state.portal_text is not None:
                self._portal_text = state.portal_text
            if state.factory_default is not None:
                self._factory_default = state.factory_default

        def _restore_from(self, last):
            """Take back the stored range as well as the stored value.

            Only the value came back before, so after a restart a parameter
            whose real range is 200-800 sat at its stored value inside the
            assumed 0-100 and could not be set at all until the next
            successful read - which, with the auto-poll off, may be never.

            Except when what is stored is the assumption itself. RestoreNumber
            persists min, max and step with or without a value, so a slot that
            existed before the placeholders did has 0/100/1 on disk although
            it was never read - and taking that back on the first start after
            an upgrade would reinstate the lock for exactly the installations
            the placeholders are for.
            """
            if last.native_value is not None:
                self._attr_native_value = last.native_value
            if self._is_the_old_assumption(last):
                _LOGGER.debug(
                    "%s: ignoring a stored range that predates the portal "
                    "ever being asked; keeping the placeholders.",
                    self._attr_name,
                )
                return
            if last.native_min_value is not None:
                self._attr_native_min_value = last.native_min_value
            if last.native_max_value is not None:
                self._attr_native_max_value = last.native_max_value
            if last.native_step is not None:
                self._attr_native_step = last.native_step

        @staticmethod
        def _is_the_old_assumption(last) -> bool:
            """Whether a stored range is the pre-fix guess rather than a fact.

            Both halves are needed. 0 to 100 in steps of 1 is a plausible real
            range - a percentage - so the numbers alone do not say. What says
            it is that there is no value with them: a real range can only have
            been learnt by reading or writing the parameter, and either would
            have stored the value too.
            """
            return (
                last.native_value is None
                and last.native_min_value == LEGACY_ASSUMED_MIN
                and last.native_max_value == LEGACY_ASSUMED_MAX
                and last.native_step == LEGACY_ASSUMED_STEP
            )

        @property
        def extra_state_attributes(self):
            """What the portal says that a number on its own cannot.

            `portal_value` is the dialog's own wording for the current
            selection. It repeats the value on an ordinary parameter, but it
            is the only readable answer where a special value is set: "Aus" is
            not a point on the scale, so the state itself goes unknown.

            `factory_default` is what the parameter left the factory with, as
            the portal states it. Home Assistant cannot colour a number that
            differs from its default - a number entity has no such option -
            but a dashboard card can compare against this and do it.
            """
            attributes = {}
            if self._portal_text is not None:
                attributes["portal_value"] = self._portal_text
            if self._factory_default is not None:
                attributes["factory_default"] = self._factory_default
            return attributes or None

        @property
        def mode(self) -> NumberMode:
            """A box while the range is a placeholder, a slider once it is real.

            Derived rather than stored, because the answer is a function of the
            bounds and those are updated in two places - a stored copy is how
            the step came to be updated by neither.

            The placeholder range spans 200000, where a slider is useless, so
            the value is typed. Once the portal has stated the real range that
            reverses: a box keeps its spinner arrows, and every arrow click is
            its own write that waits on the portal. A slider sends one value
            when the drag ends.
            """
            bounds_are_known = (
                self._attr_native_min_value != -EXPERT_UNKNOWN_BOUND
                and self._attr_native_max_value != EXPERT_UNKNOWN_BOUND
            )
            return NumberMode.AUTO if bounds_are_known else NumberMode.BOX

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
            self._apply_state(state)
            self.async_write_ha_state()

        async def async_set_native_value(self, value: float) -> None:
            """Write the value and wait for the portal to confirm it.

            The write used to run as a background task so the call returned
            at once, with the outcome going to a notification and the log.
            That told every caller the write had succeeded, whatever
            happened: an automation could carry on as if the heating had been
            set. Home Assistant's rule for entity methods is the opposite -
            a communication failure raises HomeAssistantError - and the
            domain service already worked that way, so the two disagreed
            about the same write.

            Nothing blocks but the caller. The portal work runs in an
            executor thread either way, so the event loop, the sensor poll
            and every other entity are unaffected; a second expert operation
            is refused outright rather than queued, as before.
            """
            from .models import raise_if_not_writable

            raise_if_not_writable(self._config_entry, self._attr_name)
            if self._write_in_progress:
                raise HomeAssistantError(
                    f"{self._attr_name}: a write is already in progress, please wait."
                )
            self._write_in_progress = True
            await self._async_write(value)

        async def async_will_remove_from_hass(self) -> None:
            """Stop an in-flight write as far as that is actually possible.

            The portal call runs in an executor thread, and nothing here can
            cancel a thread - the same distinction API_LOCK_TIMEOUT_SECONDS
            documents for the poll lock. A request already on the wire
            finishes, writing a heating parameter with the credentials of an
            entry being torn down. What is left is a cooperative stop:
            `_removed` is checked before the portal session is opened and
            again by the client after the login and before the writing
            request, so a write that has not reached the portal yet - the
            realistic case, since teardown and the executor race - gives up.

            Deliberately does not wait for a write in flight: that would hold
            the whole unload for as long as the portal takes, and waiting
            cannot abort the request anyway.
            """
            self._removed = True
            await super().async_will_remove_from_hass()

        def _raise_if_removed(self) -> None:
            """Abort gate handed to the portal client.

            Called from the worker thread, so it must only read state - it is
            a plain flag check on purpose.
            """
            if self._removed:
                raise ExpertOperationAborted(
                    f"{self._attr_name}: the entity was removed before the "
                    "write reached the portal"
                )

        async def _async_write(self, value: float) -> None:
            """Do the write and wait for the portal to confirm it."""
            from .expert_options import expert_client_options

            client_options = expert_client_options(self._config_entry.options)

            def _do_write():
                # Checked here AND handed to the client, which re-checks it
                # after the login and directly before the writing request.
                # One check at the top only covered a job the thread pool had
                # not started yet; an unload during the login (several
                # requests, seconds) still ran through to the write.
                self._raise_if_removed()
                # Shared per-account lock: only one expert portal operation
                # (this entity, the service, or the auto-poll) may run at a
                # time, so concurrent writes/reads don't collide on the same
                # heating parameter or open parallel portal sessions.
                lock = self._expert_lock()
                if lock is not None and not lock.acquire(blocking=False):
                    raise HomeAssistantError(
                        "Another expert operation is in progress for this "
                        "account; try again shortly."
                    )
                try:
                    client = WemPortalExpertClient(
                        self._config_entry.data.get(CONF_USERNAME),
                        self._config_entry.data.get(CONF_PASSWORD),
                        cooldown_check=self._cooldown_check(),
                        cooldown_activate=self._cooldown_activate(),
                        cookie_jar=self._cookie_jar(),
                        abort_check=self._raise_if_removed,
                        **client_options,
                    )
                    return client.write_parameter(self._entityvalue, value)
                finally:
                    if lock is not None:
                        lock.release()

            try:
                state = await self.hass.async_add_executor_job(_do_write)
            except ExpertOperationAborted as exc:
                # Somebody IS waiting on this. The write is awaited now, so
                # the service call or automation that asked for it is still
                # holding on - and returning quietly told it the heating had
                # been set when nothing reached the portal at all. That the
                # configuration went away is a reason for the write not to
                # happen, not a reason to say it did.
                _LOGGER.debug("Expert write for %s stopped: %s", self._attr_name, exc)
                raise HomeAssistantError(
                    f"Setting {self._attr_name} was stopped: {exc}"
                ) from exc
            # skipcq: PYL-W0706 - takes the range on board, then re-raises
            except ParameterWriteError as exc:
                # The write failed, and this failure knows what the portal
                # currently offers. Taking that on board is what stops the
                # next attempt failing the same way - a heating parameter's
                # limits can depend on other settings, so a range read weeks
                # ago need not still hold, and the auto-poll that would notice
                # is off by default.
                #
                # It cannot rescue a range that is wildly wrong: Home
                # Assistant validates against min/max before this entity is
                # asked, so a value outside the PUBLISHED range never gets
                # here. This corrects the overlapping case, which is the one
                # that occurs.
                #
                # Before the catch-all below, but also before HomeAssistantError:
                # WemPortalError derives from it, so the shorter clause would
                # take this one first and leave the range where it was.
                if exc.state is not None:
                    self._apply_state(exc.state)
                    self.async_write_ha_state()
                raise
            # skipcq: PYL-W0706 - shields the catch-all, not redundant
            except HomeAssistantError:
                # Already the right kind and already worded for the user -
                # the busy-lock refusal comes through here.
                raise
            except Exception as exc:
                _LOGGER.error("Expert write failed for %s: %s", self._attr_name, exc)
                raise HomeAssistantError(
                    f"Setting {self._attr_name} to {value} failed: {exc}"
                ) from exc
            finally:
                self._write_in_progress = False

            # Verified value from the portal, plus the real device range.
            self._apply_state(state)
            self.async_write_ha_state()
            _LOGGER.info(
                "Expert parameter %s set and verified: %s",
                self._attr_name,
                state.current,
            )
            self._notify_success(f"{self._attr_name} set to {state.current}.")

        def _notify_success(self, message: str) -> None:
            """Report a successful write, if the user asked to be told.

            Off by default (CONF_EXPERT_NOTIFY_ON_SUCCESS); the success is
            logged regardless. There is no failure counterpart any more: a
            failed write raises, so the caller hears about it where it can
            act on it rather than in a notification nobody is watching.
            """
            if not self._config_entry.options.get(CONF_EXPERT_NOTIFY_ON_SUCCESS, False):
                return
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "WEM Portal expert write",
                        "message": message,
                        "notification_id": f"wemportal_expert_{self._attr_unique_id}",
                    },
                    blocking=False,
                )
            )

        def _entry_api(self):
            """The api of this entity's entry, or None once it is unloaded.

            One accessor instead of the same three lines in four places -
            each of which used to spell out the store key by hand.
            """
            data = getattr(self._config_entry, "runtime_data", None)
            return data.api if data is not None else None

        def _cooldown_check(self):
            """Cooldown gate for this entity's writes.

            Uses the EXPERT gate, like the service and the auto-poll do: it
            honours a genuine portal-wide rate limit AND the expert-only
            backoff. Previously this used the global check, so an entity
            write ignored an active expert backoff.
            """
            api = self._entry_api()
            return api.check_expert_cooldown if api is not None else None

        def _cooldown_activate(self):
            """Engage the EXPERT-ONLY backoff on a 403 from this write.

            Previously this returned the GLOBAL activation, so a single
            rejected slider write paused all sensor polling - the very
            behaviour the expert-only backoff was introduced to end. The
            service and the auto-poll always used the expert one; this call
            site was missed.
            """
            api = self._entry_api()
            return api.activate_expert_cooldown if api is not None else None

        def _cookie_jar(self):
            """Shared in-memory session cache, so entity writes reuse the
            web session instead of logging in every time (the login is the
            request the portal rejects most readily)."""
            api = self._entry_api()
            return api.expert_cookies if api is not None else None

        def _expert_lock(self):
            """Shared per-entry lock (only one expert portal op at a time)."""
            data = getattr(self._config_entry, "runtime_data", None)
            return data.expert.lock if data is not None else None

        @property
        def device_info(self) -> DeviceInfo:
            return {
                "identifiers": {(DOMAIN, self._config_entry.entry_id)},
                "name": self._config_entry.title or "WEM Portal",
                "manufacturer": "Weishaupt",
            }

except ImportError:  # pragma: no cover - plain client use without HA
    pass
