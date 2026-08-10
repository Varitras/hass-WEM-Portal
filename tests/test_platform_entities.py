"""Every platform builds its entities the same way, and that way is not once.

The five platforms each walked `coordinator.data` during setup and never
looked again, so a reading that arrives on a LATER cycle had no entity and
never got one - not until somebody reloaded the entry by hand. That is not
an exotic case: a device unreachable at startup is skipped by
get_parameters, the parameter re-discovery deliberately waits for the second
cycle, and the hourly statistics - the Energy Dashboard rows - appear
minutes after setup whenever the first attempt fails.

The fix is one shared helper. This guard exists because it would be so easy
to write the old loop again in a sixth platform, or back into one of these,
and nothing about the result would look wrong: the entities that DO exist
work perfectly.

Driven off PLATFORMS rather than a list written out here, so a platform
added later is covered without anyone remembering to come back.
"""

import pathlib
import types

from custom_components.wemportal.const import PLATFORMS
from custom_components.wemportal.entity import async_add_readings_as_they_appear
from custom_components.wemportal.models import Reading

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "wemportal"
)

THE_SHARED_WAY = "async_add_readings_as_they_appear"

# What the one-shot version looked like. Any platform still walking the
# coordinator's data itself is doing the thing this guard exists against.
THE_OLD_WAY = "coordinator.data.items()"


def _platform_sources() -> dict:
    sources = {}
    for platform in PLATFORMS:
        source_file = PACKAGE / f"{platform}.py"
        assert source_file.exists(), (
            f"{platform} is in PLATFORMS but {source_file.name} does not exist - "
            "either the platform moved or this scan has gone blind"
        )
        sources[source_file.name] = source_file.read_text(encoding="utf-8")
    return sources


def test_every_platform_adds_its_entities_as_they_appear():
    """A platform that builds its list once shows nothing for a reading that
    arrives later, and looks perfectly healthy while doing it."""
    missing = [
        name
        for name, source in _platform_sources().items()
        if THE_SHARED_WAY not in source
    ]

    assert not missing, (
        f"{missing} do(es) not use {THE_SHARED_WAY}, so any reading that "
        "arrives after setup gets no entity until the entry is reloaded by "
        "hand. Call the helper in entity.py instead of walking the data here."
    )


def test_no_platform_walks_the_coordinator_data_itself():
    """The mirror of the test above: calling the helper AND keeping the old
    loop would create every entity twice."""
    offenders = [
        name for name, source in _platform_sources().items() if THE_OLD_WAY in source
    ]

    assert not offenders, (
        f"{offenders} still walk(s) the coordinator's data during setup "
        f"({THE_OLD_WAY}). One place decides which readings become entities."
    )


class _Coordinator:
    """Enough of the coordinator to hand out an update and take it back."""

    def __init__(self, data):
        self.data = data
        self.listeners: list = []

    def async_add_listener(self, update):
        self.listeners.append(update)
        return lambda: self.listeners.remove(update)


class _Entry:
    def __init__(self, coordinator):
        self.runtime_data = types.SimpleNamespace(coordinator=coordinator)

    def async_on_unload(self, remove) -> None:
        """Home Assistant keeps these; nothing here unloads."""


def test_a_reading_gets_one_entity_however_often_the_coordinator_updates():
    """Every update re-walks the data, so without a record of what already
    has an entity, each cycle would build the whole set again.

    Not visible from the outside: Home Assistant rejects the duplicate
    unique_id, so the second entity never appears in the state machine. What
    appears is an error per cycle in the log and objects nobody collects -
    which is why this asks the builder how often it was called rather than
    counting entities afterwards.
    """
    built = []
    coordinator = _Coordinator(
        {"1234": {"Heat pump-P1": Reading(value=1.0, platform="sensor")}}
    )
    entry = _Entry(coordinator)

    async_add_readings_as_they_appear(
        entry,
        built.extend,
        "sensor",
        lambda *arguments: object(),
    )
    for update in list(coordinator.listeners):
        update()
        update()

    assert len(built) == 1, (
        f"one reading was turned into {len(built)} entities - every update "
        "rebuilt the whole set"
    )


def test_the_scan_would_notice_the_shape_it_exists_for():
    """A guard that passes proves nothing. These are the two shapes it has to
    tell apart, spelled out as they appear in a platform module."""
    one_shot = (
        "    coordinator = config_entry.runtime_data.coordinator\n"
        "    for device_id, entity_data in coordinator.data.items():\n"
    )
    shared = (
        "    async_add_readings_as_they_appear(\n"
        '        config_entry, async_add_entities, "sensor", WemPortalSensor\n'
        "    )\n"
    )

    assert THE_OLD_WAY in one_shot and THE_SHARED_WAY not in one_shot
    assert THE_OLD_WAY not in shared and THE_SHARED_WAY in shared
