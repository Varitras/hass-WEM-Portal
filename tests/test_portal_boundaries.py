"""The portal's answers are validated where they arrive.

Umbau phase 2 (K4): valid JSON is not the same as the expected shape.
`{"Parameters": null}` used to travel into a loop three levels deep and die
as a TypeError no handler owned, taking the rest of the device's discovery
with it. Each answer form now has one entry point that turns an unexpected
shape into the classified handling of its site - and a registry test keeps
every `.json()` / `fromstring` call inside a declared boundary function, so
a new call site cannot appear without a conscious decision about its shape.
"""

import ast
import pathlib

import pytest

from custom_components.wemportal import exceptions, wemportalapi
from custom_components.wemportal.wemportalapi import WemPortalApi

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)


class _Answer:
    """A portal answer carrying exactly one JSON payload."""

    status_code = 200
    url = "https://api.example/answer"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _api():
    return WemPortalApi("user@example.org", "secret")


def _module_api():
    """An api holding one cached module for the description tests."""
    api = _api()
    module = {"Index": 0, "Type": 1, "Name": "Heat pump"}
    api.modules = {"1234": {(0, 1): module}}
    return api, module


# --- the module description (Parameters) ------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {"Parameters": None},
        {"Parameters": "text"},
        {"Parameters": {}},
        {"NoParameters": 1},
    ],
)
def test_an_unreadable_parameter_list_is_booked_like_a_refusal(payload):
    """Valid JSON outside the contract must book the module, not crash.

    `{"Parameters": null}` raised a TypeError past the KeyError/ValueError
    handler, aborting discovery for every remaining module of the device -
    and the module kept no timestamp, so the nonsense answer was asked
    again every cycle without limit.
    """
    api, module = _module_api()

    api._store_module_description("1234", (0, 1), module, _Answer(payload))

    assert module.get("description_refused") is True, (
        "an unreadable answer was not booked as a refusal"
    )
    # The booking itself stores an EMPTY parameter dict (nothing is ever
    # thrown away); what must not happen is parameters invented from junk.
    assert not module.get("parameters"), (
        "parameters were conjured out of an unreadable answer"
    )


def test_one_malformed_row_does_not_cost_the_module():
    """A single bad row is dropped; the described rows stay.

    Same philosophy as the mapper: one malformed data point must never cost
    the rest of the device. The whole answer used to be booked as refused
    because the row died mid-loop.
    """
    api, module = _module_api()
    payload = {"Parameters": [{"ParameterID": "P1"}, None, {"NoID": 2}]}

    api._store_module_description("1234", (0, 1), module, _Answer(payload))

    assert module["parameters"] == {"P1": {"ParameterID": "P1"}}
    assert "description_refused" not in module


def test_an_answer_that_is_not_json_still_books_the_module():
    """The pre-existing branch stays: an HTML error page books the module."""
    api, module = _module_api()

    api._store_module_description(
        "1234", (0, 1), module, _Answer(ValueError("not JSON"))
    )

    assert module.get("description_refused") is True


# --- the device list ---------------------------------------------------


@pytest.mark.parametrize(
    "payload", [None, [], "text", {}, {"Devices": None}, {"Devices": "text"}]
)
def test_a_device_list_outside_the_contract_is_a_classified_failure(payload):
    """No Devices list means nothing to set up from - said as a portal-side
    error the coordinator already classifies, not as a raw TypeError the
    catch-all reports as 'unexpected'."""
    api = _api()
    api.make_api_call = lambda url, **_kwargs: _Answer(payload)

    with pytest.raises(exceptions.ServerError, match="[Dd]evice list"):
        api.get_devices()


def test_a_device_row_outside_the_contract_is_skipped_not_fatal(caplog):
    """One malformed device row must not cost the whole account."""
    import logging

    api = _api()
    payload = {
        "Devices": [
            None,
            {"NoID": 1},
            # Dies HALFWAY through its row: the ID reads fine, the module
            # list is missing. A half-adopted ghost device must not remain.
            {"ID": 5},
            {
                "ID": 9,
                "ConnectionStatus": 0,
                "Modules": [{"Index": 0, "Type": 1, "Name": "Heat pump"}],
            },
        ]
    }
    api.make_api_call = lambda url, **_kwargs: _Answer(payload)

    with caplog.at_level(logging.WARNING):
        api.get_devices()

    assert list(api.modules) == ["9"], "the well-formed device was lost too"
    assert "device row" in caplog.text, "the skipped rows went unmentioned"


# --- the value read ----------------------------------------------------


def test_a_value_read_answered_with_null_fails_the_cycle_cleanly():
    """A body of `null` must fail with a reason a person can read.

    The cycle did not crash before - the catch-all at the end of the fetch
    already turned the AttributeError into a failure - but the reason it
    reported was `'NoneType' object has no attribute 'get'`, which sends
    whoever reads the coordinator error hunting for a code bug instead of a
    portal hiccup. The message is the contract here, so it is asserted.
    """
    api = _api()
    api.data = {"1234": {}}
    api.modules = {
        "1234": {
            (0, 1): {
                "Index": 0,
                "Type": 1,
                "Name": "Heat pump",
                "parameters": {"AktRaumSoll": {"ParameterID": "AktRaumSoll"}},
            }
        }
    }

    def make_api_call(url, **_kwargs):
        if url == wemportalapi.API_REFRESH_URL:
            return _Answer({"Status": 0, "JobID": 7})
        return _Answer(None)

    api.make_api_call = make_api_call

    failure = api._fetch_parameter_values("1234")

    assert failure is not None, "a null body was counted as a successful read"
    assert "answer object" in failure, (
        f"the failure reason reads like a code bug, not a portal answer: {failure!r}"
    )


# --- the registry: no boundary without a declaration -------------------

# Every function that may call `.json()` on a portal response. A new call
# site fails this test until it is added here - which is the moment to
# decide what shape the answer has and what a wrong shape means.
JSON_ANSWER_BOUNDARIES = {
    "statistics.py": {"_fetch_device_statistics"},
    "transport.py": {"api_login", "get_response_details"},
    "wemportalapi.py": {
        "get_devices",
        "_store_module_description",
        "_change_value",
        "_fetch_device_status",
        "_fetch_parameter_values",
    },
    "schedule.py": {"_read_one_schedule"},
}

# Every function that may parse portal HTML (or a Telerik delta stream).
HTML_PARSE_BOUNDARIES = {
    "expert_writer.py": {
        "parse_parameter_list",
        "parse_module_list",
        "_full_login",
        "_hidden_fields",
        "parse_parameter_form",
    },
    "scraper.py": {
        "_load_expert_page",
        "scrape",
        "_report_empty_page",
        "parse_expert_page",
    },
    "transport.py": {"web_login"},
    "web_protocol.py": {"maintenance_notice"},
}


def _call_sites(needle):
    """{file: {enclosing function}} for every call of `needle` in the package."""
    sites = {}
    for source_file in sorted(PACKAGE.glob("*.py")):
        source = source_file.read_text(encoding="utf-8")
        if needle not in source:
            continue
        tree = ast.parse(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for line_number, line in enumerate(source.splitlines(), start=1):
            if needle not in line:
                continue
            enclosing = [
                node.name
                for node in functions
                if node.lineno <= line_number <= (node.end_lineno or 0)
            ]
            sites.setdefault(source_file.name, set()).add(
                enclosing[-1] if enclosing else "<module>"
            )
    return sites


def test_every_json_read_sits_in_a_declared_boundary():
    assert _call_sites(".json()") == JSON_ANSWER_BOUNDARIES, (
        "a .json() call moved or appeared - declare the answer form it "
        "reads (and what a wrong shape means) in JSON_ANSWER_BOUNDARIES"
    )


def test_every_html_parse_sits_in_a_declared_boundary():
    assert _call_sites("fromstring") == HTML_PARSE_BOUNDARIES, (
        "an HTML parse moved or appeared - declare it in "
        "HTML_PARSE_BOUNDARIES and decide what an unexpected page means"
    )
