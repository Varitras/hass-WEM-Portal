"""The expert-parameter number entity (comfort layer over the write service).

The Home Assistant view half of the expert feature, split from expert_writer.py
so the protocol client stays free of the entity code and each half can be
type-checked on its own terms. Imported only when CONF_EXPERT_WRITE is on (see
number.py), which keeps curl_cffi - pulled transitively through the client - out
of the load path while the option is off; WemPortalExpertClient, the one
reference it still needs from the client, is imported inside the method that
uses it, the lazy shape tests/test_security.py enforces. The unique-id digest
comes from the light expert_options module, so building an entity pulls in no
client at all.
"""

import logging

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_EXPERT_NOTIFY_ON_SUCCESS,
    CONF_EXPERT_SLOT_ID_TEMPLATE,
    CONF_EXPERT_SLOT_NAME_TEMPLATE,
    CONF_EXPERT_WRITE,
    DOMAIN,
    EXPERT_SLOT_COUNT,
)
from .exceptions import ExpertOperationAborted, ParameterWriteError
from .expert_options import entityvalue_digest

_LOGGER = logging.getLogger(__name__)


def create_expert_number_entities(config_entry):
    """Build the configured expert number entities (comfort layer on top
    of the write service).

    number.py imports this module only when CONF_EXPERT_WRITE is on, which is
    what actually keeps it - and curl_cffi with it - out of the load path
    while the option is disabled. The check below is kept as the authoritative
    one for any other caller.

    Entities are built from the ten generic slots (name + entityvalue id).
    Empty slots are skipped; duplicate entityvalues are de-duplicated.
    """

    if not config_entry.options.get(CONF_EXPERT_WRITE, False):
        return []

    options = config_entry.options
    # Collect (name, entityvalue) pairs from the generic slots. A slot with
    # an id but no name gets a default name.
    configured_slots = []
    for slot in range(1, EXPERT_SLOT_COUNT + 1):
        entityvalue = (options.get(CONF_EXPERT_SLOT_ID_TEMPLATE % slot) or "").strip()
        if not entityvalue:
            continue
        name = (options.get(CONF_EXPERT_SLOT_NAME_TEMPLATE % slot) or "").strip()
        configured_slots.append((name or f"expert_parameter_{slot}", entityvalue))

    entities = []
    seen = set()
    for name, entityvalue in configured_slots:
        if entityvalue in seen:
            continue
        seen.add(entityvalue)
        entities.append(WemPortalExpertNumber(config_entry, name, entityvalue))
    return entities


# Placeholders for "the portal has not told us yet", not estimates of what a
# parameter looks like. Wide enough and fine enough that nothing a portal
# might offer is excluded before it has been asked - see the entity class for
# why excluding anything here is a lock rather than a label.
EXPERT_UNKNOWN_BOUND = 100000.0
# 0.01 rather than the 0.5 this started as: the heating curve is offered in
# 0.05 steps, so half-value granularity locked out four values in five. Being
# finer than any parameter needs costs nothing - while the bounds are
# placeholders the entity is a box, where the arrows are useless anyway and
# the value is typed - whereas being too coarse is a lock on the very write
# that would fetch the real step.
EXPERT_UNKNOWN_STEP = 0.01


class WemPortalExpertNumber(RestoreNumber):
    """Writable expert parameter as a number entity.

    Value updates only on writes (the verified post-write state) or
    restore after restart - by design no periodic polling.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    # No unit. The portal's edit form carries a list of allowed values and
    # nothing else - it never says what they measure. Every slot used to
    # be published as a percentage, so a flow temperature, a curve slope
    # and a delay all read as `%` and were recorded under that unit.
    #
    # The three below hold only until the parameter has been read,
    # written or restored once; all three are then replaced by what the
    # portal actually offers.
    #
    # Until then they must EXCLUDE NOTHING, which is why they are absurd
    # rather than plausible. Home Assistant validates a call against the
    # published range before the integration is asked, so a slot claiming
    # 0-100 refuses a valid 350 - and the write that would have fetched
    # the real range is precisely what it refuses. With the hourly read
    # off by default and nothing to restore on a fresh install, that lock
    # never opens. A step of 1 does the same to every half-value.
    #
    # Nothing is lost by being permissive here: what the portal will not
    # accept is caught where it is known, in write_parameter, against the
    # form's own option list.
    _attr_native_step = EXPERT_UNKNOWN_STEP
    _attr_native_min_value = -EXPERT_UNKNOWN_BOUND
    _attr_native_max_value = EXPERT_UNKNOWN_BOUND
    _attr_icon = "mdi:speedometer"

    def __init__(self, config_entry, name, entityvalue):
        self._config_entry = config_entry
        self._entityvalue = entityvalue
        # Both are only known once the portal has been read; neither is
        # restored, because a stored copy would outlive the reading that
        # produced it and there is nothing to check it against.
        self._portal_text = None
        self._factory_default = None
        # `name` comes from the slot's name field (or a default like
        # "expert_parameter_3"); use it as the stable object_id source
        # but show a readable friendly name, consistent with
        # has_entity_name on the other platforms.
        self._attr_name = name.replace("_", " ").title()
        # No translation_key: slot names are free text with no matching
        # translation entry, so the friendly name above is used directly
        # (setting an unresolvable translation_key would only log warnings).
        # unique_id carries a DIGEST of the entityvalue, not the raw id:
        # unique_ids are persisted in .storage/core.entity_registry, and
        # the installation-specific raw id shouldn't leak into files
        # people share for debugging. number.py migrates entities from
        # the old raw-id format on setup, preserving entity_id/history.
        self._attr_unique_id = (
            f"{config_entry.entry_id}:expert:{entityvalue_digest(entityvalue)}"
        )
        self._attr_native_value = None
        # Guards against a second write starting while one is still
        # running - the frontend can send two in quick succession.
        self._write_in_progress = False
        # Set once the entity is on its way out, so a queued write can
        # bail out before it opens a portal session. See
        # async_will_remove_from_hass for why cancelling is not enough.
        self._removed = False

    async def async_added_to_hass(self):
        """Restore what the last run knew, and join the poll.

        Joining HERE rather than at platform setup is what makes the
        user's decision stick. An entity disabled in the registry is
        built and handed over like any other and simply never added,
        so a list collected at setup went on asking the portal for its
        id every cycle - a login and a form read against an account
        the portal blocks after 10,000 requests - and could raise a
        repair issue about a parameter nobody is looking at.
        """
        await super().async_added_to_hass()
        self._config_entry.runtime_data.expert.attach_entity(self)
        last = await self.async_get_last_number_data()
        if last is not None:
            self._restore_from(last)

    def _apply_state(self, state):
        """Take value, range and step from a form the portal just showed.

        Both callers land here - the periodic read and the verify after a
        write - and each carried its own copy of this before. That is how
        the step came to be updated by neither: it was added to the class
        as a fixed 1 and there were two places to remember it in.
        """
        self._attr_native_value = state.current
        if state.min_value is not None:
            self._attr_native_min_value = state.min_value
        if state.max_value is not None:
            self._attr_native_max_value = state.max_value
        if state.step is not None:
            self._attr_native_step = state.step
        if state.portal_text is not None:
            self._portal_text = state.portal_text
        if state.factory_default is not None:
            self._factory_default = state.factory_default

    def _restore_from(self, last):
        """Take back the stored VALUE and nothing else.

        A stored range once came back too, and that is a trap in slow
        motion: Home Assistant validates a write against the published
        bounds before this integration is asked, and a heating
        parameter's limits can depend on other settings - so a range
        restored from last month can exclude exactly the value whose
        write would have fetched the current one. The in-session refusal
        correction cannot reach that case; it needs the write to arrive,
        and where old and new range do not overlap, it never does.

        The bounds therefore stay the permissive placeholders until the
        portal itself has answered - a read, a write or the auto-poll.
        The price, documented in the README and CHANGELOG: after a
        restart the parameter is a typing box, not a slider, until then.
        """
        if last.native_value is not None:
            self._attr_native_value = last.native_value

    @property
    def extra_state_attributes(self):
        """What the portal says that a number on its own cannot.

        `portal_value` is the dialog's own wording for the current
        selection. It repeats the value on an ordinary parameter, but it
        is the only readable answer where a special value is set: "Aus" is
        not a point on the scale, so the state itself goes unknown.

        `factory_default` is what the parameter left the factory with, as
        the portal states it. Home Assistant cannot colour a number that
        differs from its default - a number entity has no such option -
        but a dashboard card can compare against this and do it.
        """
        attributes = {}
        if self._portal_text is not None:
            attributes["portal_value"] = self._portal_text
        if self._factory_default is not None:
            attributes["factory_default"] = self._factory_default
        return attributes or None

    @property
    def mode(self) -> NumberMode:
        """A box while the range is a placeholder, a slider once it is real.

        Derived rather than stored, because the answer is a function of the
        bounds and those are updated in two places - a stored copy is how
        the step came to be updated by neither.

        The placeholder range spans 200000, where a slider is useless, so
        the value is typed. Once the portal has stated the real range that
        reverses: a box keeps its spinner arrows, and every arrow click is
        its own write that waits on the portal. A slider sends one value
        when the drag ends.
        """
        bounds_are_known = (
            self._attr_native_min_value != -EXPERT_UNKNOWN_BOUND
            and self._attr_native_max_value != EXPERT_UNKNOWN_BOUND
        )
        return NumberMode.AUTO if bounds_are_known else NumberMode.BOX

    @property
    def entityvalue(self):
        """The portal entityvalue hex ID this entity reads/writes."""
        return self._entityvalue

    @callback
    def apply_read_state(self, state):
        """Update this entity from a periodic read result (ExpertParameterState).

        Called by the hourly auto-poll after reading the parameter in a
        shared session. Updates the value and the live device range, then
        writes HA state. A no-op if state is None (read failed for this
        id) or while a write is in flight - a poll that started before
        the write carries the pre-write value, and applying it would
        briefly overwrite the freshly verified one.
        """
        if state is None:
            return
        if not self._is_in_home_assistant():
            return
        if self._write_in_progress:
            _LOGGER.debug(
                "Discarding poll result for %s: a write is in progress.",
                self._attr_name,
            )
            return
        self._apply_state(state)
        self.async_write_ha_state()

    @callback
    def forget_value(self):
        """Stop showing a value no read can confirm any more.

        This entity restores its last value after a restart and is no
        coordinator row, so no ageing pass elsewhere reaches it: once
        the auto-poll stops answering, the dashboard keeps the last
        number that worked with nothing behind it. Only the value goes,
        the rule the api and scrape paths follow.
        """
        if not self._is_in_home_assistant():
            return
        self._attr_native_value = None
        self.async_write_ha_state()

    def _is_in_home_assistant(self) -> bool:
        """Whether Home Assistant actually took this entity.

        An entity disabled in the registry is CONSTRUCTED like every
        other one and handed to the auto-poll controller, and Home
        Assistant then does not add it - so it has no `hass`, and
        publishing state for it raises. The poll applies its result to
        every configured parameter, so that happened once per cycle,
        forever, for a parameter the user had deliberately switched off.

        On the write path the damage is worse than noise: the value has
        reached the heating system by the time the state is published, so
        the raise reads like a failed write and invites a retry that
        writes twice.
        """
        return self.hass is not None

    async def async_set_native_value(self, value: float) -> None:
        """Write the value and wait for the portal to confirm it.

        The write used to run as a background task so the call returned
        at once, with the outcome going to a notification and the log.
        That told every caller the write had succeeded, whatever
        happened: an automation could carry on as if the heating had been
        set. Home Assistant's rule for entity methods is the opposite -
        a communication failure raises HomeAssistantError - and the
        domain service already worked that way, so the two disagreed
        about the same write.

        Nothing blocks but the caller. The portal work runs in an
        executor thread either way, so the event loop, the sensor poll
        and every other entity are unaffected; a second expert operation
        is refused outright rather than queued, as before.
        """
        from .models import raise_if_not_writable

        raise_if_not_writable(
            self._config_entry, self._attr_name or str(self._attr_unique_id)
        )
        if self._write_in_progress:
            raise HomeAssistantError(
                f"{self._attr_name}: a write is already in progress, please wait."
            )
        self._write_in_progress = True
        await self._async_write(value)

    async def async_will_remove_from_hass(self) -> None:
        """Stop an in-flight write as far as that is actually possible.

        The portal call runs in an executor thread, and nothing here can
        cancel a thread - the same distinction API_LOCK_TIMEOUT_SECONDS
        documents for the poll lock. A request already on the wire
        finishes, writing a heating parameter with the credentials of an
        entry being torn down. What is left is a cooperative stop:
        `_removed` is checked before the portal session is opened and
        again by the client after the login and before the writing
        request, so a write that has not reached the portal yet - the
        realistic case, since teardown and the executor race - gives up.

        Deliberately does not wait for a write in flight: that would hold
        the whole unload for as long as the portal takes, and waiting
        cannot abort the request anyway.
        """
        self._removed = True
        # And leave the poll, with this id's bookkeeping. Disabling an
        # entity in the registry lands here too, and it is a decision
        # about the parameter: a failure streak and a repair issue left
        # standing would outlive the thing they are about, and
        # re-enabling would start from a count nobody can see.
        self._config_entry.runtime_data.expert.detach_entity(self)
        await super().async_will_remove_from_hass()

    def _raise_if_removed(self) -> None:
        """Abort gate handed to the portal client.

        Called from the worker thread, so it must only read state - it is
        a plain flag check on purpose.
        """
        if self._removed:
            raise ExpertOperationAborted(
                f"{self._attr_name}: the entity was removed before the "
                "write reached the portal"
            )

    async def _async_write(self, value: float) -> None:
        """Do the write and wait for the portal to confirm it."""
        from .expert_options import expert_client_options
        from .expert_writer import WemPortalExpertClient

        client_options = expert_client_options(self._config_entry.options)

        def _do_write():
            # Checked here AND handed to the client, which re-checks it
            # after the login and directly before the writing request.
            # One check at the top only covered a job the thread pool had
            # not started yet; an unload during the login (several
            # requests, seconds) still ran through to the write.
            self._raise_if_removed()
            # Shared per-account lock: only one expert portal operation
            # (this entity, the service, or the auto-poll) may run at a
            # time, so concurrent writes/reads don't collide on the same
            # heating parameter or open parallel portal sessions.
            lock = self._expert_lock()
            if lock is not None and not lock.acquire(blocking=False):
                raise HomeAssistantError(
                    "Another expert operation is in progress for this "
                    "account; try again shortly."
                )
            try:
                client = WemPortalExpertClient(
                    self._config_entry.data.get(CONF_USERNAME),
                    self._config_entry.data.get(CONF_PASSWORD),
                    cooldown_check=self._cooldown_check(),
                    cooldown_activate=self._cooldown_activate(),
                    cookie_jar=self._cookie_jar(),
                    abort_check=self._raise_if_removed,
                    **client_options,
                )
                return client.write_parameter(self._entityvalue, value)
            finally:
                if lock is not None:
                    lock.release()

        try:
            state = await self.hass.async_add_executor_job(_do_write)
        except ExpertOperationAborted as exc:
            # Somebody IS waiting on this. The write is awaited now, so
            # the service call or automation that asked for it is still
            # holding on - and returning quietly told it the heating had
            # been set when nothing reached the portal at all. That the
            # configuration went away is a reason for the write not to
            # happen, not a reason to say it did.
            _LOGGER.debug("Expert write for %s stopped: %s", self._attr_name, exc)
            raise HomeAssistantError(
                f"Setting {self._attr_name} was stopped: {exc}"
            ) from exc
        # skipcq: PYL-W0706 - takes the range on board, then re-raises
        except ParameterWriteError as exc:
            # The write failed, and this failure knows what the portal
            # currently offers. Taking that on board is what stops the
            # next attempt failing the same way - a heating parameter's
            # limits can depend on other settings, so a range read weeks
            # ago need not still hold, and the auto-poll that would notice
            # is off by default.
            #
            # It cannot rescue a range that is wildly wrong: Home
            # Assistant validates against min/max before this entity is
            # asked, so a value outside the PUBLISHED range never gets
            # here. This corrects the overlapping case, which is the one
            # that occurs.
            #
            # Before the catch-all below, but also before HomeAssistantError:
            # WemPortalError derives from it, so the shorter clause would
            # take this one first and leave the range where it was.
            if exc.state is not None:
                self._apply_state(exc.state)
                self.async_write_ha_state()
            raise
        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except HomeAssistantError:
            # Already the right kind and already worded for the user -
            # the busy-lock refusal comes through here.
            raise
        except Exception as exc:
            _LOGGER.error("Expert write failed for %s: %s", self._attr_name, exc)
            raise HomeAssistantError(
                f"Setting {self._attr_name} to {value} failed: {exc}"
            ) from exc
        finally:
            self._write_in_progress = False

        # Through the controller, not applied here: a write the portal read
        # back is the strongest answer an id can give, and the controller is
        # what keeps the failure tally, the notified marker and the repair
        # issue that answer has to take down. Applying the state directly
        # left all three standing, so the next run of failures found the id
        # already reported and never emptied the confirmed value again.
        self._config_entry.runtime_data.expert.apply_verified_write(
            self.entityvalue, state
        )
        _LOGGER.info(
            "Expert parameter %s set and verified: %s",
            self._attr_name,
            state.current,
        )
        self._notify_success(f"{self._attr_name} set to {state.current}.")

    def _notify_success(self, message: str) -> None:
        """Report a successful write, if the user asked to be told.

        Off by default (CONF_EXPERT_NOTIFY_ON_SUCCESS); the success is
        logged regardless. There is no failure counterpart any more: a
        failed write raises, so the caller hears about it where it can
        act on it rather than in a notification nobody is watching.
        """
        if not self._config_entry.options.get(CONF_EXPERT_NOTIFY_ON_SUCCESS, False):
            return
        self.hass.async_create_task(
            self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "WEM Portal expert write",
                    "message": message,
                    "notification_id": f"wemportal_expert_{self._attr_unique_id}",
                },
                blocking=False,
            )
        )

    def _entry_api(self):
        """The api of this entity's entry, or None once it is unloaded.

        One accessor instead of the same three lines in four places -
        each of which used to spell out the store key by hand.
        """
        data = getattr(self._config_entry, "runtime_data", None)
        return data.api if data is not None else None

    def _cooldown_check(self):
        """Cooldown gate for this entity's writes.

        Uses the EXPERT gate, like the service and the auto-poll do: it
        honours a genuine portal-wide rate limit AND the expert-only
        backoff. Previously this used the global check, so an entity
        write ignored an active expert backoff.
        """
        api = self._entry_api()
        return api.check_expert_cooldown if api is not None else None

    def _cooldown_activate(self):
        """Engage the EXPERT-ONLY backoff on a 403 from this write.

        Previously this returned the GLOBAL activation, so a single
        rejected slider write paused all sensor polling - the very
        behaviour the expert-only backoff was introduced to end. The
        service and the auto-poll always used the expert one; this call
        site was missed.
        """
        api = self._entry_api()
        return api.activate_expert_cooldown if api is not None else None

    def _cookie_jar(self):
        """Shared in-memory session cache, so entity writes reuse the
        web session instead of logging in every time (the login is the
        request the portal rejects most readily)."""
        api = self._entry_api()
        return api.expert_cookies if api is not None else None

    def _expert_lock(self):
        """Shared per-entry lock (only one expert portal op at a time)."""
        data = getattr(self._config_entry, "runtime_data", None)
        return data.expert.lock if data is not None else None

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": self._config_entry.title or "WEM Portal",
            "manufacturer": "Weishaupt",
        }
