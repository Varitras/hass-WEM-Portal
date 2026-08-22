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
import logging

_LOGGER = logging.getLogger(__name__)

# The portal announces planned downtime by rendering this container on the
# login page. Matched on the CSS class, NOT on its text: the wording changes
# per announcement and is localised, while the class is purpose-built and
# language-independent. Note the login form stays fully present and
# submittable during maintenance - only the backend behind it is down - so
# "is there a form?" cannot tell the two apart.
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


def report_unexpected_maintenance_marker(
    notice: str, what: str, reported: set[str]
) -> None:
    """Note a maintenance marker on a response that is not treated as downtime.

    The marker check is currently enabled only where a real maintenance page
    was observed. Whether it is safe everywhere depends on one question that
    cannot be answered by reading the code: can the marker also appear on a
    HEALTHY portal page? Enabling it everywhere on the assumption that it
    cannot would trade a known gap for an unknown false positive - one that
    would report the portal as down while it is serving fine.

    So the question is measured instead. This fires only if the marker turns
    up somewhere it is not acted on, which under the current assumption should
    be never. Silence over a few days is the evidence that the check can be
    applied to every request; a hit names the exact request that would have
    produced a false alarm.

    Warning level, because the user has to see it without enabling debug
    logging - and once per request label, so a marker that IS on every page
    cannot flood the log.
    """
    if what in reported:
        _LOGGER.debug("Maintenance marker seen again on the %s.", what)
        return
    reported.add(what)
    _LOGGER.warning(
        "The WEM Portal maintenance marker appeared in the response to the "
        "%s, which is NOT treated as downtime. If the portal was working "
        "normally, please report this - it decides whether the maintenance "
        "check can be applied to every request. Notice text: %s",
        what,
        notice,
    )
