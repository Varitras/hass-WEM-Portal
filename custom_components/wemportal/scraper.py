"""Web scraping scraper for WEM Portal using curl_cffi."""

import logging
import time

from curl_cffi import requests
from lxml import html

from .const import (
    GITHUB_PROJECT_URL,
    PERCENTAGE_KEYWORDS,
    SCRAPER_REQUEST_TIMEOUT_SECONDS,
    TEMPERATURE_KEYWORDS,
    WEB_LOGGED_IN_MARKER,
    WEB_LOGIN_FORM_MARKER,
    WEB_LOGIN_URL,
    WEB_MAIN_URL,
)

# Relative imports and the shared integration logger, consistent with every
# other module in this package (absolute custom_components.* imports would
# break if the install directory is ever named differently).
from .exceptions import (
    AuthError,
    ForbiddenError,
    PollDeadlineExceeded,
    PortalMaintenanceError,
    ServerError,
)
from .models import Reading, account_state
from .utils import (
    maintenance_notice,
    parse_portal_number,
    report_unexpected_maintenance_marker,
    sanitize_value,
    unit_to_icon,
)

_LOGGER = logging.getLogger(__name__)

# Unit -> icon mapping for scraped sensors. Defined once at module level
# instead of being re-created for every single table row during parsing
# (it never changes, so per-row construction was pure waste).


# The panel container the expert page is built from. Named once because two
# things ask about it: the parser, and the report that explains an empty page.
PANEL_XPATH = '//div[contains(@class, "RadPanelBar RadPanelBar_Default rpbSimpleData")]'

# The three selectors inside one panel: its heading, its rows, and within a
# row the label and the reading. Named because a bare XPath in the middle of a
# parse loop says nothing about what it is looking for.
PANEL_HEADING_XPATH = './/th[contains(@class, "simpleDataHeaderTextCell")]/span/text()'
PANEL_ROW_XPATH = (
    './/div[contains(@class, "rpTemplate")]'
    '/table[contains(@class, "simpleDataTable")]/tbody/tr'
)
ROW_NAME_XPATH = './/td[contains(@class, "simpleDataNameCell")]/span/text()'
ROW_VALUE_XPATH = (
    './/td[contains(@class, "simpleDataValueCell") '
    'or contains(@class, "simpleDataValueEnumCell")]/span/text()'
)


def _panel_key(heading: str) -> str:
    """The panel heading, folded into the half of a row key it contributes.

    Scraped rows have no id from the portal, so their key is built from the
    heading plus the row label - see _warn_about_renamed_scraper_keys in
    wemportalapi for what that costs when the portal rewords one.
    """
    return (
        heading.replace("/#", "")
        .replace("  ", "")
        .replace(" - ", "_")
        .replace("/*+/*", "_")
        .replace(" ", "_")
        .casefold()
    )


def _reading_and_unit(raw_value: str):
    """Split a cell like "21,5 °C" into a number and its unit.

    Anything that does not parse as a number keeps its FULL original string
    and gets no unit: the portal writes words in these cells too, and half of
    one ("Ein" out of "Ein Betrieb") would be worse than the whole.
    """
    parts = raw_value.split(" ", 1)
    unit = parts[1] if len(parts) >= 2 else ""
    number = parse_portal_number(parts[0])
    if number is None:
        return raw_value, None
    return number, unit


def _unit_from_name(name: str) -> str:
    """The unit the portal left off, from what the row is called."""
    lowered = name.lower()
    if any(word in lowered for word in TEMPERATURE_KEYWORDS):
        return "°C"
    if any(word in lowered for word in PERCENTAGE_KEYWORDS):
        return "%"
    return ""


def _report_duplicate_row(key, panel, row_name, reported) -> None:
    """Say that one reading has just overwritten another.

    The parser assigns into the output by key, which overwrites without a
    word. Two rows that produce the same key therefore leave one of them
    showing the other's value - a plausible number from the wrong place,
    which is the worst kind of wrong there is. Upstream issue #92 is exactly
    that, seen from the outside.

    Two ways to get here, and the message names both, because from here they
    are indistinguishable: the same row name twice inside one panel, or two
    panels whose headers are identical - the second of those puts every row
    of one circuit on top of another's and looks like a missing circuit.

    Reported, not resolved. Making the key unique would mint new entities and
    leave the old ones behind as corpses, for a collision nobody has yet been
    observed to have. A real log will say which of the two it is, and that is
    what a fix should be built on.
    """
    if key in reported:
        return
    reported.add(key)
    _LOGGER.warning(
        "Two rows of the WEM Portal expert page produce the same sensor (%s): "
        "panel %r, row %r. The later one wins and the earlier reading is lost, "
        "so a value shown here may belong to the other row. This happens when "
        "one panel lists a name twice, or when two panels carry the same "
        "heading. Please report it at %s with the panel headings you see.",
        key,
        panel,
        row_name,
        GITHUB_PROJECT_URL,
    )


class WemPortalScraper:
    """Scraper for navigating and extracting data from WEM Portal using curl_cffi."""

    def __init__(self, username, password, cookie=None, budget=None):
        self.username = username
        self.password = password
        # Once-per-subject warning memory, surviving the reload that rebuilds
        # this object. See models.AccountState.
        self._account_state = account_state(username)
        self.cookie = cookie if cookie else {}
        self.session = requests.Session(impersonate="chrome110")
        # Optional callable returning the seconds left of the poll cycle this
        # scrape belongs to, or None when it belongs to none - an on-demand
        # scrape has a user waiting on it and no coordinator timeout behind
        # it. See WemPortalApi.remaining_budget.
        self._budget = budget

    def _request_timeout(self):
        """How long the next request may take.

        The scrape's own timeout, unless the poll cycle has less than that
        left - then that, so a request cannot outlive the cycle it is part
        of. Checked here rather than at the four call sites because a check
        per site is a site to forget, which is how the deadline came to be
        checked once for a sequence of up to six requests.
        """
        if self._budget is None:
            return SCRAPER_REQUEST_TIMEOUT_SECONDS
        remaining = self._budget()
        if remaining is None:
            return SCRAPER_REQUEST_TIMEOUT_SECONDS
        if remaining <= 0:
            raise PollDeadlineExceeded(
                "The poll cycle ran out of time mid-scrape and stopped before "
                "its next request. Its partial readings are kept; the next "
                "cycle continues from them."
            )
        return min(SCRAPER_REQUEST_TIMEOUT_SECONDS, remaining)

    def _transport_failure(self, exc, what):
        """The right exception for a request that never came back.

        A timeout is normally the portal's problem. But the timeout handed to
        the request may have been the cycle's REMAINING BUDGET rather than the
        scrape's own - and then running out of it is the cycle stopping
        itself, which the coordinator treats differently on purpose: its
        deadline branch keeps the warm session, while two ordinary failures
        discard it and send the next cycle through a cold login.

        Returns the exception rather than raising it, so the `raise` stays at
        the call site where the control flow is visible.
        """
        budget_is_spent = False
        if self._budget is not None:
            remaining = self._budget()
            budget_is_spent = remaining is not None and remaining <= 0
        if budget_is_spent:
            return PollDeadlineExceeded(
                f"The poll cycle ran out of time while trying to {what}. Its "
                "partial readings are kept; the next cycle continues from them."
            )
        return ServerError(f"Could not {what}: {exc}")

    def close(self):
        """Release the underlying HTTP session/connection.

        Called when the owning WemPortalApi discards this scraper (e.g.
        on credential change or API re-instantiation), so the connection
        doesn't linger open on Weishaupt's side after we stop using it.
        """
        try:
            self.session.close()
        except Exception as exc:  # noqa: BLE001
            # Closing is best-effort; the session is being discarded anyway.
            _LOGGER.debug("Ignoring error while closing scraper session: %s", exc)

    def _check_response(self, response, what, check_maintenance=False):
        """The single gate every portal response passes through.

        Same reasoning as in expert_writer: deciding per request site what to
        validate means a per-site chance to forget, and that is exactly how a
        500 kept being parsed as a real page - once per audit round, at the
        next unguarded request.

        403 first, because it is a rate-limit signal and must reach the
        caller's cooldown handling as its own type. `check_maintenance` is
        opt-in: whether the marker can appear on a healthy page has not been
        established, so it stays where a real maintenance page was observed.
        """
        status = getattr(response, "status_code", 200)
        if status == 403:
            raise ForbiddenError(
                "WEM Portal web frontend returned 403 (rate limit/forbidden)."
            )
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
            report_unexpected_maintenance_marker(
                notice, what, self._account_state.maintenance_markers_reported
            )

    def _load_expert_page(self):
        """GET the main portal page and POST to select the 'Expert' tab.

        This is the second half of the scraping flow (steps 3+4 of the
        original single-method implementation), factored out so it can be
        reused both by a full login AND by the session-reuse fast path
        below, instead of duplicating this logic in two places.

        Returns:
            The HTML of the Expert page on success, or None if the current
            session is not (or no longer) authenticated - e.g. because we
            got redirected back to the login page, or the expected
            ASP.NET form fields are missing from the response. None is
            used (instead of raising) here because "session no longer
            valid" is an expected, recoverable condition for the fast
            path's caller, not necessarily a hard error.
        """
        # Classified like the login GET below, and for the sharper reason:
        # this is the fast path's own request, and its caller re-raises the
        # three answers it recognises while reading everything else as
        # "reuse failed, log in fresh". A raw timeout therefore fired two
        # MORE requests at a portal that had just failed to answer one.
        try:
            r_main = self.session.get(WEB_MAIN_URL, timeout=self._request_timeout())
        except Exception as exc:
            raise self._transport_failure(exc, "load the WEM Portal main page") from exc
        # A 500 has no __VIEWSTATE, so without this it fell through to
        # `return None` - which the full login reports as an AuthError, i.e. a
        # server outage blamed on the credentials.
        self._check_response(r_main, "main page")
        if WEB_LOGIN_URL.lower() in r_main.url.lower():
            return None

        tree_main = html.fromstring(r_main.text)
        viewstate_main_elem = tree_main.xpath("//*[@id='__VIEWSTATE']/@value")
        eventval_main_elem = tree_main.xpath("//*[@id='__EVENTVALIDATION']/@value")
        pageview_main_elem = tree_main.xpath("//*[@id='__ECNPAGEVIEWSTATE']/@value")

        if not viewstate_main_elem or not eventval_main_elem:
            return None

        form_data = {
            "__EVENTVALIDATION": eventval_main_elem[0],
            "__VIEWSTATE": viewstate_main_elem[0],
            "__ECNPAGEVIEWSTATE": pageview_main_elem[0] if pageview_main_elem else "",
            "__EVENTTARGET": "ctl00$SubMenuControl1$subMenu",
            "__EVENTARGUMENT": "3",
            "ctl00_rdMain_ClientState": '{"Top":0,"Left":0,"DockZoneID":"ctl00_RDZParent","Collapsed":false,"Pinned":false,"Resizable":false,"Closed":false,"Width":"99%","Height":null,"ExpandedHeight":0,"Index":0,"IsDragged":false}',
            "ctl00_SubMenuControl1_subMenu_ClientState": '{"logEntries":[{"Type":3},{"Type":1,"Index":"0","Data":{"text":"Overview","value":"110"}},{"Type":1,"Index":"1","Data":{"text":"System:+dom","value":""}},{"Type":1,"Index":"2","Data":{"text":"User","value":"222"}},{"Type":1,"Index":"3","Data":{"text":"Expert","value":"223","selected":true}},{"Type":1,"Index":"4","Data":{"text":"Statistics","value":"225"}},{"Type":1,"Index":"5","Data":{"text":"Data+Loggers","value":"224"}}],"selectedItemIndex":"3"} ',
        }

        # 4. POST to select 'Expert' tab
        try:
            r_expert = self.session.post(
                WEB_MAIN_URL,
                data=form_data,
                allow_redirects=True,
                timeout=self._request_timeout(),
            )
        except Exception as exc:
            raise self._transport_failure(
                exc, "open the WEM Portal expert view"
            ) from exc
        self._check_response(r_expert, "expert page", check_maintenance=True)
        if WEB_LOGIN_URL.lower() in r_expert.url.lower():
            return None

        # A non-200 body is an error page, not the expert view. Returned
        # unchecked it was parsed like a real page: the scrape counted as
        # successful and the sensors took on whatever fell out of a 500.
        #
        # Raised rather than returned as None, and for both callers. None
        # means "this session is no longer valid", which the full login turns
        # into an AuthError - so a plain server error was reported as a wrong
        # password and fed the re-authentication counter, where three outages
        # in a row could ask the user to re-enter working credentials. It also
        # stops the reuse path from answering a 500 with two more requests.
        return r_expert.text

    def scrape(self):
        """Perform the scraping process and return the extracted data."""
        # --- Fast path: try to reuse the session/cookie from the previous
        # successful scrape first, instead of always performing a full
        # login handshake (GET login page + POST credentials) on every
        # single scrape cycle. A full login is 2 extra HTTP requests plus
        # resubmitting credentials every time, which adds avoidable load
        # on Weishaupt's server. If anything about the reuse attempt
        # fails, we fall through to the exact original full-login flow
        # below, so this can never behave worse than before - only
        # potentially faster/lighter.
        if self.cookie:
            try:
                self.session.cookies.update(self.cookie)
            except Exception as exc:  # noqa: BLE001
                # Broad: restoring a cached cookie is an optimisation.
                # Whatever goes wrong, the full login below still works.
                _LOGGER.debug(
                    "Could not restore cached WEM Portal cookies, skipping "
                    "session-reuse fast path: %s",
                    exc,
                )
            else:
                try:
                    reused_html = self._load_expert_page()
                # skipcq: PYL-W0706 - shields the catch-all, not redundant
                except (ForbiddenError, PortalMaintenanceError, ServerError):
                    # All three are answers, not reuse failures. Falling
                    # through to the full login would fire two MORE requests
                    # right after the server said "rate limited", "we are
                    # down" or "something broke" - the opposite of backing
                    # off, and for a server error it would also relabel the
                    # outage as an authentication problem.
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Broad, but the three answers that must NOT be
                    # retried are re-raised above. Everything left is a
                    # reuse failure, and the full login handles those.
                    _LOGGER.debug(
                        "Session-reuse attempt failed, falling back to full login: %s",
                        exc,
                    )
                    reused_html = None

                if reused_html is not None:
                    # required=False: for the fast path an empty page is not a
                    # broken portal. The postback that selects the Expert tab
                    # carries state from the reused session, and when the
                    # portal no longer honours it the answer is still HTTP
                    # 200 - the main page instead of the expert view, with no
                    # login redirect and no error status for the checks above
                    # to catch. Raising there lost the whole scrape for a
                    # cycle while a fresh login, which the lines below already
                    # perform, would have worked.
                    panels = self.parse_expert_page(
                        reused_html, source="the reused session", required=False
                    )
                    if panels is not None:
                        _LOGGER.debug(
                            "Reused existing WEM Portal web session (skipped full login)."
                        )
                        return panels
                    _LOGGER.debug(
                        "The reused session did not reach the expert page; "
                        "logging in fresh."
                    )

                _LOGGER.debug(
                    "Cached WEM Portal session is no longer valid, logging in again."
                )
                try:
                    self.session.cookies.clear()
                except Exception as exc:  # noqa: BLE001
                    # Non-fatal: we re-login below regardless.
                    _LOGGER.debug("Ignoring error while clearing cookies: %s", exc)

        # --- Full login sequence ---
        # 1. GET Login page
        try:
            login_page = self.session.get(
                WEB_LOGIN_URL, timeout=self._request_timeout()
            )
        except Exception as exc:
            # A transport failure (timeout, connection reset, DNS) says
            # nothing about the credentials. Reported as AuthError it fed the
            # reauth counter, so three network hiccups in a row could ask the
            # user to re-enter a working password.
            raise self._transport_failure(
                exc, "reach the WEM Portal login page"
            ) from exc
        # Deliberately outside the try block: our own ForbiddenError /
        # AuthError below must propagate as-is instead of being caught by
        # the broad network-error handler above and re-wrapped (which
        # would, among other things, hide the 403 from the caller's
        # cooldown handling).
        # Maintenance is checked HERE, before the POST, so the password is
        # never sent to a page that cannot process it: during planned
        # downtime the login form is fully present and submittable, and
        # posting would simply fail as "invalid credentials".
        self._check_response(login_page, "login page", check_maintenance=True)

        tree = html.fromstring(login_page.text)
        viewstate_elem = tree.xpath("//*[@id='__VIEWSTATE']/@value")
        eventval_elem = tree.xpath("//*[@id='__EVENTVALIDATION']/@value")

        if not viewstate_elem or not eventval_elem:
            # NOT an AuthError: the password has not been sent yet - these
            # fields are what it would be sent WITH. A 200 that is not the
            # login form is the portal misbehaving, and blaming the
            # credentials for it fed the reauth counter, so three such
            # hiccups in a row could ask for a password that was correct all
            # along. Same reasoning as the transport handler above; this was
            # the half of it that got left behind.
            raise ServerError(
                "The WEM Portal login page came back without its form fields, "
                "so there was nothing to log in with. This is a portal-side "
                "problem, not a credential one."
            )

        viewstate = viewstate_elem[0]
        eventval = eventval_elem[0]

        # 2. POST Login
        login_data = {
            "__VIEWSTATE": viewstate,
            "__EVENTVALIDATION": eventval,
            "ctl00$content$tbxUserName": self.username,
            "ctl00$content$tbxPassword": self.password,
            "ctl00$content$btnLogin": "Anmelden",
        }

        try:
            login_response = self.session.post(
                WEB_LOGIN_URL,
                data=login_data,
                allow_redirects=True,
                timeout=self._request_timeout(),
            )
        except Exception as exc:
            raise self._transport_failure(exc, "send the WEM Portal login") from exc
        # check_maintenance, like the GET above and the main page below. What
        # comes back here is one of those two pages, and both are checked for
        # the notice everywhere else - this was the only place it was not.
        # The cost of leaving it out is specific: the response arrives on the
        # login URL, which is also the test for "the portal rejected these
        # credentials", so announced downtime read as a wrong password and
        # fed the re-authentication counter. Three cycles inside one
        # maintenance window ask the user for a password that is correct.
        self._check_response(login_response, "login POST", check_maintenance=True)

        # Three outcomes, not two - the classification web_login has used all
        # along. Staying on the login URL was the whole test before, and a
        # portal error or interstitial does that too while answering HTTP 200,
        # so anything of that kind counted as "wrong password" and three in a
        # row walked into a re-authentication prompt. Only the login FORM is
        # evidence that credentials were seen and refused.
        logged_in = WEB_LOGGED_IN_MARKER in login_response.text
        if not logged_in and WEB_LOGIN_FORM_MARKER in login_response.text:
            raise AuthError("Authentication Error: Invalid username or password.")
        if not logged_in:
            raise ServerError(
                "The WEM Portal answered the login with a page that is neither "
                "a session nor the login form, so there is nothing to read and "
                "nothing to say about the credentials. This can also mean the "
                f"portal did not accept our cookies. URL: {login_response.url}"
            )

        # Wait a moment
        time.sleep(2)

        # 3+4. GET Default.aspx and select the "Expert" tab
        expert_html = self._load_expert_page()
        if expert_html is None:
            # NOT an AuthError, whichever of its two meanings this is. The
            # login succeeded a few lines up, so the credentials are the one
            # thing that has just been proven. _load_expert_page answers None
            # both for "the session is no longer valid" and for "the page
            # came back without its form state"; before a login that
            # ambiguity is harmless, because the caller's answer to both is
            # to log in fresh, and here there is no fresher login to try.
            raise ServerError(
                "Logged in, but the WEM Portal main page did not come back "
                "with the expert view on it. This is a portal-side problem, "
                "not a credential one."
            )

        # 5. Extract data
        return self.parse_expert_page(expert_html, source="the expert page")

    def _report_empty_page(self, html_content, source, level=logging.WARNING):
        """Say what the page WAS, because the failure message cannot.

        "Contained no readable panels" is true of two completely different
        problems and names neither: a page that is not the expert view at all,
        and the expert view with markup the selectors no longer match. The
        panel container is what separates them - present but unparsed means
        the portal changed its HTML, absent means we were looking at the wrong
        page - and the answer decides whether this needs a new selector or a
        fresh login.

        `level` is the caller's, not this function's, because the same empty
        page means different things to the two of them - see parse_expert_page.
        """
        text = html_content or ""
        title = ""
        containers = 0
        try:
            tree = html.fromstring(text)
            found = tree.xpath("//title/text()")
            title = found[0].strip() if found else ""
            # Counted with the SAME selector the parser uses, not by looking
            # for the class name in the text: "RadPanelBar" appears twice in
            # one container's class attribute, so a substring count answers a
            # different question than the one being asked.
            containers = len(tree.xpath(PANEL_XPATH))
        except Exception:  # noqa: BLE001
            # The report must never be the thing that fails.
            title = "<unparseable>"
        _LOGGER.log(
            level,
            "No readable panels on %s: %d bytes, title %r, %d panel container(s). %s",
            source,
            len(text),
            title,
            containers,
            "Zero containers means this was not the expert page; one or more "
            "means the page is there but its markup no longer matches.",
        )

    def _panel_rows(self, div):
        """The heading of one panel and the rows under it, or None.

        No heading means no stable sensor name can be built for anything in
        this panel, so the whole panel is skipped.
        """
        headings = div.xpath(PANEL_HEADING_XPATH)
        if not headings:
            return None
        return headings[0].strip(), _panel_key(headings[0]), div.xpath(PANEL_ROW_XPATH)

    def _row_sensor(self, heading, panel_key, row):
        """One reading from one table row, or None if the row carries none.

        Returns the row's key, the portal's own wording for it (which the
        collision report needs, and which the key has been folded past) and
        the sensor itself.
        """
        names = row.xpath(ROW_NAME_XPATH)
        values = row.xpath(ROW_VALUE_XPATH)
        if not (names and values):
            return None

        raw_name = names[0].strip()
        name = panel_key + "-" + names[0].replace("  ", "").replace(" ", "_").casefold()
        value, unit = _reading_and_unit(values[0].strip())
        if not unit:
            unit = _unit_from_name(name) or unit
        # Handle missing or boolean values (shared, language-independent
        # logic - see utils.sanitize_value for details/rationale).
        if isinstance(value, str):
            value = sanitize_value(value)

        return (
            name,
            raw_name,
            Reading(
                value=value,
                icon=unit_to_icon(unit),
                unit=unit,
                platform="sensor",
                friendly_name=f"{heading} - {raw_name.lstrip('- ')}",
                parameter_id=name,
            ),
        )

    def _panel_readings(self, heading, panel_key, rows) -> list:
        """The rows of one panel that yielded a reading, in page order.

        Returns them rather than collecting into a dict: whether a name has
        been seen before is a question about the WHOLE page, and answering it
        per panel would miss two panels sharing a heading.
        """
        readings = []
        for row in rows:
            try:
                reading = self._row_sensor(heading, panel_key, row)
            except (IndexError, ValueError):
                continue
            if reading is None:
                continue
            readings.append(reading)
        return readings

    def parse_expert_page(self, html_content, source="the expert page", required=True):
        """Turn the expert page into sensor dicts.

        `required=False` means "tell me if this is not the expert page" -
        return None instead of raising. The session-reuse path needs that: for
        it, a page with no panels is not a broken portal but a session that
        did not get us to the expert view, which is exactly what the full
        login below exists to answer.
        """
        _LOGGER.debug("Parsing expert page HTML (%s)", source)
        output = {}
        tree = html.fromstring(html_content)

        for div in tree.xpath(PANEL_XPATH):
            panel = self._panel_rows(div)
            if panel is None:
                continue
            heading, panel_key, rows = panel
            # Collected into ONE output across all panels, because a
            # collision can also be two panels carrying the same heading -
            # see _report_duplicate_row. Per-panel dicts would hide exactly
            # that half of it.
            for name, raw_name, sensor in self._panel_readings(
                heading, panel_key, rows
            ):
                if name in output:
                    _report_duplicate_row(
                        name,
                        heading,
                        raw_name,
                        self._account_state.duplicate_rows_reported,
                    )
                output[name] = sensor

        # A page that parsed to nothing is not a successful scrape. The XPaths
        # above simply find no panels on an error or placeholder page served
        # with HTTP 200, which left `output` empty - and the caller then reset
        # the retry counter and timestamp as if data had arrived, so the
        # existing readings stayed on display looking current.
        if not output:
            # The level follows `required`, because the two callers do not
            # mean the same thing by an empty page. For the reuse path it is
            # the expected end of a cheap attempt - scrape() falls back to a
            # full login three lines later and says so at debug - so a warning
            # put a self-healing normal case into the user's error log
            # sixteen times a day while the code quietly repaired it. After a
            # full login there is nothing left to try, so it stays a warning.
            self._report_empty_page(
                html_content,
                source,
                level=logging.WARNING if required else logging.DEBUG,
            )
            if not required:
                return None
            raise ServerError(
                "The WEM Portal expert page contained no readable panels."
            )

        # Save cookies for next run (extracted from requests Session)
        cookies_dict = dict(self.session.cookies)
        output["cookie"] = cookies_dict
        return [output]
