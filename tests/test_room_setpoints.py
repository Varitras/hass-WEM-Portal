"""The three room setpoints of a heating circuit: reduced, normal, comfort.

The mobile API describes each with bounds of its own, and they were taken as
the entity's range. Seen on two installations: the bounds differ from one to
the next (as reduced / normal / comfort 16-20 / 18-21 / 20-28 on one and
16-21 / 16-22 / 21-28 on the other),
they do not follow the values, and the portal does not hold to them - a
normal temperature of 23 sat above its described maximum of 21, and Home
Assistant refused to set it again. What the portal does keep is the order:
reduced below normal below comfort.

So the three share one range, the lowest minimum to the highest maximum, and
the order is checked when a value is written, against values read just then.

Both ask the module's description and the fresh answer, never the rows'
values: a neighbour that holds nothing just now is still there to be crossed.
"""

import time
from types import SimpleNamespace

import pytest

from custom_components.wemportal.const import WemDataType
from custom_components.wemportal.exceptions import WemPortalError
from custom_components.wemportal.models import ModuleRef, Reading
from custom_components.wemportal.wemportalapi import WemPortalApi
from tests.test_mapper import _modules, _parameter, _process, _value, _values

DEVICE = "1234"
MODULE = (0, 1)
# As one installation describes them.
DESCRIBED = {"Absenk": (16.0, 20.0), "Normal": (18.0, 21.0), "Komfort": (20.0, 28.0)}
HELD = {"Absenk": 18.0, "Normal": 23.0, "Komfort": 26.5}
ALL = ("Absenk", "Normal", "Komfort")


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


def test_an_answer_that_leaves_a_setpoint_out_keeps_the_range():
    """The portal answers nothing for a parameter now and then. Shared among
    the answered rows, the range fell back to the narrow one for that cycle."""
    modules = _modules(*(_setpoint(parameter_id) for parameter_id in ALL))

    rows = _process(modules, _values(_value("Normal", 23.0)))

    assert _range(rows, "Normal") == (16.0, 28.0)


def test_a_setpoint_without_described_bounds_does_not_set_the_range():
    """Its range is a guess of this integration's, 5 to 35, and shared it
    became the range of all three."""
    modules = _modules(
        _setpoint("Absenk"),
        _setpoint("Normal"),
        _parameter("Komfort", IsWriteable=True, DataType=WemDataType.NUMBER_STEP_HALF),
    )

    rows = _process(modules, _values(*(_value(name, HELD[name]) for name in ALL)))

    for parameter_id in ALL:
        assert _range(rows, parameter_id) == (16.0, 21.0), parameter_id


def test_a_bound_that_is_no_number_is_not_described():
    modules = _modules(
        _setpoint("Absenk"), _setpoint("Normal"), _setpoint("Komfort", ("NaN", 28.0))
    )

    rows = _process(modules, _values(*(_value(name, HELD[name]) for name in ALL)))

    assert _range(rows, "Normal") == (16.0, 21.0)


def test_one_described_setpoint_is_not_a_range_to_share():
    modules = _modules(
        _setpoint("Normal"),
        _parameter("Komfort", IsWriteable=True, DataType=WemDataType.NUMBER_STEP_HALF),
    )

    rows = _process(modules, _values(_value("Normal", 23.0), _value("Komfort", 26.5)))

    assert _range(rows, "Normal") == (18.0, 21.0)
    assert _range(rows, "Komfort") == (5.0, 35.0)


def test_a_setpoint_that_is_no_number_gets_no_range():
    choice = _parameter(
        "Komfort",
        IsWriteable=True,
        DataType=WemDataType.SELECT,
        EnumValues=[{"Value": 20, "Name": "low"}, {"Value": 26, "Name": "high"}],
    )
    modules = _modules(_setpoint("Absenk"), _setpoint("Normal"), choice)

    rows = _process(
        modules,
        _values(_value("Absenk", 18.0), _value("Normal", 23.0), _value("Komfort", 26)),
    )

    assert rows["Heat pump-Komfort"].platform == "select"
    assert _range(rows, "Komfort") == (None, None)
    assert _range(rows, "Normal") == (16.0, 21.0)


def test_the_bounds_the_portal_described_stay_on_the_row():
    """For the diagnostics: they are what a report about a range is about."""
    row = _rows(*ALL)["Heat pump-Normal"]

    assert (row.described_min, row.described_max) == (18.0, 21.0)
    assert row.as_dict()["described_max"] == 21.0


# --- the order, when a value is written --------------------------------


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _api(*parameter_ids, held=None, read_only=()):
    """An api that polled `held`, with the setpoints as its rows."""
    api = WemPortalApi("user@example.org", "secret")
    api.valid_login = True
    described = _modules(
        *(
            {**_setpoint(parameter_id), "IsWriteable": parameter_id not in read_only}
            for parameter_id in parameter_ids
        )
    )
    api.modules = {
        DEVICE: {
            ModuleRef(*MODULE): {"Index": 0, "Type": 1, **described[DEVICE][MODULE]}
        }
    }
    # As in a real device's rows: the connection status is no reading.
    api.data = {DEVICE: {"ConnectionStatus": 0}}
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
        self.refuses_the_refresh = False
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
            if self.refuses_the_refresh:
                raise WemPortalError("the portal is not answering")
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
    """Checked against the last poll, this write would be refused although
    the portal takes it: the comfort temperature has been raised since."""
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


def test_a_parameter_that_is_no_setpoint_has_no_neighbours():
    from custom_components.wemportal.models import room_setpoint_neighbours

    api, _portal = _api(*ALL, "Heizkennlinie", held={**HELD, "Heizkennlinie": 1.0})

    assert room_setpoint_neighbours(api.data[DEVICE], *MODULE, "Heizkennlinie") == []
    assert sorted(room_setpoint_neighbours(api.data[DEVICE], *MODULE, "Normal")) == [
        "Absenk",
        "Komfort",
    ]


def _forget_the_values(api, *parameter_ids):
    """As after a poll the portal left them out of, or after they aged."""
    for parameter_id in parameter_ids:
        api.data[DEVICE][f"Heat pump-{parameter_id}"].value = None


def test_neighbours_holding_nothing_just_now_are_still_read_and_checked():
    """Whether to read at all was decided from the last poll's values, so two
    blank neighbours meant no read, no check, and the value went out."""
    api, portal = _api(*ALL)
    _forget_the_values(api, "Absenk", "Komfort")

    with pytest.raises(WemPortalError, match="Komfort"):
        _write(api, "Normal", 27.0)

    assert portal.reads == 1
    assert portal.writes == []


@pytest.mark.parametrize("unreadable", [None, "--", float("nan"), True])
def test_a_neighbour_answered_without_a_number_stops_the_write(unreadable):
    """Named in the answer is not read: it was left out of the comparison,
    and the write passed with one neighbour unchecked."""
    api, portal = _api(*ALL)
    portal.holds["Komfort"] = unreadable

    with pytest.raises(WemPortalError, match="no number for Komfort"):
        _write(api, "Normal", 20.0)

    assert portal.writes == []


def test_a_neighbour_nobody_may_write_is_still_a_neighbour():
    api, portal = _api(*ALL, read_only=("Komfort",))
    assert api.data[DEVICE]["Heat pump-Komfort"].platform == "sensor"

    with pytest.raises(WemPortalError, match="Komfort"):
        _write(api, "Normal", 27.0)

    assert portal.writes == []


def test_a_read_that_fails_stops_the_write():
    """Its answer is then the last poll's, which is what the read replaces."""
    api, portal = _api(*ALL)
    portal.refuses_the_refresh = True

    with pytest.raises(WemPortalError, match="could not be read first"):
        _write(api, "Normal", 20.0)

    assert portal.writes == []


def test_a_device_id_given_as_a_number_is_checked_too():
    api, portal = _api(*ALL)

    with pytest.raises(WemPortalError, match="Komfort"):
        api.change_value(int(DEVICE), "Normal", *MODULE, 27.0)

    assert portal.writes == []


def test_a_row_that_is_no_reading_is_no_neighbour():
    api, _portal = _api(*ALL)
    assert not isinstance(api.data[DEVICE]["ConnectionStatus"], Reading)

    _write(api, "Normal", 24.0)


def test_the_setpoints_of_another_circuit_are_no_neighbours():
    """Every heating circuit has them, under the same parameter ids. This
    circuit has no reduced temperature; the other one's is not its neighbour,
    in value or in being there at all."""
    api, portal = _api("Normal", "Komfort")
    second = (1, 1)
    api.modules[DEVICE][ModuleRef(*second)] = {
        "Index": 1,
        "Type": 1,
        "Name": "Second circuit",
        "parameters": {name: _setpoint(name) for name in ALL},
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
    assert "Second circuit-Absenk" in api.data[DEVICE]

    _write(api, "Normal", 24.0)

    assert len(portal.writes) == 1, "refused over the other circuit's setpoints"
