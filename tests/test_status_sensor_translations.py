"""The three device status sensors are named and stated in Home Assistant's
language.

Their names and states were fixed English text in the value read -
"Connection Status", "Has Errors" with "Yes"/"No" - so a German Home
Assistant showed them in English. Unlike the parameters, they are the same
three sensors on every installation, so Home Assistant's own entity and
state translations can name them; the state values stay stable words.
"""

import json
import types
from pathlib import Path

import pytest

from custom_components.wemportal.models import Reading
from custom_components.wemportal.wemportalapi import (
    DEVICE_STATUS_CONNECTION,
    DEVICE_STATUS_ERROR_MESSAGES,
    DEVICE_STATUS_HAS_ERRORS,
)

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "wemportal" / "translations"
)


def _status_sensor(parameter_id, value):
    from custom_components.wemportal.sensor import WemPortalSensor

    key = f"1234-{parameter_id}"
    row = Reading(
        friendly_name="label from the value read",
        parameter_id=parameter_id,
        value=value,
        platform="sensor",
    )
    coordinator = types.SimpleNamespace(
        data={"1234": {key: row}},
        api=types.SimpleNamespace(api_version="2.0", modules={}),
        last_update_success=True,
        async_add_listener=lambda *_args, **_kwargs: None,
    )
    return WemPortalSensor(
        coordinator,
        types.SimpleNamespace(entry_id="e1", data={"username": "user@example.org"}),
        "1234",
        key,
        row,
    )


@pytest.mark.parametrize(
    ("parameter_id", "value", "translation_key"),
    [
        (DEVICE_STATUS_CONNECTION, "online", "connection_status"),
        (DEVICE_STATUS_HAS_ERRORS, "no", "has_errors"),
        (DEVICE_STATUS_ERROR_MESSAGES, "None", "error_messages"),
    ],
)
def test_a_status_sensor_is_named_by_its_translation(
    parameter_id, value, translation_key
):
    """A name set on the entity wins over its translation, so the label the
    value read gives it must not be one."""
    sensor = _status_sensor(parameter_id, value)

    assert sensor.translation_key == translation_key
    assert not hasattr(sensor, "_attr_name"), (
        "a fixed name hides the translation Home Assistant would show"
    )


@pytest.mark.parametrize(
    ("parameter_id", "value"),
    [(DEVICE_STATUS_CONNECTION, "busy"), (DEVICE_STATUS_HAS_ERRORS, "yes")],
)
def test_a_status_states_its_value_as_one_of_its_translated_words(parameter_id, value):
    sensor = _status_sensor(parameter_id, value)

    assert sensor.device_class == "enum"
    assert value in sensor.options
    assert sensor.native_value == value


@pytest.mark.parametrize("language", ["en", "de"])
def test_every_status_name_and_state_is_translated(language):
    """A key without its text shows as the key itself."""
    catalogue = json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )
    sensors = catalogue["entity"]["sensor"]
    for parameter_id, value in [
        (DEVICE_STATUS_CONNECTION, "online"),
        (DEVICE_STATUS_HAS_ERRORS, "no"),
    ]:
        sensor = _status_sensor(parameter_id, value)
        entry = sensors[sensor.translation_key]
        assert entry["name"]
        assert set(sensor.options) == set(entry["state"]), (language, parameter_id)
    assert sensors["error_messages"]["name"]


def test_has_errors_reads_as_a_stable_word():
    """The state is what automations compare against, in every language:
    the words the user sees come from the translation, not from the state."""
    from tests.test_hardening import FakeResponse, _api

    api = _api()
    api.data = {"1234": {}}
    api.make_api_call = lambda *_args, **_kwargs: FakeResponse(
        {"ConnectionStatus": 0, "Errors": [], "GroupTypeDescriptions": []}
    )

    api._fetch_device_status("1234")

    assert api.data["1234"]["1234-HasErrors"].value == "no"
