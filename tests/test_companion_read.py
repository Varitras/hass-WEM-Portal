"""What a date write sends along, on the real path from read to request.

Only the portal is scripted, because the other tests of this stub the read,
and a read that went wrong looked exactly like one that worked from there.
"""

import json
import time
from types import SimpleNamespace

import pytest

from custom_components.wemportal.exceptions import WemPortalError
from custom_components.wemportal.models import ModuleRef, Reading, date_companions
from custom_components.wemportal.wemportalapi import WemPortalApi

BEGIN = 86400.0
END = 172800.0
OTHER_MODULE = {
    "ModuleIndex": 1,
    "ModuleType": 1,
    "Values": [{"ParameterID": "Temperature", "NumericValue": 20}],
}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _date_row(parameter_id, value):
    return Reading(
        value=value,
        parameter_id=parameter_id,
        module_index=0,
        module_type=1,
        platform="date",
    )


def _api(parameters=("U_Beginn", "U_Ende")):
    api = WemPortalApi("user@example.org", "secret")
    api.valid_login = True
    api.modules = {
        "1234": {
            ModuleRef(0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {
                    parameter: {
                        "ParameterID": parameter,
                        "DataType": 2,
                        "IsWriteable": True,
                    }
                    for parameter in parameters
                },
            },
            ModuleRef(1, 1): {
                "Index": 1,
                "Type": 1,
                "Name": "Other",
                "parameters": {"Temperature": {"ParameterID": "Temperature"}},
            },
        }
    }
    api.data = {
        "1234": {
            "Heat pump-U_Beginn": _date_row("U_Beginn", BEGIN),
            "Heat pump-U_Ende": _date_row("U_Ende", END),
        }
    }
    return api


def _holiday_module(begin, end):
    return {
        "ModuleIndex": 0,
        "ModuleType": 1,
        "Values": [
            {"ParameterID": "U_Beginn", "NumericValue": begin},
            {"ParameterID": "U_Ende", "NumericValue": end},
        ],
    }


def _portal_answering(api, *modules):
    """Refresh works, a read answers `modules`, and writes are collected."""
    writes = []

    def call(url, data=None, **_kwargs):
        if url.endswith("Write"):
            writes.append(data)
            return SimpleNamespace(
                json=lambda: {"Status": 0, "JobID": 1}, content=b"", status_code=200
            )
        if url.endswith("Refresh"):
            return SimpleNamespace(json=lambda: {"Status": 0, "JobID": "job"})
        return SimpleNamespace(json=lambda: {"Modules": list(modules)})

    api.make_api_call = call
    return writes


def _write_the_begin(api):
    api.change_value(
        "1234",
        "U_Beginn",
        0,
        1,
        259200.0,
        together_with=lambda: date_companions(
            api.data["1234"], 0, 1, "Heat pump-U_Beginn"
        ),
    )


def test_a_companion_changed_in_the_app_reaches_the_request():
    api = _api()
    writes = _portal_answering(api, _holiday_module(BEGIN, 500000.0))

    _write_the_begin(api)

    assert len(writes) == 1
    assert "500000" in json.dumps(writes[0]), writes[0]


def test_a_read_without_the_module_sends_no_write():
    """A read can work and not carry the module. The rows then keep the last
    poll's date, and that is what went out - the very write the read is
    there to stop, and the read-back checks the written date only."""
    api = _api()
    writes = _portal_answering(api, OTHER_MODULE)

    with pytest.raises(WemPortalError, match="U_Ende"):
        _write_the_begin(api)

    assert writes == [], "a write went out with a companion nobody confirmed"


def test_a_device_with_nothing_to_read_is_said_in_words():
    """The reason reaches the user inside the failed write's message."""
    api = _api(parameters=())
    api.modules["1234"][ModuleRef(1, 1)]["parameters"] = {}
    writes = _portal_answering(api)

    with pytest.raises(WemPortalError) as refused:
        _write_the_begin(api)

    assert "NoRead" not in str(refused.value), refused.value
    assert writes == []
