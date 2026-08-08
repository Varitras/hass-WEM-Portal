"""Exceptions for the wemportal component."""

from homeassistant.exceptions import HomeAssistantError


class WemPortalError(HomeAssistantError):
    """
    Custom exception for WEM Portal errors
    """


class AuthError(WemPortalError):
    """Exception to indicate an authentication error."""


class UnknownAuthError(WemPortalError):
    """Exception to indicate an unknown authentication error."""


class ServerError(WemPortalError):
    """Exception to indicate a server error."""


class ForbiddenError(WemPortalError):
    """Exception to indicate a forbidden error (403)."""


class ExpiredSessionError(WemPortalError):
    """
    Custom exception for expired session errors
    """


class ParameterChangeError(WemPortalError):
    """
    Custom exception for parameter change errors
    """


class ParameterWriteError(WemPortalError):
    """Raised when an expert-parameter write is not confirmed by the portal.

    Carries the form state read during the attempt, where the attempt got far
    enough to read one. A refusal is the one failure that still knows exactly
    what the portal currently offers, and a caller whose idea of the range has
    gone stale is the one that lands here - a heating parameter's limits can
    depend on other settings, so a range read weeks ago need not still hold.
    Reporting the failure while throwing that knowledge away leaves the entity
    on the range that caused it.

    What this does NOT reach: a published range so far off that Home Assistant
    refuses the value first. It validates against min/max before the entity is
    asked, so the write that would correct the range never arrives. Only a
    value the published range still admits gets far enough to be refused by
    the portal - which is the case where the two ranges overlap, and that is
    the case this is for.
    """

    def __init__(self, message, state=None):
        super().__init__(message)
        self.state = state


class ApiBusyError(WemPortalError):
    """A previous poll is still running and holds the shared API lock.

    Deliberately its own type: the coordinator treats a WemPortalError as
    "the session may be corrupted" and re-instantiates the api after two of
    them - which would close the HTTP sessions the still-running thread is
    using, and hand the next poll a FRESH lock, removing the very
    serialization this error reports. The condition is the opposite of a
    broken session: everything works, it is just still busy.
    """


class PollDeadlineExceeded(BaseException):
    """The poll cycle used up its time budget and stopped itself.

    NOT an Exception subclass, and that is the point rather than a detail.
    This is a control-flow signal that has to travel out through code whose
    entire job is to keep a poll going: wemportalapi has twelve
    `except Exception` handlers, each right on its own terms - one device
    failing must not take the others down, one heating programme failing must
    not skip the rest - and every one of them swallowed this. The cycle then
    reported success, the worker ran on holding the shared lock, and the
    coordinator handler written for exactly this was never reached.

    Making each of the twelve re-raise it works until somebody writes the
    thirteenth. Python's own control-flow signals - KeyboardInterrupt,
    SystemExit, asyncio.CancelledError - are BaseException for this reason.

    `finally` still runs, so fetch_data releases the API lock as before, and
    coordinator catches this type by name and turns it into UpdateFailed.

    Raised rather than returned so a half-finished cycle fails honestly. The
    partial readings are still in api.data and the next cycle builds on them;
    what must not happen is Home Assistant recording an incomplete read as a
    successful one.
    """


class ExpertOperationAborted(BaseException):
    """Raised when the configuration a portal operation belongs to is gone.

    Its own type so the caller can tell "we deliberately stopped" apart from
    "the portal rejected the write" and skip the user-facing notification.

    NOT an Exception subclass, for the same reason as PollDeadlineExceeded:
    it is a control-flow signal that has to travel out through code whose job
    is to keep going. read_many catches per id so one unreadable parameter
    does not lose the others - correctly - and swallowed this with it, so a
    teardown was recorded as "that parameter failed" and the loop moved on to
    the next id, opening more portal navigation for a configuration that was
    already gone.

    `finally` still runs, so the sessions those paths close are still closed,
    and the two handlers that DO want it - expert_controller and the entity
    write - catch it by name.

    Lives here rather than beside the expert client because three modules now
    need to catch it, and importing that client pulls curl_cffi and lxml onto
    whatever path does the import - which is why two of those three had a
    function-local import instead.
    """


class PortalMaintenanceError(WemPortalError):
    """The portal is down for planned maintenance.

    Deliberately NOT an AuthError: the credentials are fine, the backend is
    simply unavailable. Counting it as an auth failure escalated to
    ConfigEntryAuthFailed after AUTH_ERROR_ESCALATION_THRESHOLD cycles and
    asked the user to re-enter working credentials.
    """
