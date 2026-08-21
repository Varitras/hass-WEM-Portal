"""Tables over what the mobile API can answer.

Both readers in mobile_protocol.py exist because a payload shape nobody had
looked at was read wrongly - `Status: false` as success, and a refresh naming
no job still going on to read. Those are decisions about a parsed dict and
nothing else, so they are checked as tables: one line per shape, which is
what makes adding the shape that just broke cheap enough to actually do.
"""

import pytest

from custom_components.wemportal.mobile_protocol import (
    described_parameters,
    read_refresh_ticket,
    read_write_ack,
    status_is_success,
)


@pytest.mark.parametrize(
    "unusable",
    [
        None,  # cached as a dict KEY of None, then read back by nobody
        [],  # unhashable: TypeError the moment it is used as a key
        {},  # same
        123,  # not a key the portal ever asks for
        "",  # a name nothing can be looked up under
        "   ",
    ],
)
def test_a_row_whose_parameter_id_is_not_a_name_is_dropped(unusable):
    """Presence of the key was the whole check, so the VALUE could be
    anything - and every one of these ends badly one layer down, where the
    id becomes a dict key: None caches a parameter nobody can ask for, a
    list or dict raises TypeError and takes the rest of the module's
    discovery with it.
    """
    payload = {"Parameters": [{"ParameterID": unusable}, {"ParameterID": "P1"}]}

    assert described_parameters(payload) == [{"ParameterID": "P1"}]


def test_a_usable_parameter_id_is_kept_verbatim():
    """The counter-test: whatever the portal calls a parameter is its name,
    and this must not start trimming or rewriting it."""
    payload = {"Parameters": [{"ParameterID": "Heizprogramm1", "DataType": 6}]}

    assert described_parameters(payload) == [
        {"ParameterID": "Heizprogramm1", "DataType": 6}
    ]


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        (0, True),
        (False, False),  # the bug: equal to 0 in Python, not a success
        (True, False),
        (3, False),
        (-1, False),
        (None, False),
        ("0", False),
        (0.0, False),  # also == 0, also not what the portal sends
        ([], False),
    ],
)
def test_only_the_integer_zero_is_a_portal_success(status, ok):
    assert status_is_success(status) is ok


@pytest.mark.parametrize(
    ("payload", "acknowledged"),
    [
        ({"JobID": 762338890, "Status": 0, "Message": None}, True),  # observed
        ({"Status": 0}, True),
        ({"Status": False}, False),
        ({"Status": 3, "Message": "value out of range"}, False),
        ({"Status": None}, False),
        ({"Message": "write failed"}, False),
        ({"JobID": 1}, False),  # a result, but not a verdict
        ({}, False),
        ([], False),
        (None, False),
        ("Status: 0", False),
    ],
)
def test_a_write_counts_as_done_only_on_an_explicit_status_zero(payload, acknowledged):
    assert read_write_ack(payload).acknowledged is acknowledged


def test_a_rejected_write_carries_the_portal_reason():
    """The wording is what the user is shown; dropping it leaves a number."""
    ack = read_write_ack({"Status": 3, "Message": "value out of range"})

    assert ack.acknowledged is False
    assert ack.status == 3
    assert ack.message == "value out of range"


def test_a_successful_write_may_carry_a_message_too():
    """Observed: success answers with `Message: null` present. The rule
    "Message means failure" - the only one the third-party reference gives -
    would therefore have failed every legitimate write."""
    assert read_write_ack({"Status": 0, "Message": None}).acknowledged is True


@pytest.mark.parametrize(
    ("payload", "accepted", "job_id"),
    [
        ({"Status": 0, "JobID": 100000001}, True, 100000001),
        ({"JobID": 100000001}, True, 100000001),  # no status is acceptable
        ({"Status": 0}, True, None),  # accepted, but named no job
        ({}, True, None),
        ({"Status": 3}, False, None),
        ({"Status": False}, False, None),
        ({"Status": "0"}, False, None),
        ([], False, None),
        (None, False, None),
    ],
)
def test_a_refresh_is_read_as_accepted_and_as_a_job(payload, accepted, job_id):
    ticket = read_refresh_ticket(payload)

    assert ticket.accepted is accepted
    assert ticket.job_id == job_id


def test_a_refused_refresh_says_why():
    """The reason goes into the warning, so it has to name the status."""
    assert "Status 3" in read_refresh_ticket({"Status": 3}).reason


def test_a_non_object_answer_says_what_it_was():
    assert "list" in read_refresh_ticket([]).reason


def test_accepted_and_job_id_are_separate_answers():
    """Guards the shape itself.

    Collapsing them would force the caller to treat "refused" and "named no
    job" alike - and those two must not be treated alike: one has to stop the
    read, the other is an unproven case that is only reported, because
    failing it would take a single-device installation off the air entirely.
    """
    refused = read_refresh_ticket({"Status": 3})
    jobless = read_refresh_ticket({"Status": 0})

    assert (refused.accepted, refused.job_id) == (False, None)
    assert (jobless.accepted, jobless.job_id) == (True, None)
