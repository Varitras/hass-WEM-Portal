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

import logging

import random
import threading
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from .const import (
    CONF_EXPERT_AUTO_POLL,
    CONF_EXPERT_POLL_INTERVAL,
    DEFAULT_EXPERT_POLL_INTERVAL_MINUTES,
    DOMAIN,
    MIN_EXPERT_POLL_INTERVAL_MINUTES,
)
from .exceptions import ExpertOperationAborted
from .expert_options import canonical_entityvalue, entityvalue_digest

_LOGGER = logging.getLogger(__name__)

# The issue_id stem of the per-parameter read-failure repair issue; the full
# id is "{entry_id}_{stem}_{digest}", entry-prefixed so async_remove_entry
# can clean it up by prefix (see tests/test_repairs.py).
EXPERT_POLL_FAIL_ISSUE = "expert_poll_fail"

# Fraction of extra, random delay added on top of the configured interval each
# cycle (0..20%). Jitter is added ONLY upwards, so the effective interval is
# always >= the user's setting - and thus never below the floor. The poll
# pattern is less regular without ever hitting the portal more often than
# configured.
JITTER_FRACTION = 0.20

# Consecutive misses before the user is told to check the configured id -
# and before the value that id last read stops being shown. One threshold
# for both because they answer the same question: after this many failures
# in a row, nothing about that parameter is worth asserting any more.
FAILURES_BEFORE_NOTIFYING = 3

# Consecutive cycles in which the read produced nothing at all before the
# values stop being shown. Two rather than three: the poll runs hourly, so
# three would be three hours of a number nobody confirmed - and unlike a
# single id failing, a dead batch says nothing about which parameter is at
# fault, so there is no per-id notification covering it either.
BATCH_FAILURES_BEFORE_VALUES_ARE_STALE = 2


def poll_interval_minutes(entry: ConfigEntry) -> int:
    """The configured poll interval, never below the floor and never a value
    the options flow let through in a shape int() chokes on."""
    interval_min = entry.options.get(
        CONF_EXPERT_POLL_INTERVAL, DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
    )
    try:
        return max(int(interval_min), MIN_EXPERT_POLL_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        return DEFAULT_EXPERT_POLL_INTERVAL_MINUTES


class ExpertBusy(Exception):
    """Another expert operation of this account holds the portal session."""


def read_expert_values(
    entry: ConfigEntry,
    api: Any,
    entityvalues: list[str],
    abort_check: Callable[[], None] | None = None,
    lock: threading.Lock | None = None,
) -> dict[str, Any]:
    """One shared portal session for every configured id.

    Runs in an executor thread - the expert client is blocking - and imports
    it there, which is what keeps curl_cffi off the setup path.

    Takes and releases the account lock ITSELF rather than being called
    with it held. Acquired on the event loop and released in the awaiting
    coroutine's `finally`, a cancellation - a reload, an unload, a timeout -
    freed the lock while this thread was still driving the portal, and the
    next operation of the same account opened a second session beside it.
    Owned here, the lock is held for exactly as long as the work is.
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
    if lock is not None and not lock.acquire(blocking=False):
        raise ExpertBusy("another expert operation of this account is in progress")
    try:
        return client.read_many(entityvalues)
    finally:
        if lock is not None:
            lock.release()


class ExpertController:
    """The expert path of one config entry: lock, entities, timer, tally."""

    def __init__(self) -> None:
        self.entities: list[Any] = []
        # Per-id consecutive misses. Only counted when a batch SUCCEEDED and
        # one id was missing from the answer - see apply_read.
        self.fail_counts: dict[str, int] = {}
        self.fail_notified: set[str] = set()
        # Consecutive cycles whose read produced nothing at all. Separate
        # from the per-id tally on purpose: a dead batch is not evidence
        # about any single id, but it is evidence about every VALUE.
        self._batch_failures = 0

        self._data: Any = None
        self._hass: HomeAssistant | None = None
        self._entry: ConfigEntry | None = None
        self._interval_min = DEFAULT_EXPERT_POLL_INTERVAL_MINUTES
        self._armed = False
        self._started = False
        self._stopped = False
        self._initial_task: Any = None
        self._unsubscribe: Any = None

    def bind(self, data: Any) -> None:
        """Hold the runtime store this controller belongs to."""
        self._data = data

    # --- wiring --------------------------------------------------------

    def setup_auto_poll(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
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

    @property
    def lock(self) -> threading.Lock | None:
        """The one expert lock of this ACCOUNT.

        A poll, an entity write and the domain service all drive the same
        Fachmann session, so they have to serialise - and this controller is
        per ENTRY. A legacy duplicate entry of one account is still allowed
        to load, and it built a second controller with a second lock, which
        serialises nothing. It lives with the rest of the per-account memory
        now (see models.AccountState).

        None before `bind`, which is the same answer the callers already
        handle: nothing to serialise against yet either.
        """
        from .models import account_state

        if self._data is None:
            return None
        return account_state(self._data.api.username).expert_lock

    @property
    def live_entities(self) -> list[Any]:
        """The entities Home Assistant currently has added.

        Every entry here got in through async_added_to_hass and leaves
        through async_will_remove_from_hass, so this is the list Home
        Assistant itself maintains rather than a guess about one. The
        guess it replaces was `entity.hass is not None`, which is only
        cleared when ADDING is aborted - an entity the user disables later
        keeps its reference, so it went on being polled.
        """
        return list(self.entities)

    def attach_entity(self, entity: Any) -> None:
        """One entity joins the poll, from async_added_to_hass."""
        if entity not in self.entities:
            self.entities.append(entity)
        if self._armed:
            self.start()

    def detach_entity(self, entity: Any) -> None:
        """One entity leaves, and its bookkeeping goes with it.

        The streak, the notification marker and the repair issue are
        statements about a parameter somebody is watching. Left behind,
        they outlive the entity and a re-enable starts from a count
        nobody can see.
        """
        if entity in self.entities:
            self.entities.remove(entity)
        entityvalue = entity.entityvalue
        self.fail_counts.pop(entityvalue, None)
        if entityvalue in self.fail_notified:
            self.fail_notified.discard(entityvalue)
            self._clear_read_failure_issue(entityvalue)

    def start(self) -> None:
        """Begin the timer chain. Idempotent - one chain per entry."""
        if self._started or not self._armed:
            return
        self._started = True
        # Set together in setup_auto_poll; start() only proceeds past the
        # guard above when _armed, which is set there too.
        assert self._hass is not None and self._entry is not None
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
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()
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

        TWO flags, because they are set at opposite ends of the teardown.
        `unloading` goes up first, before the platforms come down; `stop()`
        runs last, via async_on_unload. Reading only the second meant a poll
        that started just before an unload kept navigating the portal for the
        whole slow part in between - the exact window begin_unload() exists to
        close, and every other write gate already respects.
        """
        unloading = self._data is not None and self._data.unloading
        if self._stopped or unloading:
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
        assert self._hass is not None  # only scheduled after setup_auto_poll
        self._unsubscribe = async_call_later(self._hass, delay, self.poll)
        _LOGGER.debug(
            "Expert auto-poll: next read in %.1f min (base %d min + jitter).",
            delay / 60,
            self._interval_min,
        )

    async def poll(self, _now: Any = None) -> None:
        """One cycle: read every configured id in one session, apply, re-arm."""
        try:
            entities = self.live_entities
            entityvalues = [entity.entityvalue for entity in entities]
            if not entityvalues:
                return
            # Collision guard: if any entity write is in flight, skip this
            # cycle instead of opening a second concurrent portal session.
            # Reading in parallel could also briefly write a pre-write
            # (stale) value back into an entity right after its verified
            # write. The next scheduled poll picks things up again.
            if any(getattr(entity, "_write_in_progress", False) for entity in entities):
                _LOGGER.debug(
                    "Expert auto-poll: a write is in progress, skipping this cycle."
                )
                return
            # The store, not the entry: it says which api instance this
            # entry is using, the cooldown state travels with it, and it
            # survives the unload that drops runtime_data while this read is
            # still in flight.
            current_api = self._data.api

            assert self._hass is not None and self._entry is not None
            try:
                results = await self._hass.async_add_executor_job(
                    read_expert_values,
                    self._entry,
                    current_api,
                    entityvalues,
                    self._raise_if_stopped,
                    self.lock,
                )
            except ExpertBusy as exc:
                # The worker refused: another operation of this account is
                # driving the portal. Skipping is what this cycle did before,
                # the difference being who decides - the thread that will do
                # the work, not the coroutine that may be cancelled first.
                _LOGGER.debug("Expert auto-poll: %s, skipping this cycle.", exc)
                return
            except ExpertOperationAborted as exc:
                # Not a failure: the configuration this read belongs to is
                # gone. Feeding it through the counters below would blame
                # every configured id for an unload.
                _LOGGER.debug("Expert auto-poll stopped: %s", exc)
                return
            except Exception as exc:  # noqa: BLE001
                # Returns instead of falling through with an empty result:
                # the per-id counters below mean "the portal answered, but
                # not for this id". Feeding an outage through them would
                # blame every configured id for a problem that is not theirs.
                # The read itself never got anywhere - a web login that
                # failed, a session that broke. Counted like a dead batch:
                # the values are just as unconfirmed as when the portal
                # answers and every id comes back empty.
                _LOGGER.warning("Expert auto-poll read failed: %s", exc)
                self._register_batch_failure()
                return

            self.apply_read(results)
        finally:
            # Always reschedule the next run (with fresh jitter), even if this
            # cycle failed - a transient error must not stop future polls.
            self._schedule_next()

    def _note_batch_outcome(
        self, results: dict[str, Any], whole_batch_failed: bool
    ) -> None:
        """What this cycle says about the values as a whole.

        A different question from the per-id tally beside it: one outage is
        not evidence about any single parameter, but it is evidence about
        every value. Anything that answered clears the streak.
        """
        if any(state is not None for state in results.values()):
            self._batch_failures = 0
            return
        if whole_batch_failed:
            self._register_batch_failure()

    def _register_batch_failure(self) -> None:
        """One more cycle that produced no answer at all.

        The per-id tally deliberately skips these - one outage is not
        evidence about any single parameter - which left nothing happening
        for the values themselves. They are the last ones a read confirmed,
        and after this many cycles that is no longer a claim worth making.
        """
        self._batch_failures += 1
        # Exactly ON the threshold, not from then on. The count keeps rising
        # while the outage lasts and the condition stays true, so `>=` said
        # the same thing again every hour and rewrote the state of every
        # configured entity for a value that was already gone. Safe here in
        # a way it was not for the scraped rows: nothing refills an expert
        # value except a read that works, and that resets the count.
        if self._batch_failures != BATCH_FAILURES_BEFORE_VALUES_ARE_STALE:
            return
        _LOGGER.warning(
            "Expert auto-poll: %d cycles in a row produced no answer. The "
            "parameter values are no longer current and are shown as unknown "
            "rather than as the values they had then.",
            self._batch_failures,
        )
        for entity in self.live_entities:
            entity.forget_value()

    def apply_read(self, results: dict[str, Any]) -> None:
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
        failed = [
            entityvalue for entityvalue, state in results.items() if state is None
        ]
        whole_batch_failed = len(results) >= 2 and len(failed) == len(results)
        self._note_batch_outcome(results, whole_batch_failed)
        if whole_batch_failed:
            _LOGGER.warning(
                "Expert auto-poll: all %d configured parameter(s) failed to "
                "read this cycle. Treating that as one failed batch rather "
                "than %d bad ids; not counting it against them.",
                len(failed),
                len(failed),
            )

        for entity in self.live_entities:
            entityvalue = entity.entityvalue
            state = results.get(entityvalue)
            unreadable_id = entityvalue not in results
            counts_against_the_id = unreadable_id or (
                state is None and not whole_batch_failed
            )

            if counts_against_the_id:
                self.fail_counts[entityvalue] = self.fail_counts.get(entityvalue, 0) + 1
                if (
                    self.fail_counts[entityvalue] >= FAILURES_BEFORE_NOTIFYING
                    and entityvalue not in self.fail_notified
                ):
                    self.fail_notified.add(entityvalue)
                    self._report_read_failure(
                        entity, self.fail_counts[entityvalue], unreadable_id
                    )
                    # The value goes with the report, and for the same
                    # reason. Ageing used to hang off the batch counter
                    # alone, which needs at least two configured ids to
                    # apply and is cleared by any sibling that answers - so
                    # the two installations where a value most needed
                    # emptying were the two it never reached: a single
                    # configured parameter, and one broken id beside a
                    # working one. Guarded by fail_notified, so this happens
                    # once per streak rather than every cycle.
                    entity.forget_value()
            elif state is not None:
                self._note_the_id_answered(entityvalue)
            entity.apply_read_state(state)

    def _note_the_id_answered(self, entityvalue: str) -> None:
        """Everything a confirmed answer for one id undoes.

        Shared by the poll and by a verified write, because a write the
        portal read back is the strongest answer there is - stronger than a
        poll, which only asks. The write route used to update the entity and
        nothing else, so the repair issue stayed up for a parameter that had
        just demonstrably worked, and the id stayed in `fail_notified` - which
        is what decides whether the NEXT run of failures may empty the value.
        A parameter written once would then have kept its number for good.

        The batch tally goes too: it counts cycles that produced no answer at
        all, and this is one.
        """
        # Read BEFORE the discard below forgets it: only a streak that was
        # actually reported has an issue to take down.
        was_reported = entityvalue in self.fail_notified
        self.fail_counts.pop(entityvalue, None)
        self.fail_notified.discard(entityvalue)
        self._batch_failures = 0
        if was_reported:
            self._clear_read_failure_issue(entityvalue)

    def apply_verified_write(self, entityvalue: str, state: Any) -> None:
        """Show what a write read back on the entity holding that id.

        Both routes to the same parameter end in a portal read-back, and the
        entity route applies its own. The domain service had no way back to
        the entity and dropped the answer, so the same parameter set the same
        way showed the new value on one route and the old one on the other -
        until an auto-poll that is off by default, or a restart.

        Deliberately NOT routed through apply_read: that one reads an id
        MISSING from the batch as a failed read, so handing it this single
        result would count a miss against every other configured parameter
        and, after three writes, notify about ids that were never asked for.

        Compared canonically, like the allowlist that let this id through:
        a caller who spells it in another case addresses the same parameter,
        so the raw comparison found no entity and left it on the old value
        after a write the portal had already confirmed.
        """
        wanted = canonical_entityvalue(entityvalue)
        for entity in self.entities:
            if canonical_entityvalue(entity.entityvalue) == wanted:
                # Under the entity's own spelling, because that is the one
                # the tally and the issue id were built from.
                self._note_the_id_answered(entity.entityvalue)
                entity.apply_read_state(state)

    def _report_read_failure(
        self, entity: Any, failures: int, unreadable_id: bool
    ) -> None:
        """Raise a repair issue for a parameter that keeps not being read.

        A repairs entry rather than a notification: it is translatable, it
        lands where Home Assistant collects actionable problems, and the
        recovery path can take it back down again (see apply_read).

        Two separate translations, because they say only what is known. An
        id that never left the house can only be the configured value; an id
        that was requested and failed can be that OR the portal, and
        asserting the first sent people to check a setting that was correct.
        """
        digest = entityvalue_digest(entity.entityvalue)
        assert self._hass is not None and self._entry is not None
        if unreadable_id:
            async_create_issue(
                self._hass,
                DOMAIN,
                f"{self._entry.entry_id}_{EXPERT_POLL_FAIL_ISSUE}_{digest}",
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="expert_poll_unreadable_id",
                translation_placeholders={"name": entity.name},
            )
            return
        async_create_issue(
            self._hass,
            DOMAIN,
            f"{self._entry.entry_id}_{EXPERT_POLL_FAIL_ISSUE}_{digest}",
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="expert_poll_read_failures",
            translation_placeholders={
                "name": entity.name,
                "failures": str(failures),
            },
        )

    def _clear_read_failure_issue(self, entityvalue: str) -> None:
        """Take the repair issue down once its parameter reads again."""
        digest = entityvalue_digest(entityvalue)
        assert self._hass is not None and self._entry is not None
        async_delete_issue(
            self._hass,
            DOMAIN,
            f"{self._entry.entry_id}_{EXPERT_POLL_FAIL_ISSUE}_{digest}",
        )
