""" Exceptions for the wemportal component."""

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
    """Raised when an expert-parameter write is not confirmed by the portal."""


class ApiBusyError(WemPortalError):
    """A previous poll is still running and holds the shared API lock.

    Deliberately its own type: the coordinator treats a WemPortalError as
    "the session may be corrupted" and re-instantiates the api after two of
    them - which would close the HTTP sessions the still-running thread is
    using, and hand the next poll a FRESH lock, removing the very
    serialization this error reports. The condition is the opposite of a
    broken session: everything works, it is just still busy.
    """
