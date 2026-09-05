"""WemPortal integration coordinator"""

from __future__ import annotations

import logging

import asyncio
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    AUTH_ERROR_ESCALATION_THRESHOLD,
    DEFAULT_CONF_SCAN_INTERVAL_API_VALUE,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .exceptions import (
    ApiBusyError,
    AuthError,
    PollDeadlineExceeded,
    PortalMaintenanceError,
    WemPortalError,
)
from .models import account_state
from .utils import device_identifier, serialize_modules
from .wemportalapi import WemPortalApi

_LOGGER = logging.getLogger(__name__)

# Version of the on-disk format used to persist discovered device/module/
# parameter metadata (see get_modules_store()). Bump this if the structure
# of the cached data ever changes in a backwards-incompatible way.
MODULES_STORAGE_VERSION = 1

# Version of the on-disk format for the stable scraper device id
# (see get_scraper_device_store()). Just a bare string value.
SCRAPER_DEVICE_STORAGE_VERSION = 1

# A safety cap on how long the coordinator will ever wait between retries
# after repeated failures (see the backoff logic in _async_update_data).
MAX_BACKOFF_SECONDS = 6 * 3600  # 6 hours

# How many consecutive failed cycles the entities keep showing their last
# reading through. One failed cycle used to take every entity of the account
# unavailable at once: the portal answers a cycle with "Unbekannter Fehler"
# now and then and the next one succeeds, so a single failure says nothing -
# but at the default interval it costs half an hour of every graph and sends
# automations a state change on the way out and back.
#
# One rather than the scrape's three, because an API cycle is the expensive
# one: at the default interval three failures is an hour and a half of
# readings presented as current. Here rather than with the entities that read
# it, because what it bounds is `num_failed`, which lives here.
API_FAILURES_TOLERATED = 1

# The issue_id suffix of the repair issue that says the portal is
# rate-limiting this installation; one constant so the create and the delete
# site cannot drift apart. The full id is prefixed with the entry id, which
# is the cleanup contract async_remove_entry relies on (tests/test_repairs.py
# pins that, and requires the translation_key to be a literal at the call).
RATE_LIMIT_ISSUE = "rate_limited"

# The same, for the web half of `both` mode having stopped delivering. Its
# own issue because the answer for the user is a different one: the api half
# is still working, so this is about web access or the mode, not the portal
# refusing this network.
WEB_SCRAPE_ISSUE = "web_scrape_failing"

# Consecutive auth failures per config entry, kept OUTSIDE the coordinator.
#
# A failed first refresh makes Home Assistant retry the whole setup, and
# every retry builds a fresh coordinator - so a counter living on the
# coordinator restarted at zero each time and AUTH_ERROR_ESCALATION_THRESHOLD
# was unreachable during startup. A wrong password (changed while Home
# Assistant was off) then left the entry retrying forever instead of asking
# for new credentials. The account state outlives those retries on purpose;
# see models.AccountState. Cleared on success and on unload.


def forget_auth_failures(config_entry: ConfigEntry) -> None:
    """Drop the account's auth-failure count (unload/removal)."""
    account_state(config_entry.data.get(CONF_USERNAME)).auth_failures = 0


def get_modules_store(hass: HomeAssistant, entry_id: str) -> Store[Any]:
    """Return the Store used to persist discovered module/parameter metadata.

    Used both by __init__.py (to load the cache before creating the
    WemPortalApi instance) and by the coordinator itself (to save it after
    a successful update), so both sides always agree on the same file.
    """
    return Store(hass, MODULES_STORAGE_VERSION, f"{DOMAIN}_{entry_id}_modules")


def get_scraper_device_store(hass: HomeAssistant, entry_id: str) -> Store[Any]:
    """Return the Store used to persist the stable scraper device id.

    Loaded by __init__.py before creating the WemPortalApi instance and
    saved by the coordinator after a successful update, so the id decided
    once (see WemPortalApi.resolve_scraper_device_id) survives restarts and
    never silently changes on a later mode switch.
    """
    return Store(
        hass, SCRAPER_DEVICE_STORAGE_VERSION, f"{DOMAIN}_{entry_id}_scraper_device"
    )


class WemPortalDataUpdateCoordinator(DataUpdateCoordinator):
    """DataUpdateCoordinator for wemportal component"""

    # Always set (passed in __init__); narrows the base's Optional so the
    # entry_id/state accesses below do not each need a None check.
    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: WemPortalApi,
        config_entry: ConfigEntry,
        update_interval: timedelta | None,
    ) -> None:
        """Initialize DataUpdateCoordinator for the wemportal component"""
        # config_entry is passed to the base class (current HA convention;
        # the implicit assignment is on the deprecation path). The base
        # class also sets self.hass/self.config_entry, so no manual
        # assignments are needed here.
        super().__init__(
            hass,
            _LOGGER,
            name="WemPortal update",
            update_interval=update_interval,
            config_entry=config_entry,
        )
        self.api = api
        self.last_try: float | None = None
        self.num_failed = 0
        # Consecutive AuthError counter, separate from num_failed: only
        # after AUTH_ERROR_ESCALATION_THRESHOLD auth failures IN A ROW do we
        # escalate to ConfigEntryAuthFailed (reauth). Reset on any success.
        # Seeded from the account state: a failed setup triggers the very
        # reload that would otherwise reset a counter living here.
        self._account_state = account_state(config_entry.data.get(CONF_USERNAME))
        self.num_auth_failed = self._account_state.auth_failures
        self._modules_store = get_modules_store(hass, config_entry.entry_id)
        self._scraper_device_store = get_scraper_device_store(
            hass, config_entry.entry_id
        )
        # Remember the last-persisted scraper device id so we only write the
        # store when it actually changes (it's decided once and then stable).
        self._saved_scraper_device_id: str | None = None
        # Fingerprint of the last-written module cache, so an unchanged one
        # is not rewritten on every successful cycle (~288 writes a day at a
        # five-minute interval, for data that changes almost never).
        self._saved_modules_snapshot: dict[str, Any] | None = None
        # Held for the duration of a store write, and taken by the unload
        # before it lets go. The gate below decides whether a save may START;
        # the write itself is asynchronous, so a removal or a reload landing
        # after that decision would otherwise overtake it - re-creating a
        # store that had just been deleted, or putting the old api's modules
        # over what the reloaded entry already saved.
        self._store_writes = asyncio.Lock()

    def _may_still_write_to_disk(self) -> bool:
        """Whether a cycle finishing now still owns its entry's stores.

        A cycle runs in an executor thread and cannot be cancelled, so a
        removal or reload landing mid-cycle is followed by the tail of that
        cycle arriving on the event loop. Writing there re-created stores
        async_remove_entry had just deleted - left in .storage for good, and
        handed to whatever entry reuses the id - or wrote the OLD api's
        modules over what the reloaded entry had already saved.

        Absent runtime_data means two opposite things, which is what the
        first version of this got wrong: during SETUP it has not been
        published yet - and that is the cycle which discovers everything, so
        barring it wrote nothing down at all - while after an unload it has
        been taken away again. The entry's own state tells the two apart.

        Asked of both save paths rather than of the caller: they are what
        touches the disk, and a third one added later would otherwise have to
        remember this on its own.
        """
        data = getattr(self.config_entry, "runtime_data", None)
        if data is not None:
            # `unloading` beside the identity, because the store is still
            # there and still holds THIS coordinator for the whole teardown -
            # identity alone said yes for exactly the window the flag exists
            # to mark, and the save it let through is asynchronous, so it can
            # land after the stores are deleted or after a reload published
            # new ones.
            return data.coordinator is self and not data.unloading
        return self.config_entry.state is ConfigEntryState.SETUP_IN_PROGRESS

    async def async_wait_for_store_writes(self) -> None:
        """Return once no store write of this coordinator is still in flight.

        Called by the unload before it finishes. Everything AFTER it is
        refused by the gate above, so this closes the one window left: a save
        that had already passed the gate and was waiting on the disk while
        the entry was being taken down.
        """
        async with self._store_writes:
            return

    async def _save_under_store_lock(self, store: Store[Any], data: Any) -> None:
        """Persist to a Store under _store_writes - the lock the unload waits on.

        Both persisted stores (the module cache and the scraper device id)
        write through here, so the barrier the teardown depends on lives in
        one place. A save that had passed its own gate is still on the disk
        while the entry comes down; holding this lock is what makes
        async_wait_for_store_writes wait for it, so the removal or reload that
        follows cannot overtake it. Dropping it at even one writer reopens
        that window - which is why the guard mutates this line.
        """
        async with self._store_writes:
            await store.async_save(data)

    async def _async_save_scraper_device_id(self) -> None:
        """Persist the stable scraper device id once it has been decided.

        Written only when it changes (normally exactly once, on the first
        successful scrape after install/upgrade), so scraped sensors keep a
        constant device id - and history - across mode switches. Best-effort:
        a failed save only means it is re-decided next start from the same
        deterministic rule, which yields the same value in the common case.
        """
        device_id = getattr(self.api, "scraper_device_id", None)
        if not device_id or device_id == self._saved_scraper_device_id:
            return
        if not self._may_still_write_to_disk():
            return
        try:
            await self._save_under_store_lock(self._scraper_device_store, device_id)
            self._saved_scraper_device_id = device_id
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not persist WEM Portal scraper device id: %s", exc)

    async def _async_save_modules_cache(self) -> None:
        """Persist discovered device/module/parameter metadata to disk.

        This is what lets a future Home Assistant restart skip the slow,
        rate-limited per-module parameter discovery in
        WemPortalApi.get_parameters() (see get_devices()/fetch_data() in
        wemportalapi.py). This is purely a "nice to have" cache: any
        failure to save it is logged and otherwise ignored, since losing
        it only costs a slower next startup, never incorrect data.
        """
        if not self.api.modules:
            return
        if not self._may_still_write_to_disk():
            return
        try:
            serialized = serialize_modules(self.api.modules)
            if serialized == self._saved_modules_snapshot:
                return
            await self._save_under_store_lock(self._modules_store, serialized)
            self._saved_modules_snapshot = serialized
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not persist WEM Portal module cache: %s", exc)

    async def async_persist_rescan(self) -> None:
        """Write the module cache to disk after a rescan marked it due.

        The options flow's rescan sets parameters_fetched_at back to 0 on the
        api's modules; this persists that under the same store lock and unload
        gate the cycle's own saves use, so the reload the flow schedules
        rebuilds the cache WITH the marks. The flow opening the store itself
        wrote outside that lock - a removal could re-create a store it had just
        deleted, or a later cycle save could put the old timestamps back over
        the marks. The gate is re-checked here, right before the commit.

        Unlike the cycle's own cache save the failure is NOT swallowed - a lost
        rescan is the request going missing, not a slower next start - so this
        neither wraps the save nor skips it on an unchanged fingerprint. The
        snapshot is still advanced, so the next cycle does not rewrite it.
        """
        if not self.api.modules:
            return
        if not self._may_still_write_to_disk():
            return
        serialized = serialize_modules(self.api.modules)
        await self._save_under_store_lock(self._modules_store, serialized)
        self._saved_modules_snapshot = serialized

    async def _async_update_data(self) -> Any:
        """Fetch data from the wemportal api"""
        if self.num_failed > 2:
            # Wait longer than the plain scan interval before retrying,
            # and wait progressively longer the more consecutive failures
            # we've seen (capped at MAX_BACKOFF_SECONDS). This is a purely
            # additive safety margin on top of the existing, already
            # rate-limit-aware pacing inside wemportalapi.py itself (which
            # is intentionally left untouched here) - it only ever waits
            # *at least* as long as before, never less, to avoid adding any
            # extra risk of triggering a server-side block.
            required_wait = min(
                DEFAULT_CONF_SCAN_INTERVAL_API_VALUE * (self.num_failed - 2),
                MAX_BACKOFF_SECONDS,
            )
            if (
                self.last_try is not None
                and monotonic() - self.last_try < required_wait
            ):
                raise UpdateFailed("Waiting for more time to pass before retrying")

        registry = device_registry.async_get(self.hass)
        # Which devices this integration KNOWS, not which it has already read
        # this session. A UNION of three separate sources, not a fallback
        # between them: api.data is filled by get_devices() inside fetch_data
        # (empty right after a restart), api.modules is the persisted cache
        # that survives one, and the scraper keeps its own device id apart from
        # both. Reading only the first non-empty source dropped the scrape's
        # own pseudo-device from the filter after a restart, so a `web`->`both`
        # install went silently API-only. The RAW scraper id, not
        # resolve_scraper_device_id(), which files it back as a side effect;
        # a falsy id (undecided) does not join the set. Sorted for a stable
        # filter order - membership is all any consumer reads.
        known_devices = {
            str(device_id)
            for source in (self.api.data, self.api.modules)
            for device_id in (source or {})
        }
        if self.api.scraper_device_id:
            known_devices.add(str(self.api.scraper_device_id))
        enabled_devices = []
        for device_id in sorted(known_devices):
            # Look the device up under the SAME identifier the entity
            # platforms register (utils.device_identifier); previously this
            # used a bare (DOMAIN, device_id), which never matched, so a
            # disabled device kept being polled. Entry-aware, because the
            # identifier alone is ambiguous across integrations - which is
            # why the plain lookup is deprecated.
            device_entry = registry.async_get_device_by_identifier(
                device_identifier(self.config_entry.entry_id, str(device_id)),
                self.config_entry.entry_id,
            )
            if device_entry is not None and device_entry.disabled_by is not None:
                _LOGGER.debug("Skipping disabled device %s", device_id)
                continue
            enabled_devices.append(device_id)

        # None and [] mean DIFFERENT things to the api: None is "no filter,
        # do a full cycle", [] is "every known device is disabled, poll
        # nothing". Before any device is known - a fresh install - the loop
        # above yields [], which must not be read as "poll nothing" or
        # discovery never runs and no entities are ever created.
        device_filter = enabled_devices if known_devices else None

        # asyncio.timeout does NOT raise TimeoutError where you await - it
        # CANCELS the task, and CancelledError derives from BaseException, so
        # the `except Exception` inside the block below never sees it. The
        # TimeoutError only appears when the context manager exits, i.e.
        # outside that try. The catch-all's own comment claimed to handle
        # timeouts; it never did, so num_failed was not incremented and the
        # extra backoff never engaged for the one failure mode where waiting
        # longer matters most. Caught here instead of re-indenting the whole
        # error-handling block into an outer try.
        try:
            return await self._update_within_timeout(device_filter)
        except TimeoutError as exc:
            self._note_failed_cycle()
            self._sync_rate_limit_issue()
            self._sync_web_scrape_issue(device_filter)
            self._reset_auth_failures()
            _LOGGER.warning(
                "Fetching WEM Portal data timed out after %ds. Note the "
                "underlying request keeps running in its worker thread - "
                "Python cannot cancel it - so the next operation may briefly "
                "wait for it.",
                DEFAULT_TIMEOUT,
            )
            raise UpdateFailed(
                f"Timed out fetching data from wemportal after {DEFAULT_TIMEOUT}s"
            ) from exc

    def _sync_rate_limit_issue(self) -> None:
        """Report the IP-wide backoff if it is holding, withdraw it if not.

        Asked of the STATE after every cycle, not of whatever was raised -
        and that is the whole point. A 403 is usually earned inside
        statistics, schedules or the `both`-mode scrape, each of which
        catches broadly so one optional part cannot cost the readings. So
        the exception rarely reaches here, and both halves went wrong: the
        report never appeared, and a later cycle that "succeeded" while
        every request was still being refused deleted it.

        Idempotent in both directions, so running it on every path costs
        nothing.
        """
        issue_id = f"{self.config_entry.entry_id}_{RATE_LIMIT_ISSUE}"
        if self.api.is_rate_limited():
            async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="rate_limited",
            )
            return
        async_delete_issue(self.hass, DOMAIN, issue_id)

    def _sync_web_scrape_issue(self, device_filter: list[str] | None) -> None:
        """Report a web half that has stopped delivering, withdraw it if not.

        Separate from the rate-limit report beside it: that one says the
        portal is refusing this network and polling is paused. This one says
        the api half is fine and the web half is not, which points at web
        access or at switching the mode - a different answer for the user.

        Takes the same filter the poll used, because a scrape that is not
        being attempted cannot be failing - see web_scrape_is_failing.

        Idempotent in both directions, like its sibling.
        """
        issue_id = f"{self.config_entry.entry_id}_{WEB_SCRAPE_ISSUE}"
        if self.api.web_scrape_is_failing(device_filter):
            async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="web_scrape_failing",
            )
            return
        async_delete_issue(self.hass, DOMAIN, issue_id)

    def _reset_auth_failures(self) -> None:
        """Clear the consecutive-auth-failure count.

        Called on success AND on every failure that is not an auth failure.
        The threshold is documented as CONSECUTIVE, but the counter only ever
        went up: a timeout, a maintenance window or a 403 in between left it
        standing, so auth failures spread over hours - a portal that hands
        out the odd login page - still added up to a reauth prompt for
        credentials that were correct the whole time.
        """
        self.num_auth_failed = 0
        self._account_state.auth_failures = 0

    def _note_failed_cycle(self) -> None:
        """Count this failed cycle, and publish the moment the count leaves
        the tolerance behind.

        Home Assistant notifies listeners on the refresh that fails FIRST and
        on none after it, so an entity is only ever asked for `available`
        again while the count is still INSIDE the tolerance. Nothing then
        published the crossing, and every entity of the account went on
        offering its pre-outage reading as a current value for as long as the
        outage lasted - a tolerance that could not expire.

        Once, at the crossing: the cycles after it change nothing an entity
        shows, and telling every listener about each of them would write the
        same state again on every cycle of an outage.

        One method for all six raising paths, because each of them is a
        failed cycle and a seventh added later would otherwise quietly skip
        the notification - which is how this asymmetry got here in the first
        place.
        """
        self.num_failed += 1
        if self.num_failed == API_FAILURES_TOLERATED + 1:
            self.async_update_listeners()

    async def _update_within_timeout(self, device_filter: list[str] | None) -> Any:
        """The guarded update itself. Split out so the timeout can be caught
        around it without moving the error handling one level in."""
        async with asyncio.timeout(DEFAULT_TIMEOUT):
            try:
                # In `finally` below rather than per branch: every one of the
                # seven exits is a moment where the backoff either holds or
                # does not, and a branch added later would otherwise silently
                # skip the report.
                fetched = await self.hass.async_add_executor_job(
                    self.api.fetch_data, device_filter
                )
                self.num_failed = 0
                self._reset_auth_failures()
                return fetched
            except PortalMaintenanceError as exc:
                # Announced downtime, not a credential problem. Counted as a
                # normal failure (so backoff engages) but NOT as an auth
                # failure: the portal serves a working login form during
                # maintenance, so the login "fails" and three cycles of that
                # used to escalate into a reauth prompt for credentials that
                # were correct all along.
                self._note_failed_cycle()
                self._reset_auth_failures()
                _LOGGER.warning("WEM Portal is in maintenance: %s", exc)
                raise UpdateFailed(f"WEM Portal maintenance: {exc}") from exc
            except AuthError as exc:
                self._note_failed_cycle()
                self.num_auth_failed += 1
                self._account_state.auth_failures = self.num_auth_failed
                # Escalate to reauth only after several CONSECUTIVE auth
                # failures. The portal occasionally serves a transient login
                # page; treating a single such hiccup as "credentials are
                # wrong" would put the integration into the reauth state,
                # which stops all automatic retries until the user acts.
                if self.num_auth_failed >= AUTH_ERROR_ESCALATION_THRESHOLD:
                    _LOGGER.error(
                        "Authentication failed %d times in a row, raising ConfigEntryAuthFailed: %s",
                        self.num_auth_failed,
                        exc,
                    )
                    raise ConfigEntryAuthFailed(
                        "WEM Portal authentication failed. Check your credentials."
                    ) from exc
                _LOGGER.warning(
                    "Authentication error (%d/%d before reauth is required), will retry: %s",
                    self.num_auth_failed,
                    AUTH_ERROR_ESCALATION_THRESHOLD,
                    exc,
                )
                raise UpdateFailed(f"Authentication error, will retry: {exc}") from exc
            except PollDeadlineExceeded as exc:
                # The cycle stopped itself, so nothing is broken - the portal
                # was simply slower than one cycle allows. Caught BEFORE the
                # WemPortalError handler for the same reason ApiBusyError is:
                # that one resets the transport after two failures, which
                # here would throw away a warm session over slowness alone
                # and make the next cycle start from a cold login.
                #
                # Still counted as a failure, unlike ApiBusyError. This cycle
                # really did fail to deliver readings, and the extra backoff
                # is exactly what a portal that cannot answer in time needs.
                # Not an auth failure: the credentials were never in doubt.
                self._note_failed_cycle()
                self._reset_auth_failures()
                _LOGGER.warning("Poll cycle stopped on its own deadline: %s", exc)
                raise UpdateFailed(str(exc)) from exc
            except ApiBusyError as exc:
                # NOT a corrupted session: a previous poll is still running.
                # Must be caught BEFORE the WemPortalError handler below, or
                # the recovery there would close the sessions that thread is
                # actively using and give the next poll a fresh lock -
                # removing the serialization and doubling the load on a
                # portal that was already too slow to answer in time.
                #
                # Neither counter moves, and both silences are deliberate.
                # This cycle failed to take the lock, so it sent no request:
                # nothing is broken (a raised num_failed would back the
                # portal off and, past the tolerance, empty the dashboard),
                # and nothing was learnt about the credentials either - the
                # auth streak is reset by cycles that REACHED the portal
                # without an auth failure, which is evidence this one lacks.
                _LOGGER.debug("Skipping this cycle: %s", exc)
                raise UpdateFailed(str(exc)) from exc
            except WemPortalError as exc:
                self._note_failed_cycle()
                self._reset_auth_failures()
                if self.num_failed >= 2:
                    # Reset the connection, do NOT rebuild the api object.
                    # Rebuilding meant carrying nine pieces of state across by
                    # hand, so every new field was a new chance to forget one -
                    # and it silently reset two portal rate limits and the lock
                    # that serialises a write against a running poll. See
                    # WemPortalApi.reset_transport.
                    #
                    # In an executor, not here: the reset now takes the
                    # shared api lock, and waiting for a threading lock on
                    # the event loop would stall everything Home Assistant
                    # does for as long as the write it waits for runs.
                    await self.hass.async_add_executor_job(self.api.reset_transport)
                raise UpdateFailed(
                    f"Error fetching data from wemportal: {exc}"
                ) from exc
            except Exception as exc:
                # Catch-all safety net: covers cases that don't come from
                # fetch_data() itself (which already wraps its own
                # unexpected errors as WemPortalError) - most notably
                # unexpected errors from the executor job itself.
                # NOTE: it does NOT catch the asyncio.timeout() timeout -
                # that arrives as CancelledError (a BaseException) and is
                # handled by the outer `except TimeoutError` in
                # _async_update_data. Do not remove that handler on the
                # assumption this one covers it; it does not.
                self._note_failed_cycle()
                self._reset_auth_failures()
                _LOGGER.warning("Unexpected error updating WEM Portal data: %s", exc)
                raise UpdateFailed(
                    f"Unexpected error fetching data from wemportal: {exc}"
                ) from exc
            finally:
                self.last_try = monotonic()
                self._sync_rate_limit_issue()
                self._sync_web_scrape_issue(device_filter)
                # Here rather than in the success branch, where they were:
                # discovery is stopped WHERE IT STANDS when the cycle runs out
                # of time, and what it had found by then was kept in memory
                # only. An installation with enough modules to exhaust the
                # budget every cycle therefore never wrote any of it down and
                # began again from nothing after every restart - spending the
                # same five seconds and one request per module a second time.
                # Both saves are idempotent and skip an unchanged snapshot.
                await self._async_save_modules_cache()
                await self._async_save_scraper_device_id()
