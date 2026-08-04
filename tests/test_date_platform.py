"""The portal's date encoding, in both directions.

Holiday begin and end travel as Unix epoch seconds. Getting the conversion
wrong is silent in the reading direction and destructive in the writing one:
a write goes to a real heating system, and an off-by-one-day is not something
the user can see in the entity afterwards - it will simply show what it wrote.
"""

from datetime import date

import pytest

from custom_components.wemportal.date import date_to_epoch, epoch_to_date

# Measured on a live installation, not constructed: holiday begin and end came
# back as these two values, exactly 86400 apart and both exactly on a midnight
# UTC boundary.
BEGIN_EPOCH = 1785715200.0
END_EPOCH = 1785801600.0


def test_the_measured_values_decode_to_the_days_they_stood_for():
    assert epoch_to_date(BEGIN_EPOCH) == date(2026, 8, 3)
    assert epoch_to_date(END_EPOCH) == date(2026, 8, 4)


def test_a_day_survives_the_round_trip():
    for day in (date(2026, 8, 3), date(2026, 1, 1), date(2026, 12, 31)):
        assert epoch_to_date(date_to_epoch(day)) == day


def test_writing_uses_the_encoding_the_portal_sent():
    """Midnight UTC, and a whole multiple of a day.

    Local midnight would look identical in a test run in UTC and shift every
    write by the offset anywhere else - east of Greenwich onto the previous
    day.
    """
    assert date_to_epoch(date(2026, 8, 3)) == BEGIN_EPOCH
    assert date_to_epoch(date(2026, 8, 3)) % 86400 == 0


@pytest.mark.parametrize("value", [None, "", "not a number", [], {}])
def test_an_unusable_value_reads_as_no_date(value):
    """None, not a fallback date: "no holiday set" and "1st of January 1970"
    must not look the same to an automation."""
    assert epoch_to_date(value) is None


def test_an_out_of_range_epoch_reads_as_no_date():
    assert epoch_to_date(1e30) is None


def test_a_numeric_string_is_still_accepted():
    """The portal has been observed sending numbers as strings elsewhere
    (the value's own Timestamp field is one), so this stays tolerant."""
    assert epoch_to_date(str(int(BEGIN_EPOCH))) == date(2026, 8, 3)
