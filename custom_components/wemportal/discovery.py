"""The parameter discovery half of the portal client.

Which devices and modules an installation has, and which parameters each
module offers: the device list, the per-module descriptions, when they are
due again, and how a module the portal will not describe is survived. Cut
along the same seam as transport.py, statistics.py and schedule.py - its
state (modules, data, the description stamps) lives on the ONE WemPortalApi
instance, so this is a move of code, not a change of object shape.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Final

import requests

from .exceptions import (
    AuthError,
    ExpiredSessionError,
    ForbiddenError,
    ServerError,
    WemPortalError,
)
from .mapper import forget_dropped_parameters
from .mobile_protocol import as_answer_dict, described_parameters
from .models import HeavyFetchTurns, ModuleRef
from .utils import failure_is_new, short_device_id

_LOGGER = logging.getLogger(__name__)

API_DEVICE_READ_URL: Final = "https://www.wemportal.com/app/Device/Read"

API_EVENT_TYPE_READ_URL: Final = "https://www.wemportal.com/app/EventType/Read"

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


def _description_key(device_id: str, values: dict[str, Any]) -> str:
    """Which module a reported description failure is about."""
    return f"description:{device_id}:{values.get('Index')}:{values.get('Type')}"


class WemPortalDiscovery:
    """Devices, modules and their parameter descriptions.

    What this half needs from the object it is mixed into, declared for the
    same reason as in transport.py: a mixin's dependencies on its host are
    otherwise invisible, and these are the seam the split cut along.
    """

    if TYPE_CHECKING:
        data: dict[str, dict[str, Any]]
        # Optional: None until discovery populates it - the host declares
        # it so, and the two halves must agree.
        modules: dict[str, Any] | None
        device_types: dict[str, Any]
        scraper_device_id: str | None
        _previous_scraper_keys: set[str] | None
        scraping_mapper: dict[Any, Any]
        heavy_fetch_turns: HeavyFetchTurns
        _reported_failures: dict[str, str]
        _first_cycle_done: bool

        def make_api_call(
            self,
            url: str,
            headers: Any = None,
            data: Any = None,
            do_retry: bool = True,
            delay: int = 5,
            retry_transport: bool = False,
        ) -> requests.Response: ...

        @staticmethod
        def _device_is_enabled(
            device_id: str, enabled_devices: list[str] | None
        ) -> bool: ...

        def _kept_fresh_by_the_scrape(self, row_name: str) -> bool: ...

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
            if self._parameters_can_be_read(device_id, enabled_devices)
            for module in modules.values()
        ]
        missing = any("parameters" not in module for module in due)
        stale = any(self._parameters_are_stale(module) for module in due)
        if not (missing or (stale and self._first_cycle_done)):
            return
        # A MISSING definition runs without asking for the turn: without it
        # there is nothing to read at all, and a claim it did not need would
        # only queue it behind the others as a head that never asks again.
        if not missing and not self.heavy_fetch_turns.claim("parameters"):
            return
        _LOGGER.info(
            "Reading parameter definitions from the portal (%s).",
            "some are missing" if missing else "the cached ones are due",
        )
        self.get_parameters(enabled_devices)

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
        # After the guard, which counts only the devices the portal names. The
        # scraper's own device is one it never names - the placeholder of an
        # install that began in `web` mode - and dropping it here blanked every
        # web row until the next scrape, after each transport reset.
        #
        # Only the rows the scrape wrote: the scraper's device can also be a
        # real one the list stopped naming, and its api and status rows have
        # nothing left to refresh them - "no fault" stood there for good.
        scraped = {
            key: row
            for key, row in (
                previously_known_readings.get(str(self.scraper_device_id)) or {}
            ).items()
            if key in (self._previous_scraper_keys or ())
        }
        if self.scraper_device_id and scraped:
            new_data.setdefault(str(self.scraper_device_id), scraped)

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
                short_device_id(device_id),
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
                short_device_id(device_id),
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
                short_device_id(device_id),
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
                short_device_id(device_id),
                values["Index"],
                values["Type"],
                age / 3600,
            )
            return False
        _LOGGER.debug(
            "Re-reading parameters for device %s, index %s, type %s "
            "(cached list is %.1f h old).",
            short_device_id(device_id),
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
        forget_dropped_parameters(
            self.data.get(device_id),
            values,
            parameters,
            lambda parameter_id: self.scraping_mapper.get(
                (device_id, key, parameter_id), []
            ),
            self._kept_fresh_by_the_scrape,
        )
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
        self._reported_failures.pop(_description_key(device_id, values), None)

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
                self._survive_a_failed_description(device_id, values, exc)
                continue
            self._store_module_description(device_id, key, values, response)

    def _survive_a_failed_description(
        self, device_id: str, values: dict[str, Any], exc: WemPortalError
    ) -> None:
        """Book a module the portal would not describe, or raise what the whole
        cycle must hear: a refusal of the account, or a list still missing. A
        failed refresh of a list that works raised too, and no value was read
        at all - every cycle, as nothing booked the attempt."""
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
                short_device_id(device_id),
            )
            raise exc
        if status_code == 400:
            self._note_undescribed_module(
                device_id, values, "the portal rejected the request", unsupported=True
            )
            return
        # A dead session arrives here as a plain portal error - a 401, or the
        # redirect to the login page - and asking the next module on it would
        # only repeat it. Known by its cause: `valid_login` also drops for
        # failures that say nothing about the session.
        session_gone = status_code == 401 or isinstance(
            exc.__cause__, ExpiredSessionError
        )
        refused_for_the_account = session_gone or isinstance(
            exc, (AuthError, ForbiddenError)
        )
        if refused_for_the_account:
            raise exc
        if "parameters" not in values:
            self._wait_for_a_first_description(device_id, values, exc)
            return
        # An empty list is a known answer too; what the portal said about the
        # module stands, only the timestamp moves.
        self._note_undescribed_module(
            device_id,
            values,
            str(exc),
            unsupported=values.get("description_refused", False),
        )

    def _wait_for_a_first_description(
        self, device_id: str, values: dict[str, Any], exc: WemPortalError
    ) -> None:
        """A module never described, whose description failed: asked again next
        cycle while the described ones are read, or the cycle fails when there
        is nothing to read at all.

        It raised whatever else was described, out of discovery before the
        device loop - so one new module the portal failed took every entity
        of the account down, for as long as the portal kept failing it.
        """
        if not any(
            "parameters" in module
            for modules in (self.modules or {}).values()
            for module in modules.values()
        ):
            raise exc
        if failure_is_new(
            self._reported_failures, _description_key(device_id, values), str(exc)
        ):
            _LOGGER.warning(
                "Module %s of device %s could not be described (%s). The other "
                "modules are read; it is asked again next cycle.",
                values.get("Name"),
                short_device_id(device_id),
                exc,
            )

    def _parameters_can_be_read(
        self, device_id: str, enabled_devices: list[str] | None
    ) -> bool:
        """Enabled and online: the one rule for whether a device's definitions
        are due AND for whether they are read. Apart, an offline device made
        the re-read due every cycle and spent the heavy turn on a read that
        then skipped it."""
        if not self._device_is_enabled(device_id, enabled_devices):
            return False
        return self.data.get(device_id, {}).get("ConnectionStatus") == 0

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
        for device_id in self.data:
            if not self._parameters_can_be_read(device_id, enabled_devices):
                continue
            _LOGGER.debug(
                "Fetching api parameters data for device %s", short_device_id(device_id)
            )
            self._discover_device_parameters(device_id)
