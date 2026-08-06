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


class ExpertOperationAborted(Exception):
    """Raised when the configuration a portal operation belongs to is gone.

    Its own type so the caller can tell "we deliberately stopped" apart from
    "the portal rejected the write" and skip the user-facing notification.

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
