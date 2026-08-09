"""WemPortal integration coordinator"""

from __future__ import annotations

import logging

import asyncio
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry
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

# Consecutive auth failures per config entry, kept OUTSIDE the coordinator.
#
# A failed first refresh makes Home Assistant retry the whole setup, and
# every retry builds a fresh coordinator - so a counter living on the
# coordinator restarted at zero each time and AUTH_ERROR_ESCALATION_THRESHOLD
# was unreachable during startup. A wrong password (changed while Home
# Assistant was off) then left the entry retrying forever instead of asking
# for new credentials. The config entry object itself outlives those retries,
# so keying on its id does. Cleared on success and on unload.
_AUTH_FAILURES: dict[str, int] = {}


def forget_auth_failures(entry_id: str) -> None:
    """Drop the auth-failure count for an entry (unload/removal)."""
    _AUTH_FAILURES.pop(entry_id, None)


def get_modules_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Return the Store used to persist discovered module/parameter metadata.

    Used both by __init__.py (to load the cache before creating the
    WemPortalApi instance) and by the coordinator itself (to save it after
    a successful update), so both sides always agree on the same file.
    """
    return Store(hass, MODULES_STORAGE_VERSION, f"{DOMAIN}_{entry_id}_modules")


def get_scraper_device_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Return the Store used to persist the stable scraper device id.

    Loaded by __init__.py before creating the WemPortalApi instance and
    saved by the coordinator after a successful update, so the id decided
    once (see WemPortalApi.resolve_scraper_device_id) survives restarts and
    never silently changes on a later mode switch.
    """
    return Store(
        hass, SCRAPER_DEVICE_STORAGE_VERSION, f"{DOMAIN}_{entry_id}_scraper_device"
    )


def device_by_identifier(registry, identifier, config_entry_id):
    """One registered device, looked up the way this Home Assistant allows.

    `async_get_device(identifiers=...)` matches on the identifier alone, so
    two integrations that register the same one are indistinguishable - which
    is why Home Assistant deprecated it in 2026.8 (removal in 2027.8) in
    favour of a lookup that takes the config entry as well.

    That replacement arrived after 2024.12, the minimum this integration
    supports, so both shapes have to work. Detected by asking the registry
    what it can do rather than by comparing version numbers: a version says
    which release this is, not which methods the object in hand has - and a
    backport or a patched install would make the comparison wrong.
    """
    unambiguous_lookup = getattr(registry, "async_get_device_by_identifier", None)
    if unambiguous_lookup is not None:
        return unambiguous_lookup(identifier, config_entry_id)
    return registry.async_get_device(identifiers={identifier})


class WemPortalDataUpdateCoordinator(DataUpdateCoordinator):
    """DataUpdateCoordinator for wemportal component"""

    def __init__(
        self,
        hass: HomeAssistant,
        api: WemPortalApi,
        config_entry: ConfigEntry,
        update_interval,
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
        self.last_try = None
        self.num_failed = 0
        # Consecutive AuthError counter, separate from num_failed: only
        # after AUTH_ERROR_ESCALATION_THRESHOLD auth failures IN A ROW do we
        # escalate to ConfigEntryAuthFailed (reauth). Reset on any success.
        # Seeded from the cross-setup counter: see _AUTH_FAILURES for why it
        # cannot live on the coordinator alone.
        self.num_auth_failed = _AUTH_FAILURES.get(config_entry.entry_id, 0)
        self._modules_store = get_modules_store(hass, config_entry.entry_id)
        self._scraper_device_store = get_scraper_device_store(
            hass, config_entry.entry_id
        )
        # Remember the last-persisted scraper device id so we only write the
        # store when it actually changes (it's decided once and then stable).
        self._saved_scraper_device_id = None
        # Fingerprint of the last-written module cache, so an unchanged one
        # is not rewritten on every successful cycle (~288 writes a day at a
        # five-minute interval, for data that changes almost never).
        self._saved_modules_snapshot: dict | None = None

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
        try:
            await self._scraper_device_store.async_save(device_id)
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
        try:
            serialized = serialize_modules(self.api.modules)
            if serialized == self._saved_modules_snapshot:
                return
            await self._modules_store.async_save(serialized)
            self._saved_modules_snapshot = serialized
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not persist WEM Portal module cache: %s", exc)

    async def _async_update_data(self):
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
        # this session. api.data is filled by get_devices() inside fetch_data,
        # so right after a restart it is empty even for an install that has
        # been running for months - and the filter below then let a disabled
        # device be polled once per restart. The persisted module cache
        # survives the restart and answers the same question.
        known_devices = self.api.data or self.api.modules or {}
        enabled_devices = []
        for device_id in known_devices:
            # Look the device up under the SAME identifier the entity
            # platforms register (utils.device_identifier); previously this
            # used a bare (DOMAIN, device_id), which never matched, so a
            # disabled device kept being polled.
            device_entry = device_by_identifier(
                registry,
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
            self.num_failed += 1
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
        _AUTH_FAILURES.pop(self.config_entry.entry_id, None)

    async def _update_within_timeout(self, device_filter):
        """The guarded update itself. Split out so the timeout can be caught
        around it without moving the error handling one level in."""
        async with asyncio.timeout(DEFAULT_TIMEOUT):
            try:
                fetched = await self.hass.async_add_executor_job(
                    self.api.fetch_data, device_filter
                )
                self.num_failed = 0
                self._reset_auth_failures()
                await self._async_save_modules_cache()
                await self._async_save_scraper_device_id()
                return fetched
            except PortalMaintenanceError as exc:
                # Announced downtime, not a credential problem. Counted as a
                # normal failure (so backoff engages) but NOT as an auth
                # failure: the portal serves a working login form during
                # maintenance, so the login "fails" and three cycles of that
                # used to escalate into a reauth prompt for credentials that
                # were correct all along.
                self.num_failed += 1
                self._reset_auth_failures()
                _LOGGER.warning("WEM Portal is in maintenance: %s", exc)
                raise UpdateFailed(f"WEM Portal maintenance: {exc}") from exc
            except AuthError as exc:
                self.num_failed += 1
                self.num_auth_failed += 1
                _AUTH_FAILURES[self.config_entry.entry_id] = self.num_auth_failed
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
                self.num_failed += 1
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
            # ForbiddenError was named here as well, which reads as two
            # separate cases and is one: it derives from WemPortalError, so
            # this clause always covered it. test_a_forbidden_error_is_a_
            # wemportal_error keeps that true.
            except WemPortalError as exc:
                self.num_failed += 1
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
                self.num_failed += 1
                self._reset_auth_failures()
                _LOGGER.warning("Unexpected error updating WEM Portal data: %s", exc)
                raise UpdateFailed(
                    f"Unexpected error fetching data from wemportal: {exc}"
                ) from exc
            finally:
                self.last_try = monotonic()
