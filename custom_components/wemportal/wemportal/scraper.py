"""Web scraping scraper for WEM Portal using curl_cffi."""

import time
import logging
from collections import defaultdict
from curl_cffi import requests
from lxml import html
from custom_components.wemportal.exceptions import AuthError, ExpiredSessionError
from custom_components.wemportal.const import (
    WEB_LOGIN_URL,
    WEB_MAIN_URL,
    TEMPERATURE_KEYWORDS,
    PERCENTAGE_KEYWORDS,
)
from custom_components.wemportal.utils import sanitize_value

_LOGGER = logging.getLogger(__name__)

class WemPortalScraper:
    """Scraper for navigating and extracting data from WEM Portal using curl_cffi."""

    def __init__(self, username, password, cookie=None):
        self.username = username
        self.password = password
        self.cookie = cookie if cookie else {}
        self.session = requests.Session(impersonate="chrome110")
        
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
        r_main = self.session.get(WEB_MAIN_URL)
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
        r_expert = self.session.post(WEB_MAIN_URL, data=form_data, allow_redirects=True)
        if WEB_LOGIN_URL.lower() in r_expert.url.lower():
            return None

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
            except Exception as exc:
                _LOGGER.debug(
                    "Could not restore cached WEM Portal cookies, skipping "
                    "session-reuse fast path: %s", exc
                )
            else:
                try:
                    reused_html = self._load_expert_page()
                except Exception as exc:
                    _LOGGER.debug(
                        "Session-reuse attempt failed, falling back to full login: %s", exc
                    )
                    reused_html = None

                if reused_html is not None:
                    _LOGGER.debug("Reused existing WEM Portal web session (skipped full login).")
                    return self.parse_expert_page(reused_html)

                _LOGGER.debug("Cached WEM Portal session is no longer valid, logging in again.")
                try:
                    self.session.cookies.clear()
                except Exception:
                    pass

        # --- Full login sequence ---
        # 1. GET Login page
        try:
            r1 = self.session.get(WEB_LOGIN_URL)
            if r1.status_code != 200:
                raise AuthError(f"Authentication Error: Received {r1.status_code} on login page.")
        except Exception as e:
            raise AuthError(f"Authentication Error: {e}")

        tree = html.fromstring(r1.text)
        viewstate_elem = tree.xpath("//*[@id='__VIEWSTATE']/@value")
        eventval_elem = tree.xpath("//*[@id='__EVENTVALIDATION']/@value")
        
        if not viewstate_elem or not eventval_elem:
            raise AuthError("Authentication Error: Could not find VIEWSTATE or EVENTVALIDATION.")
            
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
        
        r2 = self.session.post(WEB_LOGIN_URL, data=login_data, allow_redirects=True)
        if r2.status_code != 200:
            raise AuthError(f"Authentication Error: Encountered error after login. Received {r2.status_code}.")

        # Check if we were redirected back to login with an error (like AspxAutoDetectCookieSupport)
        if "AspxAutoDetectCookieSupport" in r2.url or WEB_LOGIN_URL.lower() in r2.url.lower():
            raise AuthError(f"Authentication Error: Login failed or cookies not detected. URL: {r2.url}")

        # Wait a moment
        time.sleep(2)

        # 3+4. GET Default.aspx and select the "Expert" tab
        expert_html = self._load_expert_page()
        if expert_html is None:
            raise AuthError("Scraping Error: Could not find VIEWSTATE on main page.")

        # 5. Extract data
        return self.parse_expert_page(expert_html)

    def parse_expert_page(self, html_content):
        _LOGGER.debug("Parsing expert page HTML")
        output = {}
        tree = html.fromstring(html_content)
        
        for div in tree.xpath('//div[contains(@class, "RadPanelBar RadPanelBar_Default rpbSimpleData")]'):
            header_elems = div.xpath('.//th[contains(@class, "simpleDataHeaderTextCell")]/span/text()')
            if header_elems:
                header_raw = header_elems[0].strip()
                header = (
                    header_elems[0].replace("/#", "")
                    .replace("  ", "")
                    .replace(" - ", "_")
                    .replace("/*+/*", "_")
                    .replace(" ", "_")
                    .casefold()
                )
            else:
                header_raw = "Unknown"
                header = "unknown"
                continue
                
            for td in div.xpath('.//div[contains(@class, "rpTemplate")]/table[contains(@class, "simpleDataTable")]/tbody/tr'):
                try:
                    name_elems = td.xpath('.//td[contains(@class, "simpleDataNameCell")]/span/text()')
                    val_elems = td.xpath('.//td[contains(@class, "simpleDataValueCell") or contains(@class, "simpleDataValueEnumCell")]/span/text()')
                    
                    if name_elems and val_elems:
                        raw_name = name_elems[0].strip()
                        friendly_name = f"{header_raw} - {raw_name.lstrip('- ')}"
                        
                        name = name_elems[0].replace("  ", "").replace(" ", "_").casefold()
                        name = header + "-" + name
                        original_value = val_elems[0].strip()
                        value = original_value
                        
                        split_value = value.split(" ", 1)
                        unit = ""
                        if len(split_value) >= 2:
                            value = split_value[0]
                            unit = split_value[1]
                        else:
                            value = split_value[0]
                            
                        try:
                            value = ".".join(value.split(","))
                            value = float(value)
                        except ValueError:
                            # If it's not a number, revert to the full string
                            value = original_value
                            unit = None

                        if not unit:
                            name_lower = name.lower()
                            if any(x in name_lower for x in TEMPERATURE_KEYWORDS):
                                unit = '°C'
                            elif any(x in name_lower for x in PERCENTAGE_KEYWORDS):
                                unit = '%'

                        # Handle missing or boolean values (shared, language-independent
                        # logic - see utils.sanitize_value for details/rationale).
                        if isinstance(value, str):
                            value = sanitize_value(value, unit, name)

                        icon_mapper = defaultdict(lambda: "mdi:flash")
                        icon_mapper["°C"] = "mdi:thermometer"

                        output[name] = {
                            "value": value,
                            "name": name,
                            "icon": icon_mapper[unit],
                            "unit": unit,
                            "platform": "sensor",
                            "friendlyName": friendly_name,
                            "ParameterID": name,
                        }
                except (IndexError, ValueError):
                    continue

        # Save cookies for next run (extracted from requests Session)
        cookies_dict = dict(self.session.cookies)
        output["cookie"] = cookies_dict
        return [output]
