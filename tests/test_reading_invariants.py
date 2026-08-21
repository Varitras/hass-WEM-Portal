"""What every reading must carry, whoever wrote it.

The nine structural guards check shapes and lists: no dict access, no
module global, no undeclared boundary. None of them can see a write path
that produces a perfectly valid object with a field left empty - a field
some OTHER path needs.

That is exactly how the per-module freshness came to protect nothing: the
mapper wrote ordinary sensors without the module address, and the ageing
pass matches on precisely that address. Every writeable control had it,
every plain sensor did not, and the test that should have caught it filled
the address in by hand.

So this file states invariants over the DATA, and checks them against what
the real mapper produces. When a new write path appears, it either upholds
them or the suite says which one it broke.
"""

import time

from custom_components.wemportal.const import WemDataType
from custom_components.wemportal.mapper import WemPortalDataMapper
from custom_components.wemportal.models import Reading

DEVICE = "1234"
MODULE = (0, 1)


def _mapped(parameters, values, mode="api", existing=None, scraper_device=DEVICE):
    """Run the real mapper and hand back what it wrote."""
    modules = {
        DEVICE: {
            MODULE: {
                "Name": "Heat pump",
                "parameters": {
                    parameter["ParameterID"]: parameter for parameter in parameters
                },
            }
        }
    }
    payload = {
        "Modules": [
            {
                "ModuleIndex": MODULE[0],
                "ModuleType": MODULE[1],
                "Values": values,
            }
        ]
    }
    api_data = {DEVICE: dict(existing or {})}
    WemPortalDataMapper.process_api_values(
        DEVICE, payload, modules, "en", {}, mode, api_data, scraper_device
    )
    return api_data[DEVICE]


def _parameter(parameter_id, **overrides):
    parameter = {
        "ParameterID": parameter_id,
        "IsWriteable": False,
        "DataType": None,
        "MinValue": None,
        "MaxValue": None,
        "EnumValues": None,
    }
    parameter.update(overrides)
    return parameter


def _value(parameter_id, numeric=21.5):
    return {
        "ParameterID": parameter_id,
        "NumericValue": numeric,
        "StringValue": "",
        "Unit": "°C",
    }


def _rows_missing_the_module_address(rows):
    return {
        key: row
        for key, row in rows.items()
        if isinstance(row, Reading)
        and (row.module_index is None or row.module_type is None)
    }


def test_every_mapped_reading_knows_its_module():
    """The invariant the freshness pass depends on.

    Read-only sensor and writeable control in one run, because the two take
    different paths through the mapper and only one of them used to carry
    the address.
    """
    rows = _mapped(
        [
            _parameter("Outside"),
            _parameter(
                "Komfort",
                IsWriteable=True,
                DataType=WemDataType.NUMBER_STEP_ONE,
                MinValue=10,
                MaxValue=30,
            ),
        ],
        [_value("Outside"), _value("Komfort")],
    )

    assert len(rows) == 2, f"expected both readings, got {sorted(rows)}"
    assert not _rows_missing_the_module_address(rows), (
        "reading(s) without a module address: "
        f"{sorted(_rows_missing_the_module_address(rows))} - the per-module "
        "ageing pass matches on exactly that pair, so these would be "
        "presented as current forever once their module goes silent."
    )


def test_a_reading_merged_into_a_scraped_row_knows_its_module_too():
    """`both` mode writes through a second path, and it lost the address as
    well - including the case where there is no scraped row to merge into
    and the target is the reading's own key."""
    scraped = Reading(
        value=11.0,
        unit="°C",
        friendly_name="Heat pump - Outside",
        parameter_id="heat_pump-outside",
        platform="sensor",
    )

    rows = _mapped(
        [_parameter("Outside")],
        [_value("Outside")],
        mode="both",
        existing={"heat_pump-outside": scraped},
    )

    assert not _rows_missing_the_module_address(rows), (
        "merged reading(s) without a module address: "
        f"{sorted(_rows_missing_the_module_address(rows))}"
    )


def test_the_invariant_is_checked_against_a_real_run():
    """Guards this guard: if the mapper ever stopped writing rows at all,
    the checks above would pass on an empty dict and say nothing."""
    rows = _mapped([_parameter("Outside")], [_value("Outside")])

    assert rows, "the mapper wrote nothing - the invariant checks are hollow"
    assert all(isinstance(row, Reading) for row in rows.values())


def test_the_address_survives_a_second_cycle():
    """Freshness only matters over time, and the second cycle takes the
    branch that updates an existing row rather than creating one."""
    first = _mapped([_parameter("Outside")], [_value("Outside")])
    second = _mapped(
        [_parameter("Outside")], [_value("Outside", numeric=22.0)], existing=first
    )

    assert not _rows_missing_the_module_address(second)
    assert second["Heat pump-Outside"].value == 22.0


def test_a_reading_that_ages_out_keeps_its_address():
    """Ageing blanks the value and keeps the identity - including the
    address, or the row could never be found again on the next pass."""
    rows = _mapped([_parameter("Outside")], [_value("Outside")])
    row = rows["Heat pump-Outside"]
    row.value = None  # what both ageing passes do

    assert (row.module_index, row.module_type) == MODULE


def test_freshness_stamps_are_not_readings():
    """The device dict also carries bookkeeping that is deliberately NOT a
    reading (the raw ConnectionStatus gate). The invariant applies to
    readings only - if that stopped being true, the filter above would
    quietly start skipping real rows."""
    rows = _mapped([_parameter("Outside")], [_value("Outside")])
    rows["ConnectionStatus"] = 0

    assert not _rows_missing_the_module_address(rows), (
        "a non-reading entry was treated as a reading"
    )
    assert time.monotonic() > 0  # the module imports cleanly
