"""The bursts that used to run inside Home Assistant's setup.

Weekly programmes and statistics cost seconds to half a minute, and the first
refresh is awaited by the setup - on every restart and every saved option. So
the first cycle spends no burst, and what it turned away follows shortly
after, one kind per follow-up: the rule that spreads the hourly traffic holds
here too.
"""

import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wemportal import coordinator as coordinator_module
from custom_components.wemportal.coordinator import HEAVY_FOLLOW_UP_DELAY_SECONDS
from custom_components.wemportal.exceptions import ServerError
from custom_components.wemportal.models import HeavyFetchTurns
from custom_components.wemportal.wemportalapi import WemPortalApi

from .test_e2e import (  # noqa: F401
    _enable_custom_integrations,
    _entry,
    _fake_data,
    _mock_portal,
    _setup,
)


# The real one: the portal fixture stubs it, and the chain test below is about
# what it does.
REAL_FETCH_DATA = WemPortalApi.fetch_data


def test_a_cycle_held_for_the_setup_spends_no_burst_and_keeps_the_order():
    turns = HeavyFetchTurns()
    turns.new_cycle()
    turns.hold_for_setup()

    assert not turns.claim("schedules")
    assert not turns.claim("statistics")
    assert turns.waiting

    turns.new_cycle()
    assert turns.claim("schedules"), "the kind that asked first goes first"
    assert not turns.claim("statistics")
    assert turns.waiting

    turns.new_cycle()
    assert turns.claim("statistics")
    assert not turns.waiting


def test_only_the_first_cycle_of_an_api_is_held(monkeypatch):
    """A later cycle runs on the coordinator's timer, with nobody waiting."""
    api = WemPortalApi("user@example.org", "secret", {"mode": "api"})
    monkeypatch.setattr(api, "_ensure_api_session", lambda: None)
    claimed = []
    monkeypatch.setattr(
        api,
        "get_data",
        lambda *_args: claimed.append(api.heavy_fetch_turns.claim("statistics")),
    )

    api._fetch_data()
    api._fetch_data()

    assert claimed == [False, True]


def test_the_api_read_is_due_after_being_asked_for():
    """In `both` mode the ticks in between read no API, and the bursts are
    part of that read: a follow-up inside the interval would fetch nothing."""
    api = WemPortalApi("user@example.org", "secret")
    api._last_api_read = time.monotonic()
    api.scan_interval_api = timedelta(days=1)
    assert not api._api_read_is_due()

    api.read_api_on_the_next_tick()

    assert api._api_read_is_due()


def _cycles_leaving(monkeypatch, *waiting):
    """fetch_data that leaves each listed queue behind, then an empty one."""
    script = list(waiting)
    cycles = []

    def fetch(self, *_args, **_kwargs):
        cycles.append(self._api_read_is_due())
        step = script.pop(0) if script else []
        if isinstance(step, Exception):
            raise step
        self.heavy_fetch_turns._queue = list(step)
        return _fake_data()

    monkeypatch.setattr(WemPortalApi, "fetch_data", fetch)
    return cycles


async def _a_follow_up_later(hass, steps=1):
    for step in range(1, steps + 1):
        async_fire_time_changed(
            hass,
            dt_util.utcnow()
            + timedelta(seconds=step * (HEAVY_FOLLOW_UP_DELAY_SECONDS + 1)),
        )
        await hass.async_block_till_done()


async def test_two_kinds_waiting_follow_one_after_the_other(hass, monkeypatch):
    cycles = _cycles_leaving(monkeypatch, ["schedules", "statistics"], ["statistics"])
    entry = await _setup(hass, _entry(hass))
    assert len(cycles) == 1

    await _a_follow_up_later(hass, 3)

    assert len(cycles) == 3, "one follow-up per kind, and none after them"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_one_kind_waiting_needs_one_follow_up(hass, monkeypatch):
    cycles = _cycles_leaving(monkeypatch, ["statistics"])
    entry = await _setup(hass, _entry(hass))

    await _a_follow_up_later(hass, 3)

    assert len(cycles) == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_nothing_waiting_costs_no_follow_up(hass, monkeypatch):
    cycles = _cycles_leaving(monkeypatch)
    entry = await _setup(hass, _entry(hass))

    await _a_follow_up_later(hass, 2)

    assert len(cycles) == 1, "a request went out with nothing to fetch"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_follow_ups_stop_even_if_a_kind_keeps_waiting(hass, monkeypatch):
    """A kind can stay queued, behind one that went first. The follow-ups
    exist for the start, not as a second poll."""
    stuck = ["statistics"]
    cycles = _cycles_leaving(monkeypatch, stuck, stuck, stuck, stuck, stuck)
    entry = await _setup(hass, _entry(hass))

    await _a_follow_up_later(hass, 4)

    assert len(cycles) == 3
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_follow_up_reads_the_api_inside_the_interval(hass, monkeypatch):
    cycles = _cycles_leaving(monkeypatch, ["statistics"])
    entry = await _setup(hass, _entry(hass))
    entry.runtime_data.api._last_api_read = time.monotonic()
    entry.runtime_data.api.scan_interval_api = timedelta(days=1)

    await _a_follow_up_later(hass)

    assert len(cycles) == 2
    assert cycles[-1] is True, "the follow-up tick read nothing"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_unload_takes_the_follow_up_with_it(hass, monkeypatch):
    waiting = ["schedules", "statistics"]
    cycles = _cycles_leaving(monkeypatch, waiting, waiting)
    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator

    await hass.config_entries.async_unload(entry.entry_id)

    assert coordinator._cancel_heavy_follow_up is None, "the timer outlived the entry"
    await _a_follow_up_later(hass, 2)
    assert len(cycles) == 1, "a follow-up ran for an unloaded entry"


async def test_a_failed_follow_up_plans_no_other(hass, monkeypatch):
    """An error state must not add traffic. A second try sixty seconds on
    was also a second failure in a row, which takes every entity down at an
    interval where the next regular cycle would have come after the pause."""
    waiting = ["schedules", "statistics"]
    cycles = _cycles_leaving(monkeypatch, waiting, ServerError("no"), waiting)
    entry = await _setup(hass, _entry(hass))

    await _a_follow_up_later(hass, 3)

    assert len(cycles) == 2
    assert entry.runtime_data.coordinator.num_failed == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_pause_stops_the_follow_ups(hass, monkeypatch):
    """A 403 inside a burst is swallowed, so the cycle itself succeeds."""
    waiting = ["schedules", "statistics"]
    cycles = _cycles_leaving(monkeypatch, waiting, waiting)
    entry = await _setup(hass, _entry(hass))
    monkeypatch.setattr(WemPortalApi, "is_rate_limited", lambda self: True)

    await _a_follow_up_later(hass, 2)

    assert len(cycles) == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_regular_cycle_that_served_the_kinds_cancels_the_follow_up(
    hass, monkeypatch
):
    """Asked again when it fires: a regular cycle that finished first leaves
    a read nobody owes."""
    cycles = _cycles_leaving(monkeypatch, ["statistics"])
    entry = await _setup(hass, _entry(hass))
    entry.runtime_data.api.heavy_fetch_turns._queue = []

    await _a_follow_up_later(hass)

    assert len(cycles) == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_follow_up_runs_like_the_regular_tick(hass, monkeypatch):
    """Off the tasks Home Assistant waits for at the end of its start, which
    is the wait this exists to spare, and cancelled by its stop."""
    planned = []
    real_call_later = coordinator_module.async_call_later

    def call_later(hass_, delay, job):
        planned.append(job)
        return real_call_later(hass_, delay, job)

    background = []
    real_background_task = ConfigEntry.async_create_background_task

    def background_task(self, hass_, target, name, *args, **kwargs):
        background.append(name)
        return real_background_task(self, hass_, target, name, *args, **kwargs)

    monkeypatch.setattr(coordinator_module, "async_call_later", call_later)
    monkeypatch.setattr(ConfigEntry, "async_create_background_task", background_task)
    _cycles_leaving(monkeypatch, ["statistics"])
    entry = await _setup(hass, _entry(hass))

    assert planned[0].cancel_on_shutdown is True
    await _a_follow_up_later(hass)

    assert any("heavy follow-up" in name for name in background), background
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_follow_up_does_not_start_on_an_entry_going_away(hass, monkeypatch):
    """The timer goes with the on_unload callbacks, which run after the
    teardown began: a cycle started in between logs in again on a transport
    that is being closed."""
    cycles = _cycles_leaving(monkeypatch, ["statistics"])
    entry = await _setup(hass, _entry(hass))
    entry.runtime_data.begin_unload()

    await _a_follow_up_later(hass)

    assert len(cycles) == 1
    entry.runtime_data.unloading = False
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_stopped_follow_up_is_not_planned_again(hass, monkeypatch):
    """An unload during a running follow-up: its tail must not re-arm."""
    _cycles_leaving(monkeypatch, ["statistics"])
    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator

    coordinator._async_stop_heavy_follow_ups()
    coordinator._async_plan_heavy_follow_up()

    assert coordinator._cancel_heavy_follow_up is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_first_cycle_hands_its_bursts_to_the_follow_ups(hass, monkeypatch):
    """The whole chain on the real cycle, only the portal's part stood in for:
    the kinds ask in their order and get their turn, one follow-up each."""
    served = []
    owed = ["schedules", "statistics"]

    def get_data(self, *_args, **_kwargs):
        self.data = _fake_data()
        for kind in list(owed):
            if self.heavy_fetch_turns.claim(kind):
                owed.remove(kind)
                served.append(kind)

    monkeypatch.setattr(WemPortalApi, "fetch_data", REAL_FETCH_DATA)
    monkeypatch.setattr(WemPortalApi, "get_data", get_data)
    monkeypatch.setattr(WemPortalApi, "_ensure_api_session", lambda self: None)
    entry = await _setup(hass, _entry(hass))
    assert served == [], "a burst ran inside the setup"

    await _a_follow_up_later(hass)
    assert served == ["schedules"]

    await _a_follow_up_later(hass)
    assert served == ["schedules", "statistics"]
    assert not entry.runtime_data.api.heavy_fetch_turns.waiting
    await hass.config_entries.async_unload(entry.entry_id)
