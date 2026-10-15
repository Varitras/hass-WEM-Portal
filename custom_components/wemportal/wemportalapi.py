"""
Weishaupt webscraping and API library
"""

from typing import TYPE_CHECKING, Any, Final
from collections.abc import Callable, Iterable, Iterator, Mapping
import logging

import copy
import threading
import time
from datetime import datetime, timedelta
from enum import Enum

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
from .models import HeavyFetchTurns, ModuleRef, Reading, account_state
from .schedule import WemPortalSchedule
from .discovery import PARAMETER_REDISCOVERY_RETRY_SECONDS, WemPortalDiscovery
from .statistics import WemPortalStatistics, is_statistics_row
from .transport import WemPortalTransport
from .exceptions import (
    ApiBusyError,
    AuthError,
    ForbiddenError,
    ParameterChangeError,
    PollDeadlineExceeded,
    PortalMaintenanceError,
    ScrapeHeldBack,
    WebLoginRefused,
    WemPortalError,
)
from .mapper import WemPortalDataMapper
from .mobile_protocol import (
    as_answer_dict,
    read_refresh_ticket,
    read_write_ack,
    unanswered_parameters,
)
from .translations import translate
from .utils import (
    clamped_scan_interval,
    error_state_and_detail,
    failure_is_new,
    failure_is_over,
    looks_like_schedule,
    portal_list,
    schedule_fetch_still_feeds,
    short_device_id,
    short_device_ids,
)

if TYPE_CHECKING:
    # Imported lazily at runtime (see fetch_webscraping_data); named here only
    # to type the persistent scraper handle.
    from .scraper import WemPortalScraper

_LOGGER = logging.getLogger(__name__)

API_DATA_ACCESS_READ_URL: Final = "https://www.wemportal.com/app/DataAccess/Read"

API_DATA_ACCESS_WRITE_URL: Final = "https://www.wemportal.com/app/DataAccess/Write"


API_DEVICE_STATUS_READ_URL: Final = "https://www.wemportal.com/app/DeviceStatus/Read"


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


# A coordinator tick lands up to a second early (see _api_read_is_due), and
# the API gate allows for that and no more: wider, an interval just past a
# multiple of the tick was read a whole tick early.
TICK_TOLERANCE_SECONDS: Final = 2
# The scrape gate's own margin, older than the API gate and left as it was.
SCRAPE_TICK_TOLERANCE_SECONDS: Final = 10

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
# After the first web login turned away in a row: the scrape alone waits this
# long instead of every request waiting fifteen minutes - see WebLoginRefused.
WEB_LOGIN_RETRY_SECONDS: Final = 300

_SCRAPE_FAILURE_KEY: Final = "web-scrape"


class NoRead(Enum):
    """A device with nothing to read: not a failed refresh, and not a
    refreshed one either. Counted as refreshed, one such device made a cycle
    in which every real read failed look successful."""

    NOTHING_TO_READ = "nothing to read"


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
# The words two of those rows state. Fixed, because automations compare
# against them; what a user reads is their translation (translations/*.json,
# entity.sensor), in Home Assistant's language.
CONNECTION_STATES: Final = ("online", "wrong_secret", "busy", "offline", "unknown")
HAS_ERRORS_STATES: Final = ("yes", "no")


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
        short_device_id(device_id),
        GITHUB_PROJECT_URL,
    )


class WemPortalApi(
    WemPortalTransport, WemPortalStatistics, WemPortalSchedule, WemPortalDiscovery
):
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
        # The last values answer per device, for the read-back after a write:
        # a read that worked says nothing about a parameter it did not carry.
        self._last_values_answer: dict[str, Mapping[str, Any]] = {}
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
        # When the backoff last moved a step; see _count_down_scrape_backoff.
        self._backoff_stepped_at: float | None = None

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
        # A `both`-mode tick can end with neither source read, which is no
        # sign of recovery.
        self.last_cycle_read_nothing = False
        self.heavy_fetch_turns = HeavyFetchTurns()
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
        self.login_went_through = False
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
        # A re-login that failed during this poll; see _recover_or_raise.
        self._relogin_failure: Exception | None = None
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
        # The same idea for failures that carry a reason rather than a state:
        # what has already been announced, so a portal that keeps timing out
        # is reported once and once when it answers again (see utils).
        self._reported_failures: dict[str, str] = {}
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
        self._backoff_stepped_at = time.monotonic()
        if self.spider_retry_count == SCRAPE_FAILURES_BEFORE_VALUES_ARE_STALE:
            self._forget_scraped_values()

    def _forget_stale_device_values(
        self, device_id: str, schedule_runs: bool = True
    ) -> None:
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
            # Left to the statistics fetch only while it still asks this
            # device: one that is busy or offline it skips.
            if is_statistics_row(device_id, key) and self._statistics_reads(device_id):
                continue
            if not isinstance(row, Reading) or row.value is None:
                continue
            if self._owned_by_another_fetch(key, row, schedule_runs):
                continue
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
            short_device_id(device_id),
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
            if self._owned_by_another_fetch(row_name, row, schedule_runs):
                continue
            yield row_name, row

    def _owned_by_another_fetch(
        self, row_name: str, row: Reading, schedule_runs: bool
    ) -> bool:
        """Whether another fetch still keeps this row current, so a silent
        value read must not age it: the scrape, or the schedule fetch for a
        programme it still feeds - where that fetch runs at all.

        One answer for both API ageing passes. The device pass had its own
        list and left the fed programme out, so a current week went blank.
        """
        if self._kept_fresh_by_the_scrape(row_name):
            return True
        is_programme = row.data_type == WemDataType.PROGRAM or looks_like_schedule(
            row.value
        )
        return is_programme and schedule_runs and schedule_fetch_still_feeds(row)

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
        # The repair speaks of an API half, which `web` mode does not have.
        if self.mode != "both":
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
                    short_device_id(self.scraper_device_id),
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
            self._relogin_failure = None
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

    def _ensure_api_session(self) -> None:
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
        if self._web_login_retry_pending():
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
        return (
            elapsed + SCRAPE_TICK_TOLERANCE_SECONDS > self.scan_interval.total_seconds()
        )

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

        With a tolerance narrower than the scrape gate's. HA plans the
        next tick from when the cycle ended, but rounds the loop time down
        first, so a tick lands up to a second early; measured without the
        tolerance, a five-minute API read came every 7.5 to 9.5 minutes.
        """
        if self._last_api_read is None:
            return True
        waited: float = time.monotonic() - self._last_api_read
        return waited + TICK_TOLERANCE_SECONDS >= self.scan_interval_api.total_seconds()

    def _count_down_scrape_backoff(self) -> None:
        """One web interval closer to the next scrape attempt.

        In web intervals, not ticks: `both` mode ticks at the shorter
        interval, and counting those retried a failing scrape more often
        than a working one runs.
        """
        if self.spider_wait_interval <= 0:
            return
        now = time.monotonic()
        stepped_at = self._backoff_stepped_at
        interval = self.scan_interval.total_seconds()
        too_soon = stepped_at is not None and (
            now - stepped_at + SCRAPE_TICK_TOLERANCE_SECONDS < interval
        )
        if too_soon:
            return
        self._backoff_stepped_at = now
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

    def _web_login_retry_pending(self) -> bool:
        """Whether a turned-away web login's five minutes are still running."""
        retry_at = self._account_state.web_login_retry_at
        return retry_at is not None and time.monotonic() < retry_at

    def _collect_web(self, enabled_devices: list[str] | None) -> None:
        """`web` mode: the scrape is the only source there is."""
        if not self._scraper_enabled(enabled_devices):
            _LOGGER.debug("Skipping web scrape: its device is disabled.")
            return
        if self._web_login_retry_pending():
            raise ScrapeHeldBack(
                "Waiting before the next web login after the last was turned away."
            )
        webscraping_data = self.fetch_webscraping_data()
        self._merge_webscraping_data(self.resolve_scraper_device_id(), webscraping_data)

    def _note_scrape_failure(self, exc: Exception) -> None:
        """Say a failing scrape once, not on every retry of its backoff.

        Without the wrapper's sentence, written for the entry's error display,
        but not by the chained cause either: for an empty scrape that is an
        IndexError, an internal detail in place of the reason.
        """
        reason = str(exc).removeprefix(DATA_GATHERING_ERROR).strip(" ()") or str(exc)
        if failure_is_new(self._reported_failures, _SCRAPE_FAILURE_KEY, reason):
            _LOGGER.info(
                "Web scraper failed, using the API only until it works: %s", reason
            )
        else:
            _LOGGER.debug("Web scraper still failing: %s", reason)

    def _collect_both(self, enabled_devices: list[str] | None) -> bool:
        """`both` mode: scrape, then read the API, each when due; True if
        either delivered."""
        scraped = False
        if self._scrape_is_due(enabled_devices):
            try:
                self._scrape_and_merge()
            except Exception as exc:  # noqa: BLE001
                # Broad: the scrape is the optional half of `both` mode. No
                # scraper failure may cost the API readings that follow, so
                # this deliberately does not re-raise.
                self._note_scrape_failure(exc)
            else:
                scraped = True
                if failure_is_over(self._reported_failures, _SCRAPE_FAILURE_KEY):
                    _LOGGER.info("The web scraper works again.")
        else:
            self._count_down_scrape_backoff()

        if not self._api_read_is_due():
            return scraped
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
        return True

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
        self.heavy_fetch_turns.new_cycle()
        self.last_cycle_read_nothing = False
        try:
            if self.mode != "web":
                self._ensure_api_session()

            if self.mode == "web":
                self._collect_web(enabled_devices)
            elif self.mode == "api":
                self.get_data(enabled_devices)
            elif not self._collect_both(enabled_devices):
                self.last_cycle_read_nothing = True
                return self.data

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
        not the same and the message used to claim only the second. The
        existing sensors lose their row and go unknown; the builder makes
        entities from the new keys on this same update. It said "the next
        restart", then "a later cycle" - both wrong, and the second hid where
        the dead sensors of a one-cycle portal fault came from.

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
                "unknown; they come back if the labels do. NEW entities are "
                "made from the new labels right away and keep the history from "
                "then on; if the old labels return, remove the new ones.",
                ", ".join(sorted(gone)),
                ", ".join(sorted(added)),
            )
        return gone

    def _prepare_scraped_row(self, row: Reading, previous: Any, merged: bool) -> None:
        """Translate the row's name; keep a unit or programme it did not bring.

        Mutates `row` in place, which is what the caller stores. Split out of
        the merge loop, where it sat two levels deep and pushed the unit test
        below to three.

        The old VALUE is deliberately NOT carried over when this scrape has
        none. It used to be, to avoid "a gap in the history, even though the
        previous value is still very likely accurate". Measured on a live
        installation, that premise does not hold: the portal rendered "--"
        for a setpoint it no longer had, the scrape read that as no value, and
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
        # The page never has a programme's week; the schedule fetch owns it,
        # and only a row the merge maps is one that fetch and the forgetting
        # of a dropped programme can still find.
        if isinstance(previous, Reading) and merged:
            row.circuit_times_day = previous.circuit_times_day
            row.possible_values = previous.possible_values

        # Preserve the old unit if the current scrape is missing it (e.g. value
        # is "--"). This prevents Home Assistant from complaining about unit
        # changes.
        if row.unit not in (None, ""):
            return
        if isinstance(previous, Reading) and previous.unit not in (None, ""):
            row.unit = previous.unit

    def _scrape_with(self, scraper: WemPortalScraper) -> dict[str, Any]:
        """One scrape, and what it learnt about the session, however it ends:
        this api hands its cookie back to the scraper every cycle."""
        try:
            return scraper.scrape()[0]
        finally:
            if not scraper.cookie:
                self.webscraping_cookie = None

    def _rows_owning_a_described_week(self) -> set[str]:
        """The rows whose programme week the schedule fetch still owns.

        Through a parameter some module still describes: the merge map
        outlives a module gone from the device list, and its week, read by
        no fetch and forgotten by no re-read, was carried for the session.
        And only a parameter's first row, the one the fetch refreshes and
        drops the week on (see _schedule_row_key).
        """
        modules = self.modules or {}

        def still_described(device_id: str, module_key: Any, parameter_id: str) -> bool:
            module = modules.get(device_id, {}).get(module_key, {})
            return parameter_id in (module.get("parameters") or {})

        return {
            rows[0]
            for key, rows in self.scraping_mapper.items()
            if still_described(*key)
        }

    def _merge_webscraping_data(
        self, device_id: str, webscraping_data: dict[str, Any]
    ) -> None:
        if str(device_id) not in self.data:
            self.data[str(device_id)] = {}

        vanished = self._warn_about_renamed_scraper_keys(
            [key for key, row in webscraping_data.items() if isinstance(row, Reading)]
        )

        merged_rows = self._rows_owning_a_described_week()
        for key, new_val in webscraping_data.items():
            if isinstance(new_val, Reading):
                self._prepare_scraped_row(
                    new_val, self.data[str(device_id)].get(key), key in merged_rows
                )
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
        from .scraper import WemPortalScraper, refuse_folded_panels

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
            data = self._scrape_with(scraper)
            # In the try: a broken page counts as a failed scrape, never as rows.
            refuse_folded_panels(self._previous_scraper_keys, data)

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

        except WebLoginRefused as exc:
            # Counted like any failed scrape, but the first in a row neither
            # pauses the API nor waits a cycle: five minutes, and the next
            # login usually goes through. A second in a row is the block.
            self._register_scrape_failure()
            self.webscraping_cookie = None
            self._reset_scraper()
            if self._account_state.note_refused_web_login():
                self.spider_wait_interval = 0
                self._account_state.web_login_retry_at = (
                    time.monotonic() + WEB_LOGIN_RETRY_SECONDS
                )
                raise
            self._activate_cooldown(f"the web scrape: {exc}")
            raise

        except ForbiddenError as exc:
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
            self._activate_cooldown(f"the web scrape: {exc}")
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
        self._account_state.refused_web_logins = 0

        # Return the scraped data
        return data

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

    def reread_device_values(
        self,
        device_id: str,
        module: tuple[int | None, int | None],
        parameter_ids: Iterable[str],
    ) -> str | None:
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

        `module` and `parameter_ids` are what was written, and the answer has
        to carry each of them. Any successful read used to count, and one
        that held only another module left the written rows untouched - so
        the value that was asked for stood as the one the portal kept.
        """
        self._acquire_api_lock("value re-read")
        try:
            outcome = self._fetch_parameter_values(str(device_id))
            if outcome is NoRead.NOTHING_TO_READ:
                # Nothing read back is nothing verified.
                return "the device has nothing to read back"
            if outcome is not None:
                return outcome
            missing = unanswered_parameters(
                self._last_values_answer.get(str(device_id)), module, parameter_ids
            )
            if missing:
                return f"the answer did not carry {', '.join(missing)}"
            return None
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
            "Fetching fresh api data. enabled_devices=%s, known devices=%s",
            short_device_ids(enabled_devices),
            short_device_ids(self.data),
        )
        # `is not None`, NOT truthiness: an EMPTY list means "every device is
        # disabled", and treating that as "no filter given" polled all of them -
        # the exact opposite of what the caller asked for.
        target_devices = (
            enabled_devices if enabled_devices is not None else list(self.data.keys())
        )
        _LOGGER.debug("Computed target_devices=%s", short_device_ids(target_devices))
        # Here, with the reads, not with the session: in `both` mode the
        # session is checked on web-only ticks too, and a re-read due there
        # was the only heavy kind asking - the queue took that for a cycle in
        # which the statistics were no longer due and dropped them. Only
        # when anything needs it: with a valid cache this skips entirely.
        self._discover_parameters_if_due(enabled_devices)
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
                short_device_id(device_id),
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
                    "Skipping device %s: no API modules (scraper-only).",
                    short_device_id(device_id),
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
                # No schedule fetch on this path: a programme ages too.
                self._forget_stale_device_values(device_id, schedule_runs=False)
                continue
            # `is None`, never a truth test: the reason for a FAILURE is what
            # comes back, so a truthy answer is the bad one.
            outcome = self._fetch_parameter_values(device_id)
            if outcome is None:
                successes += 1
                self._last_device_read[device_id] = time.monotonic()
            elif outcome is not NoRead.NOTHING_TO_READ:
                failures.append(f"device {short_device_id(device_id)}: {outcome}")
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
            if raw_status is None:  # sent as null: says as little as missing
                raise KeyError("ConnectionStatus")
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
            has_errors = "yes" if errors else "no"
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

            if failure_is_over(self._reported_failures, f"device-status:{device_id}"):
                _LOGGER.info(
                    "The status of device %s is readable again.",
                    short_device_id(device_id),
                )

            previous = self._last_connection_status.get(device_id)
            self._last_connection_status[device_id] = conn_status

            if conn_status != "online":
                # Edge-triggered on purpose. An unreachable device no longer
                # fails the cycle, so this line is the only running
                # commentary there is - but repeating it every few minutes
                # for as long as the device stays away would bury everything
                # else in the log. Say it once per change, then stay quiet.
                level = logging.DEBUG if previous == conn_status else logging.WARNING
                _LOGGER.log(
                    level,
                    "Device %s is %s. Skipping data polling.",
                    short_device_id(device_id),
                    conn_status,
                )
                return False
            if previous is not None and previous != "online":
                _LOGGER.info("Device %s is back online.", short_device_id(device_id))

        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except AuthError:
            # A rejected login is not an unreadable status; see AuthError.
            raise
        except Exception as exc:  # noqa: BLE001
            # Broad: an unreadable status must not stop the poll. The
            # caller treats "unknown" as reachable, which is the safe
            # side - see device_is_reachable.
            key = f"device-status:{device_id}"
            if failure_is_new(self._reported_failures, key, str(exc)):
                _LOGGER.info(
                    "Cannot read the status of device %s: %s",
                    short_device_id(device_id),
                    exc,
                )
            else:
                _LOGGER.debug(
                    "Still cannot read the status of device %s: %s",
                    short_device_id(device_id),
                    exc,
                )
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

    def _fetch_parameter_values(self, device_id: str) -> str | NoRead | None:
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

        A device with nothing to read answers NoRead.NOTHING_TO_READ: no
        request, so neither refreshed nor failed.
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
                "%s: missing module data for device %s",
                DATA_GATHERING_ERROR,
                short_device_id(device_id),
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
                        "Device %s has nothing to read: it has no modules.",
                        short_device_id(device_id),
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
                        short_device_id(device_id),
                        len(refused),
                        len(device_modules),
                        PARAMETER_REDISCOVERY_RETRY_SECONDS // 3600,
                    )
                else:
                    _LOGGER.debug(
                        "Device %s has nothing to read: every module describes "
                        "no parameters.",
                        short_device_id(device_id),
                    )
                return NoRead.NOTHING_TO_READ

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
                short_device_id(device_id),
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
            except ValueError, AttributeError:
                # Unreadable is not the same as "no status given". Falling
                # through left the read without a JobID, which makes the
                # server return the most recent job - the PREVIOUS
                # measurement - whose values were then booked as fresh.
                _LOGGER.warning(
                    "Device %s answered the refresh with something that is not "
                    "JSON; skipping the read rather than serving the previous "
                    "measurement as current.",
                    short_device_id(device_id),
                )
                return "the refresh answered with something that is not JSON"
            ticket = read_refresh_ticket(refresh_payload)
            if not ticket.accepted:
                _LOGGER.warning(
                    "Device %s %s; not reading the previous job's values as current.",
                    short_device_id(device_id),
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
                    short_device_id(device_id),
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
                # The last good scrape's inventory, for choosing NEW merge
                # targets: a row the page dropped keeps its key, and must not
                # be revived by the api reading that arrives after the relabel.
                scraped_rows=self._previous_scraper_keys,
            )
            # Freshness lives on the MODULE, not only on the device: a
            # successful answer naming module A refreshes the device-level
            # stamp, and module B - missing from the very same answer - kept
            # presenting its last readings indefinitely. _clear_unanswered
            # cannot see B (it walks the answer), _forget_stale_device_values
            # cannot either (the device did answer).
            self._stamp_answered_modules(device_id, values)
            self._forget_unanswered_module_values(device_id)
            self._last_values_answer[device_id] = values
            return None
        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except AuthError:
            # Not this device failing: an account is one login. Returned as
            # a reason string here, it stops being an error anyone can
            # count; see AuthError.
            raise
        except Exception as exc:  # noqa: BLE001
            # Broad: one device's parameter read failing must not take
            # the other devices' readings with it. Debug, not warning: the
            # reason is RETURNED, and the caller puts it in the message Home
            # Assistant shows when the cycle fails - which its coordinator
            # logs. Warning here as well printed one outage twice.
            _LOGGER.debug("Failed to fetch parameter data... %s", exc)
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
