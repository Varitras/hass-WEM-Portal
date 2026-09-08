"""Which devices the user may delete by hand, and which are not theirs to.

An installation changes: a module is taken out, a device is replaced. Its
device in Home Assistant stayed forever - the registry only offers the delete
button to an integration that says it can decide, and this one never did. The
leftover sat in the device list with entities that would never update again.

The decision is one question - does the portal still report this device - and
two things it must not get wrong. The hub is not a portal device at all: it is
the entry itself, it carries the expert entities, and deleting it would take
them with it. And a coordinator that has not refreshed yet knows of no device
whatsoever, which looks exactly like an installation that lost all of them.
"""

import types

import pytest

from custom_components.wemportal import async_remove_config_entry_device
from custom_components.wemportal.const import DOMAIN
from custom_components.wemportal.utils import device_identifier

ENTRY_ID = "entry-1"
LIVE_DEVICE = "1234"
GONE_DEVICE = "5678"


def _entry(data):
    """An entry whose coordinator holds `data`; None for one never loaded."""
    if data is None:
        return types.SimpleNamespace(entry_id=ENTRY_ID)
    return types.SimpleNamespace(
        entry_id=ENTRY_ID,
        runtime_data=types.SimpleNamespace(
            coordinator=types.SimpleNamespace(data=data)
        ),
    )


def _device(*identifiers):
    return types.SimpleNamespace(identifiers=set(identifiers))


async def _may_remove(entry, device):
    return await async_remove_config_entry_device(None, entry, device)


async def test_a_device_the_portal_no_longer_reports_can_be_deleted():
    entry = _entry({LIVE_DEVICE: {}})
    gone = _device(device_identifier(ENTRY_ID, GONE_DEVICE))

    assert await _may_remove(entry, gone) is True


async def test_a_device_the_portal_still_reports_is_not_deletable():
    """Deleting a live device takes its entities with it and the next cycle
    builds them again - with no history and, for anything the user renamed,
    none of that either."""
    entry = _entry({LIVE_DEVICE: {}})
    live = _device(device_identifier(ENTRY_ID, LIVE_DEVICE))

    assert await _may_remove(entry, live) is False


async def test_the_hub_is_not_a_device_the_user_may_delete():
    """It is the entry, not a portal device - and the expert numbers hang off
    it, so removing it would silently take entities the portal still serves."""
    # A polled entry on purpose: with no data every device is refused
    # anyway, and the refusal under test would never have been reached.
    entry = _entry({LIVE_DEVICE: {}})
    hub = _device((DOMAIN, ENTRY_ID))

    assert await _may_remove(entry, hub) is False


@pytest.mark.parametrize("data", [None, {}])
async def test_an_entry_that_has_not_polled_yet_declares_nothing_stale(data):
    """Before the first refresh every device looks gone. Answering then would
    invite the user to delete the whole installation while it is starting up."""
    entry = _entry(data)
    any_device = _device(device_identifier(ENTRY_ID, LIVE_DEVICE))

    assert await _may_remove(entry, any_device) is False
