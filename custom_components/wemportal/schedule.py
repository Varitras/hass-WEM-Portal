"""The weekly-programme (heating schedule) half of the portal client.

Everything about the per-device CircuitTimes fetch: the hourly per-programme
throttle, the refresh-then-read request pair, recognising a programme two ways
the portal types it, and dropping stale detail when a refresh fails. Cut along
the same seam as transport.py and statistics.py - its state
(_last_circuit_times_fetch, data, modules) lives on the ONE WemPortalApi
instance, so this is a move of code, not a change of object shape. The pacing
here is load-bearing: Weishaupt blocks by IP, and the mutation gates freeze the
exact traffic.
"""

import logging

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import requests

from .const import WemDataType
from .exceptions import AuthError
from .models import ModuleRef, Reading
from .translations import friendly_name_mapper, translate
from .utils import looks_like_schedule, portal_list, week_carries_a_programme

_LOGGER = logging.getLogger(__name__)

API_CIRCUIT_TIMES_READ_URL: Final = "https://www.wemportal.com/app/CircuitTimes/Read"

API_CIRCUIT_TIMES_REFRESH_URL: Final = (
    "https://www.wemportal.com/app/CircuitTimes/Refresh"
)

# Heating schedules rarely change - only through the WEM Portal app - so
# refetching one on every coordinator cycle is load for nothing.
CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS: Final = 3600  # 1 hour

# How long to wait before retrying a schedule that failed. Back-dating the
# throttle stamp on failure shortens the wait to this without ever hammering a
# failing portal - the same shape as the statistics retry.
CIRCUIT_TIMES_RETRY_INTERVAL_SECONDS: Final = 900  # 15 minutes


def _schedule_throttle_key(
    device_id: str, module: Mapping[str, Any], parameter_id: str
) -> tuple[Any, ...]:
    """What identifies one programme for the hourly refresh throttle.

    The module is part of it. Two heating circuits are two modules of one
    type sharing one parameter catalogue, so the same programme id appears
    twice on a device - and keyed without the module, the first circuit
    fetched stamped the key and the second was never due again, on any cycle.
    Built here rather than at the two call sites: a key spelled out in two
    places is one that eventually disagrees with itself.
    """
    return (
        device_id,
        ModuleRef(module_index=module["Index"], module_type=module["Type"]),
        parameter_id,
    )


class WemPortalSchedule:
    """The weekly-programme path - throttle, request pair and detail rows.

    What this half needs from the object it is mixed into, declared for the
    same reason as in transport.py and statistics.py: a mixin's dependencies
    on its host are otherwise invisible, and these are the seam the rebuild
    cut along.
    """

    if TYPE_CHECKING:
        data: dict[str, dict[str, Any]]
        # Optional: None until discovery populates it, which the fetch below
        # treats as "no modules to walk yet".
        modules: dict[str, Any] | None
        language: str
        scraping_mapper: dict[Any, Any]
        _last_circuit_times_fetch: dict[tuple[Any, ...], float]

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

    def _schedule_row_key(
        self, device_id: str, module: Mapping[str, Any], parameter_id: str
    ) -> str:
        """Where a programme's reading actually lives: the scraped row the
        value read merged it into, if any, else its own key.

        In `both` mode the value read may have merged this programme into a
        scraped row, and all three schedule steps have to find it there:
        recognising one reads its value, the detail fetch writes onto its row,
        the failure drop clears that row's stale detail. Keyed on the
        reconstructed own key, recognition missed a 3.1.3.0 programme's JSON
        value so its fetch never ran, the read built a second detail-carrying
        row under a key no entity is made from, and the drop cleared nothing.
        Same device-scoped merge map _clear_unanswered follows.
        """
        own_key = f"{module['Name']}-{parameter_id}"
        module_ref = ModuleRef(module_index=module["Index"], module_type=module["Type"])
        merged = self.scraping_mapper.get((device_id, module_ref, parameter_id))
        return merged[0] if merged else own_key

    def _is_schedule_parameter(
        self,
        device_id: str,
        module: Mapping[str, Any],
        parameter_id: str,
        parameter_data: Mapping[str, Any],
    ) -> bool:
        """Whether this parameter is one of the portal's weekly programmes.

        Two ways the portal types one, and keying on the declared type alone
        meant the whole fetch never ran on a 3.1.3.0 portal: there every
        programme is DataType 2 with a JSON object in the value, and the
        fetch - the only path that asks the DEVICE for its schedule rather
        than reading the portal's stored copy - sat unused. It did not fail;
        it was never entered, which is why nothing about it appeared in any
        log.
        """
        row = self.data.get(device_id, {}).get(
            self._schedule_row_key(device_id, module, parameter_id)
        )
        row_value = row.value if isinstance(row, Reading) else None
        return parameter_data.get(
            "DataType"
        ) == WemDataType.PROGRAM or looks_like_schedule(row_value)

    def _schedule_is_due(
        self, device_id: str, module: Mapping[str, Any], parameter_id: str
    ) -> bool:
        """Whether this programme may be asked for again yet.

        Heating schedules rarely change - only through the WEM Portal app
        directly, since this integration shows them read-only - so refetching
        one on every coordinator cycle is load for nothing.

        Monotonic, and a missing key is "never fetched" rather than zero -
        AccountState says why each of those matters.
        """
        key = _schedule_throttle_key(device_id, module, parameter_id)
        last_fetch = self._last_circuit_times_fetch.get(key)
        if last_fetch is None:
            return True
        return time.monotonic() - last_fetch >= CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS

    def _record_schedule_attempt(
        self,
        device_id: str,
        module: Mapping[str, Any],
        parameter_id: str,
        attempted_at: float,
        fetched: bool,
    ) -> None:
        """Book the ATTEMPT, however it ended.

        Written only after a SUCCESS, as it once was, the interval guard never
        engages for a schedule that keeps failing: every coordinator cycle
        spends two more requests on it, at a portal that is already failing,
        against an IP the portal blocks past 10,000 requests per 12 hours.
        Back-dated rather than blocked outright when it did not work out, so
        one bad cycle does not cost a full hour either. Same shape and same
        reasoning as get_statistics().

        A failure also DROPS the row's stale detail attributes. The sensor
        prefers CircuitTimesDay over the raw value, so detail fetched last
        week kept overruling a newer raw plan for as long as the refresh
        failed - the existing JSON fallback takes over once the detail is
        gone, and the next successful refresh puts it back.
        """
        key = _schedule_throttle_key(device_id, module, parameter_id)
        if fetched:
            self._last_circuit_times_fetch[key] = attempted_at
            return
        self._last_circuit_times_fetch[key] = attempted_at - max(
            0,
            CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS
            - CIRCUIT_TIMES_RETRY_INTERVAL_SECONDS,
        )
        row = self.data.get(device_id, {}).get(
            self._schedule_row_key(device_id, module, parameter_id)
        )
        if isinstance(row, Reading) and row.circuit_times_day is not None:
            row.circuit_times_day = None
            row.possible_values = None
            _LOGGER.debug(
                "Schedule %s: refresh failed, dropping the stale detail so "
                "the raw plan shows instead.",
                parameter_id,
            )

    def _read_one_schedule(
        self, device_id: str, module: Mapping[str, Any], parameter_id: str
    ) -> bool:
        """Ask the device for one programme and store what it reports.

        Returns whether a schedule actually came back; the caller books the
        attempt either way.
        """
        module_index = module.get("Index")
        module_type = module.get("Type")
        address = {
            "DeviceID": int(device_id),
            "ModuleIndex": module_index,
            "ModuleType": module_type,
            "ParameterID": parameter_id,
        }

        job_resp = self.make_api_call(
            API_CIRCUIT_TIMES_REFRESH_URL, data=address, do_retry=True
        ).json()
        job_id = job_resp.get("JobID")
        if job_id is None:
            return False

        time.sleep(2)  # Give backend time to build the schedule payload
        schedule_resp = self.make_api_call(
            API_CIRCUIT_TIMES_READ_URL,
            data={**address, "JobID": job_id},
            do_retry=True,
        ).json()

        # What the portal actually delivered, before any of this counts as a
        # successful read. `{}` and `{"Status": 3}` are both answers it gives,
        # and both used to be stored and stamped as a fresh schedule: the hour
        # of throttle was spent, the sensor threw the empty week away and fell
        # back to the raw plan, and - since the ageing pass learned to exempt a
        # programme "while the fetch still feeds it" - an empty list read as
        # being fed. Not a list, or an empty one, is a failed read.
        days = (
            schedule_resp.get("CircuitTimesDay")
            if isinstance(schedule_resp, dict)
            else None
        )
        # The same question the ageing exemption asks, which is the point of
        # sharing it: a week of bare days renders to nothing, so storing it
        # and stamping the read as successful bought an hour of throttle for
        # an answer the sensor throws away - and then the exemption read that
        # very list as proof something was still feeding the row.
        if not week_carries_a_programme(days):
            _LOGGER.debug(
                "Schedule %s: the portal answered without a usable week; "
                "treating it as a failed read rather than an empty schedule.",
                parameter_id,
            )
            return False

        sensor_name = self._schedule_row_key(device_id, module, parameter_id)
        row = self.data[device_id].get(sensor_name)
        if not isinstance(row, Reading):
            row = Reading(
                friendly_name=translate(
                    self.language, friendly_name_mapper(parameter_id)
                ),
                parameter_id=parameter_id,
                unit=None,
                value="Active",
                data_type=WemDataType.PROGRAM,
                module_index=module_index,
                module_type=module_type,
                platform="sensor",
                icon="mdi:calendar-clock",
            )
            self.data[device_id][sensor_name] = row

        row.circuit_times_day = days
        row.possible_values = portal_list(schedule_resp, "PossibleValues")
        # The value is NOT touched. This fetch adds detail to a row the value
        # read already filled; writing "Active" over it replaced a readable
        # week with a placeholder once an hour, until the next cycle put the
        # programme back. Only a row that did not exist gets the placeholder,
        # above - there the fetch is the only source there is.
        return True

    def _read_and_record_one_schedule(
        self, device_id: str, module: Mapping[str, Any], parameter_id: str
    ) -> None:
        """Read one weekly programme, and stamp the attempt either way.

        The stamp is what the throttle reads, so it has to be written whether
        the read worked or not - otherwise a programme that fails every time
        is retried every cycle, which is the traffic the throttle exists to
        prevent.
        """
        attempted_at = time.monotonic()
        fetched = False
        try:
            fetched = self._read_one_schedule(device_id, module, parameter_id)
        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except AuthError:
            # A refused login is not one programme failing, and the rest
            # would each spend another one; see AuthError.
            raise
        except Exception as exc:  # noqa: BLE001
            # Broad: one heating program failing is not a reason to skip the
            # rest.
            _LOGGER.warning(
                "Failed to fetch CircuitTimes for %s: %s", parameter_id, exc
            )
        finally:
            self._record_schedule_attempt(
                device_id, module, parameter_id, attempted_at, fetched
            )

    def _fetch_circuit_times(self, device_id: str) -> None:
        """Fetch the device's own view of every weekly programme it has,
        throttled per programme."""
        try:
            for module in (self.modules or {}).get(device_id, {}).values():
                for parameter_id, parameter_data in (
                    module.get("parameters") or {}
                ).items():
                    if not self._is_schedule_parameter(
                        device_id, module, parameter_id, parameter_data
                    ):
                        continue
                    if not self._schedule_is_due(device_id, module, parameter_id):
                        continue
                    self._read_and_record_one_schedule(device_id, module, parameter_id)
        # skipcq: PYL-W0706 - shields the catch-all, not redundant
        except AuthError:
            # The outer half of the same rule: extra detail may be lost, a
            # login that is being refused may not be hidden. See AuthError.
            raise
        except Exception as exc:  # noqa: BLE001
            # Broad: heating programs are extra detail on top of the
            # readings. Losing them must never cost the update itself.
            _LOGGER.warning("Error processing CircuitTimes: %s", exc)
