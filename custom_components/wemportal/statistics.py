"""The statistics half of the portal client (Umbau P9, K8).

Everything about the energy statistics groups: the hourly throttle, the
refresh-then-read request pair per group, the never-invent-a-zero rule and
the sensor rows they produce. Cut along the same seam as transport.py -
its state (last_statistics_fetch, data) lives on the ONE WemPortalApi
instance, so this is a move of code, not a change of object shape. The
pacing here is load-bearing: Weishaupt blocks by IP, and the golden file
freezes the exact traffic.
"""

import logging

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import requests
from homeassistant.components.sensor import SensorStateClass
from homeassistant.util import dt as dt_util

from .exceptions import AuthError, ForbiddenError, WemPortalError
from .models import HeavyFetchTurns, Reading
from .translations import translate
from .utils import (
    failure_is_new,
    failure_is_over,
    latest_statistics_entry,
    portal_list,
    short_device_id,
)

_LOGGER = logging.getLogger(__name__)

API_STATISTICS_READ_URL: Final = "https://www.wemportal.com/app/Statistics/Read"

API_STATISTICS_REFRESH_URL: Final = "https://www.wemportal.com/app/Statistics/Refresh"

# Energy statistics are daily aggregates - they don't need per-cycle
# refreshes. This caps how often they're refreshed.
STATISTICS_REFRESH_INTERVAL_SECONDS: Final = 3600  # 1 hour

# How long to wait before retrying when a statistics cycle failed for EVERY
# device. The rate-limit timestamp is deliberately set BEFORE the fetch (so a
# persistently failing portal can never be hammered), which would otherwise
# make a single failure cost a full refresh interval. Shortening the wait on
# failure keeps that protection while recovering sooner. Must stay well above
# the coordinator's scan interval so a failing portal is still approached at a
# calm pace.
STATISTICS_RETRY_INTERVAL_SECONDS: Final = 900  # 15 minutes

# Server-side status code returned by Statistics/Read for a statistics
# group that isn't valid for the queried module (ModuleType 7/Index 0).
# The refresh call lists such groups, but reading them is rejected with
# this code. It's an expected, harmless per-group condition - skipped
# quietly rather than logged as a warning on every startup.
WEM_INVALID_PARAMETER_STATUS: Final = 3001
# The portal's answer for an installation that is currently off the net.
# A state, not a fault: the heat pump is unreachable and the portal says
# so cleanly. Reported as a failure it filled a user's error log all day.
WEM_INSTALLATION_OFFLINE_STATUS: Final = 5001


def _statistics_key(device_id: str) -> str:
    """The bookkeeping key for one device's statistics failures."""
    return f"statistics:{device_id}"


def _statistics_group_key(device_id: str, group_id: Any) -> str:
    """The bookkeeping key for one statistics group's failures."""
    return f"statistics:{device_id}:group:{group_id}"


def statistics_row_key(device_id: str, group_id: Any) -> str:
    """Where one statistics group of a device lives in its data."""
    return f"{device_id}-Energy_{group_id}"


# In the language of the entity names. A group the portal leaves without a
# description gets one of these; kWh are energy, so the consumption groups
# are electricity consumption, not power.
_FALLBACK_GROUP_NAMES: Final = {
    "en": {
        1: "Heating Energy Yield",
        2: "Hot Water Energy Yield",
        3: "Cooling Energy Yield",
        4: "Total Energy Yield",
        5: "Heating Electricity Consumption",
        6: "Hot Water Electricity Consumption",
        7: "Cooling Electricity Consumption",
        8: "Total Electricity Consumption",
    },
    "de": {
        1: "Wärmemenge Heizen",
        2: "Wärmemenge Warmwasser",
        3: "Wärmemenge Kühlen",
        4: "Wärmemenge gesamt",
        5: "Stromverbrauch Heizen",
        6: "Stromverbrauch Warmwasser",
        7: "Stromverbrauch Kühlen",
        8: "Stromverbrauch gesamt",
    },
}
# What a group name ends in when it does not say energy yet - in German too,
# where "energy" never appeared and every name got the English word added.
_ENERGY_WORD: Final = {"en": "Energy", "de": "Energie"}


def is_statistics_row(device_id: str, key: str) -> bool:
    """Whether a row is one this fetch owns - see the device-level ageing,
    which must leave it to the hourly read that keeps it."""
    return key.startswith(statistics_row_key(device_id, ""))


def _start_of_the_day(entry: dict[str, Any]) -> datetime | None:
    """Midnight of the day a statistics entry reports, in Home Assistant's
    time zone - the portal dates its days without one.

    Each entry is one day's energy, not a meter reading. Home Assistant would
    read a `total_increasing` sensor as a meter and start a new cycle only on
    a drop below 90 % - and the portal does not drop at midnight: it ships
    the new day as null until it has a value, so two similar days in a row
    booked only their difference. The day itself is the cycle."""
    try:
        day = datetime.fromisoformat(str(entry.get("Date")))
    except ValueError:
        return None
    return day.replace(tzinfo=dt_util.get_default_time_zone())


def _state_class_for(day_started: datetime | None) -> str:
    """`total` where the day is known; without it there is no cycle to name,
    and a meter is the reading of last resort. The plain value, as a Reading
    carries its state class - the sensor compares it by value."""
    if day_started is None:
        return SensorStateClass.TOTAL_INCREASING.value
    return SensorStateClass.TOTAL.value


class WemPortalStatistics:
    """The energy statistics path - throttle, traffic and rows.

    What this half needs from the object it is mixed into, declared for the
    same reason as in transport.py: a mixin's dependencies on its host are
    otherwise invisible, and these are the seam the rebuild cut along.
    """

    if TYPE_CHECKING:
        data: dict[str, dict[str, Any]]
        # Optional: None until discovery populates it, which the device
        # filter below treats as "no devices to ask yet".
        modules: dict[str, Any] | None
        language: str
        # Optional, because "never fetched in this session" is a state the
        # gate below reads explicitly. Declared as a plain float, the one
        # thing this declaration exists to make visible was wrong.
        last_statistics_fetch: float | None
        # Which failures have already been announced, so a portal that
        # keeps timing out is said once rather than once per cycle.
        _reported_failures: dict[str, str]
        heavy_fetch_turns: HeavyFetchTurns

        # The host's transport half; the full signature so the three mixins
        # agree on it when merged into the one WemPortalApi.
        def make_api_call(
            self,
            url: str,
            headers: Any = None,
            data: Any = None,
            do_retry: bool = True,
            delay: int = 5,
            retry_transport: bool = False,
        ) -> requests.Response: ...

    def _note_group_failure(
        self, device_id: str, group_id: Any, exc: Exception
    ) -> None:
        """Same damping as the device level, one loop further in.

        The whole-device retry is fifteen minutes, so an undamped group
        warning repeated four times an hour per dead group - the flood the
        1.13.2 change quieted above this loop, still coming from beneath it.
        """
        key = _statistics_group_key(device_id, group_id)
        if failure_is_new(self._reported_failures, key, str(exc)):
            _LOGGER.info("Statistics group %s cannot be read: %s", group_id, exc)
        else:
            _LOGGER.debug("Statistics group %s still cannot be read: %s", group_id, exc)

    def _note_statistics_failure(self, device_id: str, exc: Exception) -> None:
        """Say a statistics failure at the volume it deserves.

        Two things are not worth a warning. An installation the portal
        reports as offline is a state, handled here the same way status 3001
        is a few lines up. And a failure that is still the same one as last
        cycle has already been said - repeating it is what turns a slow
        afternoon into a full error log.
        """
        if str(getattr(exc, "server_status", None)) == str(
            WEM_INSTALLATION_OFFLINE_STATUS
        ):
            _LOGGER.debug(
                "Device %s is offline right now, so it has no statistics to read "
                "(status %s).",
                short_device_id(device_id),
                WEM_INSTALLATION_OFFLINE_STATUS,
            )
            return
        key = _statistics_key(device_id)
        if failure_is_new(self._reported_failures, key, str(exc)):
            _LOGGER.info(
                "Statistics for device %s cannot be read: %s",
                short_device_id(device_id),
                exc,
            )
        else:
            _LOGGER.debug(
                "Statistics for device %s still cannot be read: %s",
                short_device_id(device_id),
                exc,
            )

    def _statistics_devices(self, enabled_devices=None) -> list[str]:
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
        # A device the status read found offline is not asked either: the
        # portal answers it with 5001, which counted as a failed cycle and
        # brought the read back every fifteen minutes instead of hourly.
        return [
            str(device_id)
            for device_id in target_devices
            if str(device_id) in self.data
            and str(device_id) in (self.modules or {})
            and self.data[str(device_id)].get("ConnectionStatus") in (None, 0)
        ]

    def _statistics_group_name(self, group: dict[str, Any]) -> str:
        """The display name for one statistics group: the portal's own
        description where it has one, a fixed fallback where it is blank."""
        group_id = group.get("GroupType")
        group_name = group.get("Description")
        energy = _ENERGY_WORD.get(self.language, _ENERGY_WORD["en"])
        if not group_name or group_name.strip() == "":
            fallback_names = _FALLBACK_GROUP_NAMES.get(
                self.language, _FALLBACK_GROUP_NAMES["en"]
            )
            return fallback_names.get(group_id, f"{energy} {group_id}")  # type: ignore[arg-type]
        translated_group = translate(self.language, group_name)
        if energy.lower() in translated_group.lower():
            return translated_group
        return f"{translated_group} {energy}"

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
        if latest_stat is None:
            return
        current_value = latest_stat.get("Value")
        day_started = _start_of_the_day(latest_stat)
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
            old_sensor = self.data.get(device_id, {}).get(
                statistics_row_key(device_id, group_id)
            )
            if isinstance(old_sensor, Reading) and old_sensor.value is not None:
                current_value = old_sensor.value
                day_started = old_sensor.last_reset
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

        # No data_type/module fields on purpose: the -1 placeholders they
        # used to carry existed only to fill the dict shape, and nothing
        # reads a module address off a statistics sensor.
        self.data[device_id][statistics_row_key(device_id, group_id)] = Reading(
            friendly_name=group_name,
            parameter_id=sensor_name,
            unit=unit,
            value=current_value,
            platform="sensor",
            device_class="energy",
            state_class=_state_class_for(day_started),
            last_reset=day_started,
        )

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

        group_types = portal_list(refresh_resp, "GroupTypeDescriptions")
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

                if failure_is_over(
                    self._reported_failures, _statistics_group_key(device_id, group_id)
                ):
                    _LOGGER.info("Statistics group %s is being read again.", group_id)
                self._store_statistics_group(
                    device_id, group_id, group_name, stats_resp
                )
                read += 1

            # skipcq: PYL-W0706 - shields the catch-all, not redundant
            except ForbiddenError:
                # The portal is refusing this IP, which is not a fact about
                # this group. Every remaining group would take the same
                # answer - make_api_call's cooldown check now returns it
                # without a request, so the cost is not traffic but a warning
                # per group about one refusal, and a final message blaming
                # the groups rather than the block. Let it out instead: the
                # coordinator has a handler for exactly this.
                raise
            # skipcq: PYL-W0706 - shields the catch-all, not redundant
            except AuthError:
                # Same shape as the refusal above, and the same answer. The
                # session can expire mid-cycle, and transport then logs in
                # again from inside whatever request noticed - so a rejected
                # login surfaces HERE, in the middle of a partial read,
                # rather than at the top of the cycle. Kept, it costs twice:
                # the coordinator's consecutive-failure count is reset
                # instead of raised, so the reauth dialog stays one cycle
                # further away, and every request after this one goes out on
                # the dead session and spends another refused login finding
                # that out.
                raise
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
                    self._note_group_failure(device_id, group_id, exc)

        if failed and not read:
            raise WemPortalError(
                f"Every statistics group of device {short_device_id(device_id)} "
                "failed to read "
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

        Monotonic, not wall clock: the stamp lives in AccountState, which
        dies with the process, and a wall clock corrected forward - NTP
        right after a boot - would read every stamp as an hour old and
        release the guard for free.
        """
        now = time.monotonic()
        if (
            self.last_statistics_fetch is not None
            and (now - self.last_statistics_fetch) < STATISTICS_REFRESH_INTERVAL_SECONDS
        ):
            return
        # Before the stamp: a turn given to another heavy fetch leaves the
        # statistics due for the next cycle.
        if not self.heavy_fetch_turns.claim("statistics"):
            return

        self.last_statistics_fetch = now
        _LOGGER.debug("Fetching statistics data")

        # Track per-device outcomes so a completely failed cycle can retry
        # sooner. Only the outer (per-device) call counts: individual
        # statistics groups are routinely rejected (status 3001) for modules
        # they don't apply to, which is expected and not a failure.
        attempted = 0
        succeeded = 0

        try:
            for device_id in self._statistics_devices(enabled_devices):
                attempted += 1
                try:
                    self._fetch_device_statistics(device_id)
                    succeeded += 1
                    if failure_is_over(
                        self._reported_failures, _statistics_key(device_id)
                    ):
                        _LOGGER.info(
                            "Statistics for device %s are being read again.",
                            short_device_id(device_id),
                        )
                # skipcq: PYL-W0706 - shields the catch-all, not redundant
                except AuthError:
                    # An account is one login, so a refused one says nothing
                    # about this device - and every device after it would spend
                    # another login attempt finding that out.
                    raise
                # skipcq: PYL-W0706 - shields the catch-all, not redundant
                except ForbiddenError:
                    # Same shape, and the shield the group loop above is missing
                    # its other half of: it lets a refusal out "because the
                    # coordinator has a handler for exactly this", and the
                    # catch-all below caught it again one frame further up. The
                    # block was then logged as this device's statistics problem,
                    # every remaining device was walked into it, and the
                    # coordinator never learned the network is refused.
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Broad: one device's statistics failing must not stop
                    # the others. `succeeded` stays unincremented, which is
                    # what the retry back-dating below reads.
                    self._note_statistics_failure(device_id, exc)
        finally:
            # Every attempted device failed: back-date the timestamp so the
            # next cycle retries after the shorter retry interval rather than
            # waiting a full refresh interval. In a `finally` so a failure that
            # leaves this method - a re-raised AuthError/ForbiddenError, or the
            # BaseException PollDeadlineExceeded - shortens the retry on the way
            # out too, instead of leaving the full-interval stamp: a 403 arms a
            # 15-minute cooldown, and the old placement kept statistics locked
            # ~45 minutes past its end. The guard itself stays intact - a portal
            # that keeps failing is still only asked once per retry interval.
            if attempted and not succeeded:
                self.last_statistics_fetch = now - max(
                    0,
                    STATISTICS_REFRESH_INTERVAL_SECONDS
                    - STATISTICS_RETRY_INTERVAL_SECONDS,
                )
                _LOGGER.debug(
                    "Statistics failed for all %d device(s); retrying in ~%d min "
                    "instead of %d min.",
                    attempted,
                    STATISTICS_RETRY_INTERVAL_SECONDS // 60,
                    STATISTICS_REFRESH_INTERVAL_SECONDS // 60,
                )
