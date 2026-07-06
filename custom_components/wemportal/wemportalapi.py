"""
Weishaupt webscraping and API library
"""


import copy
import time
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
import requests as reqs
from homeassistant.const import CONF_SCAN_INTERVAL
from .exceptions import (
    AuthError,
    ForbiddenError,
    UnknownAuthError,
    WemPortalError,
    ExpiredSessionError,
    ParameterChangeError,
    ServerError,
)

from .const import (
    _LOGGER,
    API_DATA_ACCESS_READ_URL,
    API_DATA_ACCESS_WRITE_URL,
    API_DEVICE_READ_URL,
    API_EVENT_TYPE_READ_URL,
    API_LOGIN_URL,
    API_REFRESH_URL,
    API_DEVICE_STATUS_READ_URL,
    API_CIRCUIT_TIMES_REFRESH_URL,
    API_CIRCUIT_TIMES_READ_URL,
    API_STATISTICS_REFRESH_URL,
    API_STATISTICS_READ_URL,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DATA_GATHERING_ERROR,
    WEM_INVALID_PARAMETER_STATUS,
    DEFAULT_CONF_LANGUAGE_VALUE,
    DEFAULT_CONF_MODE_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    FORBIDDEN_COOLDOWN_SECONDS,
    CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS,
    STATISTICS_REFRESH_INTERVAL_SECONDS,
)


class WemPortalApi:
    """Wrapper class for Weishaupt WEM Portal"""

    def __init__(self, username, password, config=None, existing_data=None, cached_modules=None, blocked_until=0.0) -> None:
        if config is None:
            config = {}
        self.data = copy.deepcopy(existing_data) if existing_data else {}
        self.username = username
        self.password = password
        # Previously-discovered device/module/parameter metadata, if any
        # (e.g. persisted across Home Assistant restarts, see __init__.py).
        # When present, this lets fetch_data() skip the slow, rate-limited
        # per-module parameter discovery in get_parameters() and go
        # straight to normal polling. `None` means "no cache available" and
        # preserves the original behavior of doing a full discovery.
        self.modules = copy.deepcopy(cached_modules) if cached_modules else None
        # Tracks whether get_devices() has already run once during the
        # lifetime of this WemPortalApi instance (i.e. once per Home
        # Assistant session/restart), so it isn't repeated on every single
        # coordinator update - only the initial discovery/refresh needs it.
        self._devices_fetched_this_session = False
        self.mode = config.get(CONF_MODE, DEFAULT_CONF_MODE_VALUE)
        self.update_interval = timedelta(
            seconds=min(
                config.get(CONF_SCAN_INTERVAL, DEFAULT_CONF_SCAN_INTERVAL_VALUE),
                config.get(
                    CONF_SCAN_INTERVAL_API, DEFAULT_CONF_SCAN_INTERVAL_API_VALUE
                ),
            )
        )
        self.scan_interval = timedelta(
            seconds=config.get(CONF_SCAN_INTERVAL, DEFAULT_CONF_SCAN_INTERVAL_VALUE)
        )
        self.scan_interval_api = timedelta(
            seconds=config.get(
                CONF_SCAN_INTERVAL_API, DEFAULT_CONF_SCAN_INTERVAL_API_VALUE
            )
        )
        self.valid_login = False
        self.language = config.get(CONF_LANGUAGE, DEFAULT_CONF_LANGUAGE_VALUE)
        self.session = None
        self.webscraping_cookie = {}
        # Persistent scraper instance, kept across coordinator cycles so
        # its underlying HTTP session (TCP connection + TLS handshake) is
        # reused instead of being torn down and re-established on every
        # single scrape. Created lazily on first use in
        # fetch_webscraping_data(). Note: the session-cookie reuse (which
        # skips the login *requests*) is separate from this - keeping the
        # instance also skips the per-cycle connection setup itself.
        self._scraper = None
        self.last_scraping_update = None
        # Headers used for all API calls
        self.headers = {
            "User-Agent": "WeishauptWEMApp",
            "X-Api-Version": "3.1.3.0",
            "Accept": "*/*",
            "Host": "www.wemportal.com"
        }
        self.scraping_mapper = {}
        self.last_statistics_fetch = 0.0
        # Timestamp (per device+parameter) of the last time a heating
        # schedule (CircuitTimes) was actually fetched, so it can be
        # refreshed at most every CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS
        # instead of on every single coordinator cycle - these rarely
        # change and this integration doesn't allow editing them anyway.
        self._last_circuit_times_fetch = {}
        # Monotonic timestamp until which ALL outbound requests are
        # paused, activated after receiving a 403 (rate limit/forbidden)
        # from the server anywhere in a cycle. This is a strictly
        # additive safety measure: it only ever makes the integration
        # quieter after the server has already signaled distress, never
        # more aggressive. See _check_cooldown()/_activate_cooldown().
        # Accepted as a constructor argument so an active cooldown survives
        # the coordinator re-instantiating this object on repeated errors -
        # otherwise a fresh instance would reset it to 0.0 and resume
        # hitting a server that just told us to back off.
        self._blocked_until = blocked_until

        # Used to keep track of how many update intervals to wait before retrying spider
        self.spider_wait_interval = 0
        # Used to keep track of the number of times the spider consecutively fails
        self.spider_retry_count = 0
        self.api_version = None

    def _activate_cooldown(self, seconds=FORBIDDEN_COOLDOWN_SECONDS):
        """Pause ALL further outbound requests for a while after being
        rate-limited (HTTP 403) by the WEM Portal server.

        This intentionally affects every subsequent make_api_call(), not
        just the one that got the 403 - continuing to hit *other*
        endpoints (statistics, circuit times, ...) right after the server
        already signaled it's unhappy would defeat the purpose. Never
        shortens an existing cooldown, only extends it.
        """
        new_blocked_until = time.monotonic() + seconds
        if new_blocked_until > self._blocked_until:
            self._blocked_until = new_blocked_until
            _LOGGER.warning(
                "WEM Portal returned a rate-limit/forbidden (403) response. "
                "Pausing ALL requests for %s minutes to avoid making it worse.",
                seconds // 60,
            )

    def _check_cooldown(self):
        """Raise ForbiddenError immediately, without making any request,
        if we're still within a cooldown period from a previous 403."""
        if self._blocked_until and time.monotonic() < self._blocked_until:
            remaining = int(self._blocked_until - time.monotonic())
            raise ForbiddenError(
                f"Still cooling down after a previous rate-limit response "
                f"({remaining}s remaining). Skipping requests until then."
            )

    def fetch_data(self, enabled_devices=None):
        # Fail fast, without any network activity at all, if we're still
        # within a cooldown window from a previous 403 (see
        # _activate_cooldown). This is checked again inside
        # make_api_call() for every individual call too, but checking
        # once up front avoids even starting a cycle (login attempts,
        # etc.) that we already know will be aborted immediately.
        self._check_cooldown()
        try:
            if self.mode != "web":
                # Login and get device info
                if not self.valid_login:
                    self.api_login()
                # Refresh the device/module list once per session (cheap).
                # This intentionally runs whether or not we started with a
                # module cache: it's what discovers devices on a fresh
                # install, and what picks up newly added devices/modules on
                # an existing one - while preserving any cached parameter
                # definitions (see get_devices()).
                if not self._devices_fetched_this_session:
                    self.get_devices()
                    self._devices_fetched_this_session = True

                # Only run the slow, rate-limited per-module parameter
                # discovery (get_parameters(), ~5 sec sleep per module) if
                # something is actually missing its parameter definitions -
                # e.g. a brand new install, a newly added module, or a
                # previous discovery attempt that got interrupted/rate
                # limited. With a valid persisted cache, this is skipped
                # entirely after a restart, which is what makes Home
                # Assistant startup fast again.
                needs_recovery = False
                for _, modules in self.modules.items():
                    for module in modules.values():
                        if "parameters" not in module:
                            needs_recovery = True
                            break
                if needs_recovery:
                    _LOGGER.info("Attempting to recover missing parameter definitions...")
                    self.get_parameters()

            # Select data source based on mode
            if self.mode == "web":
                # Get data by web scraping
                webscraping_data = self.fetch_webscraping_data()
                self._merge_webscraping_data(next(iter(self.data), "0000"), webscraping_data)
            elif self.mode == "api":
                # Get data using API
                self.get_data(enabled_devices)
            else:
                # Get data using web scraping if it hasn't been updated recently,
                # otherwise use API to get data
                if self.last_scraping_update is None or (
                    (
                        (
                            datetime.now()
                            - self.last_scraping_update
                            + timedelta(seconds=10)
                        )
                        > self.scan_interval
                    )
                    and self.spider_wait_interval == 0
                ):
                    # Get data by web scraping
                    try:
                        webscraping_data = self.fetch_webscraping_data()
                        self._merge_webscraping_data(next(iter(self.data), "0000"), webscraping_data)

                        # Update last_scraping_update timestamp
                        self.last_scraping_update = datetime.now()
                    except Exception as exc:
                        _LOGGER.warning("Web scraper failed this cycle. Falling back to API only. Error: %s", exc)
                        # We intentionally do not raise, so the API can still fetch the bulk of the data
                        
                else:
                    # Reduce spider_wait_interval by 1 if > 0
                    self.spider_wait_interval = (
                        self.spider_wait_interval - 1
                        if self.spider_wait_interval > 0
                        else self.spider_wait_interval
                    )

                # Get data using API (always run as a resilient fallback)
                self.get_data(enabled_devices)


            # Return data
            return self.data

        except Exception as exc:
            if isinstance(exc, WemPortalError):
                # Re-raise known errors so we don't wrap them twice
                raise
            # Wrap any unexpected python crashes to prevent HA from halting
            raise WemPortalError("Unexpected error occurred while fetching data") from exc

    def _merge_webscraping_data(self, device_id, webscraping_data):
        if str(device_id) not in self.data:
            self.data[str(device_id)] = {}
            
        from .translations import translate
        for key, new_val in webscraping_data.items():
            if isinstance(new_val, dict):
                if "friendlyName" in new_val:
                    new_val["friendlyName"] = translate(self.language, new_val["friendlyName"])
                
                # Preserve the old unit if the current scrape is missing it (e.g. value is "--")
                # This prevents Home Assistant from complaining about unit changes.
                if new_val.get("unit") in (None, ""):
                    old_val = self.data[str(device_id)].get(key)
                    if isinstance(old_val, dict) and old_val.get("unit") not in (None, ""):
                        new_val["unit"] = old_val.get("unit")

                # Preserve the old value if the current scrape returned no
                # value at all. Without this, a single missed/garbled read
                # would make the sensor drop to "Unknown" and create a gap
                # in its history, even though the previous value is still
                # very likely accurate until the next successful update.
                if new_val.get("value") is None:
                    old_val = self.data[str(device_id)].get(key)
                    if isinstance(old_val, dict) and old_val.get("value") is not None:
                        new_val["value"] = old_val.get("value")

            self.data[str(device_id)][key] = new_val

    def _reset_scraper(self):
        """Discard the persistent scraper instance (closing its HTTP
        session) so the next scraping cycle starts with a completely
        fresh connection - used after auth/session errors where reusing
        the old connection state could keep failing."""
        if self._scraper is not None:
            self._scraper.close()
            self._scraper = None

    def fetch_webscraping_data(self):
        """
        Call scraper to crawl WEM Portal.
        This function manages the process of initiating a web scraping job, 
        handling errors, and returning the scraped data.
        """
        from .scraper import WemPortalScraper

        # Respect an active rate-limit cooldown for the scraping path too,
        # not just the API path - a 403 from either frontend means the
        # server wants us to back off everywhere.
        self._check_cooldown()

        # Reuse the existing scraper (and with it, its warm HTTP
        # connection) across cycles; only create a new one on first use
        # or after it was deliberately discarded (see _reset_scraper).
        if self._scraper is None:
            self._scraper = WemPortalScraper(
                self.username,
                self.password,
                self.webscraping_cookie
            )
        else:
            # Keep the scraper's cookie view in sync with ours (ours may
            # have been cleared after an auth error since the last cycle).
            self._scraper.cookie = self.webscraping_cookie or {}
        scraper = self._scraper

        try:
            # Attempt to run the scraping job and extract the first result
            data = scraper.scrape()[0]

        except IndexError as exc:
            # Handle the case where the job result is not found
            self.spider_retry_count += 1
            if self.spider_retry_count == 2:
                self.webscraping_cookie = None
            self.spider_wait_interval = self.spider_retry_count
            raise WemPortalError(DATA_GATHERING_ERROR) from exc

        except ForbiddenError:
            # The web frontend rate-limited us (403). Activate the same
            # global cooldown the API path uses, discard the scraper
            # (fresh connection once the cooldown expires), and let the
            # error propagate so the coordinator's backoff kicks in too.
            self._activate_cooldown()
            self._reset_scraper()
            raise

        except AuthError as exc:
            # Handle authentication errors. Also discard the persistent
            # scraper: its connection/cookie state just failed to
            # authenticate, so the next attempt should start fresh.
            self.webscraping_cookie = None
            self._reset_scraper()
            raise AuthError(
                "AuthenticationError: Could not login with provided username and password. "
                "Check if your config contains the right credentials"
            ) from exc

        except ExpiredSessionError as exc:
            # Handle errors due to expired session (fresh start next cycle).
            self.webscraping_cookie = None
            self._reset_scraper()
            raise ExpiredSessionError(
                "ExpiredSessionError: Session expired. Next update will try to login again."
            ) from exc

        except Exception as exc:
            # Catch-all for anything else (e.g. a plain network failure -
            # connection reset, DNS issue, timeout - during the scraping
            # login/request sequence). Previously these weren't caught
            # here at all, so a simple network hiccup skipped the same
            # retry-count/backoff bookkeeping that IndexError gets above,
            # even though it's just as recoverable.
            self.spider_retry_count += 1
            self.spider_wait_interval = self.spider_retry_count
            raise WemPortalError(f"{DATA_GATHERING_ERROR} ({exc})") from exc

        try:
            # Attempt to update the cookie from the scraped data
            self.webscraping_cookie = data["cookie"]
            del data["cookie"]
        except KeyError:
            # If the cookie is not found in the data, simply pass
            pass

        # Reset retry count and wait interval after a successful operation
        self.spider_retry_count = 0
        self.spider_wait_interval = 0

        # Return the scraped data
        return data

    def api_login(self):
        payload = {
            "Name": self.username,
            "PasswordUTF8": self.password,
            "AppID": "com.weishaupt.wemapp",
            "AppVersion": "2.0.2",
            "ClientOS": "Android",
        }
        if self.session is not None:
            self.session.close()
        self.session = reqs.Session()
        self.session.cookies.clear()
        self.session.headers.update(self.headers)
        try:
            response = self.session.post(
                API_LOGIN_URL,
                data=payload,
            )
            response.raise_for_status()
            
            # Verify the response is actually valid JSON and successful
            response_data = response.json()
            if response_data.get("Status") != 0:
                raise AuthError(f"Login failed: Server returned {response_data}")
                
            self.api_version = response_data.get("Version")
            _LOGGER.debug("API login successful for %s", self.username)
            self.valid_login = True
            
        except ValueError as exc: # Catches JSONDecodeError if response is HTML
            _LOGGER.warning("API login failed for %s. Received HTML instead of JSON.", self.username)
            self.valid_login = False
            raise WemPortalError("API login failed: received HTML instead of JSON (Possible rate limit or WAF block)") from exc
        except reqs.exceptions.RequestException as exc:
            # Broader than just HTTPError: also covers ConnectionError,
            # Timeout, etc. - genuine network failures that aren't tied to
            # a specific HTTP status code, which previously weren't caught
            # here at all and would fall through to the generic
            # "unexpected error" wrapper in fetch_data() instead of a
            # clear, specific error message.
            _LOGGER.warning("API login failed for %s with a network/HTTP error.", self.username)
            self.valid_login = False
            response_status, response_message = self.get_response_details(response)
            if response is None:
                raise UnknownAuthError(
                    f"Authentication Error: Could not reach WEM Portal ({exc})."
                ) from exc
            elif response.status_code == 400:
                raise AuthError(
                    f"Authentication Error: Check if your login credentials are correct. Received response code: {response.status_code}, response: {response.content}. Server returned internal status code: {response_status} and message: {response_message}"
                ) from exc
            elif response.status_code == 403:
                self._activate_cooldown()
                raise ForbiddenError(
                    f"WemPortal forbidden error: Server returned internal status code: {response_status} and message: {response_message}"
                ) from exc
            elif response.status_code == 500:
                raise ServerError(
                    f"WemPortal server error: Server returned internal status code: {response_status} and message: {response_message}"
                ) from exc
            else:
                raise UnknownAuthError(
                    f"Authentication Error: Encountered an unknown authentication error. Received response code: {response.status_code}, response: {response.content}. Server returned internal status code: {response_status} and message: {response_message}"
                ) from exc


    def web_login(self):
        """
        Logs into the WEM Portal web interface by mimicking browser behavior.
        Args:
            username (str): The user's username (email).
            password (str): The user's password.
        Returns:
            dict: Session cookies for the authenticated session.
        Raises:
            AuthError: If the login credentials are invalid.
            ForbiddenError: If access is forbidden.
            UnknownAuthError: For other unknown login errors.
        """
        session = reqs.Session()
        from .const import WEB_LOGIN_URL
        login_url = WEB_LOGIN_URL

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "de,en;q=0.9",
        }

        # Step 1: Fetch the login page
        try:
            initial_response = session.get(login_url, headers=headers)
            initial_response.raise_for_status()
        except reqs.exceptions.RequestException as exc:
            raise UnknownAuthError(f"Failed to load the login page: {exc}") from exc

        # Step 2: Parse the login page and extract hidden form fields
        soup = BeautifulSoup(initial_response.text, "html.parser")
        form_data = {}
        for input_tag in soup.find_all("input"):
            if input_tag.get("type") == "hidden" and input_tag.get("name"):
                form_data[input_tag["name"]] = input_tag.get("value", "")

        # Add username and password to the form data
        form_data["ctl00$content$tbxUserName"] = self.username
        form_data["ctl00$content$tbxPassword"] = self.password
        form_data["ctl00$content$btnLogin"] = "Anmelden"  # Login button value

        # Step 3: Submit the login form
        response = None
        try:
            response = session.post(
                login_url,
                data=form_data,
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response.raise_for_status()

            # Step 4: Check if login was successful
            if "ctl00_btnLogout" in response.text:
                _LOGGER.debug("WEB login successful for %s", self.username)
                return
            else:
                raise AuthError("Login failed: Invalid username or password.")
        except reqs.exceptions.RequestException as exc:
            if response is not None and response.status_code == 403:
                self._activate_cooldown()
                raise ForbiddenError("Access forbidden during login.") from exc
            raise UnknownAuthError(f"Failed to submit the login form: {exc}") from exc

    def get_response_details(self, response: reqs.Response):
        server_status = ""
        server_message = ""
        # Use "is not None" rather than a plain truthiness check: a
        # requests.Response object is falsy whenever status_code >= 400
        # (see Response.__bool__), i.e. exactly when there's an error to
        # diagnose. The previous check silently skipped reading the body
        # in that case, discarding the server's own error details.
        if response is not None:
            try:
                response_data = response.json()
                _LOGGER.debug(response_data)
                # Status we get back from server
                server_status = response_data["Status"]
                server_message = response_data["Message"]
            except (KeyError, ValueError):
                pass
        return server_status, server_message


    def make_api_call(
        self, url: str, headers=None, data=None, do_retry=True, delay=5
    ) -> reqs.Response:
        attempts = 2 if do_retry else 1
        response = None

        for attempt in range(attempts):
            # Fail fast if we're still cooling down from a previous 403 -
            # applies to every single call site that goes through here,
            # not just the one that originally triggered it.
            self._check_cooldown()

            time.sleep(1)  # Wait 1 sec between requests to be graceful to the API.
            # Merge any call-specific headers on top of the default headers,
            # instead of replacing them outright. Previously, passing e.g.
            # headers={"X-Api-Version": "2.0.0.0"} (as get_statistics() does)
            # would silently drop "Host", "User-Agent" and "Accept" for that
            # call, which could cause it to be rejected by the server.
            current_headers = {**self.headers, **(headers or {})}

            try:
                if not data:
                    _LOGGER.debug("Sending GET request to %s with headers: %s", url, current_headers)
                    response = self.session.get(url, headers=current_headers, timeout=10)
                else:
                    _LOGGER.debug("Sending POST request to %s with headers: %s and data: %s", url, current_headers, data)
                    response = self.session.post(url, headers=current_headers, json=data, timeout=10)

                response.raise_for_status()

                # Check for stealthy session expiration (HTML redirect)
                if "Account/Login" in response.url or (hasattr(response, "redirect_url") and response.redirect_url and "Account/Login" in str(response.redirect_url)):
                    raise ExpiredSessionError("Redirected to Account/Login")

                _LOGGER.debug(response)
                return response

            except (reqs.exceptions.RequestException, ExpiredSessionError) as exc:
                status_code = (
                    response.status_code
                    if isinstance(exc, reqs.exceptions.RequestException) and response is not None
                    else None
                )

                if status_code == 403:
                    # A 403 means the server is already unhappy with our
                    # request rate - immediately retrying with a fresh
                    # login (as we do for a plain expired session below)
                    # would itself be an extra request at exactly the
                    # wrong time. Back off hard instead: no retry, pause
                    # everything for a while, and surface it as
                    # ForbiddenError so callers' existing 403-handling
                    # (e.g. get_parameters()'s forbidden_count) still works.
                    self._activate_cooldown()
                    server_status, server_message = self.get_response_details(response)
                    self.valid_login = False
                    forbidden_error = ForbiddenError(
                        f"{DATA_GATHERING_ERROR} Server returned status code: {server_status} and message: {server_message}"
                    )
                    forbidden_error.server_status = server_status
                    raise forbidden_error from exc

                # A genuinely expired session (401, or a stealthy redirect
                # to the login page) is worth one immediate retry with a
                # fresh login - unlike a 403, this isn't a sign we're
                # sending too many requests, just that the current session
                # is no longer valid.
                is_session_error = isinstance(exc, ExpiredSessionError) or status_code == 401

                if is_session_error and attempt < attempts - 1:
                    _LOGGER.info("Session expired for %s. Re-authenticating...", url)
                    self.api_login()
                    time.sleep(delay)
                    continue  # Loop back around and retry

                # If we're out of retries or it's a completely different error:
                server_status, server_message = self.get_response_details(response)
                
                # The old logic recreated the entire API instance when this happened.
                # To emulate that recovery mechanism without losing cached metadata,
                # we invalidate the login state so the next cycle creates a fresh requests.Session.
                self.valid_login = False
                
                wem_error = WemPortalError(
                    f"{DATA_GATHERING_ERROR} Server returned status code: {server_status} and message: {server_message}"
                )
                # Expose the server-side status code so callers can react to
                # specific ones (e.g. Statistics skips an invalid group)
                # without parsing the message string.
                wem_error.server_status = server_status
                raise wem_error from exc

        return response

    def get_devices(self):
        """Fetch the current device/module list from the API.

        This refreshes the device list, module list and connection status
        (one relatively cheap API call) every time it's called. Crucially,
        it does NOT discard already-known "parameters" for modules that
        still exist (e.g. loaded from a persisted cache, or discovered
        earlier this session) - only get_parameters() populates/refreshes
        those, and that step is comparatively slow/rate-limited (a sleep
        per module). Preserving cached parameters here is what allows
        fetch_data() to skip that slow discovery after a Home Assistant
        restart when a valid cache exists.
        """
        _LOGGER.debug("Fetching api device data")
        previously_known_modules = self.modules or {}
        self.modules = {}
        self.data = {}
        data = self.make_api_call(API_DEVICE_READ_URL, do_retry=True).json()

        for device in data["Devices"]:
            device_id_str = str(device["ID"])
            self.data[device_id_str] = {}
            self.modules[device_id_str] = {}
            previously_known_device_modules = previously_known_modules.get(device_id_str, {})
            for module in device["Modules"]:
                module_key = (module["Index"], module["Type"])
                module_entry = {
                    "Index": module["Index"],
                    "Type": module["Type"],
                    "Name": module["Name"],
                }
                cached_module = previously_known_device_modules.get(module_key)
                if cached_module and "parameters" in cached_module:
                    module_entry["parameters"] = cached_module["parameters"]
                self.modules[device_id_str][module_key] = module_entry
            self.data[device_id_str]["ConnectionStatus"] = device["ConnectionStatus"]

    def get_parameters(self):
        if self.modules is None:
            _LOGGER.debug("get_parameters() called with no module data available yet; skipping.")
            return
        for device_id, device_data in self.data.items():
            if device_data.get("ConnectionStatus") != 0:
                continue
            _LOGGER.debug("Fetching api parameters data for device %s", device_id)
            _LOGGER.debug(self.data)
            _LOGGER.debug(self.modules[device_id])
            delete_candidates = []
            forbidden_count = 0
            for key, values in self.modules[device_id].items():
                # Check if parameters are already cached
                if "parameters" in values and values["parameters"]:
                    _LOGGER.debug(
                        "Parameters for device %s, index %s, and type %s are already cached.",
                        device_id, values["Index"], values["Type"]
                    )
                    continue
                data = {
                    "DeviceID": int(device_id),
                    "ModuleIndex": values["Index"],
                    "ModuleType": values["Type"],
                }
                try:
                    time.sleep(5)
                    response = self.make_api_call(
                        API_EVENT_TYPE_READ_URL, data=data, do_retry=False
                    )
                except WemPortalError as exc:
                    if isinstance(exc.__cause__, reqs.exceptions.HTTPError):
                        status_code = exc.__cause__.response.status_code
                        if status_code == 403:
                            forbidden_count += 1
                            if forbidden_count >= 3:
                                _LOGGER.error(
                                    "Rate limited (403) three times while fetching parameters "
                                    "for device %s. Aborting.",
                                    device_id
                                )
                                self._activate_cooldown()
                                raise ForbiddenError("Rate limited during get_parameters") from exc
                            
                            _LOGGER.warning(
                                "Rate limit warning (403) for device %s module %s. Strike %s of 3.",
                                device_id,
                                values["Index"],
                                forbidden_count
                            )
                            continue
                        elif status_code == 400:
                            _LOGGER.warning(
                                "Module index %s type %s is unsupported by WEM Portal. "
                                "Deleting from cache.",
                                values["Index"],
                                values["Type"]
                            )
                            delete_candidates.append((values["Index"], values["Type"]))
                            continue
                    raise
                parameters = {}
                try:
                    for parameter in response.json()["Parameters"]:
                        parameters[parameter["ParameterID"]] = parameter
                    if not parameters:
                        delete_candidates.append((values["Index"], values["Type"]))
                    else:
                        self.modules[device_id][(values["Index"], values["Type"])][
                            "parameters"
                        ] = parameters
                except (KeyError, ValueError):
                    # ValueError also covers a JSON-decode failure (e.g. an
                    # HTML error page returned instead of JSON) - without
                    # it, a single malformed response here would abort
                    # discovery for every remaining module on this device,
                    # not just skip this one.
                    _LOGGER.warning(
                        "An error occurred while gathering parameters data for module %s. Skipping this module. "
                        "If this problem persists, open an issue at "
                        "https://github.com/erikkastelec/hass-WEM-Portal/issues",
                        values
                    )
                    continue
            for key in delete_candidates:
                del self.modules[device_id][key]

    def change_value(
        self,
        device_id,
        parameter_id,
        module_index,
        module_type,
        numeric_value,
        login=True,
    ):
        """POST request to API to change a specific value"""
        _LOGGER.debug("Changing value for %s", parameter_id)

        data = {
            "DeviceID": int(device_id),
            "Modules": [
                {
                    "ModuleIndex": int(module_index),
                    "ModuleType": int(module_type),
                    "Parameters": [
                        {
                            "ParameterID": parameter_id,
                            "NumericValue": float(numeric_value),
                        }
                    ],
                }
            ],
        }
        # _LOGGER.info(data)

        try:
            self.make_api_call(
                API_DATA_ACCESS_WRITE_URL,
                data=data,
                do_retry=True
            )
        except Exception as exc:
            raise ParameterChangeError(
                f"Error changing parameter {parameter_id} value"
            ) from exc

    # Refresh data and retrieve new data
    def get_data(self, enabled_devices=None):
        _LOGGER.debug("Fetching fresh api data. enabled_devices=%s, self.data.keys()=%s", enabled_devices, list(self.data.keys()))
        target_devices = enabled_devices if enabled_devices else list(self.data.keys())
        _LOGGER.debug("Computed target_devices=%s", target_devices)
        for device_id in target_devices:
            _LOGGER.debug("Processing device_id=%s (type %s). Is in self.data? %s", device_id, type(device_id), str(device_id) in self.data)
            if str(device_id) not in self.data:
                continue
                
            # 1. Fetch Device Status First
            try:
                status_response = self.make_api_call(
                    API_DEVICE_STATUS_READ_URL,
                    data={"DeviceID": int(device_id)},
                    do_retry=True
                ).json()

                status_map = {0: "online", 7: "wrong_secret", 8: "busy", 50: "offline"}
                conn_status = status_map.get(status_response.get("ConnectionStatus", -1), "unknown")

                self.data[device_id][f"{device_id}-ConnectionStatus"] = {
                    "friendlyName": "Connection Status",
                    "ParameterID": "ConnectionStatus",
                    "unit": None,
                    "value": conn_status,
                    "IsWriteable": False,
                    "DataType": -1,
                    "ModuleIndex": -1,
                    "ModuleType": -1,
                    "platform": "sensor",
                    "icon": "mdi:network"
                }

                errors = status_response.get("Errors", [])
                has_errors = "Yes" if errors else "No"
                error_msg = ", ".join([str(e) for e in errors]) if errors else "None"

                self.data[device_id][f"{device_id}-HasErrors"] = {
                    "friendlyName": "Has Errors",
                    "ParameterID": "HasErrors",
                    "unit": None,
                    "value": has_errors,
                    "IsWriteable": False,
                    "DataType": -1,
                    "ModuleIndex": -1,
                    "ModuleType": -1,
                    "platform": "sensor",
                    "icon": "mdi:alert"
                }

                self.data[device_id][f"{device_id}-ErrorMessages"] = {
                    "friendlyName": "Error Messages",
                    "ParameterID": "ErrorMessages",
                    "unit": None,
                    "value": error_msg[:255],
                    "IsWriteable": False,
                    "DataType": -1,
                    "ModuleIndex": -1,
                    "ModuleType": -1,
                    "platform": "sensor",
                    "icon": "mdi:message-alert"
                }

                if conn_status != "online":
                    _LOGGER.warning("Device %s is %s. Skipping data polling.", device_id, conn_status)
                    continue

            except Exception as exc:
                _LOGGER.warning("Failed to fetch Device Status: %s", exc)

            # 2. Proceed with data fetch
            try:
                data = {
                    "DeviceID": int(device_id),
                    "Modules": [
                        {
                            "ModuleIndex": module["Index"],
                            "ModuleType": module["Type"],
                            "Parameters": [
                                {"ParameterID": parameter}
                                for parameter in module["parameters"].keys()
                            ],
                        }
                        for module in self.modules[device_id].values()
                        if "parameters" in module and module["parameters"]
                    ],
                }
            except KeyError as exc:
                _LOGGER.debug("%s: %s", DATA_GATHERING_ERROR, self.modules[device_id])
                raise WemPortalError(DATA_GATHERING_ERROR) from exc

            try:
                self.make_api_call(
                    API_REFRESH_URL,
                    data=data,
                )
                time.sleep(5)
                values = self.make_api_call(
                    API_DATA_ACCESS_READ_URL,
                    data=data,
                    do_retry=True
                ).json()
                from .mapper import WemPortalDataMapper
                WemPortalDataMapper.process_api_values(
                    device_id=device_id,
                    values_json=values,
                    modules_dict=self.modules,
                    language=self.language,
                    scraping_mapper=self.scraping_mapper,
                    mode=self.mode,
                    api_data=self.data,
                )
            except Exception as exc:
                _LOGGER.warning("Failed to fetch parameter data... %s", exc)

            # 3. Fetch Heating Schedules (DataType == 6)
            try:
                for module in self.modules[device_id].values():
                    module_index = module.get("Index")
                    module_type = module.get("Type")
                    if "parameters" in module:
                        for param_id, param_data in module["parameters"].items():
                            if param_data.get("DataType") == 6:  # WemDataType.PROGRAM
                                # Heating schedules rarely change (only via
                                # the WEM Portal app directly - this
                                # integration only ever shows them
                                # read-only), so refetching them on every
                                # single coordinator cycle is unnecessary
                                # load. Skip if we already fetched this
                                # specific schedule recently enough.
                                cache_key = (device_id, param_id)
                                last_fetch = self._last_circuit_times_fetch.get(cache_key, 0)
                                if time.time() - last_fetch < CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS:
                                    continue
                                try:
                                    refresh_payload = {
                                        "DeviceID": int(device_id),
                                        "ModuleIndex": module_index,
                                        "ModuleType": module_type,
                                        "ParameterID": param_id
                                    }

                                    job_resp = self.make_api_call(
                                        API_CIRCUIT_TIMES_REFRESH_URL,
                                        data=refresh_payload,
                                        do_retry=True
                                    ).json()

                                    job_id = job_resp.get("JobID")
                                    if job_id is None:
                                        continue

                                    time.sleep(2)  # Give backend time to build the schedule payload

                                    read_payload = {
                                        "DeviceID": int(device_id),
                                        "JobID": job_id,
                                        "ModuleIndex": module_index,
                                        "ModuleType": module_type,
                                        "ParameterID": param_id
                                    }

                                    schedule_resp = self.make_api_call(
                                        API_CIRCUIT_TIMES_READ_URL,
                                        data=read_payload,
                                        do_retry=True
                                    ).json()

                                    sensor_name = f"{module['Name']}-{param_id}"
                                    if sensor_name not in self.data[device_id]:
                                        from .translations import friendly_name_mapper, translate
                                        self.data[device_id][sensor_name] = {
                                            "friendlyName": translate(self.language, friendly_name_mapper(param_id)),
                                            "ParameterID": param_id,
                                            "unit": None,
                                            "value": "Active",
                                            "IsWriteable": False,
                                            "DataType": 6,
                                            "ModuleIndex": module_index,
                                            "ModuleType": module_type,
                                            "platform": "sensor",
                                            "icon": "mdi:calendar-clock",
                                        }

                                    self.data[device_id][sensor_name]["CircuitTimesDay"] = schedule_resp.get("CircuitTimesDay", [])
                                    self.data[device_id][sensor_name]["PossibleValues"] = schedule_resp.get("PossibleValues", [])
                                    self.data[device_id][sensor_name]["value"] = "Active"
                                    self._last_circuit_times_fetch[cache_key] = time.time()

                                except Exception as exc:
                                    _LOGGER.warning("Failed to fetch CircuitTimes for %s: %s", param_id, exc)
            except Exception as exc:
                _LOGGER.warning("Error processing CircuitTimes: %s", exc)

        # 4. Fetch Energy Statistics (Rate limited)
        self.get_statistics(enabled_devices)

    def get_statistics(self, enabled_devices=None):
        """Fetch historical statistics from the API, rate limited to once per hour."""
        now = time.time()
        if self.last_statistics_fetch is not None and (now - self.last_statistics_fetch) < STATISTICS_REFRESH_INTERVAL_SECONDS:
            return
            
        self.last_statistics_fetch = now
        _LOGGER.debug("Fetching statistics data")
        
        target_devices = enabled_devices if enabled_devices else list(self.data.keys())
        for device_id in target_devices:
            if device_id not in self.data:
                continue
            try:
                refresh_resp = self.make_api_call(
                    API_STATISTICS_REFRESH_URL,
                    data={"DeviceID": int(device_id)},
                    do_retry=True
                ).json()
                
                group_types = refresh_resp.get("GroupTypeDescriptions", [])
                headers = {"X-Api-Version": "2.0.0.0"}
                
                for group in group_types:
                    group_id = group.get("GroupType")
                    group_name = group.get("Description")
                    if not group_name or group_name.strip() == "":
                        fallback_names = {
                            1: "Heating Energy Yield",
                            2: "Hot Water Energy Yield",
                            3: "Cooling Energy Yield",
                            4: "Total Energy Yield",
                            5: "Power Consumption Heating",
                            6: "Power Consumption Hot Water",
                            7: "Power Consumption Cooling",
                            8: "Total Power Consumption"
                        }
                        group_name = fallback_names.get(group_id, f"Energy {group_id}")
                    else:
                        from .translations import translate
                        translated_group = translate(self.language, group_name)
                        if "energy" not in translated_group.lower():
                            group_name = f"{translated_group} Energy"
                        else:
                            group_name = translated_group
                    
                    read_payload = {
                        "DeviceID": int(device_id),
                        "ModuleType": 7,
                        "ModuleIndex": 0,
                        "GroupType": group_id,
                        "Type": 1
                    }
                    
                    try:
                        time.sleep(2)  # Avoid hammering the API
                        stats_resp = self.make_api_call(
                            API_STATISTICS_READ_URL,
                            headers=headers,
                            data=read_payload,
                            do_retry=True
                        ).json()
                        
                        values = stats_resp.get("Values", [])
                        if not values:
                            continue
                            
                        # The last value in the array is the current day's consumption
                        latest_stat = values[-1]
                        current_value = latest_stat.get("Value")

                        sensor_name = f"Energy_{group_id}"

                        if current_value is None:
                            # Missing reading this cycle - keep the last known
                            # value instead of falling back to 0.0, which would
                            # otherwise show up as a false drop/spike on the
                            # Energy Dashboard.
                            old_sensor = self.data.get(device_id, {}).get(f"{device_id}-{sensor_name}")
                            if isinstance(old_sensor, dict) and old_sensor.get("value") is not None:
                                current_value = old_sensor.get("value")
                            else:
                                current_value = 0.0

                        unit = stats_resp.get("Unit", "kWh")

                        self.data[device_id][f"{device_id}-{sensor_name}"] = {
                            "friendlyName": group_name,
                            "ParameterID": sensor_name,
                            "unit": unit,
                            "value": current_value,
                            "IsWriteable": False,
                            "DataType": -1,
                            "ModuleIndex": -1,
                            "ModuleType": -1,
                            "platform": "sensor",
                            "icon": "mdi:lightning-bolt",
                            "device_class": "energy",
                            "state_class": "total_increasing"
                        }
                        
                    except Exception as exc:
                        # Status 3001 = this statistics group isn't valid for
                        # the queried module. The refresh call lists such
                        # groups but reading them is rejected; that's expected
                        # and harmless, so skip it quietly instead of warning
                        # on every startup. Any other error is still surfaced.
                        # Compared as str so an int or str server_status both match.
                        server_status = getattr(exc, "server_status", None)
                        if str(server_status) == str(WEM_INVALID_PARAMETER_STATUS):
                            _LOGGER.debug(
                                "Skipping statistics group %s: not valid for this module (status %s).",
                                group_id, WEM_INVALID_PARAMETER_STATUS,
                            )
                        else:
                            _LOGGER.warning("Failed to fetch Statistics for group %s: %s", group_id, exc)
                        
            except Exception as exc:
                _LOGGER.warning("Error processing Statistics: %s", exc)
