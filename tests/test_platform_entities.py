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

# The shared lookup, and the raw one it replaced. `_coordinator_row` asks two
# questions - is the row there, and is it still THIS platform's - and the
# second one is what a raw subscript cannot answer. After a reclassification
# both the old and the new entity are loaded and both find the row, so a
# platform reaching into the dict itself renders someone else's value as its
# own type: a holiday epoch as a switch that is on, a 0/1 as a date in 1970.
THE_SHARED_LOOKUP = "_coordinator_row()"
THE_RAW_LOOKUP = "coordinator.data[self._device_id]"


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


def _calls_the_shared_helper(source: str) -> bool:
    """Whether a module CALLS the helper, rather than merely naming it.

    An import names it. Every platform imports it at the top, so a substring
    scan was answered by that line alone: delete the actual call in
    async_setup_entry and the guard stayed green while the platform stopped
    producing entities altogether. Same blindness as a guard reading its own
    banner, and the third one of that shape in this repository.
    """
    import ast

    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == THE_SHARED_WAY
        for node in ast.walk(ast.parse(source))
    )


def test_the_scan_is_not_satisfied_by_the_import_line():
    """Proof that the check above can fail - a module that imports the helper
    and never calls it is exactly the state it has to report."""
    imports_only = f"from .entity import {THE_SHARED_WAY}\n\n\ndef setup():\n    pass\n"
    assert not _calls_the_shared_helper(imports_only)
    assert _calls_the_shared_helper(f"{THE_SHARED_WAY}(entry, add, 'switch', Cls)\n")


def test_every_platform_adds_its_entities_as_they_appear():
    """A platform that builds its list once shows nothing for a reading that
    arrives later, and looks perfectly healthy while doing it."""
    missing = [
        name
        for name, source in _platform_sources().items()
        if not _calls_the_shared_helper(source)
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


def test_no_platform_reaches_into_the_coordinator_for_its_own_row():
    """Reading a row and owning a row are two different questions.

    The platform check was added to the write path when a reclassification
    turned out to leave both entities loaded - and every one of the five
    display paths kept its own raw subscript, so the leftover entity went on
    publishing the new platform's value as its own type. Five copies of a
    lookup is five chances for the next one to drift back out, which is the
    same way the availability rule once reached three platforms out of four.

    Not limited to the update handler on purpose: the second offender this
    caught was a sensor helper feeding `extra_state_attributes`, which no
    test of the update handler would ever have looked at.
    """
    offenders = [
        name for name, source in _platform_sources().items() if THE_RAW_LOOKUP in source
    ]

    assert not offenders, (
        f"{offenders} read(s) the coordinator's dict directly "
        f"({THE_RAW_LOOKUP}), which cannot tell a row that is still this "
        f"platform's from one that is not. Use {THE_SHARED_LOOKUP} in "
        "entity.py."
    )


def test_the_lookup_scan_would_notice_the_shape_it_exists_for():
    """The two shapes, spelled out as they appear in a platform module. A
    guard nobody has seen fail is a guard nobody knows works."""
    raw = "            row = self.coordinator.data[self._device_id][self._data_key]\n"
    shared = "        row = self._coordinator_row()\n"

    assert THE_RAW_LOOKUP in raw and THE_SHARED_LOOKUP not in raw
    assert THE_RAW_LOOKUP not in shared and THE_SHARED_LOOKUP in shared


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
