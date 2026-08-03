"""Reading what the mobile API answers, with no HTTP anywhere in sight.

Two of its answers decide whether a cycle did what it claims, and both were
read wrongly here for the same kind of reason - a payload shape nobody had
looked at:

  * `Status: false` counted as success, because Python compares False equal
    to 0. A write the portal had rejected was reported as done, on a heating
    parameter.
  * A refresh that named no job still went on to read, and a read without a
    JobID is answered from whichever job the portal considers newest - the
    previous measurement, booked as current.

Both are decisions about a parsed payload and nothing else. Keeping them here
as plain functions is what makes a table of shapes possible: each case is one
line, so adding the shape that just broke is cheap enough that it actually
happens. Everything impure - the request, the logging, the exceptions - stays
with the caller.
"""

from __future__ import annotations

from typing import Any, NamedTuple


def status_is_success(status: Any) -> bool:
    """Whether the portal's `Status` field means "this worked".

    Only the integer 0 does. `type(status) is int` rather than isinstance:
    bool is a subclass of int, which is precisely how `Status: false` passed
    for success at all three places that used to ask `status != 0`.
    """
    return type(status) is int and status == 0


class WriteAck(NamedTuple):
    """What the portal said about a parameter write."""

    acknowledged: bool
    status: Any
    message: Any


def read_write_ack(payload: Any) -> WriteAck:
    """Read the answer to a write.

    Anything that is not an explicit Status 0 is a rejection. Reporting a
    write as done when it was not is the worse error by far: the entity shows
    the requested value until the next poll quietly replaces it.
    """
    if not isinstance(payload, dict):
        return WriteAck(False, None, None)
    status = payload.get("Status")
    # Message carries the portal's own wording. It is present on SUCCESS too,
    # simply as null - so its presence says nothing, which is why the rule
    # suggested by the only available third-party reference ("Message means
    # failure") would have failed every legitimate write.
    return WriteAck(status_is_success(status), status, payload.get("Message"))


class RefreshTicket(NamedTuple):
    """What the portal said when asked to start a measurement.

    `accepted` and `job_id` are deliberately separate. A refused refresh must
    stop the read; a refresh that was accepted but named no job is a
    different, unproven case - see the caller.
    """

    accepted: bool
    job_id: Any
    reason: str | None


def read_refresh_ticket(payload: Any) -> RefreshTicket:
    """Read the answer to a measurement refresh.

    A rejected refresh comes back as HTTP 200 with a non-zero Status, exactly
    like a rejected login. Only the JobID used to be read here, so a rejection
    went unnoticed and the read below returned the previous measurement.
    """
    if not isinstance(payload, dict):
        return RefreshTicket(
            False, None,
            f"answered with {type(payload).__name__} instead of an object",
        )
    status = payload.get("Status")
    # An ABSENT status stays acceptable: not every answer carries one, and
    # refusing those would fail cycles that work today.
    if status is not None and not status_is_success(status):
        return RefreshTicket(False, None, f"refused the refresh (Status {status})")
    return RefreshTicket(True, payload.get("JobID"), None)
