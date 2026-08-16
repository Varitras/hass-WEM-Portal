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
from typing import TYPE_CHECKING, Any, Final

from .exceptions import AuthError, ForbiddenError, WemPortalError
from .models import Reading
from .translations import translate
from .utils import latest_statistics_entry

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


class WemPortalStatistics:
    """The energy statistics path - throttle, traffic and rows.

    What this half needs from the object it is mixed into, declared for the
    same reason as in transport.py: a mixin's dependencies on its host are
    otherwise invisible, and these are the seam the rebuild cut along.
    """

    if TYPE_CHECKING:
        data: dict[str, dict[str, Any]]
        modules: dict[str, Any]
        language: str
        last_statistics_fetch: float

        def make_api_call(self, url: str, **kwargs: Any) -> Any: ...

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
        return [
            str(device_id)
            for device_id in target_devices
            if str(device_id) in self.data and str(device_id) in self.modules
        ]

    def _statistics_group_name(self, group: dict[str, Any]) -> str:
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
            group_name = fallback_names.get(group_id, f"Energy {group_id}")  # type: ignore[arg-type]
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
            if isinstance(old_sensor, Reading) and old_sensor.value is not None:
                current_value = old_sensor.value
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
        self.data[device_id][f"{device_id}-{sensor_name}"] = Reading(
            friendly_name=group_name,
            parameter_id=sensor_name,
            unit=unit,
            value=current_value,
            platform="sensor",
            device_class="energy",
            state_class="total_increasing",
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
