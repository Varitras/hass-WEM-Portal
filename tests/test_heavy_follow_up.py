"""The bursts that used to run inside Home Assistant's setup.

Weekly programmes and statistics cost seconds to half a minute, and the first
refresh is awaited by the setup - on every restart and every saved option. So
the first cycle spends no burst, and what it turned away follows shortly
after, one kind per follow-up: the rule that spreads the hourly traffic holds
here too.
"""

import time
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wemportal.coordinator import HEAVY_FOLLOW_UP_DELAY_SECONDS
from custom_components.wemportal.models import HeavyFetchTurns
from custom_components.wemportal.wemportalapi import WemPortalApi

from .test_e2e import (  # noqa: F401
    _enable_custom_integrations,
    _entry,
    _fake_data,
    _mock_portal,
    _setup,
)


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
    script = [list(queue) for queue in waiting]
    cycles = []

    def fetch(self, *_args, **_kwargs):
        cycles.append(self._api_read_is_due())
        self.heavy_fetch_turns._queue = script.pop(0) if script else []
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
    """A portal failing the burst leaves it queued; the regular cycles retry
    it. The follow-ups exist for the start, not as a second poll."""
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


@pytest.mark.parametrize("waiting", [["schedules", "statistics"]])
async def test_an_unload_takes_the_follow_up_with_it(hass, monkeypatch, waiting):
    cycles = _cycles_leaving(monkeypatch, waiting, waiting)
    entry = await _setup(hass, _entry(hass))
    coordinator = entry.runtime_data.coordinator

    await hass.config_entries.async_unload(entry.entry_id)

    assert coordinator._cancel_heavy_follow_up is None, "the timer outlived the entry"
    await _a_follow_up_later(hass, 2)
    assert len(cycles) == 1, "a follow-up ran for an unloaded entry"
