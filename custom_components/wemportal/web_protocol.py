"""The web login's maintenance protocol, with no idea of the domain.

The maintenance marker and how a downtime page is recognised are the same
question for three callers - the mobile transport's web_login, the scraper and
the expert client - and none of it knows what a device, a module or a reading
is. It lived in utils.py, which DOES pull in the sensor and reading models, so
the transport half imported the whole domain transitively just to read a
downtime page. Here it stands on its own; tests/test_transport_boundary pins
that the transport reaches for this rather than utils.
"""

from typing import Final
from urllib.parse import urlsplit
import logging
import re

from .const import WEB_LOGGED_IN_MARKER

_LOGGER = logging.getLogger(__name__)

# The portal announces planned downtime by rendering this container - on the
# login page and on the logged-in pages, and hours BEFORE the window opens as
# well as during it (measured 2026-09-23: on every page from 13:05 for a
# 17:00-20:00 window, the portal working normally throughout). Matched on the
# CSS class, NOT on its text: the wording changes per announcement and is
# localised, while the class is purpose-built and language-independent. So the
# marker says "downtime is announced", not "the portal is down" - see
# maintenance_blocking for when it means the latter.
WEB_MAINTENANCE_MARKER: Final = "offlinecontent"


def maintenance_notice(html_text: str) -> str | None:
    """Return the portal's maintenance notice, or None if there is none.

    Detected via the dedicated `offlinecontent` container (see
    WEB_MAINTENANCE_MARKER), not via keywords: the announcement text is
    localised and changes every time, the class does not. The text is only
    read out afterwards, to put the actual window into the log.
    """
    if not html_text or WEB_MAINTENANCE_MARKER not in html_text:
        return None
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html_text)
        for div in tree.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '),"
            " ' " + WEB_MAINTENANCE_MARKER + " ')]"
        ):
            text = " ".join(div.text_content().split())
            if text:
                return text
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not read the maintenance notice: %s", exc)
    # Marker present but unreadable - still a maintenance page.
    return "The portal reports scheduled maintenance."


# The mobile API has no structural downtime marker like the web page's
# offlinecontent class - during maintenance it answers a login with an ordinary
# error carrying a maintenance MESSAGE. These roots are matched in that message.
# Text rather than a status code on purpose: the one internal status seen with
# maintenance so far (8000) is not confirmed to mean only that, whereas a
# credentials error never carries these words - so matching them cannot hide a
# real auth failure, and a miss only leaves today's behaviour. DE covers this
# portal's own wording, EN the localised form.
API_MAINTENANCE_MESSAGE_MARKERS: Final = ("wartung", "gewartet", "maintenance")


def message_reports_maintenance(message: str | None) -> bool:
    """Whether an API error message announces planned maintenance.

    Unlike the web page (see maintenance_notice), the mobile API gives no
    structural downtime marker, only an ordinary error carrying a maintenance
    message. Matched on stable maintenance roots rather than the unconfirmed
    internal status code. A credentials rejection carries none of them, so a
    match cannot mask a real authentication failure.
    """
    if not message:
        return False
    lowered = message.casefold()
    return any(marker in lowered for marker in API_MAINTENANCE_MESSAGE_MARKERS)


def maintenance_blocking(html_text: str) -> str | None:
    """The maintenance notice, if it is why this page is not a session.

    The one rule for what the marker means, for all three logins. It explains
    a failure and never overrides a success: a logged-in page with the notice
    on it is an announcement - the portal is working - while a page that is
    not logged in and carries it is the portal refusing because of downtime.

    Nothing before the login can make this call. The login page looks the
    same before and during a window, form and notice alike, so a check there
    reported every announcement as an outage: four hours of it on 2026-09-23,
    and a failure count of ten that then held the scrape back for fifty
    minutes after the real window had closed.
    """
    if WEB_LOGGED_IN_MARKER in (html_text or ""):
        return None
    return maintenance_notice(html_text)


def note_maintenance_announcement(notice: str, reported: set[str]) -> None:
    """Pass an announced window on to the user, once per announcement.

    Info, not warning: nothing is wrong yet, and the integration keeps working
    until the window actually opens. Keyed on the text, which carries the
    window's dates, so the next announcement is news again.
    """
    if notice in reported:
        return
    reported.add(notice)
    _LOGGER.info("The WEM Portal announces maintenance: %s", notice)


# ASP.NET embeds a cookieless session id in the PATH - credential-equivalent,
# so it must never reach a log or a user-facing string. The documented form is
# /(S(<id>))/, but the token letter is not case-sensitive and several tokens
# can share one segment (/(A(..)S(..)F(..))/), so match the general shape
# rather than the single upper-case example.
_COOKIELESS_SESSION_RE = re.compile(r"/\((?:[A-Za-z]\([^)]*\))+\)")

# What redact_url returns when there is no endpoint left to name. A fixed
# string rather than an empty one: it goes into log lines that ask "which
# request did the portal reject?", where a blank reads as a formatting bug.
_UNKNOWN_URL = "unknown URL"


def redact_url(url: object) -> str:
    """Return only the endpoint of a portal URL, stripped of identifying data.

    A 403 has to stay diagnosable ("which request did the portal reject?")
    without publishing anything installation-specific. Two parts have to go:
    the QUERY, which carries the full entityvalue on the parameter-dialog
    requests, and a cookieless ASP.NET session id in the PATH. The endpoint
    alone answers the diagnostic question.
    """
    if not url:
        return _UNKNOWN_URL
    try:
        parts = urlsplit(str(url))
        path = _COOKIELESS_SESSION_RE.sub("", parts.path)
        if parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{path}"
        return path or _UNKNOWN_URL
    except Exception:  # noqa: BLE001
        # Redaction must never be the thing that breaks error handling.
        return _UNKNOWN_URL
