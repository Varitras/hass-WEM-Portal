"""
Weishaupt webscraping and API library
"""

import copy
import threading
import time
from datetime import datetime, timedelta

import requests
from homeassistant.const import CONF_SCAN_INTERVAL
from lxml import html
from lxml.etree import ParserError

from .const import (
    _LOGGER,
    API_CIRCUIT_TIMES_READ_URL,
    API_CIRCUIT_TIMES_REFRESH_URL,
    API_DATA_ACCESS_READ_URL,
    API_DATA_ACCESS_WRITE_URL,
    API_DEVICE_READ_URL,
    API_DEVICE_STATUS_READ_URL,
    API_EVENT_TYPE_READ_URL,
    API_LOCK_TIMEOUT_SECONDS,
    API_LOGIN_URL,
    API_REFRESH_URL,
    API_REQUEST_TIMEOUT_SECONDS,
    API_STATISTICS_READ_URL,
    API_STATISTICS_REFRESH_URL,
    API_TRANSPORT_RETRY_DELAY_SECONDS,
    CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS,
    CIRCUIT_TIMES_RETRY_INTERVAL_SECONDS,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DATA_GATHERING_ERROR,
    DEFAULT_CONF_LANGUAGE_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    DEFAULT_MODE,
    DEFAULT_TIMEOUT,
    EXPERT_FORBIDDEN_COOLDOWN_SECONDS,
    FORBIDDEN_COOLDOWN_SECONDS,
    GITHUB_PROJECT_URL,
    MIN_SCAN_INTERVAL_API_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    PARAMETER_REDISCOVERY_INTERVAL_SECONDS,
    PARAMETER_REDISCOVERY_RETRY_SECONDS,
    POLL_DEADLINE_SECONDS,
    SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE,
    SCRAPER_FALLBACK_DEVICE_ID,
    SCRAPER_REQUEST_TIMEOUT_SECONDS,
    STATISTICS_REFRESH_INTERVAL_SECONDS,
    STATISTICS_RETRY_INTERVAL_SECONDS,
    WEB_LOGGED_IN_MARKER,
    WEB_LOGIN_FORM_MARKER,
    WEB_LOGIN_URL,
    WEM_INVALID_PARAMETER_STATUS,
    WemDataType,
)
from .exceptions import (
    ApiBusyError,
    AuthError,
    ExpiredSessionError,
    ForbiddenError,
    ParameterChangeError,
    PollDeadlineExceeded,
    PortalMaintenanceError,
    ServerError,
    UnknownAuthError,
    WemPortalError,
)
from .mapper import WemPortalDataMapper
from .mobile_protocol import (
    read_refresh_ticket,
    read_write_ack,
    status_is_success,
)
from .translations import friendly_name_mapper, translate
from .utils import (
    clamped_scan_interval,
    error_state_and_detail,
    latest_statistics_entry,
    looks_like_schedule,
    maintenance_notice,
)

# The three rows a device status read owns. Named once because they are
# written in one place and forgotten in another when the read fails: a
# fourth row added to only one of the two lists would go on being published
# as current long after nobody could read it. They happen to be named after
# the portal fields they carry, which is why the literals look familiar.
DEVICE_STATUS_CONNECTION = "ConnectionStatus"
DEVICE_STATUS_HAS_ERRORS = "HasErrors"
DEVICE_STATUS_ERROR_MESSAGES = "ErrorMessages"
DEVICE_STATUS_ROWS = (
    DEVICE_STATUS_CONNECTION,
    DEVICE_STATUS_HAS_ERRORS,
    DEVICE_STATUS_ERROR_MESSAGES,
)


# Devices whose refresh answered without a JobID, so the warning below is
# raised once per device instead of on every cycle.
_MISSING_JOB_ID_REPORTED = set()


# The 403 backoffs live here rather than on the instance, and that placement
# is the fix for a defect, not a style choice.
#
# They used to be instance state carried forward by whoever built the next
# WemPortalApi. That works only where there IS a previous instance to copy
# from. There isn't, in the case that matters most: when the first refresh
# fails, Home Assistant discards everything and calls async_setup_entry again,
# so the retry built a fresh object with the backoff reset to zero - and the
# most likely reason for that first refresh to fail is the very 403 that set
# it. The same hole let a config-flow validation walk straight into an active
# cooldown, because that path has no previous instance either.
#
# Nothing can forget to pass on what it never has to pass on.
#
# The global one is global because the limit is: the portal counts requests
# per IP, so a 403 earned by one account is a statement about every account
# behind the same address. The expert one is per account on purpose - a 403
# there is frequently one rejected request rather than an IP-wide limit (see
# activate_expert_cooldown), so it must not spread.
_BLOCKED_UNTIL = 0.0
_EXPERT_BLOCKED_UNTIL: dict[str, float] = {}


def _extend_cooldown(current: float, until: float) -> float:
    """Only ever later, never sooner - the rule both backoffs already had."""
    return max(current, until)


def reset_cooldowns_for_tests() -> None:
    """Drop both backoffs. Only the test suite has any business calling this;
    production has no situation in which forgetting a 403 is correct."""
    global _BLOCKED_UNTIL
    _BLOCKED_UNTIL = 0.0
    _EXPERT_BLOCKED_UNTIL.clear()


def _report_missing_job_id(device_id):
    """Say once that a device started no identifiable measurement job.

    A read without a JobID is answered from the most recent job, which may be
    the PREVIOUS measurement served as current. Whether a healthy portal ever
    answers this way is not established - this is what would establish it.
    """
    if device_id in _MISSING_JOB_ID_REPORTED:
        return
    _MISSING_JOB_ID_REPORTED.add(device_id)
    _LOGGER.warning(
        "Device %s answered the measurement refresh without a JobID. The "
        "read then returns whichever job the portal considers newest, which "
        "may be the previous measurement. Please report this together with "
        "your portal model - see %s",
        device_id,
        GITHUB_PROJECT_URL,
    )


class WemPortalApi:
    """Wrapper class for Weishaupt WEM Portal"""

    def __init__(
        self,
        username,
        password,
        config=None,
        existing_data=None,
        cached_modules=None,
        blocked_until=0.0,
        scraper_device_id=None,
        expert_blocked_until=0.0,
        scraper_backoff=None,
    ) -> None:
        """Assemble the api object from three sources, kept apart because
        they have different lifetimes: the user's options, the state the
        coordinator persisted across a restart, and the state that always
        starts empty. Which of these a RECOVERY may reset is a separate
        question - see reset_transport.
        """
        self.username = username
        self.password = password
        self._init_from_config(config)
        self._init_from_storage(
            existing_data,
            cached_modules,
            scraper_device_id,
            blocked_until,
            expert_blocked_until,
            scraper_backoff,
        )
        self._init_runtime_state()

    @property
    def _blocked_until(self) -> float:
        """Monotonic time until which ALL outbound requests are paused.

        Set after a 403 anywhere in a cycle - a strictly additive safety
        measure: it only ever makes the integration quieter after the server
        has already signalled distress. Backed by module state, so it survives
        every way this object gets rebuilt. See _BLOCKED_UNTIL.
        """
        return _BLOCKED_UNTIL

    @_blocked_until.setter
    def _blocked_until(self, value: float) -> None:
        global _BLOCKED_UNTIL
        _BLOCKED_UNTIL = _extend_cooldown(_BLOCKED_UNTIL, value or 0.0)

    @property
    def _expert_blocked_until(self) -> float:
        """The EXPERT-ONLY backoff, per account.

        A 403 on the Fachmann path pauses that path alone (see
        EXPERT_FORBIDDEN_COOLDOWN_SECONDS for why); the polling paths keep
        running. The reverse still holds: a genuine rate limit seen by the
        API or scraper pauses the expert path too, because
        check_expert_cooldown() consults check_cooldown() first.
        """
        return _EXPERT_BLOCKED_UNTIL.get(self.username, 0.0)

    @_expert_blocked_until.setter
    def _expert_blocked_until(self, value: float) -> None:
        _EXPERT_BLOCKED_UNTIL[self.username] = _extend_cooldown(
            _EXPERT_BLOCKED_UNTIL.get(self.username, 0.0), value or 0.0
        )

    def _init_from_config(self, config):
        """Everything the user chose in the options flow."""
        if config is None:
            config = {}
        self.mode = config.get(CONF_MODE, DEFAULT_MODE)
        # Clamped, not read verbatim: the floors live in the options-flow
        # schema, which only sees values the user enters now - a value stored
        # by an older release is otherwise used exactly as it was saved.

        scan_interval = clamped_scan_interval(
            config,
            CONF_SCAN_INTERVAL,
            DEFAULT_CONF_SCAN_INTERVAL_VALUE,
            MIN_SCAN_INTERVAL_SECONDS,
        )
        scan_interval_api = clamped_scan_interval(
            config,
            CONF_SCAN_INTERVAL_API,
            DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
            MIN_SCAN_INTERVAL_API_SECONDS,
        )
        self.update_interval = timedelta(seconds=min(scan_interval, scan_interval_api))
        self.scan_interval = timedelta(seconds=scan_interval)
        self.scan_interval_api = timedelta(seconds=scan_interval_api)
        self.language = config.get(CONF_LANGUAGE, DEFAULT_CONF_LANGUAGE_VALUE)

    def _init_from_storage(
        self,
        existing_data,
        cached_modules,
        scraper_device_id,
        blocked_until,
        expert_blocked_until,
        scraper_backoff,
    ):
        """State the coordinator persisted, handed back after a restart.

        Every argument here exists because starting from zero was wrong:
        a fresh object would poll a portal that had just asked us to back
        off, rediscover modules it already knew, and give the scraped
        sensors a new device id - and with it a new history."""
        self.data = copy.deepcopy(existing_data) if existing_data else {}
        # Stable device id under which scraped (web) sensors are stored, so
        # their entity unique_ids ("<entry>:<device_id>:<name>") - and thus
        # their history - stay constant across mode switches. Decided once
        # (see resolve_scraper_device_id) and persisted by the coordinator,
        # so it never silently changes even when a real API device id
        # becomes known later (e.g. a pure-web install switching to `both`).
        # None here means "not yet decided / not yet loaded from storage".
        self.scraper_device_id = scraper_device_id
        # Previously-discovered device/module/parameter metadata, if any
        # (e.g. persisted across Home Assistant restarts, see __init__.py).
        # When present, this lets fetch_data() skip the slow, rate-limited
        # per-module parameter discovery in get_parameters() and go
        # straight to normal polling. `None` means "no cache available" and
        # preserves the original behavior of doing a full discovery.
        self.modules = copy.deepcopy(cached_modules) if cached_modules else None
        # Monotonic timestamp until which ALL outbound requests are
        # paused, activated after receiving a 403 (rate limit/forbidden)
        # from the server anywhere in a cycle. This is a strictly
        # additive safety measure: it only ever makes the integration
        # quieter after the server has already signaled distress, never
        # more aggressive. See check_cooldown()/_activate_cooldown().
        # Accepted as a constructor argument so an active cooldown survives
        # the coordinator re-instantiating this object on repeated errors -
        # otherwise a fresh instance would reset it to 0.0 and resume
        # hitting a server that just told us to back off.
        # Seeded, not stored: both backoffs live at module level so that no
        # construction site can drop them (see _BLOCKED_UNTIL). The arguments
        # remain because callers that DO hold a previous value still pass it,
        # and extending is harmless - a caller can only ever push the backoff
        # further out, never pull it in.
        self._blocked_until = blocked_until
        self._expert_blocked_until = expert_blocked_until

        # Scrape backoff, carried across a coordinator swap when given.
        #
        # The coordinator builds a fresh WemPortalApi to recover from repeated
        # errors, and a fresh instance started at zero - so the backoff the
        # scraper had just earned was discarded by the very recovery those
        # failures triggered, and the next cycle scraped immediately.
        # `last_scraping_update` belongs to the same state: without it the
        # interval check has no reference point and scrapes at once.
        wait_interval, retry_count, last_update = scraper_backoff or (0, 0, None)
        # Used to keep track of how many update intervals to wait before retrying spider
        self.spider_wait_interval = wait_interval
        # Used to keep track of the number of times the spider consecutively fails
        self.spider_retry_count = retry_count
        self.last_scraping_update = last_update

    def _init_runtime_state(self):
        """State that always starts empty: the HTTP transport, the
        per-session caches and the timestamps a cycle fills in.
        """
        # Whether a full cycle has completed in this session. The daily
        # parameter re-read waits for it: the FIRST refresh runs inside Home
        # Assistant's setup, and a discovery there (five seconds of sleep per
        # module) turns every restart with an expired cache into a slow
        # startup.
        self._first_cycle_done = False
        # Tracks whether get_devices() has already run once during the
        # lifetime of this WemPortalApi instance (i.e. once per Home
        # Assistant session/restart), so it isn't repeated on every single
        # coordinator update - only the initial discovery/refresh needs it.
        self._devices_fetched_this_session = False
        self.valid_login = False
        self.session = None
        # Serialises a full poll cycle (fetch_data) against on-demand writes
        # (change_value): both run in executor threads and share self.session
        # and self.data, so without this they could interleave and corrupt the
        # session/state. A plain Lock is safe - writes never call fetch_data
        # and vice versa, so the two never nest on one thread.
        self._api_lock = threading.Lock()
        # When the poll cycle currently in progress has to stop, or None when
        # no poll is running. Only fetch_data sets it: an on-demand write or a
        # service call has a user waiting on it and no coordinator timeout
        # behind it, so neither gets a deadline.
        self._deadline = None
        self.webscraping_cookie = {}
        # Persistent scraper instance, kept across coordinator cycles so
        # its underlying HTTP session (TCP connection + TLS handshake) is
        # reused instead of being torn down and re-established on every
        # single scrape. Created lazily on first use in
        # fetch_webscraping_data(). Note: the session-cookie reuse (which
        # skips the login *requests*) is separate from this - keeping the
        # instance also skips the per-cycle connection setup itself.
        self._scraper = None
        # Headers used for all API calls
        self.headers = {
            "User-Agent": "WeishauptWEMApp",
            "X-Api-Version": "3.1.3.0",
            "Accept": "*/*",
            "Host": "www.wemportal.com",
        }
        # DeviceType per device id, as reported by Device/Read. Only feeds
        # the model name shown in Home Assistant.
        self.device_types = {}
        # Scraped keys seen in the previous cycle, to notice when the
        # portal relabels a row (see _warn_about_renamed_scraper_keys).
        self._previous_scraper_keys = None
        # Last connection status per device, so the offline log line is
        # edge-triggered rather than repeated every cycle.
        self._last_connection_status = {}
        self.scraping_mapper = {}
        self.last_statistics_fetch = 0.0
        # Timestamp (per device+parameter) of the last time a heating
        # schedule (CircuitTimes) was actually fetched, so it can be
        # refreshed at most every CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS
        # instead of on every single coordinator cycle - these rarely
        # change and this integration doesn't allow editing them anyway.
        self._last_circuit_times_fetch = {}
        # In-memory cookie cache shared by the short-lived expert clients,
        # so they can continue an existing web session instead of logging in
        # for every single operation (see expert_writer._try_cached_session).
        # Never persisted: a live session cookie is credential-equivalent.
        self.expert_cookies = {}
        self.api_version = None

    def _register_scrape_failure(self):
        """Count one failed scrape and make the next cycles wait for it.

        Every failing exit from the scrape has to go through here. Four of
        them did not - maintenance, wrong credentials and an expired session
        re-raised without touching the counters - and in `both` mode those
        errors are swallowed so the API can still poll, so the scraper walked
        into the same wall on every single API cycle.
        """
        self.spider_retry_count += 1
        self.spider_wait_interval = self.spider_retry_count
        if self.spider_retry_count == SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE:
            self._forget_scraped_values()

    def _forget_scraped_values(self):
        """Stop presenting readings from a scrape that stopped working.

        The API path has the same rule and a much easier job: the portal
        ANSWERS and leaves a parameter out, which is evidence the value is
        gone (see mapper._clear_unanswered). A failed scrape produces no
        answer at all, so nothing here can be read as "that reading ended" -
        only the fact that several attempts in a row produced nothing.

        Which rows those are is not guessed either: `both` mode merges
        scraped rows into the same device as the API ones, so they are taken
        from the last scrape that worked - the set already kept to notice
        relabelled rows.

        Only the `value` goes, as on the API path. Unit, name and icon stay,
        so the entity keeps its identity and Home Assistant is not told a
        unit changed.
        """
        device = self.data.get(str(self.resolve_scraper_device_id()))
        if not device:
            return
        forgotten = []
        for key in self._previous_scraper_keys:
            row = device.get(key)
            if isinstance(row, dict) and row.get("value") is not None:
                row["value"] = None
                forgotten.append(key)
        if forgotten:
            _LOGGER.warning(
                "The web scrape has failed %d times in a row. The %d reading(s) "
                "it provided are no longer current and are now shown as unknown "
                "rather than as the value they had %s.",
                self.spider_retry_count,
                len(forgotten),
                self.last_scraping_update or "at the last successful scrape",
            )

    @property
    def scraper_backoff(self):
        """The scrape backoff as the constructor takes it back.

        Exposed as one value so a caller carrying state across an instance
        swap cannot pick up two of the three and silently lose the third.
        """
        return (
            self.spider_wait_interval,
            self.spider_retry_count,
            self.last_scraping_update,
        )

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

    def activate_expert_cooldown(self, seconds=EXPERT_FORBIDDEN_COOLDOWN_SECONDS):
        """Back off the EXPERT (Fachmann) path only, after a 403 there.

        Deliberately does NOT touch the global cooldown: an expert 403 is
        frequently a rejected individual request rather than an IP-wide rate
        limit, and pausing sensor polling because of it costs the user their
        readings for no reason. Never shortens an existing backoff.
        """
        new_blocked_until = time.monotonic() + seconds
        if new_blocked_until > self._expert_blocked_until:
            self._expert_blocked_until = new_blocked_until
            _LOGGER.warning(
                "Expert (Fachmann) path returned 403. Pausing EXPERT requests "
                "for %s minutes; sensor polling is unaffected.",
                seconds // 60,
            )

    def check_expert_cooldown(self):
        """Gate for the expert path: raise if either backoff is active.

        Checks the global cooldown first - a genuine rate limit seen by the
        API/scraper must still stop expert requests - then the expert-only one.
        """
        self.check_cooldown()
        if self._expert_blocked_until and time.monotonic() < self._expert_blocked_until:
            remaining = int(self._expert_blocked_until - time.monotonic())
            if remaining >= 60:
                remaining_str = f"~{(remaining + 59) // 60} min"
            else:
                remaining_str = f"{remaining}s"
            raise ForbiddenError(
                f"Expert path is backing off after a previous 403 "
                f"({remaining_str} remaining). Sensor polling is unaffected."
            )

    def check_cooldown(self):
        """Raise ForbiddenError immediately, without making any request,
        if we're still within a cooldown period from a previous 403.

        Public on purpose: besides the internal API/scraping paths, the
        standalone expert writer holds a reference to this method as its
        shared cooldown gate, so a 403 seen anywhere pauses the expert
        path too. Renamed from the former underscore-prefixed name to stop
        advertising a private-only intent it never actually had.
        """
        if self._blocked_until and time.monotonic() < self._blocked_until:
            remaining = int(self._blocked_until - time.monotonic())
            # Human-readable: minutes for anything over a minute, so the
            # message surfaced in the frontend is immediately meaningful.
            if remaining >= 60:
                remaining_str = f"~{(remaining + 59) // 60} min"
            else:
                remaining_str = f"{remaining}s"
            raise ForbiddenError(
                f"Still cooling down after a previous rate-limit response "
                f"({remaining_str} remaining). Skipping requests until then."
            )

    def resolve_scraper_device_id(self):
        """Return the stable device id to store scraped sensors under.

        Locked in ONCE, then reused forever (persisted by the coordinator):
        - If already set (loaded from storage or decided earlier), return it
          unchanged - this is what keeps a pure-web install pinned to the
          placeholder even after it later discovers a real device via `both`.
        - Otherwise prefer a real, API-discovered device id (from the module
          cache or this session's discovery), so scraped sensors share a
          device - and history - with the api/both-mode entities.
        - Fall back to the placeholder only when no real device was ever
          known (a pure-web install).

        Keyed off self.modules (populated by get_devices() before the merge
        in `both` mode, and carried over from the persisted cache in `web`
        mode), NOT self.data, since self.data can already contain the
        placeholder key from an earlier scrape this session.
        """
        if self.scraper_device_id:
            return self.scraper_device_id
        if self.modules:
            self.scraper_device_id = next(iter(self.modules))
            if len(self.modules) > 1:
                # Said once, because it cannot be resolved from here. The
                # scraper reads ONE expert page and has no device concept -
                # the page the portal serves is whichever it considers
                # current - while this picks the first device the API
                # reported. On an account with several devices those two are
                # not necessarily the same one, so the scraped sensors may
                # sit under a device they did not come from.
                #
                # Not guessed at: correlating the two would mean driving the
                # portal's device selector, which needs an account with more
                # than one device to develop against. Upstream issue #43 has
                # been open since 2022 for the same reason. Saying so beats
                # a quiet mis-attribution.
                _LOGGER.warning(
                    "This account has %d devices, and the web scraper reads a "
                    "single expert page with no way to say which device that "
                    "is. Its sensors are filed under device %s. If they look "
                    "like they belong to another device, use `api` mode for "
                    "this account.",
                    len(self.modules),
                    self.scraper_device_id,
                )
        else:
            self.scraper_device_id = SCRAPER_FALLBACK_DEVICE_ID
        return self.scraper_device_id

    def _scraper_enabled(self, enabled_devices) -> bool:
        """Whether the web scraper's pseudo-device is in the caller's filter.

        Scraping produces exactly ONE device (see resolve_scraper_device_id).
        The device filter was only honoured on the API and statistics paths,
        so disabling every device still triggered a full portal scrape - the
        heaviest request the integration makes.

        `None` means "no filter". An undecided scraper id means nothing is
        known yet, so there is nothing to filter on and the scrape must run
        (that first scrape is what decides the id). Deliberately reads the
        stored id instead of resolve_scraper_device_id(), which would lock
        one in as a side effect.
        """
        if enabled_devices is None:
            return True
        if not enabled_devices:
            # An EXPLICIT empty list means every known device is disabled.
            # That is unambiguous even when the scraper id is not decided
            # yet - previously the undecided-id escape below let the scrape
            # run anyway, which is this guard's own failure mode.
            return False
        device_id = self.scraper_device_id
        if not device_id:
            return True
        return str(device_id) in {str(enabled) for enabled in enabled_devices}

    def _acquire_api_lock(self, what):
        """Take the shared API lock, or fail with a message the user can act on.

        Blocking forever was the old behaviour: a poll whose await had already
        timed out keeps its executor thread - and this lock - so the next
        operation hung with no feedback.
        """
        if not self._api_lock.acquire(timeout=API_LOCK_TIMEOUT_SECONDS):
            raise ApiBusyError(
                f"Timed out waiting for the WEM Portal connection to become "
                f"free ({what}). A previous poll is still running; try again "
                f"in a moment."
            )

    def fetch_data(self, enabled_devices=None):
        """Run a full poll cycle under the shared API lock, so it can't
        interleave with an on-demand write (change_value) on the same
        session/state."""
        started = time.monotonic()
        self._acquire_api_lock("poll cycle")
        try:
            # Set AFTER taking the lock, but measured from BEFORE it. Both
            # halves matter. Queueing is part of the budget - a cycle that
            # spent it waiting has nothing left to spend at the portal, and
            # would only be cut off mid-request anyway. Setting it before the
            # acquire would be worse than useless: the field is shared, so
            # this cycle's deadline would land on whatever operation is
            # holding the lock right now, which has its own caller waiting.
            self._deadline = started + POLL_DEADLINE_SECONDS
            return self._fetch_data(enabled_devices)
        finally:
            self._deadline = None
            self._api_lock.release()

    def check_deadline(self):
        """Stop the poll cycle if it has used up its time budget.

        Checked at the two points every long cycle passes through - each
        mobile-API request and the entry to the scrape - rather than inside
        the loops that call them. Those loops all funnel through here, so
        guarding them individually would be six places to forget instead of
        two.

        Does nothing when no poll is running: see the note on `_deadline`.
        """
        if self._deadline is None:
            return
        if time.monotonic() >= self._deadline:
            raise PollDeadlineExceeded(
                f"This poll cycle passed its {POLL_DEADLINE_SECONDS}s budget "
                f"and stopped. Home Assistant abandons the cycle at "
                f"{DEFAULT_TIMEOUT}s regardless; stopping first releases the "
                f"connection for the next one instead of holding it."
            )

    def _discover_parameters_if_due(self):
        """Read the per-module parameter definitions, if any are due.

        Two different reasons to run it, with different urgency.

        MISSING definitions have to be fetched now: without them there is
        nothing to read and nothing to show.

        STALE ones are the daily re-read, and it deliberately waits for the
        second cycle. The first refresh runs INSIDE Home Assistant's setup,
        and this discovery sleeps five seconds per module - a re-read there
        would make every restart with an expired cache a slow startup, which
        Home Assistant then complains about. Nothing is lost by waiting one
        interval for something that is a day old already.
        """
        missing = any(
            "parameters" not in module
            for modules in self.modules.values()
            for module in modules.values()
        )
        stale = any(
            self._parameters_are_stale(module)
            for modules in self.modules.values()
            for module in modules.values()
        )
        if not (missing or (stale and self._first_cycle_done)):
            return
        _LOGGER.info(
            "Reading parameter definitions from the portal (%s).",
            "some are missing" if missing else "the cached ones are due",
        )
        self.get_parameters()

    def _ensure_api_session(self):
        """Everything the API paths need before they can read anything."""
        if not self.valid_login:
            self.api_login()
        # Refresh the device/module list once per session (cheap). This
        # intentionally runs whether or not we started with a module cache:
        # it's what discovers devices on a fresh install, and what picks up
        # newly added devices/modules on an existing one - while preserving
        # any cached parameter definitions (see get_devices()).
        if not self._devices_fetched_this_session:
            self.get_devices()
            self._devices_fetched_this_session = True
        # Only run the slow, rate-limited per-module discovery if something
        # actually needs it. With a valid persisted cache this is skipped
        # entirely after a restart, which is what makes startup fast again.
        self._discover_parameters_if_due()

    def _scrape_is_due(self, enabled_devices) -> bool:
        """Whether `both` mode should scrape this cycle.

        The order of these guards is load-bearing, which is why they are
        guards and no longer one long condition. The device filter comes
        FIRST: as part of an or-branch after `last_scraping_update is None`,
        a check placed here never ran on the first cycle, which is exactly
        when it mattered. The backoff is asked at the TOP level for the same
        reason - it used to sit inside that branch, and `last_scraping_update
        is None` is true on every fresh WemPortalApi, which the coordinator
        builds whenever it recovers from repeated errors. The scrape backoff
        was therefore skipped right after the failures that set it.
        """
        if not self._scraper_enabled(enabled_devices):
            return False
        if self.spider_wait_interval != 0:
            return False
        if self.last_scraping_update is None:
            return True
        waited = datetime.now() - self.last_scraping_update + timedelta(seconds=10)
        return waited > self.scan_interval

    def _count_down_scrape_backoff(self):
        """One cycle closer to the next scrape attempt."""
        if self.spider_wait_interval > 0:
            self.spider_wait_interval -= 1

    def _scrape_and_merge(self):
        """Scrape once and merge the result. The timestamp moves only after
        both have worked."""
        webscraping_data = self.fetch_webscraping_data()
        self._merge_webscraping_data(self.resolve_scraper_device_id(), webscraping_data)
        self.last_scraping_update = datetime.now()

    def _collect_web(self, enabled_devices):
        """`web` mode: the scrape is the only source there is."""
        if not self._scraper_enabled(enabled_devices):
            _LOGGER.debug("Skipping web scrape: its device is disabled.")
            return
        webscraping_data = self.fetch_webscraping_data()
        self._merge_webscraping_data(self.resolve_scraper_device_id(), webscraping_data)

    def _collect_both(self, enabled_devices):
        """`both` mode: scrape when due, then read the API either way."""
        if self._scrape_is_due(enabled_devices):
            try:
                self._scrape_and_merge()
            except Exception as exc:  # noqa: BLE001
                # Broad: the scrape is the optional half of `both` mode. No
                # scraper failure may cost the API readings that follow, so
                # this deliberately does not re-raise.
                _LOGGER.warning(
                    "Web scraper failed this cycle. Falling back to API only. Error: %s",
                    exc,
                )
        else:
            self._count_down_scrape_backoff()

        # Always run as a resilient fallback.
        self.get_data(enabled_devices)

    def _fetch_data(self, enabled_devices=None):
        # Fail fast, without any network activity at all, if we're still
        # within a cooldown window from a previous 403 (see
        # _activate_cooldown). This is checked again inside
        # make_api_call() for every individual call too, but checking
        # once up front avoids even starting a cycle (login attempts,
        # etc.) that we already know will be aborted immediately.
        self.check_cooldown()
        try:
            if self.mode != "web":
                self._ensure_api_session()

            if self.mode == "web":
                self._collect_web(enabled_devices)
            elif self.mode == "api":
                self.get_data(enabled_devices)
            else:
                self._collect_both(enabled_devices)

            # Set only after a cycle got this far, so a setup that fails
            # halfway does not let the next attempt count as "not the first
            # one" and slow itself down with a re-read.
            self._first_cycle_done = True

            return self.data

        except Exception as exc:
            # Broad: this is the outermost boundary of a coordinator
            # cycle. Anything unexpected has to arrive at Home Assistant
            # as an update failure, not as a crashed integration.
            if isinstance(exc, WemPortalError):
                # Re-raise known errors so we don't wrap them twice
                raise
            # Wrap any unexpected python crashes to prevent HA from halting
            raise WemPortalError(
                "Unexpected error occurred while fetching data"
            ) from exc

    def _warn_about_renamed_scraper_keys(self, scraped_keys):
        """Point out a relabelled row before the user has to guess.

        Scraped sensors have no stable id from the portal - the entityvalue
        in the row embeds the current VALUE, so it changes whenever the
        reading does and cannot serve as one. Their key is therefore built
        from the panel heading and the row label, so a wording change at the
        portal produces a different key for the same reading.

        Nothing can prevent that here, but silently splitting a sensor's
        history is the kind of thing people notice weeks later. If keys
        disappear and others appear in the same cycle, say so.

        The message names the display language first, because that is by far
        the likeliest cause and the only one the reader can undo: switching it
        in the portal's account settings relabels every row at once. Reported
        as a portal rename, a language switch someone made themselves reads
        like a fault in the integration - it cost an evening of looking for
        one.

        What happens NEXT is deliberately split in two, because the two are
        not the same and the message used to claim only the second. Entities
        are created once, during setup, so nothing new appears right now: the
        existing sensors simply lose their row and go unknown. Only a restart
        builds entities from the new keys, and that is when the history stays
        behind with the old ones.

        Returns the keys that are no longer scraped, which the caller needs
        for a second reason: whatever they were showing is not current any
        more either.
        """
        previous = self._previous_scraper_keys
        self._previous_scraper_keys = set(scraped_keys)
        if not previous:
            # First cycle of this session: nothing to compare against.
            return set()
        gone = previous - set(scraped_keys)
        added = set(scraped_keys) - previous
        if gone and added:
            _LOGGER.warning(
                "The web portal labelled scraped rows differently this cycle: "
                "%s no longer appear, while %s are new. The usual cause is the "
                "display language being changed in the portal's own account "
                "settings, which relabels every row at once; a reworded row is "
                "the other. Scraped sensors are keyed by those labels, so the "
                "affected sensors have no reading this cycle and show as "
                "unknown. They come back if the labels do. If the new labels "
                "stay, the next Home Assistant restart creates NEW entities "
                "from them and the history stays with the old ones.",
                ", ".join(sorted(gone)),
                ", ".join(sorted(added)),
            )
        return gone

    def _merge_webscraping_data(self, device_id, webscraping_data):
        if str(device_id) not in self.data:
            self.data[str(device_id)] = {}

        vanished = self._warn_about_renamed_scraper_keys(
            [key for key, row in webscraping_data.items() if isinstance(row, dict)]
        )

        for key, new_val in webscraping_data.items():
            if isinstance(new_val, dict):
                if "friendlyName" in new_val:
                    new_val["friendlyName"] = translate(
                        self.language, new_val["friendlyName"]
                    )

                # Preserve the old unit if the current scrape is missing it (e.g. value is "--")
                # This prevents Home Assistant from complaining about unit changes.
                if new_val.get("unit") in (None, ""):
                    old_val = self.data[str(device_id)].get(key)
                    if isinstance(old_val, dict) and old_val.get("unit") not in (
                        None,
                        "",
                    ):
                        new_val["unit"] = old_val.get("unit")

                # The old value is deliberately NOT carried over when this
                # scrape has none.
                #
                # It used to be, to avoid "a gap in the history, even though
                # the previous value is still very likely accurate". Measured
                # on a live installation, that premise does not hold: the
                # portal renders "--" for a value it does not currently have,
                # the scrape maps that to None, and the sensor then reported a
                # setpoint of 50.5 degrees for three hours while the portal
                # and the heat pump both showed nothing. Only reloading the
                # integration cleared it.
                #
                # A gap is the truthful record of an hour with no reading. A
                # flat line at the last value is not, and it is the shape
                # automations act on.
                #
                # We only get here after a scrape that produced rows at all -
                # a page with no readings is rejected earlier - so a row that
                # came back without a value is the portal saying it has none,
                # not evidence that the read went wrong.

            self.data[str(device_id)][key] = new_val

        # Same reasoning for a row that stopped coming back entirely: it is
        # not being scraped any more, so whatever it still shows is old. The
        # key itself stays, because the entity does too and Home Assistant
        # would otherwise report it as merely missing from the data.
        for key in vanished:
            entry = self.data[str(device_id)].get(key)
            if isinstance(entry, dict) and entry.get("value") is not None:
                _LOGGER.debug(
                    "Scraped row %s is no longer on the page; its last value "
                    "is not current any more.",
                    key,
                )
                entry["value"] = None

    def reset_transport(self):
        """Throw away the HTTP state and force a fresh login next cycle.

        This is the recovery the coordinator reaches for after repeated
        errors, and it replaces rebuilding the whole object. Rebuilding was
        never the remedy - the remedy is a clean connection - and it kept
        causing the problem it was meant to solve: a fresh instance starts
        every one of its 31 fields from scratch, so each piece of state that
        had to survive was carried across by hand, and each new field was a
        new chance to forget one. Two were forgotten in practice.

        Three things it silently reset are worth naming, because they were
        never intended and no test would have caught them:

          * the statistics and circuit-times timestamps, which are portal
            RATE LIMITS (an hour each). Every recovery let the next cycle
            refetch both immediately - on a portal that had just been failing.
          * the shared API lock. A poll running in a worker thread held the
            OLD lock while the new object handed out a fresh, unheld one, so
            a write could interleave with the very poll the lock exists to
            serialise against.

        What actually needs to go is the transport: the HTTP sessions, the
        login state and the cookies. Everything else - discovered modules,
        cooldowns, backoffs, timestamps, the lock - is deliberately kept,
        because none of it is what "corrupted session" refers to.

        `expert_cookies` is the one that looks like transport and is not. It
        caches a live web session for the expert path, and dropping it would
        force a full Fachmann login on the next expert operation - requests
        against a portal that blocks the IP for 12 hours past 10,000 of them.
        An API failure says nothing about that session, so it stays.

        Which field is which is not left to this docstring: the split is
        declared and enforced in tests/test_hardening.py, so a new field
        fails the suite until someone classifies it.

        Runs UNDER the shared api lock, and that is not decoration. A failed
        poll releases the lock in its `finally`; a change_value() worker that
        was waiting takes it in that same instant, and this then closed the
        session out from under the write. Taking the lock is also why the
        coordinator hands this to an executor instead of calling it on the
        event loop - see WemPortalDataUpdateCoordinator.
        """
        if not self._api_lock.acquire(timeout=API_LOCK_TIMEOUT_SECONDS):
            # Best-effort by design. The recovery must not raise into the
            # coordinator's error handling, and a connection still in use is
            # exactly when tearing it down does the damage. The next failed
            # cycle tries again.
            _LOGGER.warning(
                "Not resetting the connection: it is still in use by another "
                "operation. Trying again on the next failed cycle."
            )
            return
        try:
            self._reset_transport_locked()
        finally:
            self._api_lock.release()

    def close_transport(self, timeout=API_LOCK_TIMEOUT_SECONDS):
        """Close the HTTP sessions because this object is being discarded.

        Unlike reset_transport this WAITS for the lock rather than skipping:
        an unload that leaves a session open leaks it for the life of the
        process, so the close has to happen even if it has to wait for the
        operation holding the lock. It still closes on timeout - a leaked
        connection is worse than a request that fails as everything around it
        is being torn down anyway.

        Taking the lock at all is the point. A write or a poll can be inside
        make_api_call at this moment, and closing its session underneath it
        turns an orderly teardown into a connection error.
        """
        acquired = self._api_lock.acquire(timeout=timeout)
        if not acquired:
            _LOGGER.debug(
                "Closing the HTTP sessions while another operation still holds "
                "the api lock; it waited %ss.",
                timeout,
            )
        try:
            self._close_sessions()
        finally:
            if acquired:
                self._api_lock.release()

    def _close_sessions(self):
        """Close both HTTP sessions. Never raises: every caller is either
        discarding this object or replacing its transport."""
        if self.session is not None:
            try:
                self.session.close()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Ignoring error while closing the API session: %s", exc)
            self.session = None
        self._reset_scraper()

    def _reset_transport_locked(self):
        """The teardown itself. Only called with the api lock held."""
        _LOGGER.info(
            "Persistent API errors: dropping the HTTP sessions and logging in "
            "again on the next cycle."
        )
        self._close_sessions()
        self.valid_login = False
        self.api_version = None
        # The web cookie belongs to the discarded scraper session.
        self.webscraping_cookie = {}
        # Re-run device discovery once on the next cycle: it is cheap, and the
        # failures being recovered from may have left the module view partial.
        self._devices_fetched_this_session = False

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
        # Function-local on purpose: the scraper pulls curl_cffi and lxml
        # (~140 ms, measured), and `api` mode never gets here.
        from .scraper import WemPortalScraper

        # Respect an active rate-limit cooldown for the scraping path too,
        # not just the API path - a 403 from either frontend means the
        # server wants us to back off everywhere.
        self.check_cooldown()
        # The scrape does not go through make_api_call - it has its own
        # session and its own request sequence - so the cycle's deadline has
        # to be checked here as well. A scrape is the single most expensive
        # thing a cycle does; starting one with no budget left guarantees the
        # coordinator abandons it half-way.
        self.check_deadline()

        # Reuse the existing scraper (and with it, its warm HTTP
        # connection) across cycles; only create a new one on first use
        # or after it was deliberately discarded (see _reset_scraper).
        if self._scraper is None:
            self._scraper = WemPortalScraper(
                self.username, self.password, self.webscraping_cookie
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
            self._register_scrape_failure()
            if self.spider_retry_count == 2:
                self.webscraping_cookie = None
            raise WemPortalError(DATA_GATHERING_ERROR) from exc

        except PortalMaintenanceError:
            # Announced downtime, not a credential or connection problem.
            # Must be re-raised BEFORE the catch-all below, which would
            # otherwise turn it into a generic data-gathering error and cost
            # the coordinator its ability to tell the two apart.
            #
            # Backed off like any other failed scrape. In `both` mode the
            # error is swallowed so the API can still poll, so without this
            # the scraper walked into the same announced outage on every
            # single API cycle.
            self._register_scrape_failure()
            self._reset_scraper()
            raise

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
            #
            # Backed off too: in `both` mode this error is swallowed so the
            # API keeps polling, so a wrong web password meant a fresh login
            # attempt on every API cycle - the request the portal is least
            # willing to see repeated.
            self._register_scrape_failure()
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
            self._register_scrape_failure()
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
        # The cooldown gate belongs on every outbound request, and a login is
        # the most expensive one to get wrong. _fetch_data and make_api_call
        # both ask, so the polling path was covered - but the config and
        # reauth flows call this directly, and those are exactly where
        # somebody lands after deleting and re-adding the integration to
        # "fix" a blockade. Every one of those attempts extended it.
        self.check_cooldown()
        payload = {
            "Name": self.username,
            "PasswordUTF8": self.password,
            "AppID": "com.weishaupt.wemapp",
            "AppVersion": "2.0.2",
            "ClientOS": "Android",
        }
        if self.session is not None:
            self.session.close()
        self.session = requests.Session()
        self.session.cookies.clear()
        self.session.headers.update(self.headers)
        # Initialized BEFORE the try block: if the POST itself fails with a
        # pure network error (connection reset, DNS, timeout), `response`
        # would otherwise not exist yet and the error handler below would
        # crash with an UnboundLocalError instead of raising the intended
        # UnknownAuthError.
        response = None
        try:
            response = self.session.post(
                API_LOGIN_URL,
                data=payload,
                timeout=API_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            # Verify the response is actually valid JSON and successful
            response_data = response.json()
            if not status_is_success(response_data.get("Status")):
                raise AuthError(f"Login failed: Server returned {response_data}")

            self.api_version = response_data.get("Version")
            # No username: there is one account per config entry, so naming it adds
            # nothing - and a debug log is exactly what people paste into an
            # issue when asking for help.
            _LOGGER.debug("API login successful.")
            self.valid_login = True

        except ValueError as exc:  # Catches JSONDecodeError if response is HTML
            # Username (email) is PII and deliberately kept out of the log
            # entirely - people paste logs into issues/forums, and with one
            # account per config entry naming it adds nothing.
            _LOGGER.warning("API login failed. Received HTML instead of JSON.")
            self.valid_login = False
            raise WemPortalError(
                "API login failed: received HTML instead of JSON (Possible rate limit or WAF block)"
            ) from exc
        except requests.exceptions.RequestException as exc:
            # Broader than just HTTPError: also covers ConnectionError,
            # Timeout, etc. - genuine network failures that aren't tied to
            # a specific HTTP status code, which previously weren't caught
            # here at all and would fall through to the generic
            # "unexpected error" wrapper in fetch_data() instead of a
            # clear, specific error message.
            _LOGGER.warning("API login failed with a network/HTTP error.")
            self.valid_login = False
            self._raise_login_failure(response, exc)

    def _raise_login_failure(self, response, exc):
        """Turn a failed login into the error that fits what came back.

        Always raises - the type is what the caller acts on: wrong password,
        rate limit, portal fault, or "never got there". Written as guards
        rather than an if/elif chain, which nested one level per status and
        put the last case five deep.

        Messages carry the HTTP status plus the server's own status and
        message fields, but NOT the raw response body: they surface in the UI
        and in logs, and a whole HTML error page does not belong there.
        """
        if response is None:
            raise UnknownAuthError(
                f"Authentication Error: Could not reach WEM Portal ({exc})."
            ) from exc

        response_status, response_message = self.get_response_details(response)
        server_said = (
            f"Server returned internal status code: {response_status} "
            f"and message: {response_message}"
        )

        if response.status_code == 400:
            raise AuthError(
                "Authentication Error: Check if your login credentials are "
                f"correct. Received response code: {response.status_code}. "
                f"{server_said}"
            ) from exc
        if response.status_code == 403:
            self._activate_cooldown()
            raise ForbiddenError(f"WemPortal forbidden error: {server_said}") from exc
        if response.status_code == 500:
            raise ServerError(f"WemPortal server error: {server_said}") from exc
        raise UnknownAuthError(
            "Authentication Error: Encountered an unknown authentication "
            f"error. Received response code: {response.status_code}. "
            f"{server_said}"
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
        self.check_cooldown()
        session = requests.Session()
        login_url = WEB_LOGIN_URL

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "de,en;q=0.9",
        }

        # Step 1: Fetch the login page
        initial_response = None
        try:
            initial_response = session.get(
                login_url,
                headers=headers,
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            )
            initial_response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            # A 403 here is the same refusal the POST below already
            # recognises, and it arrives FIRST - this is the request that
            # meets a blocked IP. Reported as "could not load the page" it
            # read like a network problem, invited an immediate retry, and
            # started no cooldown, so the next cycle walked into it again.
            if initial_response is not None and initial_response.status_code == 403:
                self._activate_cooldown()
                raise ForbiddenError(
                    "Access forbidden while loading the login page."
                ) from exc
            raise UnknownAuthError(f"Failed to load the login page: {exc}") from exc

        # Planned downtime: bail out BEFORE posting the credentials. The form
        # is fully present during maintenance, so submitting would just fail
        # as "invalid username or password" and, after three cycles, ask the
        # user to re-enter working credentials. It also avoids sending the
        # password to a page that cannot process it.
        notice = maintenance_notice(initial_response.text)
        if notice:
            raise PortalMaintenanceError(notice)

        # Step 2: Parse the login page and extract hidden form fields.
        #
        # Read with lxml, which the scraper already uses for the far more
        # involved expert page - so this is the only thing beautifulsoup4 was
        # installed for, three lines of it, and the dependency is gone.
        #
        # The `string(@name)` half of the selector is not decoration: the old
        # code tested the name for truthiness, which skips `name=""`, while a
        # bare `[@name]` would keep it and post a field the portal never sent.
        try:
            page = html.fromstring(initial_response.text)
        except ParserError as exc:
            # An empty or unparseable body. The old parser returned no fields
            # here and let the login POST go ahead, which sent the password to
            # a page that had answered with nothing, collected no ASP.NET
            # state to echo back, and could only be refused. Same reasoning as
            # the maintenance bail-out above: do not hand over credentials to
            # a page that cannot process them.
            raise UnknownAuthError(
                "The WEM Portal login page could not be read; no credentials were sent."
            ) from exc
        form_data = {
            element.get("name"): element.get("value", "")
            for element in page.xpath('//input[@type="hidden"][string(@name)]')
        }

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
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            # Step 4: Read the answer. Three outcomes, not two.
            if WEB_LOGGED_IN_MARKER in response.text:
                _LOGGER.debug("WEB login successful.")
                return
            # Maintenance is checked on this answer too, not only on the page
            # fetched above: the window can open between the two requests, and
            # the portal serves the notice with HTTP 200 either way.
            notice = maintenance_notice(response.text)
            if notice:
                raise PortalMaintenanceError(notice)
            if WEB_LOGIN_FORM_MARKER in response.text:
                raise AuthError("Login failed: Invalid username or password.")
            # Neither logged in, nor the login form back, nor maintenance:
            # some other page. Saying "wrong password" about it counted a
            # portal hiccup towards the reauth prompt, and three of those in a
            # row take the integration down until somebody re-enters
            # credentials that were correct the whole time.
            raise UnknownAuthError(
                "Login failed: the portal answered with a page that is neither "
                "the logged-in view nor the login form."
            )
        except requests.exceptions.RequestException as exc:
            if response is not None and response.status_code == 403:
                self._activate_cooldown()
                raise ForbiddenError("Access forbidden during login.") from exc
            raise UnknownAuthError(f"Failed to submit the login form: {exc}") from exc

    def get_response_details(self, response: requests.Response):
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
        self,
        url: str,
        headers=None,
        data=None,
        do_retry=True,
        delay=5,
        retry_transport=False,
    ) -> requests.Response:
        """One mobile-API request, with two kinds of retry sharing one attempt.

        `do_retry` covers an expired session: re-login once and try again.
        `retry_transport` covers a request that never reached the portal at
        all - a timeout, a reset connection, DNS.

        They are separate REASONS, not separate budgets: there is exactly one
        extra attempt, whichever reason claims it. A transport hiccup followed
        by an expired session is therefore not retried twice, on purpose - the
        cycle has a time budget and a third attempt would eat into it.

        `retry_transport` is opt-in per call site rather than on by default,
        for two different reasons. Cost: an hourly cycle is roughly fifteen
        requests on a single-device installation and more on larger ones, so
        letting all of them retry into a timeout would push a bad cycle well
        past the coordinator's own limit. Safety: a request that starts
        something at the portal must not be repeated when only its answer was
        lost. Only the two reads that carry values ask for this.
        """
        attempts = 2 if (do_retry or retry_transport) else 1
        response = None

        for attempt in range(attempts):
            # Reset per attempt: on a network failure during a retry the
            # error handler below would otherwise read stale details from
            # the PREVIOUS attempt's response.
            response = None
            # Fail fast if we're still cooling down from a previous 403 -
            # applies to every single call site that goes through here,
            # not just the one that originally triggered it.
            self.check_cooldown()
            # Same idea, different budget: stop a poll cycle that is out of
            # time before spending another request on it. Inside the attempt
            # loop on purpose, so a retry cannot carry a cycle past the
            # deadline the first attempt was still inside of.
            self.check_deadline()

            time.sleep(1)  # Wait 1 sec between requests to be graceful to the API.
            # Merge any call-specific headers on top of the default headers,
            # instead of replacing them outright. Previously, passing e.g.
            # headers={"X-Api-Version": "2.0.0.0"} (as get_statistics() does)
            # would silently drop "Host", "User-Agent" and "Accept" for that
            # call, which could cause it to be rejected by the server.
            current_headers = {**self.headers, **(headers or {})}

            try:
                if not data:
                    _LOGGER.debug(
                        "Sending GET request to %s with headers: %s",
                        url,
                        current_headers,
                    )
                    response = self.session.get(
                        url,
                        headers=current_headers,
                        timeout=API_REQUEST_TIMEOUT_SECONDS,
                    )
                else:
                    _LOGGER.debug(
                        "Sending POST request to %s with headers: %s and data: %s",
                        url,
                        current_headers,
                        data,
                    )
                    response = self.session.post(
                        url,
                        headers=current_headers,
                        json=data,
                        timeout=API_REQUEST_TIMEOUT_SECONDS,
                    )

                response.raise_for_status()

                # Check for stealthy session expiration (HTML redirect)
                if "Account/Login" in response.url or (
                    hasattr(response, "redirect_url")
                    and response.redirect_url
                    and "Account/Login" in str(response.redirect_url)
                ):
                    raise ExpiredSessionError("Redirected to Account/Login")

                _LOGGER.debug(response)
                return response

            except (requests.exceptions.RequestException, ExpiredSessionError) as exc:
                status_code = (
                    response.status_code
                    if isinstance(exc, requests.exceptions.RequestException)
                    and response is not None
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

                # Nothing came back at all: the request timed out, the
                # connection was reset, DNS failed. `response` is set to None
                # at the top of every attempt and only ever assigned by the
                # get/post below, so this is exactly "no HTTP response was
                # received" - a 403, a 401 and the login-redirect check all
                # need a response to have been raised in the first place.
                is_transport_error = response is None

                if is_transport_error and retry_transport and attempt < attempts - 1:
                    # Deliberately no re-login: the session is fine, the
                    # network was not. Logging in again would spend an extra
                    # request at the worst possible moment and throw away a
                    # session that nothing is wrong with.
                    _LOGGER.info(
                        "Request to %s did not reach the portal (%s). Retrying once.",
                        url,
                        exc,
                    )
                    time.sleep(API_TRANSPORT_RETRY_DELAY_SECONDS)
                    continue

                # A genuinely expired session (401, or a stealthy redirect
                # to the login page) is worth one immediate retry with a
                # fresh login - unlike a 403, this isn't a sign we're
                # sending too many requests, just that the current session
                # is no longer valid.
                is_session_error = (
                    isinstance(exc, ExpiredSessionError) or status_code == 401
                )

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

                if is_transport_error:
                    # There was no server and no answer, so there is no status
                    # code and no message to report. Saying "Server returned
                    # status code:  and message: " anyway - which is what a
                    # timeout produced - sends every reader of that line
                    # looking at the portal for a fault that is on this side
                    # of the connection. The web path already words this
                    # correctly; see scraper.py's login handler.
                    wem_error = WemPortalError(
                        f"{DATA_GATHERING_ERROR} Could not reach the WEM Portal: {exc}"
                    )
                else:
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
        # Build the fresh device/module view in LOCAL dicts first and only
        # assign to self.modules/self.data once everything succeeded.
        # Previously both were wiped BEFORE the API call: a single failing
        # call (e.g. one 403) then left them empty, and the next successful
        # run saw no previously-known modules - silently discarding all
        # cached parameter definitions and forcing the slow, rate-limited
        # full discovery in get_parameters() that the cache exists to avoid.
        data = self.make_api_call(API_DEVICE_READ_URL, do_retry=True).json()

        new_modules = {}
        new_data = {}
        for device in data["Devices"]:
            device_id_str = str(device["ID"])
            new_data[device_id_str] = {}
            new_modules[device_id_str] = {}
            previously_known_device_modules = previously_known_modules.get(
                device_id_str, {}
            )
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
                    # Carried across with the list it belongs to. Left behind,
                    # every session would look like the cache had just expired
                    # and re-read every module on the first cycle - the exact
                    # portal load the interval exists to avoid.
                    module_entry["parameters_fetched_at"] = cached_module.get(
                        "parameters_fetched_at", 0
                    )
                new_modules[device_id_str][module_key] = module_entry
            new_data[device_id_str]["ConnectionStatus"] = device["ConnectionStatus"]
            # Kept out of new_data: the entity platforms iterate that dict
            # and would try to build an entity from it.
            if device.get("DeviceType") is not None:
                self.device_types[device_id_str] = device["DeviceType"]
        self.modules = new_modules
        self.data = new_data

    def _note_undescribed_module(self, device_id, key, values, why, unsupported):
        """A module the portal would not describe. Nothing is ever thrown away.

        This is what makes a re-scan purely ADDITIVE, and it is the condition
        the whole TTL rests on. Discovery used to run once per session, so
        dropping a module the portal refused to describe was harmless. On a
        timer that same path runs again and again, and a 403, a maintenance
        window or one bad answer would throw away a parameter list that was
        working - straight into "device has no parameters", the state the
        empty-read guard exists for.

        A module that already HAS parameters keeps them and is simply not
        asked again for a while.

        One that never had any is recorded with an empty list and a
        timestamp - it used to be deleted outright, and that is upstream
        issue #126: a heating circuit whose very first description answered
        400 was gone from the cache, and only get_devices() could bring it
        back. That runs once per session, so the circuit stayed missing
        until a reload. Whether an installation showed one circuit or two
        came down to what the portal happened to answer in the second the
        integration started - "sometimes only the first and sometimes both",
        as the thread puts it.

        The cost of keeping it is one description request per genuinely
        unsupported module per day. The cost of deleting it was a heating
        circuit nobody could get back. `unsupported` now only decides how
        loudly this is said: a rejected request is worth a warning, an empty
        description is not.
        """
        if values.get("parameters"):
            _LOGGER.warning(
                "Could not re-read the parameters of device %s module %s/%s (%s). "
                "Keeping the %d already known; trying again in about %d h.",
                device_id,
                values["Index"],
                values["Type"],
                why,
                len(values["parameters"]),
                PARAMETER_REDISCOVERY_RETRY_SECONDS // 3600,
            )
            values["parameters_fetched_at"] = time.time() - max(
                0,
                PARAMETER_REDISCOVERY_INTERVAL_SECONDS
                - PARAMETER_REDISCOVERY_RETRY_SECONDS,
            )
            return

        # An empty description is a normal answer, not a fault: some modules
        # simply have nothing to poll. A rejected request might be the same
        # thing said less politely, or a portal having a bad minute - from
        # here the two are indistinguishable, which is exactly why neither
        # may be treated as final.
        #
        # Both are recorded with an empty list and a timestamp, so the normal
        # interval applies and the module is asked once a day like everything
        # else. If it ever does gain parameters, that is when they are found.
        # An empty list is falsy, so nothing is asked of it in the meantime -
        # the value read skips modules without parameters.
        if unsupported:
            _LOGGER.warning(
                "Device %s module %s/%s (%s) would not describe itself (%s). "
                "Keeping it and asking again in about %d h - a module dropped "
                "here is one Home Assistant cannot get back until a reload.",
                device_id,
                values["Index"],
                values["Type"],
                values.get("Name", "?"),
                why,
                PARAMETER_REDISCOVERY_INTERVAL_SECONDS // 3600,
            )
        else:
            _LOGGER.debug(
                "Device %s module %s/%s (%s) describes no parameters; nothing "
                "to poll from it. Asking again in about %d h.",
                device_id,
                values["Index"],
                values["Type"],
                values.get("Name", "?"),
                PARAMETER_REDISCOVERY_INTERVAL_SECONDS // 3600,
            )
        values["parameters"] = {}
        values["parameters_fetched_at"] = time.time()

    def _parameters_are_stale(self, module) -> bool:
        """Whether this module's parameter list is due for a re-read.

        A module with no list at all is NOT stale - it is missing, which is a
        different urgency and a different caller decision.
        """
        if "parameters" not in module:
            return False
        age = time.time() - module.get("parameters_fetched_at", 0)
        return age >= PARAMETER_REDISCOVERY_INTERVAL_SECONDS

    @staticmethod
    def _http_status(exc):
        """The HTTP status behind a WemPortalError, or None if it had none.

        Read explicitly rather than through getattr with a default: a typo in
        an attribute name would then silently mean "no status", and this is
        what decides between backing off, dropping a module and re-raising.
        """
        cause = exc.__cause__
        if not isinstance(cause, requests.exceptions.HTTPError):
            return None
        return cause.response.status_code

    def _module_description_is_due(self, device_id, values) -> bool:
        """Whether this module's parameter list has to be read again.

        Cached AND still young enough to trust. Without the age check the list
        was kept forever, so a parameter added on a module the integration
        already knew - activating an input or output in the portal - was never
        discovered, with no error and no way to force a re-scan short of
        removing the integration. A NEW module was always found; a new
        parameter on an existing one never was.

        Keyed on the timestamp, not on having parameters: a module the portal
        describes as empty is answered too, and asking it again every cycle is
        the waste this replaced.
        """
        if "parameters" not in values:
            return True
        age = time.time() - values.get("parameters_fetched_at", 0)
        if age < PARAMETER_REDISCOVERY_INTERVAL_SECONDS:
            _LOGGER.debug(
                "Parameters for device %s, index %s, and type %s are "
                "cached and %.1f h old.",
                device_id,
                values["Index"],
                values["Type"],
                age / 3600,
            )
            return False
        _LOGGER.debug(
            "Re-reading parameters for device %s, index %s, type %s "
            "(cached list is %.1f h old).",
            device_id,
            values["Index"],
            values["Type"],
            age / 3600,
        )
        return True

    def _note_rate_limited_module(self, device_id, values, forbidden_count, exc) -> int:
        """Count one 403 against this device, and give up after three.

        Returns the new strike count. Three in a row means the portal is
        refusing this IP rather than this request, so the whole integration
        backs off instead of walking the remaining modules into the same wall.
        """
        forbidden_count += 1
        if forbidden_count >= 3:
            _LOGGER.error(
                "Rate limited (403) three times while fetching parameters "
                "for device %s. Aborting.",
                device_id,
            )
            self._activate_cooldown()
            raise ForbiddenError("Rate limited during get_parameters") from exc
        _LOGGER.warning(
            "Rate limit warning (403) for device %s module %s. Strike %s of 3.",
            device_id,
            values["Index"],
            forbidden_count,
        )
        return forbidden_count

    def _store_module_description(self, device_id, key, values, response) -> None:
        """Keep what the portal said this module has, or book why it did not."""
        parameters = {}
        try:
            for parameter in response.json()["Parameters"]:
                parameters[parameter["ParameterID"]] = parameter
            if not parameters:
                self._note_undescribed_module(
                    device_id,
                    key,
                    values,
                    "it described no parameters",
                    unsupported=False,
                )
            else:
                self.modules[device_id][key]["parameters"] = parameters
                self.modules[device_id][key]["parameters_fetched_at"] = time.time()
        except (KeyError, ValueError):
            # ValueError also covers a JSON-decode failure (e.g. an HTML error
            # page returned instead of JSON) - without it, a single malformed
            # response here would abort discovery for every remaining module
            # on this device, not just skip this one.
            #
            # Booked like every other unusable answer. Skipping with only a
            # log line left the module with no timestamp at all, so the age
            # check never held it back and a portal answering nonsense was
            # asked again every single cycle, without limit - the one failure
            # mode the whole retry budget exists to bound.
            self._note_undescribed_module(
                device_id,
                key,
                values,
                "its description could not be read",
                unsupported=True,
            )

    def _discover_device_parameters(self, device_id) -> None:
        """Read every module description of one device that is due."""
        forbidden_count = 0
        for key, values in self.modules[device_id].items():
            if not self._module_description_is_due(device_id, values):
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
                status_code = self._http_status(exc)
                if status_code == 403:
                    forbidden_count = self._note_rate_limited_module(
                        device_id, values, forbidden_count, exc
                    )
                    continue
                if status_code == 400:
                    self._note_undescribed_module(
                        device_id,
                        key,
                        values,
                        "the portal rejected the request",
                        unsupported=True,
                    )
                    continue
                raise
            self._store_module_description(device_id, key, values, response)

    def get_parameters(self):
        if self.modules is None:
            _LOGGER.debug(
                "get_parameters() called with no module data available yet; skipping."
            )
            return
        for device_id, device_data in self.data.items():
            if device_data.get("ConnectionStatus") != 0:
                continue
            _LOGGER.debug("Fetching api parameters data for device %s", device_id)
            _LOGGER.debug(self.data)
            _LOGGER.debug(self.modules[device_id])
            self._discover_device_parameters(device_id)

    def change_value(
        self,
        device_id,
        parameter_id,
        module_index,
        module_type,
        numeric_value,
        login=True,
        together_with=None,
    ):
        """Change a value under the shared API lock, so a write can't
        interleave with a poll cycle on the same session/state."""
        self._acquire_api_lock("parameter write")
        try:
            return self._change_value(
                device_id,
                parameter_id,
                module_index,
                module_type,
                numeric_value,
                login=login,
                together_with=together_with,
            )
        finally:
            self._api_lock.release()

    def reread_device_values(self, device_id) -> str | None:
        """Read one device's parameter values again, under the shared lock.

        For asking the portal what it actually stored. `Status: 0` on a write
        means the request was accepted, NOT that the value was kept - a
        holiday range that ends before it starts is answered exactly that way
        and silently discarded. Anything that must not report such a write as
        a success has to look.

        The lock is the point of this method existing at all: the read itself
        is the same one a poll does, and calling it straight from an entity
        would let it interleave with a running cycle on the same session.

        Same polarity as _fetch_parameter_values: None means it worked.
        """
        self._acquire_api_lock("value re-read")
        try:
            return self._fetch_parameter_values(str(device_id))
        finally:
            self._api_lock.release()

    def _change_value(
        self,
        device_id,
        parameter_id,
        module_index,
        module_type,
        numeric_value,
        login=True,
        together_with=None,
    ):
        """POST request to API to change a specific value.

        `together_with` maps further parameter ids of the SAME module to the
        values they are to carry, and they go out in one request with the one
        being changed. The portal's own payload is a list of parameters per
        module, so this is the shape it already expects - what it is for is
        a parameter that is only half of something. Holiday begin and end are
        the measured case: written one at a time, each write comes back as
        Status -1, while an ordinary setpoint on the same account and the
        same endpoint is accepted and answered with a JobID.

        The parameter being changed always wins, so a companion that repeats
        it cannot overwrite the new value with the old one.
        """
        _LOGGER.debug("Changing value for %s", parameter_id)

        parameters = [
            {"ParameterID": companion_id, "NumericValue": float(companion_value)}
            for companion_id, companion_value in (together_with or {}).items()
            if companion_id != parameter_id
        ]
        parameters.append(
            {"ParameterID": parameter_id, "NumericValue": float(numeric_value)}
        )
        data = {
            "DeviceID": int(device_id),
            "Modules": [
                {
                    "ModuleIndex": int(module_index),
                    "ModuleType": int(module_type),
                    "Parameters": parameters,
                }
            ],
        }
        # _LOGGER.info(data)

        try:
            response = self.make_api_call(
                API_DATA_ACCESS_WRITE_URL, data=data, do_retry=True
            )
        except Exception as exc:
            # Broad: every way a write can fail must reach the caller as
            # one failure type, so the service and the entities can
            # report it.
            #
            # The cause is quoted, not just chained. Home Assistant shows a
            # failed service call as str(exception) and nothing else, so a
            # message that says only "Error changing parameter X value"
            # discards what the portal actually answered - which at that
            # moment is the one thing anybody wants. A rejected holiday date
            # read exactly that in practice, while "Server returned status
            # code: -1 and message: Unbekannter Fehler" sat one exception
            # deeper where only a debug log would show it.
            raise ParameterChangeError(
                f"Error changing parameter {parameter_id}: {exc}"
            ) from exc

        # What a SUCCESSFUL write actually answers, captured from the real
        # portal rather than derived:
        #
        #   HTTP 200  {"JobID":762338890,"Status":0,"Message":null,
        #              "DetailMessages":null}
        #
        # Two things follow, and the second one is why this was measured
        # instead of reasoned out. Success DOES carry Status: 0, so demanding
        # it is safe. But `Message` is present on success too - it is simply
        # null - so the rule suggested by the only available reference
        # ("Message means failure") would have failed every legitimate write.
        #
        # Anything that is not an explicit Status 0 is therefore treated as a
        # rejection. Reporting a write as done when it was not is the worse
        # error by far: it is a heating parameter, and the entity would show
        # the requested value until the next poll quietly replaced it.
        # Kept on purpose, not left over from debugging: the check below
        # accepts ONLY an explicit Status 0, so the day the portal changes its
        # answer - new firmware, new API version - every write starts failing
        # at once. Without the raw body in the log that is unfalsifiable
        # guesswork; with it, one report settles it. Debug level, so it costs
        # nothing until someone goes looking.
        body = getattr(response, "content", b"")[:500]
        _LOGGER.debug(
            "Write response for %s: HTTP %s, body %r",
            parameter_id,
            getattr(response, "status_code", "?"),
            body,
        )

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            # On the failure path the body goes out at WARNING, because this
            # is exactly when someone asks what the portal said - and debug
            # logging is exactly what is not enabled at that moment.
            _LOGGER.warning(
                "Write for %s was not answered with a result. Portal said: %r",
                parameter_id,
                body,
            )
            raise ParameterChangeError(
                f"Portal answered the write for parameter {parameter_id} with "
                "something other than a result; the value was not changed."
            ) from exc

        ack = read_write_ack(payload)
        if not ack.acknowledged:
            # Message carries the portal's own wording; DetailMessages is a
            # list on failure. Both go to the log, only Message to the user.
            status, detail = ack.status, ack.message
            _LOGGER.warning(
                "Portal rejected the write for %s. Full answer: %r",
                parameter_id,
                body,
            )
            raise ParameterChangeError(
                f"Portal rejected the write for parameter {parameter_id} "
                f"(Status {status}" + (f": {detail}" if detail else "") + ")."
            )

    # Refresh data and retrieve new data
    def get_data(self, enabled_devices=None):
        """Refresh all data for the target devices.

        Thin per-device orchestration; the actual work is split into the
        three focused steps below (status, parameter values, heating
        schedules) plus the rate-limited statistics fetch. Purely a
        readability refactor - order, error handling and behaviour of the
        former inline blocks are unchanged.
        """
        _LOGGER.debug(
            "Fetching fresh api data. enabled_devices=%s, self.data.keys()=%s",
            enabled_devices,
            list(self.data.keys()),
        )
        # `is not None`, NOT truthiness: an EMPTY list means "every device is
        # disabled", and treating that as "no filter given" polled all of them -
        # the exact opposite of what the caller asked for.
        target_devices = (
            enabled_devices if enabled_devices is not None else list(self.data.keys())
        )
        _LOGGER.debug("Computed target_devices=%s", target_devices)
        successes = 0
        failures: list[str] = []
        for device_id in target_devices:
            # Normalize once: self.data is keyed by str, but callers may
            # pass ints. Previously the membership check used str() while
            # the accesses below used the raw value - a latent KeyError for
            # any int id that only the broad per-device handlers would catch.
            device_id = str(device_id)
            _LOGGER.debug(
                "Processing device_id=%s. Is in self.data? %s",
                device_id,
                device_id in self.data,
            )
            if device_id not in self.data:
                continue
            # Skip devices that only exist on the web-scraper side (e.g. the
            # persisted "0000" placeholder of a web->both install): they have
            # no API-discovered modules, so the API refresh below has nothing
            # to fetch and self.modules[device_id] would raise KeyError.
            # Their scraped sensors are handled entirely by the scraper path.
            if device_id not in self.modules:
                _LOGGER.debug(
                    "Skipping device %s: no API modules (scraper-only).", device_id
                )
                continue
            if not self._fetch_device_status(device_id):
                # Read fine, the device just is not online, so there is
                # nothing to poll from it this cycle. Deliberately NOT
                # counted as a failure of the cycle: an unreachable device is
                # reported by its own entities, which go unavailable via
                # utils.device_is_reachable, and by the connection-status
                # sensor, which stays available to say why.
                #
                # Failing the cycle instead would take down every OTHER
                # entity with it - the connection-status sensor included -
                # discard a web scrape that had already succeeded this cycle
                # in `both` mode, and put the coordinator into a backoff of
                # up to six hours, so the device coming back would be noticed
                # late.
                continue
            # `is None`, never a truth test: the reason for a FAILURE is what
            # comes back, so a truthy answer is the bad one.
            failure = self._fetch_parameter_values(device_id)
            if failure is None:
                successes += 1
            else:
                failures.append(f"device {device_id}: {failure}")
            self._fetch_circuit_times(device_id)

        # Fetch Energy Statistics (rate limited internally)
        self.get_statistics(enabled_devices)

        # If every attempted device failed to refresh its parameters (and at
        # least one was attempted), surface it so the coordinator treats the
        # cycle as failed (backoff / eventual reauth) instead of marking stale
        # values as a successful update. A partial success (at least one
        # device refreshed) is still treated as success.
        if failures and not successes:
            # Every reason, not a sample: Home Assistant shows this string and
            # nothing else, and a cap here would read as "that was all of it".
            raise WemPortalError(
                "All API parameter fetches failed this cycle. " + "; ".join(failures)
            )

    def _fetch_device_status(self, device_id: str) -> bool:
        """Fetch the device's connection status and error sensors.

        Returns False only when the status was read successfully and the
        device is not online (polling it further is pointless this cycle).
        A FAILED status fetch returns True: the device may well be fine,
        so the parameter fetch still gets its chance - same behaviour as
        the former inline block.
        """
        try:
            status_response = self.make_api_call(
                API_DEVICE_STATUS_READ_URL,
                data={"DeviceID": int(device_id)},
                do_retry=True,
                retry_transport=True,
            ).json()

            raw_status = status_response.get("ConnectionStatus", -1)
            status_map = {0: "online", 7: "wrong_secret", 8: "busy", 50: "offline"}
            conn_status = status_map.get(raw_status, "unknown")

            # Keep the RAW status current as well. get_parameters() gates on
            # this unprefixed field, which was otherwise written only by
            # get_devices() - and that runs once per session. A device that
            # was offline at startup therefore never got its parameters
            # discovered, not even after coming back, because the gate still
            # saw the status from the moment Home Assistant started. Only a
            # reload fixed it.
            self.data[device_id]["ConnectionStatus"] = raw_status

            self.data[device_id][f"{device_id}-{DEVICE_STATUS_CONNECTION}"] = {
                "friendlyName": "Connection Status",
                "ParameterID": DEVICE_STATUS_CONNECTION,
                "unit": None,
                "value": conn_status,
                "IsWriteable": False,
                "DataType": -1,
                "ModuleIndex": -1,
                "ModuleType": -1,
                "platform": "sensor",
                "icon": "mdi:network",
            }

            errors = status_response.get("Errors", [])
            has_errors = "Yes" if errors else "No"
            error_message, error_detail = error_state_and_detail(errors)

            self.data[device_id][f"{device_id}-{DEVICE_STATUS_HAS_ERRORS}"] = {
                "friendlyName": "Has Errors",
                "ParameterID": DEVICE_STATUS_HAS_ERRORS,
                "unit": None,
                "value": has_errors,
                "IsWriteable": False,
                "DataType": -1,
                "ModuleIndex": -1,
                "ModuleType": -1,
                "platform": "sensor",
                "icon": "mdi:alert",
            }

            self.data[device_id][f"{device_id}-{DEVICE_STATUS_ERROR_MESSAGES}"] = {
                "friendlyName": "Error Messages",
                "ParameterID": DEVICE_STATUS_ERROR_MESSAGES,
                "unit": None,
                "value": error_message,
                # Every fault, whatever the state could hold. The state is
                # capped by Home Assistant; this is not.
                "Errors": error_detail,
                "IsWriteable": False,
                "DataType": -1,
                "ModuleIndex": -1,
                "ModuleType": -1,
                "platform": "sensor",
                "icon": "mdi:message-alert",
            }

            previous = self._last_connection_status.get(device_id)
            self._last_connection_status[device_id] = conn_status

            if conn_status != "online":
                # Edge-triggered on purpose. An unreachable device no longer
                # fails the cycle, so this line is the only running
                # commentary there is - but repeating it every few minutes
                # for as long as the device stays away would bury everything
                # else in the log. Say it once per change, then stay quiet.
                log = _LOGGER.debug if previous == conn_status else _LOGGER.warning
                log("Device %s is %s. Skipping data polling.", device_id, conn_status)
                return False
            if previous is not None and previous != "online":
                _LOGGER.info("Device %s is back online.", device_id)

        except Exception as exc:  # noqa: BLE001
            # Broad: an unreadable status must not stop the poll. The
            # caller treats "unknown" as reachable, which is the safe
            # side - see device_is_reachable.
            _LOGGER.warning("Failed to fetch Device Status: %s", exc)
            self._forget_device_status(device_id)
        return True

    def _forget_device_status(self, device_id: str) -> None:
        """Stop presenting the last known status as the current one.

        These three rows are written only by a successful status read. Left
        alone when one fails, they go on publishing whatever the previous
        answer said - and for "Has Errors" that means answering "No" because
        nothing is known rather than because nothing is wrong. That is the
        one direction a fault sensor must never fail in: an automation
        waiting for a fault sees the quiet and concludes there is none.

        The raw ConnectionStatus that get_parameters() gates on is
        deliberately NOT cleared. A failed status read says nothing about
        whether the device is there, and stopping discovery over it would
        turn one missed request into an installation with no parameters.
        device_is_reachable is unaffected too: None is not one of the
        definitively-dead states, so the entities stay available and show
        "unknown", which is exactly what is true.
        """
        device_data = self.data.get(device_id)
        if not isinstance(device_data, dict):
            return
        for row_name in DEVICE_STATUS_ROWS:
            row = device_data.get(f"{device_id}-{row_name}")
            if isinstance(row, dict):
                row["value"] = None

    def _fetch_parameter_values(self, device_id: str) -> str | None:
        """Refresh and read all known parameter values for one device.

        Returns None when the values were refreshed, and otherwise the reason
        they were not. get_data() collects those so a cycle in which every
        device failed is reported as a failed update instead of a successful
        one - and can say why.

        NOTE the polarity, because it is the reverse of what it looks like:
        None is the GOOD answer and a non-empty string the bad one, so
        `if self._fetch_parameter_values(...)` reads exactly backwards. Every
        caller compares against None explicitly. It used to return a plain
        bool, and the reason - the one thing anybody wants when an update
        fails - was written to a warning and then dropped, leaving Home
        Assistant to report "all API parameter fetches failed this cycle" and
        nothing else. Correlating that with the warning above it by timestamp
        was work the caller could do for the user.
        """
        try:
            data = {
                "DeviceID": int(device_id),
                "Modules": [
                    {
                        "ModuleIndex": module["Index"],
                        "ModuleType": module["Type"],
                        "Parameters": [
                            {"ParameterID": parameter}
                            for parameter in module["parameters"]
                        ],
                    }
                    for module in self.modules[device_id].values()
                    if module.get("parameters")
                ],
            }
        except KeyError as exc:
            # Don't re-index self.modules[device_id] here: if that key is the
            # one missing, the log call itself would raise a second KeyError
            # and escape this handler unhandled.
            _LOGGER.debug(
                "%s: missing module data for device %s", DATA_GATHERING_ERROR, device_id
            )
            raise WemPortalError(DATA_GATHERING_ERROR) from exc

        if not data["Modules"]:
            # Nothing to ask for, and asking anyway is not merely pointless:
            # the portal answers a read with an empty module list with 400
            # Bad Request. Upstream issue #66 is a log of exactly that shape,
            # {'DeviceID': ..., 'Modules': []}, failing every cycle until the
            # reporter gave up and switched to web mode. Reachable here from
            # the other side too - a module whose EventType/Read answers 400
            # is dropped from the cache as unsupported, so a device where
            # that happens to all of them ends up with this list.
            #
            # The two ways to get here are NOT the same thing, and reporting
            # them alike was the first version of this guard: it made a
            # device that genuinely has nothing to poll fail every cycle,
            # which three existing tests object to for good reason.
            device_modules = self.modules.get(device_id) or {}
            # Nothing to poll, and nothing wrong: no modules at all, or every
            # module described and every description empty. One branch,
            # because `all()` of nothing is true - and because failing the
            # cycle for either would drag every other device into a backoff
            # over a device that simply has nothing to say.
            #
            # The second half is what a module the portal refuses to describe
            # now looks like: kept with an empty list rather than deleted
            # (see _note_undescribed_module). Deleting it used to empty the
            # module dict, so an installation whose every module was rejected
            # landed in the first half and counted as a quiet success.
            # Keeping them WITHOUT this branch would have turned that into a
            # cycle that fails for ever - backoff, recovery, eventually a
            # re-authentication prompt. One bug traded for a worse one.
            if all("parameters" in module for module in device_modules.values()):
                if not device_modules:
                    reason = "it has no modules"
                else:
                    reason = "every module describes no parameters"
                _LOGGER.debug("Device %s has nothing to read: %s.", device_id, reason)
                return None

            # Modules whose description has not arrived at all: discovery has
            # not produced any yet. That IS a failed refresh - no values were
            # read - and saying so is what keeps the cycle honest. Discovery
            # runs again next cycle for any module without parameters, so it
            # can still recover on its own.
            _LOGGER.warning(
                "Device %s has modules but no known parameters, so there is "
                "nothing to read. Parameter discovery has not produced any "
                "yet and runs again next cycle. Not sending the refresh - "
                "the portal rejects a read with an empty module list.",
                device_id,
            )
            return "it has modules but no known parameters yet"

        try:
            # Deliberately NO retry_transport here, unlike the two reads
            # around it. This POST starts a measurement job, so it is the one
            # request on the value path that is not safe to repeat: if the
            # portal received it and only the answer was lost, a second
            # attempt starts a SECOND job - and overlapping jobs handing back
            # each other's values is the exact hazard the JobID handling below
            # exists to prevent. A lost refresh costs this cycle; a duplicated
            # one can cost the next cycle its correctness.
            refresh_response = self.make_api_call(
                API_REFRESH_URL,
                data=data,
            )
            # /Refresh answers with the JobID of the measurement it started,
            # and /Read needs it to say which one to return. Reading without
            # it "works" only in the sense that the server answers: it falls
            # back to the most recent job, so two overlapping refreshes can
            # hand back each other's values.
            read_data = data
            try:
                refresh_payload = refresh_response.json()
            except (ValueError, AttributeError):
                # Unreadable is not the same as "no status given". Falling
                # through left the read without a JobID, which makes the
                # server return the most recent job - the PREVIOUS
                # measurement - whose values were then booked as fresh.
                _LOGGER.warning(
                    "Device %s answered the refresh with something that is not "
                    "JSON; skipping the read rather than serving the previous "
                    "measurement as current.",
                    device_id,
                )
                return "the refresh answered with something that is not JSON"
            ticket = read_refresh_ticket(refresh_payload)
            if not ticket.accepted:
                _LOGGER.warning(
                    "Device %s %s; not reading the previous job's values as current.",
                    device_id,
                    ticket.reason,
                )
                return ticket.reason
            if ticket.job_id is None:
                # Reported, not enforced - on purpose, and this is the whole
                # reasoning:
                #
                # Refusing the read here would be the strict reading of "the
                # server falls back to the most recent job". But a device that
                # fails is a failed device, and with a single device (the
                # normal case) `failures and not successes` below turns that
                # into a failed CYCLE: backoff, recovery, eventually reauth.
                # So if any installation's portal legitimately answers without
                # a JobID, the strict version does not degrade that
                # installation, it takes it off the air.
                #
                # Whether that happens is not established - the JobID has only
                # ever been observed present. So this measures instead of
                # guessing: one warning per device, and the decision can be
                # made on evidence. Same approach as the maintenance marker.
                _report_missing_job_id(device_id)
            else:
                read_data = {**data, "JobID": ticket.job_id}
            time.sleep(5)
            values = self.make_api_call(
                API_DATA_ACCESS_READ_URL,
                data=read_data,
                do_retry=True,
                retry_transport=True,
            ).json()
            # An HTTP 200 with nothing in it is not a refreshed device. The
            # mapper simply finds no modules to walk, so this used to return
            # True and count as a success: the cycle was reported as good and
            # every entity kept presenting its previous reading as current.
            # Guarded by what we ASKED for, so a device that genuinely has no
            # modules is not turned into a failure.
            if data.get("Modules") and not values.get("Modules"):
                _LOGGER.warning(
                    "Device %s answered the value read without any modules; "
                    "treating the cycle as failed rather than keeping stale "
                    "readings.",
                    device_id,
                )
                return "the value read came back without any modules"
            WemPortalDataMapper.process_api_values(
                device_id=device_id,
                values_json=values,
                modules_dict=self.modules,
                language=self.language,
                scraping_mapper=self.scraping_mapper,
                mode=self.mode,
                api_data=self.data,
                # Resolved only in `both` mode: the call locks the id in as a
                # side effect, and in the other modes there is no scrape to
                # merge with anyway.
                scraper_device_id=(
                    self.resolve_scraper_device_id() if self.mode == "both" else None
                ),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            # Broad: one device's parameter read failing must not take
            # the other devices' readings with it. The reason goes to the
            # caller as well as into this warning - it is what Home Assistant
            # ends up showing the user when the whole cycle fails.
            _LOGGER.warning("Failed to fetch parameter data... %s", exc)
            return str(exc)

    def _is_schedule_parameter(
        self, device_id, module, parameter_id, parameter_data
    ) -> bool:
        """Whether this parameter is one of the portal's weekly programmes.

        Two ways the portal types one, and keying on the declared type alone
        meant the whole fetch never ran on a 3.1.3.0 portal: there every
        programme is DataType 2 with a JSON object in the value, and the
        fetch - the only path that asks the DEVICE for its schedule rather
        than reading the portal's stored copy - sat unused. It did not fail;
        it was never entered, which is why nothing about it appeared in any
        log.
        """
        row = self.data.get(device_id, {}).get(f"{module['Name']}-{parameter_id}")
        return parameter_data.get(
            "DataType"
        ) == WemDataType.PROGRAM or looks_like_schedule((row or {}).get("value"))

    def _schedule_is_due(self, device_id, parameter_id) -> bool:
        """Whether this programme may be asked for again yet.

        Heating schedules rarely change - only through the WEM Portal app
        directly, since this integration shows them read-only - so refetching
        one on every coordinator cycle is load for nothing.
        """
        last_fetch = self._last_circuit_times_fetch.get((device_id, parameter_id), 0)
        return time.time() - last_fetch >= CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS

    def _record_schedule_attempt(self, device_id, parameter_id, attempted_at, fetched):
        """Book the ATTEMPT, however it ended.

        Written only after a SUCCESS, as it once was, the interval guard never
        engages for a schedule that keeps failing: every coordinator cycle
        spends two more requests on it, at a portal that is already failing,
        against an IP the portal blocks past 10,000 requests per 12 hours.
        Back-dated rather than blocked outright when it did not work out, so
        one bad cycle does not cost a full hour either. Same shape and same
        reasoning as get_statistics().
        """
        if fetched:
            self._last_circuit_times_fetch[(device_id, parameter_id)] = attempted_at
            return
        self._last_circuit_times_fetch[(device_id, parameter_id)] = attempted_at - max(
            0,
            CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS
            - CIRCUIT_TIMES_RETRY_INTERVAL_SECONDS,
        )

    def _read_one_schedule(self, device_id, module, parameter_id) -> bool:
        """Ask the device for one programme and store what it reports.

        Returns whether a schedule actually came back; the caller books the
        attempt either way.
        """
        module_index = module.get("Index")
        module_type = module.get("Type")
        address = {
            "DeviceID": int(device_id),
            "ModuleIndex": module_index,
            "ModuleType": module_type,
            "ParameterID": parameter_id,
        }

        job_resp = self.make_api_call(
            API_CIRCUIT_TIMES_REFRESH_URL, data=address, do_retry=True
        ).json()
        job_id = job_resp.get("JobID")
        if job_id is None:
            return False

        time.sleep(2)  # Give backend time to build the schedule payload
        schedule_resp = self.make_api_call(
            API_CIRCUIT_TIMES_READ_URL,
            data={**address, "JobID": job_id},
            do_retry=True,
        ).json()

        sensor_name = f"{module['Name']}-{parameter_id}"
        if sensor_name not in self.data[device_id]:
            self.data[device_id][sensor_name] = {
                "friendlyName": translate(
                    self.language, friendly_name_mapper(parameter_id)
                ),
                "ParameterID": parameter_id,
                "unit": None,
                "value": "Active",
                "IsWriteable": False,
                "DataType": 6,
                "ModuleIndex": module_index,
                "ModuleType": module_type,
                "platform": "sensor",
                "icon": "mdi:calendar-clock",
            }

        self.data[device_id][sensor_name]["CircuitTimesDay"] = schedule_resp.get(
            "CircuitTimesDay", []
        )
        self.data[device_id][sensor_name]["PossibleValues"] = schedule_resp.get(
            "PossibleValues", []
        )
        # The value is NOT touched. This fetch adds detail to a row the value
        # read already filled; writing "Active" over it replaced a readable
        # week with a placeholder once an hour, until the next cycle put the
        # programme back. Only a row that did not exist gets the placeholder,
        # above - there the fetch is the only source there is.
        return True

    def _fetch_circuit_times(self, device_id: str) -> None:
        """Fetch the device's own view of every weekly programme it has,
        throttled per programme."""
        try:
            for module in self.modules[device_id].values():
                for parameter_id, parameter_data in (
                    module.get("parameters") or {}
                ).items():
                    if not self._is_schedule_parameter(
                        device_id, module, parameter_id, parameter_data
                    ):
                        continue
                    if not self._schedule_is_due(device_id, parameter_id):
                        continue
                    attempted_at = time.time()
                    fetched = False
                    try:
                        fetched = self._read_one_schedule(
                            device_id, module, parameter_id
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Broad: one heating program failing is not a reason
                        # to skip the rest.
                        _LOGGER.warning(
                            "Failed to fetch CircuitTimes for %s: %s",
                            parameter_id,
                            exc,
                        )
                    finally:
                        self._record_schedule_attempt(
                            device_id, parameter_id, attempted_at, fetched
                        )
        except Exception as exc:  # noqa: BLE001
            # Broad: heating programs are extra detail on top of the
            # readings. Losing them must never cost the update itself.
            _LOGGER.warning("Error processing CircuitTimes: %s", exc)

    def _statistics_devices(self, enabled_devices=None) -> list:
        """The devices this cycle should ask the portal about."""
        # `is not None`, NOT truthiness: an EMPTY list means "every device is
        # disabled", and treating that as "no filter given" polled all of them -
        # the exact opposite of what the caller asked for.
        target_devices = (
            enabled_devices if enabled_devices is not None else list(self.data.keys())
        )
        # Same str-normalization as in get_data(): self.data is keyed
        # by str, callers may pass ints. Scraper-only devices (e.g. the "0000"
        # placeholder) have no API statistics; they are skipped so
        # int("0000")=0 isn't sent to the portal.
        return [
            str(device_id)
            for device_id in target_devices
            if str(device_id) in self.data and str(device_id) in self.modules
        ]

    def _statistics_group_name(self, group: dict) -> str:
        """The display name for one statistics group: the portal's own
        description where it has one, a fixed fallback where it is blank."""
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
                8: "Total Power Consumption",
            }
            group_name = fallback_names.get(group_id, f"Energy {group_id}")
        else:
            translated_group = translate(self.language, group_name)
            if "energy" not in translated_group.lower():
                group_name = f"{translated_group} Energy"
            else:
                group_name = translated_group
        return group_name

    def _store_statistics_group(
        self, device_id, group_id, group_name, stats_resp
    ) -> None:
        """Turn one group's read response into its energy sensor.

        Returns without writing wherever the group loop used to `continue`:
        either way there is nothing left to do for this group.
        """
        values = stats_resp.get("Values", [])
        if not values:
            return

        # Pick by the Date the entry carries, not by list
        # position - see utils.latest_statistics_entry.
        latest_stat = latest_statistics_entry(values)
        current_value = latest_stat.get("Value")
        _LOGGER.debug(
            "Statistics group %s: using entry dated %s of %d",
            group_id,
            latest_stat.get("Date", "?"),
            len(values),
        )

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
                # No previous value either: skip rather than
                # invent a 0.0, which the Energy Dashboard
                # reads as a meter reset on a
                # total_increasing sensor.
                _LOGGER.debug(
                    "Statistics group %s has no value yet; "
                    "skipping instead of reporting 0.",
                    group_id,
                )
                return

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
            "device_class": "energy",
            "state_class": "total_increasing",
        }

    def _fetch_device_statistics(self, device_id: str) -> None:
        """Read every statistics group the portal lists for one device.

        Lets the refresh call's exception through: a device whose refresh
        failed is a failed device for the retry bookkeeping in get_statistics.
        A single rejected GROUP is a different matter and handled here - the
        portal routinely lists groups it then refuses to read.

        But "a single group" quietly became "all of them": every group error
        was swallowed here, so a device whose every group failed still
        returned normally and counted as a success in get_statistics. The
        shorter retry then never engaged and the readings waited the full
        refresh interval - the one case where waiting is most clearly wrong.
        Raises when nothing came back AND something actually failed.

        Status 3001 is not a failure. It means the group does not apply to
        this module, so there is nothing to fetch sooner; a device whose
        groups are all 3001 has no statistics at all and retrying earlier
        would only cost requests.
        """
        refresh_resp = self.make_api_call(
            API_STATISTICS_REFRESH_URL, data={"DeviceID": int(device_id)}, do_retry=True
        ).json()

        group_types = refresh_resp.get("GroupTypeDescriptions", [])
        headers = {"X-Api-Version": "2.0.0.0"}
        read = 0
        failed = 0

        for group in group_types:
            group_id = group.get("GroupType")
            group_name = self._statistics_group_name(group)

            read_payload = {
                "DeviceID": int(device_id),
                "ModuleType": 7,
                "ModuleIndex": 0,
                "GroupType": group_id,
                "Type": 1,
            }

            try:
                time.sleep(2)  # Avoid hammering the API
                stats_resp = self.make_api_call(
                    API_STATISTICS_READ_URL,
                    headers=headers,
                    data=read_payload,
                    do_retry=True,
                ).json()

                self._store_statistics_group(
                    device_id, group_id, group_name, stats_resp
                )
                read += 1

            except Exception as exc:  # noqa: BLE001
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
                        group_id,
                        WEM_INVALID_PARAMETER_STATUS,
                    )
                else:
                    failed += 1
                    _LOGGER.warning(
                        "Failed to fetch Statistics for group %s: %s", group_id, exc
                    )

        if failed and not read:
            raise WemPortalError(
                f"Every statistics group of device {device_id} failed to read "
                f"({failed} of {len(group_types)})."
            )

    def get_statistics(self, enabled_devices=None):
        """Fetch historical statistics from the API, rate limited to once per hour.

        The timestamp is set BEFORE fetching, on purpose: it records the last
        ATTEMPT, so a portal that keeps failing (or is rate-limiting us) is
        never asked more than once per interval. The downside is that a single
        failure would otherwise cost a full hour of statistics, so a cycle that
        failed for every device shortens the wait to
        STATISTICS_RETRY_INTERVAL_SECONDS instead (see the end of this method).
        """
        now = time.time()
        if (
            self.last_statistics_fetch is not None
            and (now - self.last_statistics_fetch) < STATISTICS_REFRESH_INTERVAL_SECONDS
        ):
            return

        self.last_statistics_fetch = now
        _LOGGER.debug("Fetching statistics data")

        # Track per-device outcomes so a completely failed cycle can retry
        # sooner. Only the outer (per-device) call counts: individual
        # statistics groups are routinely rejected (status 3001) for modules
        # they don't apply to, which is expected and not a failure.
        attempted = 0
        succeeded = 0

        for device_id in self._statistics_devices(enabled_devices):
            attempted += 1
            try:
                self._fetch_device_statistics(device_id)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                # Broad: one device's statistics failing must not stop
                # the others. `succeeded` stays unincremented, which is
                # what the retry back-dating below reads.
                _LOGGER.warning("Error processing Statistics: %s", exc)

        # Every attempted device failed: back-date the timestamp so the next
        # cycle retries after the shorter retry interval rather than waiting a
        # full refresh interval. The guard itself stays intact - a portal that
        # keeps failing is still only asked once per retry interval, never on
        # every coordinator cycle.
        if attempted and not succeeded:
            self.last_statistics_fetch = now - max(
                0,
                STATISTICS_REFRESH_INTERVAL_SECONDS - STATISTICS_RETRY_INTERVAL_SECONDS,
            )
            _LOGGER.debug(
                "Statistics failed for all %d device(s); retrying in ~%d min "
                "instead of %d min.",
                attempted,
                STATISTICS_RETRY_INTERVAL_SECONDS // 60,
                STATISTICS_REFRESH_INTERVAL_SECONDS // 60,
            )
