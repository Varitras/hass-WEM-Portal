"""Everything one entry's expert path owns.

Nine fields on the runtime store and four closures in a setup function used to
hold this between them: the shared lock, the entities, whether the timer was
armed, the timer handle, the initial task, and the per-id failure bookkeeping.
Every one of them was reachable from anywhere that could reach the store, and
"is the poll running" was a question you answered by reading three of them.

Nothing here imports expert_writer at module level. That module pulls
curl_cffi (114 ms, measured) and this one is reachable from the runtime store,
so a module-level import would put it back on every installation's setup path -
see the structural guard in tests/test_security.py.
"""

from __future__ import annotations

import random
import threading
from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from .exceptions import ExpertOperationAborted
from .const import (
    _LOGGER,
    CONF_EXPERT_AUTO_POLL,
    CONF_EXPERT_POLL_INTERVAL,
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
)

# Fraction of extra, random delay added on top of the configured interval each
# cycle (0..20%). Jitter is added ONLY upwards, so the effective interval is
# always >= the user's setting - and thus never below the floor. The poll
# pattern is less regular without ever hitting the portal more often than
# configured.
JITTER_FRACTION = 0.20

# Consecutive misses before the user is told to check the configured id.
FAILURES_BEFORE_NOTIFYING = 3


def poll_interval_minutes(entry) -> int:
    """The configured poll interval, never below the floor and never a value
    the options flow let through in a shape int() chokes on."""
    interval_min = entry.options.get(
        CONF_EXPERT_POLL_INTERVAL, DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
    )
    try:
        return max(int(interval_min), MIN_EXPERT_POLL_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        return DEFAULT_EXPERT_POLL_INTERVAL_MINUTES


def read_expert_values(entry, api, entityvalues: list, abort_check=None) -> dict:
    """One shared portal session for every configured id.

    Runs in an executor thread - the expert client is blocking - and imports
    it there, which is what keeps curl_cffi off the setup path.
    """
    from .expert_options import expert_client_options
    from .expert_writer import WemPortalExpertClient

    client = WemPortalExpertClient(
        entry.data.get(CONF_USERNAME),
        entry.data.get(CONF_PASSWORD),
        cooldown_check=api.check_expert_cooldown,
        cooldown_activate=api.activate_expert_cooldown,
        cookie_jar=api.expert_cookies,
        abort_check=abort_check,
        **expert_client_options(entry.options),
    )
    return client.read_many(entityvalues)


class ExpertController:
    """The expert path of one config entry: lock, entities, timer, tally."""

    def __init__(self) -> None:
        # Shared per-account lock. A poll, an entity write and the domain
        # service all use the same portal session for this account, so they
        # serialise against each other here.
        self.lock: threading.Lock = threading.Lock()
        self.entities: list[Any] = []
        # Per-id consecutive misses. Only counted when a batch SUCCEEDED and
        # one id was missing from the answer - see apply_read.
        self.fail_counts: dict[str, int] = {}
        self.fail_notified: set[str] = set()

        self._data: Any = None
        self._hass: HomeAssistant | None = None
        self._entry = None
        self._interval_min = DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
        self._armed = False
        self._started = False
        self._stopped = False
        self._initial_task: Any = None
        self._unsub: Any = None

    def bind(self, data) -> None:
        """Hold the runtime store this controller belongs to."""
        self._data = data

    # --- wiring --------------------------------------------------------

    def setup_auto_poll(self, hass: HomeAssistant, entry) -> None:
        """Arm the optional timer, if the option is on.

        OFF unless CONF_EXPERT_AUTO_POLL is enabled. Each read is a full
        Fachmann navigation, so this is deliberately infrequent and reads ALL
        configured ids in ONE shared session. A 403 engages the shared
        cooldown via the client's cooldown_check, and the poll then skips
        until it clears.

        The entities are created by number.py's platform setup, which may run
        after this - so whichever of the two happens last starts the timer.
        """
        if not entry.options.get(CONF_EXPERT_AUTO_POLL, False):
            return
        self._hass = hass
        self._entry = entry
        self._interval_min = poll_interval_minutes(entry)
        self._armed = True
        if self.entities:
            self.start()

    def attach_entities(self, entities: list) -> None:
        """Hand the expert entities over, and start if the timer is armed."""
        self.entities = entities
        if self._armed:
            self.start()

    def start(self) -> None:
        """Begin the timer chain. Idempotent - one chain per entry."""
        if self._started or not self._armed:
            return
        self._started = True
        self._entry.async_on_unload(self.stop)
        _LOGGER.info(
            "Expert auto-poll enabled: reading configured parameters about "
            "every %d min (with up to +%d%% random jitter).",
            self._interval_min,
            int(JITTER_FRACTION * 100),
        )
        # Initial read shortly after startup; it reschedules itself
        # afterwards. Tracked so stop() can cancel it mid-run.
        self._initial_task = self._hass.async_create_background_task(
            self.poll(), name="wemportal_expert_initial_poll"
        )

    def stop(self) -> None:
        """Stop the chain, including a read that is already in flight."""
        self._stopped = True
        unsub, self._unsub = self._unsub, None
        if unsub is not None:
            unsub()
        # The initial read is a background task that would otherwise keep
        # going after the entry is unloaded - only the scheduled timer used
        # to be cancelled.
        task, self._initial_task = self._initial_task, None
        if task is not None and not task.done():
            task.cancel()

    # --- the cycle -----------------------------------------------------

    def _raise_if_stopped(self) -> None:
        """Abort gate handed to the portal client.

        Called from the worker thread, so it must only read state - a plain
        flag check on purpose, like the entity write's own gate.
        """
        if self._stopped:
            raise ExpertOperationAborted(
                "the entry was unloaded while the auto-poll was reading"
            )

    def _next_delay_seconds(self) -> float:
        base = self._interval_min * 60
        return base + random.uniform(0, base * JITTER_FRACTION)

    def _schedule_next(self) -> None:
        # Never re-arm after the entry was unloaded. poll()'s `finally` runs
        # even when the entry went away mid-read, so without this an
        # in-flight poll would schedule a fresh timer into an orphaned
        # controller - a chain nothing can cancel any more, one more per
        # reload.
        if self._stopped:
            _LOGGER.debug("Expert auto-poll: entry unloaded, not rescheduling.")
            return
        delay = self._next_delay_seconds()
        self._unsub = async_call_later(self._hass, delay, self.poll)
        _LOGGER.debug(
            "Expert auto-poll: next read in %.1f min (base %d min + jitter).",
            delay / 60,
            self._interval_min,
        )

    async def poll(self, _now=None) -> None:
        """One cycle: read every configured id in one session, apply, re-arm."""
        try:
            entities = self.entities
            entityvalues = [e.entityvalue for e in entities]
            if not entityvalues:
                return
            # Collision guard: if any entity write is in flight, skip this
            # cycle instead of opening a second concurrent portal session.
            # Reading in parallel could also briefly write a pre-write
            # (stale) value back into an entity right after its verified
            # write. The next scheduled poll picks things up again.
            if any(getattr(e, "_write_in_progress", False) for e in entities):
                _LOGGER.debug(
                    "Expert auto-poll: a write is in progress, skipping this cycle."
                )
                return
            # The store, not the entry: it says which api instance this
            # entry is using, the cooldown state travels with it, and it
            # survives the unload that drops runtime_data while this read is
            # still in flight.
            current_api = self._data.api
            lock = self.lock
            if lock is not None and not lock.acquire(blocking=False):
                _LOGGER.debug(
                    "Expert auto-poll: another expert operation in progress, "
                    "skipping this cycle."
                )
                return

            try:
                results = await self._hass.async_add_executor_job(
                    read_expert_values,
                    self._entry,
                    current_api,
                    entityvalues,
                    self._raise_if_stopped,
                )
            except ExpertOperationAborted as exc:
                # Not a failure: the configuration this read belongs to is
                # gone. Feeding it through the counters below would blame
                # every configured id for an unload.
                _LOGGER.debug("Expert auto-poll stopped: %s", exc)
                return
            except Exception as exc:  # pylint: disable=broad-except
                # Returns instead of falling through with an empty result:
                # the per-id counters below mean "the portal answered, but
                # not for this id". Feeding an outage through them would
                # blame every configured id for a problem that is not theirs.
                _LOGGER.warning("Expert auto-poll read failed: %s", exc)
                return
            finally:
                if lock is not None:
                    lock.release()

            self.apply_read(results)
        finally:
            # Always reschedule the next run (with fresh jitter), even if this
            # cycle failed - a transient error must not stop future polls.
            self._schedule_next()

    def apply_read(self, results: dict) -> None:
        """Hand a batch to the entities and keep the per-id failure tally.

        A persistently failing id would otherwise only produce an hourly
        debug line nobody sees. After three consecutive failures raise ONE
        notification per id; reset on the next success, so a recurring
        problem re-notifies at most once per streak.

        The tally has to say WHY, because it ends up in front of the user,
        and the result carries two different answers:

          * an id that is not in the result at all was rejected as
            unreadable before any request was sent (see read_many). Nothing
            but the configured value can cause that.
          * an id that IS in the result with no state was requested and the
            read failed - after the client's own retries. A wrong id does
            that, and so does the portal.

        And when every requested id failed, none of them is evidence about
        itself: that is one bad batch, and blaming each configured id for it
        would tell the user to go fix settings that are fine.

        That last rule needs at least TWO requested ids to mean anything.
        With one configured parameter "all of them failed" is true every time
        it fails, so applying it there would silence the notification for
        exactly the installation that has the least other information - the
        same trap as refusing a read with no JobID, which took single-device
        installations off the air. One id keeps being counted, and the
        message below says plainly that the portal is the other candidate.
        """
        requested = {ev: state for ev, state in results.items()}
        failed = [ev for ev, state in requested.items() if state is None]
        whole_batch_failed = len(requested) >= 2 and len(failed) == len(requested)
        if whole_batch_failed:
            _LOGGER.warning(
                "Expert auto-poll: all %d configured parameter(s) failed to "
                "read this cycle. Treating that as one failed batch rather "
                "than %d bad ids; not counting it against them.",
                len(failed),
                len(failed),
            )

        for entity in self.entities:
            ev = entity.entityvalue
            state = results.get(ev)
            unreadable_id = ev not in results
            counts_against_the_id = unreadable_id or (
                state is None and not whole_batch_failed
            )

            if counts_against_the_id:
                self.fail_counts[ev] = self.fail_counts.get(ev, 0) + 1
                if (
                    self.fail_counts[ev] >= FAILURES_BEFORE_NOTIFYING
                    and ev not in self.fail_notified
                ):
                    self.fail_notified.add(ev)
                    self._notify_read_failure(
                        entity, self.fail_counts[ev], unreadable_id
                    )
            elif state is not None:
                self.fail_counts.pop(ev, None)
                self.fail_notified.discard(ev)
            entity.apply_read_state(state)

    def _notify_read_failure(self, entity, failures: int, unreadable_id: bool) -> None:
        """Tell the user about a parameter that keeps not being read.

        The wording says only what is known. An id that never left the house
        can only be the configured value; an id that was requested and failed
        can be that OR the portal, and asserting the first sent people to
        check a setting that was correct.
        """
        from .expert_writer import ev_digest

        ev = entity.entityvalue
        if unreadable_id:
            reason = (
                f"The configured ID for '{entity.name}' is not a readable "
                "parameter ID, so it was never requested. Fix or clear it in "
                "the integration options."
            )
        else:
            reason = (
                f"Reading '{entity.name}' has failed {failures} times in a "
                "row while other parameters were read successfully. That is "
                "usually a wrong ID in the integration options, but the "
                "portal can refuse a single parameter too."
            )
        self._hass.async_create_task(
            self._hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "WEM Portal expert auto-poll",
                    "message": reason,
                    "notification_id": f"wemportal_poll_fail_{ev_digest(ev)}",
                },
                blocking=False,
            )
        )
