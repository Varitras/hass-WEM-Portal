"""What happens to an entity whose platform changed between releases.

Our unique_id does not carry the platform, so a parameter that moves from one
platform to another leaves its old registry entry behind: it stops being
provided, goes unavailable, and sits next to the working entity forever.
Holiday begin and end did exactly that when the portal's parameter list showed
them to be dates rather than switches.

Removal is destructive, so the rules are pinned here rather than assumed: only
our own unique_ids, only our own platforms, and never the entity that is
currently correct.
"""

from custom_components.wemportal import (
    _remove_entities_from_a_previous_platform,
    get_wemportal_unique_id,
)
from custom_components.wemportal.const import DOMAIN

ENTRY_ID = "entry-1"
DEVICE = "1234"


class FakeConfigEntry:
    entry_id = ENTRY_ID


class FakeRegistry:
    """Just enough registry: a lookup by (platform, unique_id) and a remove."""

    def __init__(self, entries):
        self.entries = dict(entries)
        self.removed = []
        self.domains_asked = set()

    def async_get_entity_id(self, platform, domain, unique_id):
        self.domains_asked.add(domain)
        return self.entries.get((platform, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


def _uid(key):
    return get_wemportal_unique_id(ENTRY_ID, DEVICE, key)


def _run(registry, data):
    _remove_entities_from_a_previous_platform(registry, FakeConfigEntry(), DEVICE, data)


def test_the_entity_left_behind_by_a_platform_change_is_removed():
    registry = FakeRegistry(
        {
            ("switch", _uid("Heat pump-U_Beginn")): "switch.heat_pump_holiday_begin",
            ("date", _uid("Heat pump-U_Beginn")): "date.heat_pump_holiday_begin",
        }
    )

    _run(registry, {"Heat pump-U_Beginn": {"platform": "date"}})

    assert registry.removed == ["switch.heat_pump_holiday_begin"]


def test_the_entity_that_is_currently_correct_is_never_removed():
    registry = FakeRegistry(
        {
            ("date", _uid("Heat pump-U_Beginn")): "date.heat_pump_holiday_begin",
        }
    )

    _run(registry, {"Heat pump-U_Beginn": {"platform": "date"}})

    assert registry.removed == []


def test_an_unchanged_platform_removes_nothing():
    registry = FakeRegistry(
        {
            ("switch", _uid("Heat pump-Pump")): "switch.heat_pump_pump",
        }
    )

    _run(registry, {"Heat pump-Pump": {"platform": "switch"}})

    assert registry.removed == []


def test_only_our_own_unique_ids_are_touched():
    """The lookup must use the FULL unique_id, not the bare data key.

    The bare key is not a hypothetical: releases before the current id format
    registered entities under exactly that, which is why the migration next
    door still looks for it. An entry found under it belongs to an entity we
    are supposed to be preserving history for - removing it here would delete
    the very thing the migration exists to keep.

    Another config entry's copy of the same parameter is the same mistake one
    step further out.
    """
    registry = FakeRegistry(
        {
            ("switch", "Heat pump-U_Beginn"): "switch.registered_under_the_bare_key",
            ("switch", "some-other-integrations-id"): "switch.someone_elses",
            (
                "switch",
                get_wemportal_unique_id("entry-2", DEVICE, "Heat pump-U_Beginn"),
            ): "switch.other_entry",
        }
    )

    _run(registry, {"Heat pump-U_Beginn": {"platform": "date"}})

    assert registry.removed == []


def test_the_lookup_stays_inside_this_integration():
    registry = FakeRegistry({})

    _run(registry, {"Heat pump-U_Beginn": {"platform": "date"}})

    assert registry.domains_asked == {DOMAIN}


def test_a_plain_counter_in_the_data_is_skipped():
    """coordinator.data carries non-dict bookkeeping entries alongside the
    entities; walking into one would raise."""
    registry = FakeRegistry({})

    _run(registry, {"ConnectionStatus": 0})

    assert registry.removed == []


def test_a_value_without_a_platform_counts_as_a_sensor():
    """The default the platforms themselves use, so a sensor is not mistaken
    for a leftover and deleted."""
    registry = FakeRegistry(
        {
            ("sensor", _uid("Heat pump-Outside")): "sensor.heat_pump_outside",
        }
    )

    _run(registry, {"Heat pump-Outside": {"friendlyName": "Outside"}})

    assert registry.removed == []
