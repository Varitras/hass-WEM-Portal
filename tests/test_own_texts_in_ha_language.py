"""Texts the integration writes itself - not names from the portal - are in
Home Assistant's language.

With Home Assistant in German, three of them were still English: the name a
slot without a name of its own gets ("Expert Parameter 3"), the notification
after a confirmed expert write, and the status line of the options form
when discovery could not run. And "Error Messages" said "None" when there
was no fault.
"""

import json
import types
from pathlib import Path

import pytest

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "wemportal" / "translations"
)


@pytest.fixture
def _custom_integrations(enable_custom_integrations):
    """Home Assistant finds a custom integration's texts only with this."""
    yield


def _catalogue(language):
    return json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))


def test_a_slot_without_a_name_is_named_by_its_translation():
    from custom_components.wemportal import expert_number

    entry = types.SimpleNamespace(
        entry_id="e1", data={}, options={}, title="WEM Portal"
    )
    entity = expert_number.WemPortalExpertNumber(entry, "expert_parameter_3", "A" * 36)

    assert entity.translation_key == "expert_parameter"
    assert entity.translation_placeholders == {"slot": "3"}
    assert not hasattr(entity, "_attr_name"), (
        "a set name hides the translation Home Assistant would show"
    )


def test_a_slot_with_a_name_keeps_it():
    from custom_components.wemportal import expert_number

    entry = types.SimpleNamespace(
        entry_id="e1", data={}, options={}, title="WEM Portal"
    )
    entity = expert_number.WemPortalExpertNumber(entry, "WW Soll", "A" * 36)

    assert entity.name == "Ww Soll"
    assert entity.translation_key is None


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_own_texts_exist_in_both_languages(language):
    catalogue = _catalogue(language)
    assert "{slot}" in catalogue["entity"]["number"]["expert_parameter"]["name"]
    assert catalogue["entity"]["sensor"]["error_messages"]["state"]["none"]
    exceptions = catalogue["exceptions"]
    for key in (
        "expert_write_succeeded_title",
        "expert_write_succeeded",
        "discovery_paused_expert",
        "discovery_paused_network",
        "discovery_refused",
    ):
        assert exceptions[key]["message"], (language, key)


def test_no_fault_is_a_stable_word():
    """Translated where it is shown; the state an automation reads stays the
    same in every language."""
    from custom_components.wemportal.utils import error_state_and_detail

    assert error_state_and_detail([]) == ("none", [])


async def test_a_confirmed_write_is_announced_in_home_assistants_language(
    hass, _custom_integrations
):
    from custom_components.wemportal.utils import async_translated_message

    hass.config.language = "de"

    message = await async_translated_message(
        hass, "expert_write_succeeded", parameter="WW Soll", value="55"
    )

    assert message == "WW Soll auf 55 gesetzt."


async def test_a_paused_expert_path_is_reported_in_home_assistants_language(
    hass, _custom_integrations
):
    from custom_components.wemportal.exceptions import PausedAfterRefusal
    from custom_components.wemportal.options_flow import blocked_discovery_status

    hass.config.language = "de"

    status = await blocked_discovery_status(
        hass, PausedAfterRefusal("paused", remaining="~4 min", expert_only=True)
    )

    assert "~4 min" in status
    assert "Fachmann" in status


async def test_a_paused_network_and_a_fresh_refusal_are_said_too(
    hass, _custom_integrations
):
    """The other two answers of the status line - only the expert pause had a
    test, so the other two could have said anything."""
    from custom_components.wemportal.exceptions import (
        ForbiddenError,
        PausedAfterRefusal,
    )
    from custom_components.wemportal.options_flow import blocked_discovery_status

    hass.config.language = "de"
    texts = _catalogue("de")["exceptions"]

    paused = await blocked_discovery_status(
        hass, PausedAfterRefusal("paused", remaining="~4 min", expert_only=False)
    )
    refused = await blocked_discovery_status(hass, ForbiddenError("refused"))

    assert paused == texts["discovery_paused_network"]["message"].format(
        remaining="~4 min"
    )
    assert refused == texts["discovery_refused"]["message"]


def test_a_pause_says_whose_it_is_and_how_long_it_holds():
    """The form tells the expert path's own pause from the network's."""
    import time

    from custom_components.wemportal import transport
    from custom_components.wemportal.exceptions import PausedAfterRefusal
    from custom_components.wemportal.wemportalapi import WemPortalApi

    transport.reset_cooldowns_for_tests()
    api = WemPortalApi("user@example.org", "secret")
    api._account_state.expert_blocked_until = time.monotonic() + 120
    try:
        with pytest.raises(PausedAfterRefusal) as expert_pause:
            api.check_expert_cooldown()
        api._activate_cooldown("a test 403")
        with pytest.raises(PausedAfterRefusal) as network_pause:
            api.check_cooldown()
    finally:
        transport.reset_cooldowns_for_tests()
        api._account_state.expert_blocked_until = 0.0

    assert expert_pause.value.expert_only is True
    assert "min" in expert_pause.value.remaining
    assert network_pause.value.expert_only is False
