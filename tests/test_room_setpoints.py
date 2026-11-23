"""The three room setpoints of a heating circuit: reduced, normal, comfort.

The mobile API describes each with bounds of its own, and they were taken as
the entity's range. Seen on two installations: the bounds differ from one to
the next (20-28 / 18-21 / 16-20 on one, 21-28 / 16-22 / 16-21 on the other),
they do not follow the values, and the portal does not hold to them - a
normal temperature of 23 sat above its described maximum of 21, and Home
Assistant refused to set it again. What the portal does keep is the order:
reduced below normal below comfort.

So the three share one range, the lowest minimum to the highest maximum, and
the order is checked when a value is written, against values read just then.
"""

import time
from types import SimpleNamespace

import pytest

from custom_components.wemportal.const import WemDataType
from custom_components.wemportal.exceptions import WemPortalError
from custom_components.wemportal.models import ModuleRef
from custom_components.wemportal.wemportalapi import WemPortalApi
from tests.test_mapper import _modules, _parameter, _process, _value, _values

DEVICE = "1234"
MODULE = (0, 1)
# As one installation describes them.
DESCRIBED = {"Absenk": (16.0, 20.0), "Normal": (18.0, 21.0), "Komfort": (20.0, 28.0)}
HELD = {"Absenk": 18.0, "Normal": 23.0, "Komfort": 26.5}


def _setpoint(parameter_id, bounds=None):
    low, high = bounds or DESCRIBED.get(parameter_id, (0.2, 3.0))
    return _parameter(
        parameter_id,
        IsWriteable=True,
        DataType=WemDataType.NUMBER_STEP_HALF,
        MinValue=low,
        MaxValue=high,
    )


def _rows(*parameter_ids, extra=()):
    return _process(
        _modules(*(_setpoint(parameter_id) for parameter_id in parameter_ids), *extra),
        _values(
            *(
                _value(parameter_id, HELD[parameter_id])
                for parameter_id in parameter_ids
            ),
            *(_value(parameter["ParameterID"], 1.0) for parameter in extra),
        ),
    )


def _range(rows, parameter_id):
    row = rows[f"Heat pump-{parameter_id}"]
    return row.min_value, row.max_value


# --- the range ---------------------------------------------------------


@pytest.mark.parametrize("parameter_id", ["Absenk", "Normal", "Komfort"])
def test_the_three_setpoints_share_one_range(parameter_id):
    rows = _rows("Absenk", "Normal", "Komfort")

    assert _range(rows, parameter_id) == (16.0, 28.0), (
        "a normal temperature of 23 cannot be set within 18 to 21"
    )


def test_two_of_them_share_a_range_too():
    rows = _rows("Normal", "Komfort")

    assert _range(rows, "Normal") == (18.0, 28.0)
    assert _range(rows, "Komfort") == (18.0, 28.0)


def test_a_setpoint_on_its_own_keeps_the_described_range():
    assert _range(_rows("Komfort"), "Komfort") == (20.0, 28.0)


def test_another_number_of_the_module_keeps_its_own_range():
    curve = _setpoint("Heizkennlinie", bounds=(0.2, 3.0))

    rows = _rows("Absenk", "Normal", "Komfort", extra=(curve,))

    assert _range(rows, "Heizkennlinie") == (0.2, 3.0)


# --- the order, when a value is written --------------------------------


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _api(*parameter_ids, held=None):
    """An api that polled `held`, with the setpoints as its rows."""
    api = WemPortalApi("user@example.org", "secret")
    api.valid_login = True
    described = _modules(*(_setpoint(parameter_id) for parameter_id in parameter_ids))
    api.modules = {
        DEVICE: {
            ModuleRef(*MODULE): {"Index": 0, "Type": 1, **described[DEVICE][MODULE]}
        }
    }
    api.data = {DEVICE: {}}
    portal = _Portal(api, parameter_ids, held or HELD)
    assert api._fetch_parameter_values(DEVICE) is None
    portal.reads = 0
    return api, portal


class _Portal:
    """Answers a read with `holds` and collects the writes."""

    def __init__(self, api, parameter_ids, holds):
        self.parameter_ids = parameter_ids
        self.holds = dict(holds)
        self.answers = True
        self.reads = 0
        self.writes = []
        api.make_api_call = self._call

    def _call(self, url, data=None, **_kwargs):
        if url.endswith("Write"):
            self.writes.append(data)
            return SimpleNamespace(
                json=lambda: {"Status": 0, "JobID": 1}, content=b"", status_code=200
            )
        if url.endswith("Refresh"):
            return SimpleNamespace(json=lambda: {"Status": 0, "JobID": "job"})
        self.reads += 1
        values = [
            {"ParameterID": parameter_id, "NumericValue": self.holds[parameter_id]}
            for parameter_id in self.parameter_ids
            if self.answers
        ]
        module = {"ModuleIndex": MODULE[0], "ModuleType": MODULE[1], "Values": values}
        return SimpleNamespace(json=lambda: {"Modules": [module]})


def _write(api, parameter_id, value):
    api.change_value(DEVICE, parameter_id, *MODULE, value)


ALL = ("Absenk", "Normal", "Komfort")


@pytest.mark.parametrize(
    ("parameter_id", "value", "neighbour"),
    [
        ("Normal", 27.0, "Komfort"),
        ("Normal", 26.5, "Komfort"),
        ("Normal", 18.0, "Absenk"),
        ("Absenk", 23.5, "Normal"),
        ("Komfort", 22.5, "Normal"),
    ],
    ids=[
        "normal above comfort",
        "normal equal to comfort",
        "normal equal to reduced",
        "reduced above normal",
        "comfort below normal",
    ],
)
def test_a_value_that_crosses_its_neighbour_is_not_written(
    parameter_id, value, neighbour
):
    api, portal = _api(*ALL)

    with pytest.raises(WemPortalError, match=neighbour):
        _write(api, parameter_id, value)

    assert portal.writes == []


def test_the_refusal_names_the_value_the_neighbour_holds():
    api, _portal = _api(*ALL)

    with pytest.raises(WemPortalError, match=r"26\.5"):
        _write(api, "Normal", 27.0)


@pytest.mark.parametrize(
    ("parameter_id", "value"),
    [("Normal", 26.0), ("Normal", 18.5), ("Absenk", 22.5), ("Komfort", 23.5)],
)
def test_a_value_between_its_neighbours_is_written(parameter_id, value):
    api, portal = _api(*ALL)

    _write(api, parameter_id, value)

    assert len(portal.writes) == 1


def test_a_neighbour_raised_in_the_app_since_the_last_poll_counts():
    """Checked against the last poll, this write was refused although the
    portal would take it: the comfort temperature had been raised since."""
    api, portal = _api(*ALL)
    portal.holds["Komfort"] = 28.0

    _write(api, "Normal", 27.0)

    assert len(portal.writes) == 1


def test_a_neighbour_lowered_in_the_app_since_the_last_poll_counts():
    api, portal = _api(*ALL)
    portal.holds["Komfort"] = 24.0

    with pytest.raises(WemPortalError, match=r"24\.0"):
        _write(api, "Normal", 25.0)

    assert portal.writes == []


def test_neighbours_that_cannot_be_read_stop_the_write():
    """Not checked is not the same as in order, and the last poll's values
    are what this read exists to replace."""
    api, portal = _api(*ALL)
    portal.answers = False

    with pytest.raises(WemPortalError, match="could not be read first"):
        _write(api, "Normal", 20.0)

    assert portal.writes == []


def test_a_setpoint_without_neighbours_is_written_without_a_read():
    api, portal = _api("Komfort")

    _write(api, "Komfort", 21.0)

    assert len(portal.writes) == 1
    assert portal.reads == 0, "a read for an order nothing can break"


def test_another_parameter_is_written_without_a_read():
    api, portal = _api(*ALL, "Heizkennlinie", held={**HELD, "Heizkennlinie": 1.0})

    _write(api, "Heizkennlinie", 1.5)

    assert len(portal.writes) == 1
    assert portal.reads == 0


def test_the_setpoints_of_another_circuit_are_no_neighbours():
    """Every heating circuit has the three, under the same parameter ids."""
    api, portal = _api(*ALL)
    second = (1, 1)
    api.modules[DEVICE][ModuleRef(*second)] = {
        "Index": 1,
        "Type": 1,
        "Name": "Second circuit",
        "parameters": api.modules[DEVICE][ModuleRef(*MODULE)]["parameters"],
    }
    answer_first = portal._call

    def answer_both(url, data=None, **kwargs):
        answer = answer_first(url, data, **kwargs).json()
        if "Modules" in answer:
            answer["Modules"].append(
                {
                    "ModuleIndex": second[0],
                    "ModuleType": second[1],
                    "Values": [
                        {"ParameterID": "Absenk", "NumericValue": 16.0},
                        {"ParameterID": "Normal", "NumericValue": 17.0},
                        {"ParameterID": "Komfort", "NumericValue": 20.0},
                    ],
                }
            )
        return SimpleNamespace(json=lambda: answer, content=b"", status_code=200)

    api.make_api_call = answer_both
    assert api._fetch_parameter_values(DEVICE) is None

    _write(api, "Normal", 24.0)

    assert len(portal.writes) == 1, "refused over the other circuit's comfort of 20"
