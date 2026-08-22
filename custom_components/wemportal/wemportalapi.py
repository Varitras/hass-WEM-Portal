"""
Weishaupt webscraping and API library
"""

from typing import TYPE_CHECKING, Any, Final
from collections.abc import Callable, Iterator, Mapping
import logging

import copy
import threading
import time
from datetime import datetime, timedelta

import requests
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.util import dt as dt_util

from .const import (
    API_LOCK_TIMEOUT_SECONDS,
    CONF_LANGUAGE,
    CONF_MODE,
    CONF_SCAN_INTERVAL_API,
    DATA_GATHERING_ERROR,
    DEFAULT_CONF_LANGUAGE_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_CONF_SCAN_INTERVAL_VALUE,
    DEFAULT_MODE,
    DEFAULT_TIMEOUT,
    GITHUB_PROJECT_URL,
    MIN_SCAN_INTERVAL_API_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    WemDataType,
)
from .models import ModuleRef, Reading, account_state
from .schedule import WemPortalSchedule
from .statistics import WemPortalStatistics
from .transport import WemPortalTransport
from .exceptions import (
    ApiBusyError,
    AuthError,
    ForbiddenError,
    ParameterChangeError,
    PollDeadlineExceeded,
    PortalMaintenanceError,
    ServerError,
    WemPortalError,
)
from .mapper import WemPortalDataMapper, forget_dropped_parameters
from .mobile_protocol import (
    as_answer_dict,
    described_parameters,
    read_refresh_ticket,
    read_write_ack,
)
from .translations import translate
from .utils import (
    clamped_scan_interval,
    error_state_and_detail,
    looks_like_schedule,
    portal_list,
    schedule_fetch_still_feeds,
    short_device_id,
)

if TYPE_CHECKING:
    # Imported lazily at runtime (see fetch_webscraping_data); named here only
    # to type the persistent scraper handle.
    from .scraper import WemPortalScraper

_LOGGER = logging.getLogger(__name__)

API_DATA_ACCESS_READ_URL: Final = "https://www.wemportal.com/app/DataAccess/Read"

API_DATA_ACCESS_WRITE_URL: Final = "https://www.wemportal.com/app/DataAccess/Write"

API_DEVICE_READ_URL: Final = "https://www.wemportal.com/app/Device/Read"

API_DEVICE_STATUS_READ_URL: Final = "https://www.wemportal.com/app/DeviceStatus/Read"

API_EVENT_TYPE_READ_URL: Final = "https://www.wemportal.com/app/EventType/Read"

API_REFRESH_URL: Final = "https://www.wemportal.com/app/DataAccess/Refresh"


# How long one device's readings may stay on display without a successful
# read of its own.
#
# A cycle where one device fails and another succeeds is reported as a
# SUCCESS, and rightly so: failing it would take every other device's
# entities down with it. But that also means the failing device keeps
# publishing whatever it last returned, with nothing saying otherwise -
# mapper._clear_unanswered only runs when the portal ANSWERED and left a
# parameter out, which is not this case.
#
# A duration rather than a count of failed cycles, unlike the scrape: there
# the backoff stretches the gap between attempts, so counting attempts was
# the only honest measure. Here the cycle counts as successful, so the
# interval stays whatever the user configured - and a limit in minutes then
# means the same thing whether that is 5 minutes or 30.
DEVICE_VALUES_STALE_AFTER_SECONDS: Final = 30 * 60

# ...but never shorter than this many polls. The limit above is a
# duration on purpose, and nothing caps the API interval from above -
# the options only enforce a floor. An installation polling every 45
# minutes was therefore past a fixed half hour before its next attempt
# even ran, so the first miss emptied everything: the opposite of the
# one-failed-cycle tolerance this exists for. Two, so exactly one
# missed cycle is survivable and the second is not.
DEVICE_VALUES_STALE_AFTER_POLLS: Final = 2

# Heating schedules (CircuitTimes) rarely change - only when a user edits
# them directly in the WEM Portal app (this integration only ever shows
# them as read-only sensors). Refetching them every single coordinator
# cycle is unnecessary load; this caps how often they're refreshed.
# How long a module's discovered parameter list is trusted before the portal
# is asked again.
#
# The list used to be cached forever: activating an input or output on a
# module the integration already knew produced a parameter it would never
# discover, with no error and no way to force a re-scan short of removing and
# re-adding the integration. A NEW module was found (it has no cached
# parameters), a new parameter on an existing one was not.
#
# One JSON request per module makes this cheap enough to do on a timer -
# unlike the Fachmann discovery, which is a full web navigation and stays
# on-demand only. Four modules once a day is 0.04% of the portal's 10,000
# requests per 12 hours.
#
# Wall clock, not monotonic: the timestamp is persisted with the module cache
# and has to survive a restart, which monotonic does not.
PARAMETER_REDISCOVERY_INTERVAL_SECONDS: Final = 24 * 3600  # 1 day

# How soon a FAILED re-scan is attempted again. Shorter than the interval
# above, but not immediate: a portal that just refused must not be asked once
# per cycle. Same shape as the statistics and schedule retries.
PARAMETER_REDISCOVERY_RETRY_SECONDS: Final = 3600  # 1 hour

# How long one poll cycle may spend before it stops itself.
#
# The same reasoning as the lock timeout above, one step further along.
# asyncio.timeout cancels the coordinator's AWAIT; it cannot cancel the
# executor thread behind it. A cycle that overran therefore ran on to
# completion - holding the shared lock, still spending requests at a portal
# that counts them per IP - while Home Assistant had already recorded the
# failure and moved on. Nobody was waiting for that work any more.
#
# Below DEFAULT_TIMEOUT so the worker is gone BEFORE the coordinator gives
# up rather than after: the next cycle then finds a free lock instead of
# queueing behind an abandoned one. Never below API_LOCK_TIMEOUT_SECONDS.
POLL_DEADLINE_SECONDS: Final = DEFAULT_TIMEOUT - 30

# Placeholder device id the web scraper falls back to when no real
# API-discovered device is known (a pure-web install that never ran the
# mobile API). The scraper itself has no device concept - it reads a web
# page - but entity unique_ids are "<entry>:<device_id>:<name>", so scraped
# sensors need a STABLE device id or their history breaks on mode switches.
# See WemPortalApi.resolve_scraper_device_id() for how this is locked in
# once and then persisted.
SCRAPER_FALLBACK_DEVICE_ID: Final = "0000"

# How many scrapes in a row may fail before the values they produced stop
# being presented as current.
#
# Counted in failures rather than measured as an age, because the two are not
# proportional: each failure adds a growing pause of its own, so with a
# five-minute interval the third failure lands about six intervals after the
# last success, and with a thirty-minute one about three. A multiple of the
# interval would therefore mean a different thing on every installation, while
# a count means the same everywhere - and still clears sooner where the
# interval is shorter, which is the right way round.
#
# Three rather than one: a single failed scrape is ordinary, and the second is
# where the cached session is discarded and a full login retried. Only the
# third says the portal is not delivering. The counter resets on any
# successful scrape.
#
# Note this counts ATTEMPTS THAT FAILED, not "we have not looked". A scrape
# that is never run - because its device is disabled - leaves the values
# alone; nothing was asked, so nothing was refused.
SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE: Final = 3


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


def _report_missing_job_id(device_id: str, reported: set[str]) -> None:
    """Say once that a device started no identifiable measurement job.

    A read without a JobID is answered from the most recent job, which may be
    the PREVIOUS measurement served as current. Whether a healthy portal ever
    answers this way is not established - this is what would establish it.
    `reported` is the account's own once-per-device memory.
    """
    if device_id in reported:
        return
    reported.add(device_id)
    _LOGGER.warning(
        "Device %s answered the measurement refresh without a JobID. The "
        "read then returns whichever job the portal considers newest, which "
        "may be the previous measurement. Please report this together with "
        "your portal model - see %s",
        device_id,
        GITHUB_PROJECT_URL,
    )


class WemPortalApi(WemPortalTransport, WemPortalStatistics, WemPortalSchedule):
    """Wrapper class for Weishaupt WEM Portal"""

    def __init__(
        self,
        username: str,
        password: str,
        config: Mapping[str, Any] | None = None,
        existing_data: dict[str, dict[str, Any]] | None = None,
        cached_modules: dict[str, Any] | None = None,
        blocked_until: float = 0.0,
        scraper_device_id: str | None = None,
        expert_blocked_until: float = 0.0,
        scraper_backoff: tuple[int, int, datetime | None] | None = None,
    ) -> None:
        """Assemble the api object from three sources, kept apart because
        they have different lifetimes: the user's options, the state the
        coordinator persisted across a restart, and the state that always
        starts empty. Which of these a RECOVERY may reset is a separate
        question - see reset_transport.
        """
        self.username = username
        self.password = password
        # The account's reload-surviving memory: expert backoff, auth streak,
        # once-per-subject warnings. See models.AccountState.
        self._account_state = account_state(username)
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

    def _init_from_config(self, config: Mapping[str, Any] | None) -> None:
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
        existing_data: dict[str, dict[str, Any]] | None,
        cached_modules: dict[str, Any] | None,
        scraper_device_id: str | None,
        blocked_until: float,
        expert_blocked_until: float,
        scraper_backoff: tuple[int, int, datetime | None] | None,
    ) -> None:
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
        self.modules: dict[str, Any] | None = (
            copy.deepcopy(cached_modules) if cached_modules else None
        )
        # When each module was last named in a values answer, per device.
        # Deliberately NOT inside self.modules: the list is replaced wholesale
        # on every re-discovery, and a module that drops out of it is exactly
        # the one whose readings then have nothing left to refresh OR age
        # them. See _stamp_answered_modules.
        self._module_answered_at: dict[str, dict[ModuleRef, float]] = {}
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

    def _init_runtime_state(self) -> None:
        """State that always starts empty: the HTTP transport, the
        per-session caches and the timestamps a cycle fills in.
        """
        # Whether a full cycle has completed in this session. The daily
        # parameter re-read waits for it: the FIRST refresh runs inside Home
        # Assistant's setup, and a discovery there (five seconds of sleep per
        # module) turns every restart with an expired cache into a slow
        # startup.
        self._first_cycle_done = False
        # When the mobile API was last read, for the `both`-mode gate. None
        # rather than 0.0: zero on the monotonic clock is the moment the
        # machine booted, which would read as "long overdue" only by luck.
        self._last_api_read: float | None = None
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
        self._deadline: float | None = None
        # device_id -> when its values were last refreshed, for the staleness
        # check above. Monotonic because it measures a duration and never
        # appears in output; a restart starts empty, which is correct - there
        # is nothing on display yet either.
        self._last_device_read: dict[str, float] = {}
        self.webscraping_cookie = {}
        # Persistent scraper instance, kept across coordinator cycles so
        # its underlying HTTP session (TCP connection + TLS handshake) is
        # reused instead of being torn down and re-established on every
        # single scrape. Created lazily on first use in
        # fetch_webscraping_data(). Note: the session-cookie reuse (which
        # skips the login *requests*) is separate from this - keeping the
        # instance also skips the per-cycle connection setup itself.
        self._scraper: WemPortalScraper | None = None
        # Headers used for all API calls
        self.headers = {
            "User-Agent": "WeishauptWEMApp",
            "X-Api-Version": "3.1.3.0",
            "Accept": "*/*",
            "Host": "www.wemportal.com",
        }
        # DeviceType per device id, as reported by Device/Read. Only feeds
        # the model name shown in Home Assistant.
        self.device_types: dict[str, Any] = {}
        # Scraped keys seen in the previous cycle, to notice when the
        # portal relabels a row (see _warn_about_renamed_scraper_keys).
        self._previous_scraper_keys: set[str] | None = None
        # Last connection status per device, so the offline log line is
        # edge-triggered rather than repeated every cycle.
        self._last_connection_status: dict[str, Any] = {}
        self.scraping_mapper = {}
        # The two hourly gates. Here rather than on the account state, so
        # they are forgotten by the same reload that forgets the readings
        # they guard - see the note in models.AccountState for why keeping
        # them was worse. "Never fetched" is None and a missing key, never
        # zero: on the monotonic clock these are read on, zero is the moment
        # the machine booted.
        self.last_statistics_fetch: float | None = None
        self._last_circuit_times_fetch: dict[tuple[Any, ...], float] = {}
        # In-memory cookie cache shared by the short-lived expert clients,
        # so they can continue an existing web session instead of logging in
        # for every single operation (see expert_writer._try_cached_session).
        # Never persisted: a live session cookie is credential-equivalent.
        self.expert_cookies = {}
        self.api_version = None

    def _register_scrape_failure(self) -> None:
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

    def _forget_stale_device_values(self, device_id: str) -> None:
        """Stop presenting one device's readings once they are too old.

        The same rule _forget_scraped_values applies to the web source, for
        the case the API path has no other answer to: the device did not
        reply at all. mapper._clear_unanswered cannot help there - it needs a
        reply that left a parameter out, which is evidence the reading ended.
        A refusal is no evidence about any reading, only about the request.

        Deliberately NOT tied to the cycle's success: a cycle where another
        device answered is reported as successful, so without this the silent
        device keeps publishing its last values indefinitely, and the only
        symptom is a number that never changes.

        Only `value` goes, as on both other paths - unit, name and icon stay,
        so the entity keeps its identity and Home Assistant is not told a unit
        changed.
        """
        last_read = self._last_device_read.get(device_id)
        if last_read is None:
            # Never read successfully in this session, so there is nothing on
            # display that this could be about.
            return
        stale_for = time.monotonic() - last_read
        if stale_for < self._values_stale_after_seconds():
            return

        # Only the rows this device's API half actually owns.
        #
        # The three a status read owns are excluded because they are the
        # freshest thing here - the status read succeeded in this very cycle,
        # which is the only reason a parameter read was attempted - and they
        # are what the entities promise will stay available to explain the
        # silence. Blanking them answered "no idea" about the one thing that
        # WAS known.
        #
        # The scraped rows are excluded because "this device has not
        # answered" is a statement about the API only. In `both` mode both
        # sources write into one device's dict on purpose - see
        # resolve_scraper_device_id, which files scraped sensors under a real
        # device so they share its history - and the scrape runs on its own
        # schedule, so it can be minutes old while the API side has been
        # quiet for hours. They are not left unwatched: _forget_scraped_values
        # ages them on the scrape's own terms, after three failures in a row.
        status_rows = {f"{device_id}-{row_name}" for row_name in DEVICE_STATUS_ROWS}
        forgotten = []
        for key, row in (self.data.get(device_id) or {}).items():
            if key in status_rows:
                continue
            if self._kept_fresh_by_the_scrape(key):
                continue
            if isinstance(row, Reading) and row.value is not None:
                row.value = None
                forgotten.append(key)
        if not forgotten:
            return
        # Reset, so the next failure is measured from here rather than
        # repeating this warning on every cycle for as long as the device
        # stays away.
        self._last_device_read[device_id] = time.monotonic()
        _LOGGER.warning(
            "Device %s has not answered for %d minutes. Its %d reading(s) are "
            "no longer current and are now shown as unknown rather than as "
            "the values they had then.",
            device_id,
            int(stale_for // 60),
            len(forgotten),
        )

    def _rows_this_module_owns(
        self,
        device_rows: dict[str, Any],
        module_key: ModuleRef,
        schedule_runs: bool = True,
    ) -> Iterator[tuple[str, Reading]]:
        """The rows a silent module may take down with it.

        Four things disqualify a row, and none of them is about the module
        having gone quiet: it is not a reading at all (the raw status gate),
        it belongs to another module, the scrape is still feeding it, or it
        is a weekly programme the schedule fetch is still feeding.

        That last one is a condition, not a category, and it takes two
        answers. The exemption rests on the schedule fetch keeping the row
        current: that fetch drops its own detail the moment a due refresh
        fails, so a programme without a usable week is one nothing refreshes
        any more - and it walks the MODULE LIST, so a module the re-discovery
        has dropped is one it will never visit again whatever is still
        attached to the row (`schedule_runs`). Either way the plan from
        before stood as the current one with no limit at all.
        """
        for row_name, row in device_rows.items():
            if not isinstance(row, Reading):
                continue
            if (row.module_index, row.module_type) != module_key:
                continue
            if self._kept_fresh_by_the_scrape(row_name):
                continue
            is_programme = row.data_type == WemDataType.PROGRAM or looks_like_schedule(
                row.value
            )
            if is_programme and schedule_runs and schedule_fetch_still_feeds(row):
                continue
            yield row_name, row

    def _kept_fresh_by_the_scrape(self, row_name: str) -> bool:
        """Whether the scrape is still delivering this row.

        Both ageing passes ask this - the device-level one and the
        per-module one - because in `both` mode one row can carry an api
        reading AND a scraped one. "The api has not answered" is a statement
        about the api only: the scrape runs on its own schedule and can be
        minutes old while the api side has been quiet for hours. Ageing such
        a row would blank a value that arrived seconds ago.

        The retry count is what makes this honest. `_previous_scraper_keys`
        cannot answer it alone: it is refreshed by a SUCCESSFUL scrape, so
        after a failure it still names every key the last good one wrote.
        And _forget_scraped_values fires on exactly the third failure, not
        from then on - so without the count a shared row would be cleared
        once, refilled by the merge from the api side, and then aged by
        nobody: not by the scrape, which has stopped acting, and not here,
        because the exemption still covered it. An old reading sat there as
        current with both sources dead behind it.
        """
        if self.spider_retry_count >= SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE:
            return False
        return row_name in (self._previous_scraper_keys or ())

    def web_scrape_is_failing(self, enabled_devices: list[str] | None = None) -> bool:
        """Whether the web half has stopped delivering, as a question about
        STATE - the same shape as is_rate_limited, and for the same reason.

        In `both` mode a failing scrape is swallowed so it cannot cost the
        api readings. Nothing therefore propagates, each successful api
        cycle clears the coordinator's counters, and on a fresh setup there
        are no scraped entities whose absence could be noticed.

        The threshold is the one that stops presenting the scraped values as
        current, so the report appears exactly when they cease to be
        trustworthy - and only where a scrape was expected at all.

        "Expected at all" includes the device filter, and that one is not
        symmetrical: the poll skips a disabled scraper device, so nothing is
        attempted - and the count that raised the report can never come down
        either, because only a scrape that WORKS resets it. Asked without the
        filter, the report stood for as long as the user left the device off,
        with no action available that would clear it.
        """
        if self.mode == "api":
            return False
        if not self._scraper_enabled(enabled_devices):
            return False
        return self.spider_retry_count >= SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE

    def _forget_scraped_values(self) -> None:
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
        # `or ()`: the set is None until a scrape has SUCCEEDED once, and this
        # runs on the third failure - so three failures before the first good
        # scrape raised TypeError here and that replaced the actual reason
        # (maintenance, credentials, the network) on its way out. Reachable in
        # `both` mode, where the API has already put the device into self.data
        # so the early return above does not fire.
        for key in self._previous_scraper_keys or ():
            row = device.get(key)
            if isinstance(row, Reading) and row.value is not None:
                row.value = None
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
    def scraper_backoff(self) -> tuple[int, int, datetime | None]:
        """The scrape backoff as the constructor takes it back.

        Exposed as one value so a caller carrying state across an instance
        swap cannot pick up two of the three and silently lose the third.
        """
        return (
            self.spider_wait_interval,
            self.spider_retry_count,
            self.last_scraping_update,
        )

    def resolve_scraper_device_id(self) -> str:
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

    @staticmethod
    def _device_is_enabled(device_id: str, enabled_devices: list[str] | None) -> bool:
        """Whether the caller's filter admits this device.

        The two meanings the coordinator builds, spelled out once: `None` is
        "no filter, poll everything" - a fresh install before any device is
        known - and an explicit empty list is "every known device is
        disabled". Compared as strings because the portal's ids arrive as
        both, and the filter is built from the keys of a different dict than
        the one being walked here.
        """
        if enabled_devices is None:
            return True
        return str(device_id) in {str(enabled) for enabled in enabled_devices}

    def _scraper_enabled(self, enabled_devices: list[str] | None) -> bool:
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

    def _acquire_api_lock(self, what: str) -> None:
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

    def fetch_data(
        self, enabled_devices: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
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

    def remaining_budget(self) -> float | None:
        """Seconds this cycle has left, or None when no poll is running.

        The scrape needs the NUMBER, not just a yes/no: its requests carry
        their own 30s timeout, so a check that only says "still time" lets a
        request started one second before the deadline run 30 seconds past
        it - six times over, for a scrape that reuses a session and then
        logs in fresh. Capping each request at what is left is what keeps
        the worker inside the budget rather than merely aware of it.
        """
        if self._deadline is None:
            return None
        return self._deadline - time.monotonic()

    def check_deadline(self) -> None:
        """Stop the poll cycle if it has used up its time budget.

        Checked at the two points every long cycle passes through - each
        mobile-API request and the entry to the scrape - rather than inside
        the loops that call them. Those loops all funnel through here, so
        guarding them individually would be six places to forget instead of
        two. Inside the scrape the check is per request, on the remaining
        budget: see remaining_budget.

        Does nothing when no poll is running: see the note on `_deadline`.
        """
        remaining = self.remaining_budget()
        if remaining is not None and remaining <= 0:
            raise PollDeadlineExceeded(
                f"This poll cycle passed its {POLL_DEADLINE_SECONDS}s budget "
                f"and stopped. Home Assistant abandons the cycle at "
                f"{DEFAULT_TIMEOUT}s regardless; stopping first releases the "
                f"connection for the next one instead of holding it."
            )

    def _discover_parameters_if_due(
        self, enabled_devices: list[str] | None = None
    ) -> None:
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
        due = [
            module
            for device_id, modules in (self.modules or {}).items()
            if self._device_is_enabled(device_id, enabled_devices)
            for module in modules.values()
        ]
        missing = any("parameters" not in module for module in due)
        stale = any(self._parameters_are_stale(module) for module in due)
        if not (missing or (stale and self._first_cycle_done)):
            return
        _LOGGER.info(
            "Reading parameter definitions from the portal (%s).",
            "some are missing" if missing else "the cached ones are due",
        )
        self.get_parameters(enabled_devices)

    def _ensure_api_session(self, enabled_devices: list[str] | None = None) -> None:
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
        self._discover_parameters_if_due(enabled_devices)

    def _scrape_is_due(self, enabled_devices: list[str] | None) -> bool:
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
        # POSIX timestamps, not a datetime subtraction: two aware stamps with
        # the SAME tzinfo object subtract as naive wall-clock times, so a DST
        # change lands in the difference - spring scrapes an hour early, autumn
        # skips an hour of cycles. Epoch seconds are absolute. The stamp stays
        # local aware for the "no longer current" warning that prints it (see
        # _scrape_and_merge).
        elapsed: float = (
            dt_util.now().timestamp() - self.last_scraping_update.timestamp()
        )
        return elapsed + 10 > self.scan_interval.total_seconds()

    def _api_read_is_due(self) -> bool:
        """Whether `both` mode should read the mobile API this cycle.

        The coordinator ticks at min(web, api) so that whichever interval is
        shorter is served on time. Without this gate the LONGER one was
        served just as often: a web=5min/api=30min installation spent six
        times the API budget it had been configured for, against a portal
        that counts 10,000 requests per 12 hours per IP.

        Monotonic rather than the wall clock the scrape gate uses: nothing
        persists this stamp, so it has no restart to survive, and a clock
        change must not hand out a free read (or withhold one for hours).

        `>=` and no jitter tolerance, unlike the scrape gate: the stamp is
        taken inside the cycle, and Home Assistant plans the next tick from
        when that cycle ENDED - so the grid drifts along with the stamp
        rather than away from it. Measured with `>`, an installation whose
        API interval IS the tick would lose every second reading.
        """
        if self._last_api_read is None:
            return True
        waited: float = time.monotonic() - self._last_api_read
        return waited >= self.scan_interval_api.total_seconds()

    def _count_down_scrape_backoff(self) -> None:
        """One cycle closer to the next scrape attempt."""
        if self.spider_wait_interval > 0:
            self.spider_wait_interval -= 1

    def _scrape_and_merge(self) -> None:
        """Scrape once and merge the result. The timestamp moves only after
        both have worked."""
        webscraping_data = self.fetch_webscraping_data()
        self._merge_webscraping_data(self.resolve_scraper_device_id(), webscraping_data)
        # Local time rather than UTC: this value is also printed to the user in
        # the "no longer current" warning, and dt_util.now() is both aware (see
        # _scrape_is_due) and in the timezone Home Assistant is configured for.
        self.last_scraping_update = dt_util.now()

    def _collect_web(self, enabled_devices: list[str] | None) -> None:
        """`web` mode: the scrape is the only source there is."""
        if not self._scraper_enabled(enabled_devices):
            _LOGGER.debug("Skipping web scrape: its device is disabled.")
            return
        webscraping_data = self.fetch_webscraping_data()
        self._merge_webscraping_data(self.resolve_scraper_device_id(), webscraping_data)

    def _collect_both(self, enabled_devices: list[str] | None) -> None:
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

        if not self._api_read_is_due():
            return
        try:
            self.get_data(enabled_devices)
        finally:
            # Whether it worked or not: what the interval bounds is requests,
            # and a cycle that failed spent them. Skipping the stamp on
            # failure and leaving the pacing to the coordinator's backoff read
            # well and did not hold - that backoff wants three failures in a
            # row and any success in between clears it, so a portal answering
            # every other cycle with an error put the api half back on the WEB
            # interval. Same rule as the schedule fetch and the statistics
            # stamp, both of which book the attempt.
            #
            # After the read rather than before it, which is what the `>=` in
            # _api_read_is_due is measured against: Home Assistant plans the
            # next tick from when the cycle ENDED, so the stamp and the grid
            # drift together instead of apart.
            self._last_api_read = time.monotonic()

    def _fetch_data(
        self, enabled_devices: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        # Fail fast, without any network activity at all, if we're still
        # within a cooldown window from a previous 403 (see
        # _activate_cooldown). This is checked again inside
        # make_api_call() for every individual call too, but checking
        # once up front avoids even starting a cycle (login attempts,
        # etc.) that we already know will be aborted immediately.
        self.check_cooldown()
        try:
            if self.mode != "web":
                self._ensure_api_session(enabled_devices)

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

    def _warn_about_renamed_scraper_keys(self, scraped_keys: list[str]) -> set[str]:
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
        # The merge cache answers "which scraped row shows this api reading",
        # falling back to the reading's own key when there is none. Both
        # answers describe THIS scrape's rows, so a changed inventory - the
        # first successful scrape included - makes them answers from before.
        # Rebuilding is a pass over the rows, not a request.
        if previous != set(scraped_keys):
            self.scraping_mapper.clear()
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

    def _prepare_scraped_row(self, row: Reading, previous: Any) -> None:
        """Translate the row's name and keep a unit this scrape did not bring.

        Mutates `row` in place, which is what the caller stores. Split out of
        the merge loop, where it sat two levels deep and pushed the unit test
        below to three.

        The old VALUE is deliberately NOT carried over when this scrape has
        none. It used to be, to avoid "a gap in the history, even though the
        previous value is still very likely accurate". Measured on a live
        installation, that premise does not hold: the portal renders "--" for
        a value it does not currently have, the scrape maps that to None, and
        the sensor then reported a setpoint of 50.5 degrees for three hours
        while the portal and the heat pump both showed nothing. Only
        reloading the integration cleared it.

        A gap is the truthful record of an hour with no reading. A flat line
        at the last value is not, and it is the shape automations act on.

        We only get here after a scrape that produced rows at all - a page
        with no readings is rejected earlier - so a row that came back
        without a value is the portal saying it has none, not evidence that
        the read went wrong.
        """
        if row.friendly_name is not None:
            row.friendly_name = translate(self.language, row.friendly_name)

        # Preserve the old unit if the current scrape is missing it (e.g. value
        # is "--"). This prevents Home Assistant from complaining about unit
        # changes.
        if row.unit not in (None, ""):
            return
        if isinstance(previous, Reading) and previous.unit not in (None, ""):
            row.unit = previous.unit

    def _merge_webscraping_data(
        self, device_id: str, webscraping_data: dict[str, Any]
    ) -> None:
        if str(device_id) not in self.data:
            self.data[str(device_id)] = {}

        vanished = self._warn_about_renamed_scraper_keys(
            [key for key, row in webscraping_data.items() if isinstance(row, Reading)]
        )

        for key, new_val in webscraping_data.items():
            if isinstance(new_val, Reading):
                self._prepare_scraped_row(new_val, self.data[str(device_id)].get(key))
            self.data[str(device_id)][key] = new_val

        # Same reasoning for a row that stopped coming back entirely: it is
        # not being scraped any more, so whatever it still shows is old. The
        # key itself stays, because the entity does too and Home Assistant
        # would otherwise report it as merely missing from the data.
        for key in vanished:
            entry = self.data[str(device_id)].get(key)
            if isinstance(entry, Reading) and entry.value is not None:
                _LOGGER.debug(
                    "Scraped row %s is no longer on the page; its last value "
                    "is not current any more.",
                    key,
                )
                entry.value = None

    def _reset_scraper(self) -> None:
        """Discard the persistent scraper instance (closing its HTTP
        session) so the next scraping cycle starts with a completely
        fresh connection - used after auth/session errors where reusing
        the old connection state could keep failing."""
        if self._scraper is not None:
            self._scraper.close()
            self._scraper = None

    def fetch_webscraping_data(self) -> dict[str, Any]:
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
                self.username,
                self.password,
                self.webscraping_cookie,
                budget=self.remaining_budget,
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
            #
            # Counted like every other failed exit, and this one was missed
            # the longest. The count does two jobs: it ages the scraped
            # readings after three failures, and it is what says the scrape
            # is still keeping a shared row fresh (see
            # _kept_fresh_by_the_scrape). Left at zero, a rate-limited
            # scrape delivered nothing while its last values were exempt
            # from every ageing pass in the integration.
            self._register_scrape_failure()
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

    def get_devices(self) -> None:
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
        previously_known_readings = self.data or {}
        # Build the fresh device/module view in LOCAL dicts first and only
        # assign to self.modules/self.data once everything succeeded.
        # Previously both were wiped BEFORE the API call: a single failing
        # call (e.g. one 403) then left them empty, and the next successful
        # run saw no previously-known modules - silently discarding all
        # cached parameter definitions and forcing the slow, rate-limited
        # full discovery in get_parameters() that the cache exists to avoid.
        payload = as_answer_dict(
            self.make_api_call(API_DEVICE_READ_URL, do_retry=True).json()
        )
        device_rows = payload.get("Devices") if payload is not None else None
        if not isinstance(device_rows, list):
            # Valid JSON outside the contract. Said as the portal-side error
            # it is, so the coordinator classifies it like any other server
            # fault - a raw KeyError here arrived as "unexpected error" with
            # no hint that the portal answered at all.
            raise ServerError(
                "The WEM Portal answered the device list without a Devices "
                "list - nothing to set up from."
            )

        new_modules: dict[str, Any] = {}
        new_data: dict[str, dict[str, Any]] = {}
        for device in device_rows:
            try:
                self._register_device(
                    device,
                    previously_known_modules,
                    previously_known_readings,
                    new_modules,
                    new_data,
                )
            except (KeyError, TypeError) as exc:
                # One malformed device row must not cost the whole account.
                _LOGGER.warning(
                    "Skipping one device row the portal answered outside "
                    "its contract: %s",
                    exc,
                )

        if not new_data:
            # Skipping ONE unusable row costs that device; skipping every row
            # and adopting the result costs the account. Committing here
            # replaced the readings with nothing and reported a successful
            # cycle - on a one-device installation, everything gone with no
            # error, and get_devices only runs once per session, so nothing
            # brought it back before a reload.
            raise ServerError(
                f"The WEM Portal answered with {len(device_rows)} device "
                "row(s) and none of them was readable - keeping what was "
                "there rather than reporting an empty account."
            )

        self.modules = new_modules
        self.data = new_data

    def _register_device(
        self,
        device: dict[str, Any],
        previously_known_modules: dict[str, Any],
        previously_known_readings: dict[str, dict[str, Any]],
        new_modules: dict[str, Any],
        new_data: dict[str, dict[str, Any]],
    ) -> None:
        """Adopt one device row of the device-list answer.

        Raises KeyError/TypeError on a row outside the contract; the caller
        skips that row. Split out so the skip does not wrap thirty lines in
        a try block. Everything is read into locals FIRST and committed only
        at the end: a row that dies halfway must leave no half-adopted
        device behind.
        """
        device_id_str = str(device["ID"])
        previously_known_device_modules = previously_known_modules.get(
            device_id_str, {}
        )
        device_modules = {}
        for module in device["Modules"]:
            module_key = ModuleRef(
                module_index=module["Index"], module_type=module["Type"]
            )
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
            device_modules[module_key] = module_entry
        connection_status = device["ConnectionStatus"]

        new_modules[device_id_str] = device_modules
        # The readings come across with the device. This call refreshes the
        # device and module LIST; it runs once per session, and a transport
        # recovery starts a new one. The api half is rewritten in the same
        # cycle either way - but a web-only row has no api half, so wiping
        # here took those values away for as long as the scrape was not due
        # or was in its backoff.
        # Rows of a module that has since disappeared come across with the
        # rest, and the per-module ageing walks the NEW list, so nothing
        # owns them. Left that way on purpose: reconciling here would drop
        # good values whenever one answer omits a module, and it would miss
        # the case that actually turns up - a RENAMED module keeps its index
        # and type, so only its row keys change. They do age out with the
        # device the next time it stops answering.
        new_data[device_id_str] = {
            **previously_known_readings.get(device_id_str, {}),
            "ConnectionStatus": connection_status,
        }
        # Kept out of new_data: the entity platforms iterate that dict
        # and would try to build an entity from it.
        if device.get("DeviceType") is not None:
            self.device_types[device_id_str] = device["DeviceType"]

    def _note_undescribed_module(
        self, device_id: str, values: dict[str, Any], why: str, unsupported: bool
    ) -> None:
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

        # Both are recorded with an empty list and a timestamp rather than
        # deleted, so the module survives and is asked again. What differs is
        # how soon, and that difference is the point.
        #
        # An empty description is a real answer: the module says it has
        # nothing to poll. Believe it and ask again on the normal daily
        # round.
        #
        # A REJECTED description is not an answer at all. The module may well
        # have parameters; the portal just would not say. Treated like the
        # empty case it waited a full day - and a device whose every module
        # was rejected then showed nothing at all for that day, with only a
        # debug line to explain it. That is the wrong way round: a module
        # that already has parameters keeps showing them while it retries in
        # an hour, and one that has none yet has nothing to show at all, so
        # it is the more urgent of the two, not the less.
        #
        # So a rejection gets the same short retry a failed RE-read gets, and
        # `description_refused` records which of the two happened - the value
        # read needs it to tell "this device has nothing" from "this device
        # was not told anything".
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
                PARAMETER_REDISCOVERY_RETRY_SECONDS // 3600,
            )
            values["description_refused"] = True
            values["parameters_fetched_at"] = time.time() - max(
                0,
                PARAMETER_REDISCOVERY_INTERVAL_SECONDS
                - PARAMETER_REDISCOVERY_RETRY_SECONDS,
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
            values.pop("description_refused", None)
            values["parameters_fetched_at"] = time.time()
        values["parameters"] = {}

    def _parameters_are_stale(self, module: dict[str, Any]) -> bool:
        """Whether this module's parameter list is due for a re-read.

        A module with no list at all is NOT stale - it is missing, which is a
        different urgency and a different caller decision.
        """
        if "parameters" not in module:
            return False
        age: float = time.time() - module.get("parameters_fetched_at", 0)
        return age >= PARAMETER_REDISCOVERY_INTERVAL_SECONDS

    @staticmethod
    def _http_status(exc: Exception) -> int | None:
        """The HTTP status behind a WemPortalError, or None if it had none.

        Read explicitly rather than through getattr with a default: a typo in
        an attribute name would then silently mean "no status", and this is
        what decides between backing off, dropping a module and re-raising.
        """
        cause = exc.__cause__
        if not isinstance(cause, requests.exceptions.HTTPError):
            return None
        # HTTPError.response is Optional in the type stubs and genuinely
        # unset for a locally-raised error; only a real HTTP answer has a
        # status. Read explicitly for the same reason as the isinstance above.
        if cause.response is None:
            return None
        return cause.response.status_code

    def _module_description_is_due(
        self, device_id: str, values: dict[str, Any]
    ) -> bool:
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

    def _store_module_description(
        self,
        device_id: str,
        key: ModuleRef,
        values: dict[str, Any],
        response: requests.Response,
    ) -> None:
        """Keep what the portal said this module has, or book why it did not.

        Every unusable answer is BOOKED, never merely logged: skipping with a
        log line left the module with no timestamp at all, so the age check
        never held it back and a portal answering nonsense was asked again
        every single cycle, without limit - the one failure mode the whole
        retry budget exists to bound.
        """
        try:
            payload = response.json()
        except ValueError:
            # Not JSON at all - an HTML error page, typically.
            self._note_undescribed_module(
                device_id,
                values,
                "its description could not be read",
                unsupported=True,
            )
            return

        described = described_parameters(payload)
        if described is None:
            # Valid JSON outside the contract: {"Parameters": null}, a bare
            # list, a string. This used to travel into the loop below and die
            # as a TypeError past the KeyError handler, aborting discovery
            # for every remaining module of the device.
            self._note_undescribed_module(
                device_id,
                values,
                "its parameter list was not readable",
                unsupported=True,
            )
            return

        parameters = {parameter["ParameterID"]: parameter for parameter in described}
        if not parameters:
            self._note_undescribed_module(
                device_id,
                values,
                "it described no parameters",
                unsupported=False,
            )
            return

        # Before the replacement: it needs the list as it stands today.
        forget_dropped_parameters(self.data.get(device_id), values, parameters)
        # Non-None here: this path runs only under get_parameters, which
        # returns early while the module list is still None.
        modules = self.modules or {}
        modules[device_id][key]["parameters"] = parameters
        modules[device_id][key]["parameters_fetched_at"] = time.time()
        # The portal answered this time. Clearing it here rather than only on
        # the empty branch matters: the flag is persisted with the module
        # cache, so a refusal that was never cleared would outlive the
        # restart that fixed it.
        modules[device_id][key].pop("description_refused", None)

    def _discover_device_parameters(self, device_id: str) -> None:
        """Read every module description of one device that is due."""
        for key, values in (self.modules or {})[device_id].items():
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
                    # One refusal is the whole budget, and the code used to
                    # promise three: make_api_call activates the shared
                    # cooldown as soon as the portal answers 403, so the next
                    # module's request is refused before it is sent - by a
                    # ForbiddenError carrying no HTTP status, which misses
                    # this branch and re-raises below. The counter could
                    # never reach two while the log said "strike 1 of 3".
                    _LOGGER.error(
                        "Rate limited (403) while reading parameters for "
                        "device %s. Discovery stops here: the portal is "
                        "refusing this network, not this request.",
                        device_id,
                    )
                    raise
                if status_code == 400:
                    self._note_undescribed_module(
                        device_id,
                        values,
                        "the portal rejected the request",
                        unsupported=True,
                    )
                    continue
                raise
            self._store_module_description(device_id, key, values, response)

    def get_parameters(self, enabled_devices: list[str] | None = None) -> None:
        """Read the per-module parameter definitions of every enabled device.

        The filter matters more here than anywhere else this integration
        honours it: discovery sleeps five seconds and spends at least one
        request PER MODULE, and the portal counts requests per IP. A device
        the user switched off used to pay all of that, daily.
        """
        if self.modules is None:
            _LOGGER.debug(
                "get_parameters() called with no module data available yet; skipping."
            )
            return
        for device_id, device_data in self.data.items():
            if not self._device_is_enabled(device_id, enabled_devices):
                continue
            if device_data.get("ConnectionStatus") != 0:
                continue
            _LOGGER.debug("Fetching api parameters data for device %s", device_id)
            self._discover_device_parameters(device_id)

    def _publish_accepted_values(
        self,
        device_id: str,
        module_index: int,
        module_type: int,
        written: dict[str, Any],
    ) -> None:
        """Bring the stored readings in line with what the portal just took.

        Matched on the module ADDRESS plus the parameter id rather than on a
        row key: which key a reading lives under depends on whether it was
        merged into a scraped row, while the address is what the write itself
        was addressed with. The module is part of that because two heating
        circuits share one parameter catalogue - the same ParameterID in
        another module is another reading, and writing one must not touch it.

        Only what the portal accepted: a refused write raises before this,
        which is what keeps the integration from being certain of a value the
        heating system never took.
        """
        for row in (self.data.get(str(device_id)) or {}).values():
            if not isinstance(row, Reading):
                continue
            if (row.module_index, row.module_type) != (module_index, module_type):
                continue
            if row.parameter_id in written:
                row.value = written[row.parameter_id]

    def change_value(
        self,
        device_id: str,
        parameter_id: str,
        module_index: int | None,
        module_type: int | None,
        numeric_value: float | str,
        together_with: dict[str, float] | Callable[[], dict[str, float]] | None = None,
    ) -> None:
        """Change a value under the shared API lock, so a write can't
        interleave with a poll cycle on the same session/state.

        Reading the companions and publishing the result both belong here,
        and they are two halves of one thing: a write queued behind another
        has to READ the module's other rows after the wait, and the write in
        front of it has to have WRITTEN what the portal accepted before it
        lets go of the lock.

        Publishing used to be an optional callback the caller passed in,
        which failed twice over: the holiday service passed none at all, so
        a two-date write published nothing, and the entity path passed one
        for its main value only, leaving the companions it had just sent
        showing their old readings. Whoever was next in line then read
        exactly those and sent them back, undoing part of a write that had
        just succeeded. This is the only place that knows the whole request.
        """
        # A writable entity always carries a real module address; the write
        # payload cannot express a missing one, and the int() coercion in
        # _change_value has nothing to work on without it. Narrowed here, the
        # same shape as the expert path's asserts.
        assert module_index is not None and module_type is not None
        self._acquire_api_lock("parameter write")
        try:
            # A write does not go through _ensure_api_session, and after two
            # failing cycles the transport has dropped its session and given
            # up `valid_login`. A poll puts both back; a service call or an
            # automation landing in that window went straight to the wire
            # with nothing to send on. Under the lock, so it cannot race a
            # poll doing the same thing.
            if not self.valid_login:
                self.api_login()
            if callable(together_with):
                together_with = together_with()
            self._change_value(
                device_id,
                parameter_id,
                module_index,
                module_type,
                numeric_value,
                together_with=together_with,
            )
            # Still under the lock, and only after the portal accepted: the
            # next writer reads these rows the moment it gets in.
            self._publish_accepted_values(
                device_id,
                module_index,
                module_type,
                {parameter_id: numeric_value, **(together_with or {})},
            )
        finally:
            self._api_lock.release()

    def reread_device_values(self, device_id: str) -> str | None:
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

        Including the refused login, which that method raises for the poll's
        benefit. This is the one caller that is NOT a poll: it runs after a
        write the portal has already accepted, and both callers decide what
        to publish from the return value. Raised past them, the service call
        reports a failure for a value that reached the heating system - and
        their `_forget_written_value()` never runs, so the value recorded
        before this read stands as verified, which is the single claim the
        read-back exists to prevent. The failure is not lost: api_login gives
        up `valid_login` before raising, so the next cycle logs in and the
        coordinator counts it there.
        """
        self._acquire_api_lock("value re-read")
        try:
            return self._fetch_parameter_values(str(device_id))
        except AuthError as exc:
            return str(exc)
        finally:
            self._api_lock.release()

    def _change_value(
        self,
        device_id: str,
        parameter_id: str,
        module_index: int,
        module_type: int,
        numeric_value: float | str,
        together_with: dict[str, float] | None = None,
    ) -> None:
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
        except Exception as exc:
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
    def get_data(self, enabled_devices: list[str] | None = None) -> None:
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
            if device_id not in (self.modules or {}):
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
                #
                # Its readings still have to age, though. Skipping the poll
                # used to skip this too, so a device reporting `busy` cycle
                # after cycle kept publishing the same numbers as current -
                # and `busy` is not one of the states that make a device
                # unreachable, so its entities stayed available saying them.
                self._forget_stale_device_values(device_id)
                continue
            # `is None`, never a truth test: the reason for a FAILURE is what
            # comes back, so a truthy answer is the bad one.
            failure = self._fetch_parameter_values(device_id)
            if failure is None:
                successes += 1
                self._last_device_read[device_id] = time.monotonic()
            else:
                failures.append(f"device {short_device_id(device_id)}: {failure}")
                self._forget_stale_device_values(device_id)
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

            # Indexed, not `.get(..., -1)`: an answer that does not say is not
            # an answer that says "unknown". Defaulted, a payload with the
            # field missing became the unknown STATE - which this method
            # reports as a successful read of a device that is not online, so
            # it returned False and the parameter read never ran, while the
            # error sensors went out saying nothing is wrong on evidence
            # nobody had. The KeyError lands in the handler below, which is
            # the one that clears what it cannot vouch for and still lets the
            # parameters have their chance.
            raw_status = status_response["ConnectionStatus"]
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

            self.data[device_id][f"{device_id}-{DEVICE_STATUS_CONNECTION}"] = Reading(
                friendly_name="Connection Status",
                parameter_id=DEVICE_STATUS_CONNECTION,
                value=conn_status,
                platform="sensor",
                icon="mdi:network",
            )

            errors = portal_list(status_response, "Errors")
            has_errors = "Yes" if errors else "No"
            error_message, error_detail = error_state_and_detail(errors)

            self.data[device_id][f"{device_id}-{DEVICE_STATUS_HAS_ERRORS}"] = Reading(
                friendly_name="Has Errors",
                parameter_id=DEVICE_STATUS_HAS_ERRORS,
                value=has_errors,
                platform="sensor",
                icon="mdi:alert",
            )

            self.data[device_id][f"{device_id}-{DEVICE_STATUS_ERROR_MESSAGES}"] = (
                Reading(
                    friendly_name="Error Messages",
                    parameter_id=DEVICE_STATUS_ERROR_MESSAGES,
                    value=error_message,
                    # Every fault, whatever the state could hold. The state
                    # is capped by Home Assistant; this attribute is not.
                    errors=error_detail,
                    platform="sensor",
                    icon="mdi:message-alert",
                )
            )

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

        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except AuthError:
            # A rejected login is not an unreadable status; see AuthError.
            raise
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
            if isinstance(row, Reading):
                row.value = None

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
        # Reached only from get_data, which skips any device_id that is not in
        # self.modules - so the module list is a real dict at this point.
        assert self.modules is not None
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
                refused = [
                    module
                    for module in device_modules.values()
                    if module.get("description_refused")
                ]
                if not device_modules:
                    _LOGGER.debug(
                        "Device %s has nothing to read: it has no modules.", device_id
                    )
                elif refused:
                    # Still not a failed cycle - the device may genuinely have
                    # nothing, and failing here would drag every other device
                    # into a backoff. But it is not the silent nothing the
                    # debug line below describes either: the portal refused to
                    # say what these modules hold, so "no entities appeared"
                    # has a cause, and the user gets to see it rather than
                    # guess. Said once per cycle at most, and it stops as soon
                    # as one description comes back.
                    _LOGGER.warning(
                        "Device %s produced no readable parameters: the portal "
                        "refused to describe %d of its %d modules. Home "
                        "Assistant therefore shows no entities for it. Retrying "
                        "those descriptions in about %d h; if this persists, the "
                        "modules may not be supported by your installation.",
                        device_id,
                        len(refused),
                        len(device_modules),
                        PARAMETER_REDISCOVERY_RETRY_SECONDS // 3600,
                    )
                else:
                    _LOGGER.debug(
                        "Device %s has nothing to read: every module describes "
                        "no parameters.",
                        device_id,
                    )
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
                _report_missing_job_id(
                    device_id, self._account_state.missing_job_ids_reported
                )
            else:
                read_data = {**data, "JobID": ticket.job_id}
            time.sleep(5)
            values = as_answer_dict(
                self.make_api_call(
                    API_DATA_ACCESS_READ_URL,
                    data=read_data,
                    do_retry=True,
                    retry_transport=True,
                ).json()
            )
            if values is None:
                # `null` is valid JSON and used to die on the .get below
                # instead of taking the treat-as-failed path.
                _LOGGER.warning(
                    "Device %s answered the value read with something that "
                    "is not an answer object; treating the cycle as failed "
                    "rather than keeping stale readings.",
                    short_device_id(device_id),
                )
                return "the value read did not come back as an answer object"
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
                # So the ageing pass keeps a merged row the scrape delivered
                # this cycle even when the API omits its counterpart - only the
                # api instance knows which rows the scrape still feeds.
                scrape_still_feeds=self._kept_fresh_by_the_scrape,
            )
            # Freshness lives on the MODULE, not only on the device: a
            # successful answer naming module A refreshes the device-level
            # stamp, and module B - missing from the very same answer - kept
            # presenting its last readings indefinitely. _clear_unanswered
            # cannot see B (it walks the answer), _forget_stale_device_values
            # cannot either (the device did answer).
            self._stamp_answered_modules(device_id, values)
            self._forget_unanswered_module_values(device_id)
            return None
        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except AuthError:
            # Not this device failing: an account is one login. Returned as
            # a reason string here, it stops being an error anyone can
            # count; see AuthError.
            raise
        except Exception as exc:  # noqa: BLE001
            # Broad: one device's parameter read failing must not take
            # the other devices' readings with it. The reason goes to the
            # caller as well as into this warning - it is what Home Assistant
            # ends up showing the user when the whole cycle fails.
            _LOGGER.warning("Failed to fetch parameter data... %s", exc)
            return str(exc)

    def _values_stale_after_seconds(self) -> float:
        """How long a device or module may stay silent before what it last
        said stops being shown as current."""
        return max(
            DEVICE_VALUES_STALE_AFTER_SECONDS,
            DEVICE_VALUES_STALE_AFTER_POLLS * self.scan_interval_api.total_seconds(),
        )

    def _stamp_answered_modules(self, device_id: str, values: dict[str, Any]) -> None:
        """Note WHEN each module last appeared in a values answer.

        Kept BESIDE the module list rather than inside it, and that is the
        whole point of the separate dict: a module that drops out of the
        device list used to take its own stamp with it, so the ageing pass -
        which reads those stamps - never visited its readings again. They
        were then neither refreshed (the mapper skips a module it has no
        description for) nor aged, and sat on the dashboard as current for
        good. Outliving the list is exactly what makes them reachable.

        Monotonic, and never persisted: it is meaningless across restarts.
        """
        answered = self._module_answered_at.setdefault(device_id, {})
        for module in values.get("Modules") or []:
            if not isinstance(module, dict):
                continue
            # Read as Any on purpose: these are raw portal fields, and a
            # ModuleRef is built from whatever they hold - the guard is the
            # try below, not the type.
            module_index: Any = module.get("ModuleIndex")
            module_type: Any = module.get("ModuleType")
            key = ModuleRef(module_index, module_type)
            # Around the ASSIGNMENT, like mapper._described_module: an id the
            # portal sent as a list builds a ModuleRef without complaint and
            # only raises where something hashes it.
            try:
                answered[key] = time.monotonic()
            except TypeError:
                continue

    def _forget_unanswered_module_values(self, device_id: str) -> None:
        """Stop presenting a module's readings once IT has stopped answering.

        The per-module half of _forget_stale_device_values, for the case
        that one cannot see: the device answers - with module A - and module
        B is simply absent from every answer. Same TTL, same rule (only the
        value goes, identity stays), same no-evidence exemption: a module
        never stamped this session has nothing on display this could be
        about.

        Weekly programmes are exempt like in _clear_unanswered - the
        schedule fetch owns their staleness and drops its own detail when a
        due refresh fails.
        """
        now = time.monotonic()
        device_rows = self.data.get(device_id) or {}
        answered = self._module_answered_at.get(device_id, {})
        # Over the STAMPS, not over the module list: a module the portal has
        # stopped listing is exactly the one whose readings nothing else can
        # reach, and walking the list skipped it.
        for module_key, answered_at in list(answered.items()):
            stale_for = now - answered_at
            if stale_for < self._values_stale_after_seconds():
                continue

            forgotten = []
            for row_name, row in self._rows_this_module_owns(
                device_rows,
                module_key,
                # Whether the schedule fetch can still reach this module at
                # all: it walks the module list, and the stamps this loop
                # runs over deliberately outlive it.
                schedule_runs=module_key in (self.modules or {}).get(device_id, {}),
            ):
                if row.value is not None:
                    row.value = None
                    forgotten.append(row_name)
            if not forgotten:
                continue
            # Reset, so the next silence is measured from here rather than
            # repeating this warning every cycle - same as the device level.
            answered[module_key] = now
            _LOGGER.warning(
                "Device %s module %d/%d has not been in an answer for %d "
                "minutes. Its %d reading(s) are no longer current and are "
                "now shown as unknown rather than as the values they had "
                "then.",
                short_device_id(device_id),
                module_key.module_index,
                module_key.module_type,
                int(stale_for // 60),
                len(forgotten),
            )
