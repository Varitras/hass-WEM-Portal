"""The service that writes a holiday as one range.

The reason it exists is measured, not assumed (see date.py and the runbook):
the portal refuses a holiday parameter written on its own, and answers a pair
whose begin falls after its end with Status 0 while storing nothing. Setting
the two date entities one after the other therefore depends on the order the
user happens to click in, which is not something a user can be expected to
know.

These tests build the collaborators by hand rather than starting Home
Assistant: the service only needs an entity registry, a config entry and a
coordinator, and a full instance per test costs about sixteen seconds.
"""

import types
from datetime import date

import pytest

from custom_components.wemportal import holiday
from custom_components.wemportal.date import date_to_epoch
from custom_components.wemportal.models import WemPortalData

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

BEGIN_ROW = {
    "friendlyName": "Holiday begin",
    "ParameterID": "U_Beginn",
    "value": date_to_epoch(date(2026, 8, 3)),
    "unit": None,
    "platform": "date",
    "ModuleIndex": 1,
    "ModuleType": 2,
}
END_ROW = {
    "friendlyName": "Holiday end",
    "ParameterID": "U_Ende",
    "value": date_to_epoch(date(2026, 8, 4)),
    "unit": None,
    "platform": "date",
    "ModuleIndex": 1,
    "ModuleType": 2,
}


class _Api:
    """Records the writes, and can be told to refuse them."""

    def __init__(self, refuse=False):
        self.calls = []
        self.refuse = refuse

    def change_value(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.refuse:
            raise RuntimeError("portal said no")


def _world(monkeypatch, rows=None, refuse=False):
    """One loaded account with a holiday pair, wired to a fake registry."""
    rows = (
        rows
        if rows is not None
        else {
            "Circuit-U_Beginn": dict(BEGIN_ROW),
            "Circuit-U_Ende": dict(END_ROW),
        }
    )
    coordinator = types.SimpleNamespace(
        data={"1234": rows}, async_update_listeners=lambda: None
    )
    api = _Api(refuse=refuse)
    entry = types.SimpleNamespace(entry_id="e1")
    entry.runtime_data = WemPortalData(api=api, coordinator=coordinator)

    registry = types.SimpleNamespace(
        async_get=lambda entity_id: {
            "date.holiday_begin": types.SimpleNamespace(
                platform="wemportal",
                config_entry_id="e1",
                unique_id="e1:1234:Circuit-U_Beginn",
            ),
            "date.holiday_end": types.SimpleNamespace(
                platform="wemportal",
                config_entry_id="e1",
                unique_id="e1:1234:Circuit-U_Ende",
            ),
            "date.other_module": types.SimpleNamespace(
                platform="wemportal",
                config_entry_id="e1",
                unique_id="e1:1234:Other-U_Ende",
            ),
            "date.not_ours": types.SimpleNamespace(
                platform="demo",
                config_entry_id="e1",
                unique_id="whatever",
            ),
        }.get(entity_id)
    )
    monkeypatch.setattr(holiday.entity_registry, "async_get", lambda _hass: registry)

    async def _executor(function, *args):
        return function(*args)

    hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_get_entry=lambda entry_id: entry if entry_id == "e1" else None,
            async_entries=lambda _domain: [entry],
        ),
        async_add_executor_job=_executor,
    )
    return hass, api, rows


def _call(
    begin=date(2026, 8, 20),
    end=date(2026, 8, 27),
    begin_entity="date.holiday_begin",
    end_entity="date.holiday_end",
):
    return types.SimpleNamespace(
        data={
            "begin_entity": begin_entity,
            "begin": begin,
            "end_entity": end_entity,
            "end": end,
        }
    )


async def test_both_dates_travel_in_one_request(monkeypatch):
    """The whole point: two parameters, one write."""
    hass, api, _rows = _world(monkeypatch)

    await holiday._write_holiday(hass, _call())

    assert len(api.calls) == 1, "the pair went out as two separate writes"
    args, kwargs = api.calls[0]
    device_id, parameter_id, module_index, module_type, value = args
    assert (device_id, parameter_id) == ("1234", "U_Beginn")
    assert (module_index, module_type) == (1, 2)
    assert value == date_to_epoch(date(2026, 8, 20))
    assert kwargs["together_with"] == {"U_Ende": date_to_epoch(date(2026, 8, 27))}


async def test_the_written_range_reaches_the_coordinator(monkeypatch):
    """Both rows, not just the one that was addressed - otherwise the next
    write sends the other date back as a stale companion."""
    hass, _api, rows = _world(monkeypatch)

    await holiday._write_holiday(hass, _call())

    assert rows["Circuit-U_Beginn"]["value"] == date_to_epoch(date(2026, 8, 20))
    assert rows["Circuit-U_Ende"]["value"] == date_to_epoch(date(2026, 8, 27))


async def test_a_refused_write_changes_nothing(monkeypatch):
    """Recording a range the heating system never took would make the
    integration certain of the wrong thing."""
    hass, _api, rows = _world(monkeypatch, refuse=True)

    with pytest.raises(RuntimeError):
        await holiday._write_holiday(hass, _call())

    assert rows["Circuit-U_Beginn"]["value"] == date_to_epoch(date(2026, 8, 3))
    assert rows["Circuit-U_Ende"]["value"] == date_to_epoch(date(2026, 8, 4))


async def test_a_range_that_ends_before_it_starts_is_refused(monkeypatch):
    """The portal answers such a pair with Status 0 and stores nothing, so
    letting it through would report a setting that never happened."""
    hass, api, _rows = _world(monkeypatch)

    with pytest.raises(HomeAssistantError) as excinfo:
        await holiday._write_holiday(
            hass, _call(begin=date(2026, 8, 27), end=date(2026, 8, 20))
        )

    assert "before it starts" in str(excinfo.value)
    assert api.calls == [], "a backwards range was put on the wire anyway"


async def test_a_single_day_holiday_is_allowed(monkeypatch):
    """Begin equal to end is a holiday of one day, not a backwards range."""
    hass, api, _rows = _world(monkeypatch)

    await holiday._write_holiday(
        hass, _call(begin=date(2026, 8, 20), end=date(2026, 8, 20))
    )

    assert len(api.calls) == 1


async def test_two_dates_of_different_modules_are_refused(monkeypatch):
    """The portal addresses parameters per module; one request cannot carry
    two modules, and pretending otherwise would write to the wrong circuit."""
    rows = {
        "Circuit-U_Beginn": dict(BEGIN_ROW),
        "Other-U_Ende": {**END_ROW, "ModuleIndex": 2},
    }
    hass, api, _rows = _world(monkeypatch, rows=rows)

    with pytest.raises(HomeAssistantError) as excinfo:
        await holiday._write_holiday(hass, _call(end_entity="date.other_module"))

    assert "different" in str(excinfo.value)
    assert api.calls == []


async def test_the_same_entity_twice_is_refused(monkeypatch):
    hass, api, _rows = _world(monkeypatch)

    with pytest.raises(HomeAssistantError):
        await holiday._write_holiday(hass, _call(end_entity="date.holiday_begin"))

    assert api.calls == []


def test_an_entity_of_another_integration_is_refused(monkeypatch):
    hass, _api, _rows = _world(monkeypatch)

    with pytest.raises(HomeAssistantError) as excinfo:
        holiday.resolve_date_target(hass, "date.not_ours")

    assert "not a WEM Portal entity" in str(excinfo.value)


def test_an_entity_that_is_not_a_date_is_refused(monkeypatch):
    """A number entity carries a setpoint, not a day - and its row has no
    epoch to write."""
    hass, _api, _rows = _world(monkeypatch)

    with pytest.raises(HomeAssistantError) as excinfo:
        holiday.resolve_date_target(hass, "number.some_setpoint")

    assert "not a date entity" in str(excinfo.value)


async def test_a_write_into_an_unloading_entry_is_refused(monkeypatch):
    """The same gate the entities pass through: a call can land inside a
    teardown, and the write would start into a session about to close."""
    hass, api, _rows = _world(monkeypatch)
    hass.config_entries.async_get_entry("e1").runtime_data.begin_unload()

    with pytest.raises(HomeAssistantError):
        await holiday._write_holiday(hass, _call())

    assert api.calls == []
