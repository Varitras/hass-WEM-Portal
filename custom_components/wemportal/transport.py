"""The transport half of the portal client (Umbau P9, K8).

Everything here is about GETTING a request to the portal and surviving its
answers: logging in, the shared HTTP session, the retry-and-recover ladder
around make_api_call, and the two 403 backoffs. Nothing in this module knows
what a device, a module or a reading is - the import guard test pins that.

Mixin rather than a separate object on purpose: the transport state (session,
lock, cooldowns, deadline) lives on the ONE WemPortalApi instance and is
shared with the domain half, so the split is a move of code, not a change of
object shape.
"""

from typing import TYPE_CHECKING, Any, Final
import logging

import threading
import time

import requests
from lxml import html
from lxml.etree import ParserError

if TYPE_CHECKING:
    from .models import AccountState

from .const import (
    API_LOCK_TIMEOUT_SECONDS,
    API_REQUEST_TIMEOUT_SECONDS,
    DATA_GATHERING_ERROR,
    SCRAPER_REQUEST_TIMEOUT_SECONDS,
    WEB_LOGGED_IN_MARKER,
    WEB_LOGIN_FORM_MARKER,
    WEB_LOGIN_URL,
)
from .exceptions import (
    AuthError,
    ExpiredSessionError,
    ForbiddenError,
    PortalMaintenanceError,
    ServerError,
    UnknownAuthError,
    WemPortalError,
)

# Protocol, not domain: how a portal answer is READ, with no idea what a
# device or a reading is. The import guard in tests/test_transport_boundary
# lists the domain modules, and neither of these is one of them - status_is_success
# reads a status field, maintenance_notice reads a downtime page.
from .mobile_protocol import as_answer_dict, status_is_success
from .web_protocol import maintenance_notice, message_reports_maintenance

_LOGGER = logging.getLogger(__name__)

# The mobile API's login endpoint. Here rather than beside the other API URLs
# in wemportalapi.py because that is a domain module this one must not import,
# and api_login - the only thing that posts to it - lives here now.
API_LOGIN_URL: Final = "https://www.wemportal.com/app/Account/Login"

# How long to wait before the single retry of a request that never reached
# the portal at all. Deliberately shorter than make_api_call()'s `delay`,
# which covers the re-login path: nothing has to settle here, we are only
# avoiding an instant second attempt into the same hiccup.
API_TRANSPORT_RETRY_DELAY_SECONDS: Final = 2

# Backoff after a 403 on the EXPERT (Fachmann) path only.
#
# A 403 does not necessarily mean the portal is rate-limiting our IP: it can
# just as well mean "I do not accept this particular request" (an unexpected
# postback shape, a session that is not in the required state, ...). Treating
# every expert 403 as an IP-wide rate limit paused the whole integration for
# FORBIDDEN_COOLDOWN_SECONDS because of a single rejected request - verified
# in practice while the portal was demonstrably reachable in a browser at the
# same time. The expert path therefore backs off on its own now, while the
# polling paths keep running; a 403 seen by the API/scraper still pauses
# everything, including the expert path, because that IS the rate-limit signal.
EXPERT_FORBIDDEN_COOLDOWN_SECONDS: Final = 300  # 5 minutes

# How long to pause ALL outbound requests after the server responds with a
# 403 (rate limit / forbidden), before trying again. This is intentionally
# generous: a 403 means the server is already unhappy with our request
# rate, so backing off hard (rather than continuing to poll other
# endpoints in the same cycle) is the safer choice.
FORBIDDEN_COOLDOWN_SECONDS: Final = 15 * 60  # 15 minutes

# The IP-wide 403 backoff lives here rather than on the instance, and that
# placement is the fix for a defect, not a style choice.
#
# They used to be instance state carried forward by whoever built the next
# WemPortalApi. That works only where there IS a previous instance to copy
# from. There isn't, in the case that matters most: when the first refresh
# fails, Home Assistant discards everything and calls async_setup_entry again,
# so the retry built a fresh object with the backoff reset to zero - and the
# most likely reason for that first refresh to fail is the very 403 that set
# it. The same hole let a config-flow validation walk straight into an active
# cooldown, because that path has no previous instance either.
#
# Nothing can forget to pass on what it never has to pass on.
#
# This one stays a module global ON PURPOSE, whitelisted by the guard test:
# the portal counts requests per IP, so a 403 earned by one account is a
# statement about every account behind the same address - it is
# installation-wide, not account state. The expert backoff, per account by
# design, lives in models.AccountState with the rest of the reload-surviving
# account memory.
_BLOCKED_UNTIL = 0.0


def _extend_cooldown(current: float, until: float) -> float:
    """Only ever later, never sooner - the rule both backoffs already had."""
    return max(current, until)


def reset_cooldowns_for_tests() -> None:
    """Drop the IP-wide backoff. Only the test suite has any business calling
    this; production has no situation in which forgetting a 403 is correct.
    The per-account state has its own reset in models."""
    global _BLOCKED_UNTIL
    _BLOCKED_UNTIL = 0.0


class WemPortalTransport:
    """Session, retry, recovery and cooldown - the wire side of the api.

    The block below is what this half needs from the object it is mixed
    into. Declaring it beats assuming it: a mixin's dependencies on its host
    are invisible otherwise, and these five are exactly the seam along which
    the god module was cut. mypy checks them, so a rename on either side
    fails here rather than at runtime.
    """

    if TYPE_CHECKING:
        _account_state: AccountState
        headers: dict[str, str]
        username: str
        password: str
        valid_login: bool
        api_version: str | None
        _api_lock: threading.Lock
        session: requests.Session | None
        expert_cookies: Any

        def _reset_scraper(self) -> None: ...

        def check_deadline(self) -> None: ...

    @property
    def _blocked_until(self) -> float:
        """Monotonic time until which ALL outbound requests are paused.

        Set after a 403 anywhere in a cycle - a strictly additive safety
        measure: it only ever makes the integration quieter after the server
        has already signalled distress. Backed by module state, so it survives
        every way this object gets rebuilt. See _BLOCKED_UNTIL.
        """
        return _BLOCKED_UNTIL

    @_blocked_until.setter
    def _blocked_until(self, value: float) -> None:
        global _BLOCKED_UNTIL
        _BLOCKED_UNTIL = _extend_cooldown(_BLOCKED_UNTIL, value or 0.0)

    @property
    def _expert_blocked_until(self) -> float:
        """The EXPERT-ONLY backoff, per account.

        A 403 on the Fachmann path pauses that path alone (see
        EXPERT_FORBIDDEN_COOLDOWN_SECONDS for why); the polling paths keep
        running. The reverse still holds: a genuine rate limit seen by the
        API or scraper pauses the expert path too, because
        check_expert_cooldown() consults check_cooldown() first.
        """
        return self._account_state.expert_blocked_until

    @_expert_blocked_until.setter
    def _expert_blocked_until(self, value: float) -> None:
        self._account_state.expert_blocked_until = _extend_cooldown(
            self._account_state.expert_blocked_until, value or 0.0
        )

    def _activate_cooldown(self, seconds=FORBIDDEN_COOLDOWN_SECONDS):
        """Pause ALL further outbound requests for a while after being
        rate-limited (HTTP 403) by the WEM Portal server.

        This intentionally affects every subsequent make_api_call(), not
        just the one that got the 403 - continuing to hit *other*
        endpoints (statistics, circuit times, ...) right after the server
        already signaled it's unhappy would defeat the purpose. Never
        shortens an existing cooldown, only extends it.
        """
        new_blocked_until = time.monotonic() + seconds
        if new_blocked_until > self._blocked_until:
            self._blocked_until = new_blocked_until
            _LOGGER.warning(
                "WEM Portal returned a rate-limit/forbidden (403) response. "
                "Pausing ALL requests for %s minutes to avoid making it worse.",
                seconds // 60,
            )

    def activate_expert_cooldown(self, seconds=EXPERT_FORBIDDEN_COOLDOWN_SECONDS):
        """Back off the EXPERT (Fachmann) path only, after a 403 there.

        Deliberately does NOT touch the global cooldown: an expert 403 is
        frequently a rejected individual request rather than an IP-wide rate
        limit, and pausing sensor polling because of it costs the user their
        readings for no reason. Never shortens an existing backoff.
        """
        new_blocked_until = time.monotonic() + seconds
        if new_blocked_until > self._expert_blocked_until:
            self._expert_blocked_until = new_blocked_until
            _LOGGER.warning(
                "Expert (Fachmann) path returned 403. Pausing EXPERT requests "
                "for %s minutes; sensor polling is unaffected.",
                seconds // 60,
            )

    def check_expert_cooldown(self):
        """Gate for the expert path: raise if either backoff is active.

        Checks the global cooldown first - a genuine rate limit seen by the
        API/scraper must still stop expert requests - then the expert-only one.
        """
        self.check_cooldown()
        if self._expert_blocked_until and time.monotonic() < self._expert_blocked_until:
            remaining = int(self._expert_blocked_until - time.monotonic())
            if remaining >= 60:
                remaining_str = f"~{(remaining + 59) // 60} min"
            else:
                remaining_str = f"{remaining}s"
            raise ForbiddenError(
                f"Expert path is backing off after a previous 403 "
                f"({remaining_str} remaining). Sensor polling is unaffected."
            )

    def is_rate_limited(self) -> bool:
        """Whether the IP-wide 403 backoff is currently holding requests.

        The question the coordinator asks after every cycle, and it has to
        be a question about STATE rather than about what was raised: a 403
        is usually earned inside statistics, schedules or the scrape, all of
        which catch broadly on purpose so one optional part cannot cost the
        readings. Nothing propagates, and the user was told nothing while
        every request was being refused.
        """
        return bool(self._blocked_until) and time.monotonic() < self._blocked_until

    def check_cooldown(self):
        """Raise ForbiddenError immediately, without making any request,
        if we're still within a cooldown period from a previous 403.

        Public on purpose: besides the internal API/scraping paths, the
        standalone expert writer holds a reference to this method as its
        shared cooldown gate, so a 403 seen anywhere pauses the expert
        path too. Renamed from the former underscore-prefixed name to stop
        advertising a private-only intent it never actually had.
        """
        if self._blocked_until and time.monotonic() < self._blocked_until:
            remaining = int(self._blocked_until - time.monotonic())
            # Human-readable: minutes for anything over a minute, so the
            # message surfaced in the frontend is immediately meaningful.
            if remaining >= 60:
                remaining_str = f"~{(remaining + 59) // 60} min"
            else:
                remaining_str = f"{remaining}s"
            raise ForbiddenError(
                f"Still cooling down after a previous rate-limit response "
                f"({remaining_str} remaining). Skipping requests until then."
            )

    def reset_transport(self):
        """Throw away the HTTP state and force a fresh login next cycle.

        This is the recovery the coordinator reaches for after repeated
        errors, and it replaces rebuilding the whole object. Rebuilding was
        never the remedy - the remedy is a clean connection - and it kept
        causing the problem it was meant to solve: a fresh instance starts
        every one of its 31 fields from scratch, so each piece of state that
        had to survive was carried across by hand, and each new field was a
        new chance to forget one. Two were forgotten in practice.

        Three things it silently reset are worth naming, because they were
        never intended and no test would have caught them:

          * the statistics and circuit-times timestamps, which are portal
            RATE LIMITS (an hour each). Every recovery let the next cycle
            refetch both immediately - on a portal that had just been failing.
          * the shared API lock. A poll running in a worker thread held the
            OLD lock while the new object handed out a fresh, unheld one, so
            a write could interleave with the very poll the lock exists to
            serialise against.

        What actually needs to go is the transport: the HTTP sessions, the
        login state and the cookies. Everything else - discovered modules,
        cooldowns, backoffs, timestamps, the lock - is deliberately kept,
        because none of it is what "corrupted session" refers to.

        `expert_cookies` is the one that looks like transport and is not. It
        caches a live web session for the expert path, and dropping it would
        force a full Fachmann login on the next expert operation - requests
        against a portal that blocks the IP for 12 hours past 10,000 of them.
        An API failure says nothing about that session, so it stays.

        Which field is which is not left to this docstring: the split is
        declared and enforced in tests/test_hardening.py, so a new field
        fails the suite until someone classifies it.

        Runs UNDER the shared api lock, and that is not decoration. A failed
        poll releases the lock in its `finally`; a change_value() worker that
        was waiting takes it in that same instant, and this then closed the
        session out from under the write. Taking the lock is also why the
        coordinator hands this to an executor instead of calling it on the
        event loop - see WemPortalDataUpdateCoordinator.
        """
        if not self._api_lock.acquire(timeout=API_LOCK_TIMEOUT_SECONDS):
            # Best-effort by design. The recovery must not raise into the
            # coordinator's error handling, and a connection still in use is
            # exactly when tearing it down does the damage. The next failed
            # cycle tries again.
            _LOGGER.warning(
                "Not resetting the connection: it is still in use by another "
                "operation. Trying again on the next failed cycle."
            )
            return
        try:
            self._reset_transport_locked()
        finally:
            self._api_lock.release()

    def close_transport(self, timeout=API_LOCK_TIMEOUT_SECONDS):
        """Close the HTTP sessions because this object is being discarded.

        Unlike reset_transport this WAITS for the lock rather than skipping:
        an unload that leaves a session open leaks it for the life of the
        process, so the close has to happen even if it has to wait for the
        operation holding the lock. It still closes on timeout - a leaked
        connection is worse than a request that fails as everything around it
        is being torn down anyway.

        Taking the lock at all is the point. A write or a poll can be inside
        make_api_call at this moment, and closing its session underneath it
        turns an orderly teardown into a connection error.
        """
        acquired = self._api_lock.acquire(timeout=timeout)
        if not acquired:
            _LOGGER.debug(
                "Closing the HTTP sessions while another operation still holds "
                "the api lock; it waited %ss.",
                timeout,
            )
        try:
            self._close_sessions()
        finally:
            if acquired:
                self._api_lock.release()

    def _close_sessions(self):
        """Close both HTTP sessions. Never raises: every caller is either
        discarding this object or replacing its transport."""
        if self.session is not None:
            try:
                self.session.close()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Ignoring error while closing the API session: %s", exc)
            self.session = None
        self._reset_scraper()

    def _reset_transport_locked(self):
        """The teardown itself. Only called with the api lock held."""
        _LOGGER.info(
            "Persistent API errors: dropping the HTTP sessions and logging in "
            "again on the next cycle."
        )
        self._close_sessions()
        self.valid_login = False
        self.api_version = None
        # The web cookie belongs to the discarded scraper session.
        self.webscraping_cookie: dict[str, Any] | None = {}
        # Re-run device discovery once on the next cycle: it is cheap, and the
        # failures being recovered from may have left the module view partial.
        self._devices_fetched_this_session = False

    def get_response_details(self, response: requests.Response):
        server_status = ""
        server_message = ""
        # Use "is not None" rather than a plain truthiness check: a
        # requests.Response object is falsy whenever status_code >= 400
        # (see Response.__bool__), i.e. exactly when there's an error to
        # diagnose. The previous check silently skipped reading the body
        # in that case, discarding the server's own error details.
        if response is not None:
            try:
                response_data = as_answer_dict(response.json())
                _LOGGER.debug(response_data)
                if response_data is not None:
                    # Status we get back from server
                    server_status = response_data.get("Status", "")
                    server_message = response_data.get("Message", "")
            except ValueError:
                # Not JSON at all. The body is read to EXPLAIN a failure,
                # never to cause one - and valid JSON that is not an object
                # (`null`, a bare list) used to raise TypeError right here,
                # which is not a caught type, so the diagnosis travelled up
                # in place of the failure it was diagnosing.
                pass
        return server_status, server_message

    def _send(self, url, headers, data) -> requests.Response:
        """GET when the call carries no body, POST when it does.

        The session is established by the caller's login before any request
        gets here; None means make_api_call was entered without one, which
        is a programming error rather than a portal failure.
        """
        assert self.session is not None, "no API session - login runs first"
        if not data:
            _LOGGER.debug("Sending GET request to %s", url)
            return self.session.get(
                url, headers=headers, timeout=API_REQUEST_TIMEOUT_SECONDS
            )
        # The field NAMES, not their values. A debug log is what people paste
        # into an issue, and the payload of a write carries the
        # installation's device id and the value being written. Which fields
        # went out is what makes the line useful; what was in them is what
        # makes it somebody's heating system. The headers are gone for a
        # duller reason: four constants, repeated on every request.
        _LOGGER.debug(
            "Sending POST request to %s with fields: %s",
            url,
            ", ".join(sorted(data)) if isinstance(data, dict) else type(data).__name__,
        )
        return self.session.post(
            url, headers=headers, json=data, timeout=API_REQUEST_TIMEOUT_SECONDS
        )

    def _recover_or_raise(
        self, exc, response, url, *, last_attempt, delay, retry_transport
    ) -> None:
        """What a failed attempt means: returns to retry it, raises otherwise.

        Returning is the signal to loop round again, so every path that is
        NOT worth another request ends in a raise. Split out of make_api_call
        because all of it sat two levels deep, inside a loop and a handler,
        which put every one of these decisions at a nesting cost of three.
        """
        status_code = (
            response.status_code
            if isinstance(exc, requests.exceptions.RequestException)
            and response is not None
            else None
        )

        if status_code == 403:
            # A 403 means the server is already unhappy with our request
            # rate - immediately retrying with a fresh login (as we do for a
            # plain expired session below) would itself be an extra request
            # at exactly the wrong time. Back off hard instead: no retry,
            # pause everything for a while, and surface it as ForbiddenError
            # so callers' existing 403-handling (e.g. get_parameters()'s
            # forbidden_count) still works.
            self._activate_cooldown()
            server_status, server_message = self.get_response_details(response)
            # Not at odds with the "no extra request" note above: this login
            # is spent after the cooldown, when the session has idled the 15
            # minutes at which the expert path stops trusting one
            # (EXPERT_SESSION_MAX_AGE_SECONDS) - and a failed reuse costs two
            # requests where a fresh login costs one. Assumed, not measured:
            # that number is the web session's, this is the mobile API's.
            self.valid_login = False
            forbidden_error = ForbiddenError(
                f"{DATA_GATHERING_ERROR} Server returned status code: {server_status} and message: {server_message}"
            )
            forbidden_error.server_status = server_status
            raise forbidden_error from exc

        # Nothing came back at all: the request timed out, the connection was
        # reset, DNS failed. `response` is set to None at the top of every
        # attempt and only ever assigned by the send below, so this is exactly
        # "no HTTP response was received" - a 403, a 401 and the login-redirect
        # check all need a response to have been raised in the first place.
        is_transport_error = response is None

        if is_transport_error and retry_transport and not last_attempt:
            # Deliberately no re-login: the session is fine, the network was
            # not. Logging in again would spend an extra request at the worst
            # possible moment and throw away a session that nothing is wrong
            # with.
            _LOGGER.info(
                "Request to %s did not reach the portal (%s). Retrying once.", url, exc
            )
            time.sleep(API_TRANSPORT_RETRY_DELAY_SECONDS)
            return

        # A genuinely expired session (401, or a stealthy redirect to the
        # login page) is worth one immediate retry with a fresh login - unlike
        # a 403, this isn't a sign we're sending too many requests, just that
        # the current session is no longer valid.
        is_session_error = isinstance(exc, ExpiredSessionError) or status_code == 401

        if is_session_error and not last_attempt:
            _LOGGER.info("Session expired for %s. Re-authenticating...", url)
            self.api_login()
            time.sleep(delay)
            return

        # Out of retries, or an error of a completely different kind:
        server_status, server_message = self.get_response_details(response)

        # The old logic recreated the entire API instance when this happened.
        # To emulate that recovery mechanism without losing cached metadata,
        # we invalidate the login state so the next cycle creates a fresh
        # requests.Session.
        self.valid_login = False

        if is_transport_error:
            # There was no server and no answer, so there is no status code
            # and no message to report. Saying "Server returned status code:
            # and message: " anyway - which is what a timeout produced - sends
            # every reader of that line looking at the portal for a fault that
            # is on this side of the connection. The web path already words
            # this correctly; see scraper.py's login handler.
            wem_error = WemPortalError(
                f"{DATA_GATHERING_ERROR} Could not reach the WEM Portal: {exc}"
            )
        else:
            wem_error = WemPortalError(
                f"{DATA_GATHERING_ERROR} Server returned status code: {server_status} and message: {server_message}"
            )
        # Expose the server-side status code so callers can react to specific
        # ones (e.g. Statistics skips an invalid group) without parsing the
        # message string.
        wem_error.server_status = server_status
        raise wem_error from exc

    def make_api_call(
        self,
        url: str,
        headers=None,
        data=None,
        do_retry=True,
        delay=5,
        retry_transport=False,
    ) -> requests.Response:
        """One mobile-API request, with two kinds of retry sharing one attempt.

        `do_retry` covers an expired session: re-login once and try again.
        `retry_transport` covers a request that never reached the portal at
        all - a timeout, a reset connection, DNS.

        They are separate REASONS, not separate budgets: there is exactly one
        extra attempt, whichever reason claims it. A transport hiccup followed
        by an expired session is therefore not retried twice, on purpose - the
        cycle has a time budget and a third attempt would eat into it.

        `retry_transport` is opt-in per call site rather than on by default,
        for two different reasons. Cost: an hourly cycle is roughly fifteen
        requests on a single-device installation and more on larger ones, so
        letting all of them retry into a timeout would push a bad cycle well
        past the coordinator's own limit. Safety: a request that starts
        something at the portal must not be repeated when only its answer was
        lost. Only the two reads that carry values ask for this.
        """
        attempts = 2 if (do_retry or retry_transport) else 1
        response = None

        for attempt in range(attempts):
            # Reset per attempt: on a network failure during a retry the
            # error handler below would otherwise read stale details from
            # the PREVIOUS attempt's response.
            response = None
            # Fail fast if we're still cooling down from a previous 403 -
            # applies to every single call site that goes through here,
            # not just the one that originally triggered it.
            self.check_cooldown()
            # Courtesy pause, then the deadline - in that order. A sub-second
            # budget must not pass a check placed ahead of the one-second
            # sleep, only for the sleep to carry the cycle past the deadline
            # and fire the request anyway. Both stay inside the attempt loop,
            # so a retry cannot carry a cycle past the deadline the first
            # attempt was still inside of.
            time.sleep(1)  # Wait 1 sec between requests to be graceful to the API.
            self.check_deadline()
            # Merge any call-specific headers on top of the default headers,
            # instead of replacing them outright. Previously, passing e.g.
            # headers={"X-Api-Version": "2.0.0.0"} (as get_statistics() does)
            # would silently drop "Host", "User-Agent" and "Accept" for that
            # call, which could cause it to be rejected by the server.
            current_headers = {**self.headers, **(headers or {})}

            try:
                response = self._send(url, current_headers, data)

                response.raise_for_status()

                # Check for stealthy session expiration (HTML redirect)
                if "Account/Login" in response.url or (
                    hasattr(response, "redirect_url")
                    and response.redirect_url
                    and "Account/Login" in str(response.redirect_url)
                ):
                    raise ExpiredSessionError("Redirected to Account/Login")

                _LOGGER.debug(response)
                return response

            except (requests.exceptions.RequestException, ExpiredSessionError) as exc:
                self._recover_or_raise(
                    exc,
                    response,
                    url,
                    last_attempt=attempt >= attempts - 1,
                    delay=delay,
                    retry_transport=retry_transport,
                )

        # Unreachable in practice: the loop either returns a response or
        # _recover_or_raise raises on the last attempt. Stated rather than
        # left to `return response`, which mypy reads as possibly-None and
        # a reader as "sometimes this falls through".
        raise WemPortalError(
            f"{DATA_GATHERING_ERROR} Request to {url} ended without a response."
        )

    def api_login(self):
        # The cooldown gate belongs on every outbound request, and a login is
        # the most expensive one to get wrong. _fetch_data and make_api_call
        # both ask, so the polling path was covered - but the config and
        # reauth flows call this directly, and those are exactly where
        # somebody lands after deleting and re-adding the integration to
        # "fix" a blockade. Every one of those attempts extended it.
        self.check_cooldown()
        # And the deadline, for the same reason: this is a request, and it is
        # reached without passing make_api_call - from _ensure_api_session
        # after a long wait for the lock, and again on the reauth retry after
        # a request came back expired. A cycle with nothing left could start
        # a fresh login from either and run past the coordinator's timeout
        # still holding the lock. A no-op outside a poll, so the config and
        # reauth flows are unaffected.
        self.check_deadline()
        payload = {
            "Name": self.username,
            "PasswordUTF8": self.password,
            "AppID": "com.weishaupt.wemapp",
            "AppVersion": "2.0.2",
            "ClientOS": "Android",
        }
        if self.session is not None:
            self.session.close()
        self.session = requests.Session()
        self.session.cookies.clear()
        self.session.headers.update(self.headers)
        # The session this claim belonged to was just closed, so the claim
        # goes with it. Here rather than per failure branch: the rejection
        # path cleared nothing, so the next cycle skipped the login and
        # spent itself on 401s instead of raising an AuthError to count.
        self.valid_login = False
        # Initialized BEFORE the try block: if the POST itself fails with a
        # pure network error (connection reset, DNS, timeout), `response`
        # would otherwise not exist yet and the error handler below would
        # crash with an UnboundLocalError instead of raising the intended
        # UnknownAuthError.
        response = None
        try:
            response = self.session.post(
                API_LOGIN_URL,
                data=payload,
                timeout=API_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            # Verify the response is actually valid JSON and successful
            response_data = response.json()
            if not status_is_success(response_data.get("Status")):
                raise AuthError(f"Login failed: Server returned {response_data}")

            self.api_version = response_data.get("Version")
            # No username: there is one account per config entry, so naming it adds
            # nothing - and a debug log is exactly what people paste into an
            # issue when asking for help.
            _LOGGER.debug("API login successful.")
            self.valid_login = True

        except ValueError as exc:  # Catches JSONDecodeError if response is HTML
            # Username (email) is PII and deliberately kept out of the log
            # entirely - people paste logs into issues/forums, and with one
            # account per config entry naming it adds nothing.
            _LOGGER.warning("API login failed. Received HTML instead of JSON.")
            self.valid_login = False
            raise WemPortalError(
                "API login failed: received HTML instead of JSON (Possible rate limit or WAF block)"
            ) from exc
        except requests.exceptions.RequestException as exc:
            # Broader than just HTTPError: also covers ConnectionError,
            # Timeout, etc. - genuine network failures that aren't tied to
            # a specific HTTP status code, which previously weren't caught
            # here at all and would fall through to the generic
            # "unexpected error" wrapper in fetch_data() instead of a
            # clear, specific error message.
            _LOGGER.warning("API login failed with a network/HTTP error.")
            self.valid_login = False
            self._raise_login_failure(response, exc)

    def _raise_login_failure(self, response, exc):
        """Turn a failed login into the error that fits what came back.

        Always raises - the type is what the caller acts on: wrong password,
        rate limit, portal fault, or "never got there". Written as guards
        rather than an if/elif chain, which nested one level per status and
        put the last case five deep.

        Messages carry the HTTP status plus the server's own status and
        message fields, but NOT the raw response body: they surface in the UI
        and in logs, and a whole HTML error page does not belong there.
        """
        if response is None:
            raise UnknownAuthError(
                f"Authentication Error: Could not reach WEM Portal ({exc})."
            ) from exc

        response_status, response_message = self.get_response_details(response)
        server_said = (
            f"Server returned internal status code: {response_status} "
            f"and message: {response_message}"
        )

        if message_reports_maintenance(response_message):
            # A login refused during planned downtime, not a credential problem.
            # The API carries no offlinecontent marker like the web page - only
            # this message - so it is read from the wording. The internal status
            # is logged (8000 in the one case seen) so a later sighting can tell
            # whether that code is maintenance-specific and worth keying on
            # structurally; the message alone drives the decision for now. See
            # PortalMaintenanceError.
            _LOGGER.debug(
                "Login refused during maintenance (internal status %s): %s",
                response_status,
                response_message,
            )
            raise PortalMaintenanceError(response_message) from exc

        if response.status_code == 400:
            raise AuthError(
                "Authentication Error: Check if your login credentials are "
                f"correct. Received response code: {response.status_code}. "
                f"{server_said}"
            ) from exc
        if response.status_code == 403:
            self._activate_cooldown()
            raise ForbiddenError(f"WemPortal forbidden error: {server_said}") from exc
        if response.status_code == 500:
            raise ServerError(f"WemPortal server error: {server_said}") from exc
        raise UnknownAuthError(
            "Authentication Error: Encountered an unknown authentication "
            f"error. Received response code: {response.status_code}. "
            f"{server_said}"
        ) from exc

    def web_login(self):
        """Log into the web interface, or raise saying why it did not work.

        Takes nothing and returns nothing - the credentials come from the
        object, and the session it opens is thrown away: this is the check
        "would a web login succeed", asked by the config flow before an
        entry is created. The docstring used to promise a username and
        password parameter and a dict of session cookies, none of which this
        has ever had.

        Raises:
            AuthError: the portal rejected the credentials.
            PortalMaintenanceError: announced downtime, not a credential problem.
            ForbiddenError: this network is refused (starts the cooldown).
            UnknownAuthError: anything else, including an unreadable answer.
        """
        self.check_cooldown()
        session = requests.Session()
        login_url = WEB_LOGIN_URL

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "de,en;q=0.9",
        }

        # Step 1: Fetch the login page
        initial_response = None
        try:
            initial_response = session.get(
                login_url,
                headers=headers,
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            )
            initial_response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            # A 403 here is the same refusal the POST below already
            # recognises, and it arrives FIRST - this is the request that
            # meets a blocked IP. Reported as "could not load the page" it
            # read like a network problem, invited an immediate retry, and
            # started no cooldown, so the next cycle walked into it again.
            if initial_response is not None and initial_response.status_code == 403:
                self._activate_cooldown()
                raise ForbiddenError(
                    "Access forbidden while loading the login page."
                ) from exc
            raise UnknownAuthError(f"Failed to load the login page: {exc}") from exc

        # Planned downtime: bail out BEFORE posting the credentials. The form
        # is fully present during maintenance, so submitting would just fail
        # as "invalid username or password" and, after three cycles, ask the
        # user to re-enter working credentials. It also avoids sending the
        # password to a page that cannot process it.
        notice = maintenance_notice(initial_response.text)
        if notice:
            raise PortalMaintenanceError(notice)

        # Step 2: Parse the login page and extract hidden form fields.
        #
        # Read with lxml, which the scraper already uses for the far more
        # involved expert page - so this is the only thing beautifulsoup4 was
        # installed for, three lines of it, and the dependency is gone.
        #
        # The `string(@name)` half of the selector is not decoration: the old
        # code tested the name for truthiness, which skips `name=""`, while a
        # bare `[@name]` would keep it and post a field the portal never sent.
        try:
            page = html.fromstring(initial_response.text)
        except ParserError as exc:
            # An empty or unparseable body. The old parser returned no fields
            # here and let the login POST go ahead, which sent the password to
            # a page that had answered with nothing, collected no ASP.NET
            # state to echo back, and could only be refused. Same reasoning as
            # the maintenance bail-out above: do not hand over credentials to
            # a page that cannot process them.
            raise UnknownAuthError(
                "The WEM Portal login page could not be read; no credentials were sent."
            ) from exc
        form_data = {
            element.get("name"): element.get("value", "")
            for element in page.xpath('//input[@type="hidden"][string(@name)]')
        }
        # A page can parse perfectly and still not be a login page. These two
        # are the ASP.NET state a login is posted WITH, so without them there
        # is nothing to log in with - and posting anyway sends the password to
        # a page that can only refuse it, which then reads as a wrong one. The
        # scraper and the expert client both check this before posting; this
        # was the third of the same three lines, and the one still missing.
        if not {"__VIEWSTATE", "__EVENTVALIDATION"} <= form_data.keys():
            raise UnknownAuthError(
                "The WEM Portal login page came back without its form fields, "
                "so no credentials were sent."
            )

        # Add username and password to the form data
        form_data["ctl00$content$tbxUserName"] = self.username
        form_data["ctl00$content$tbxPassword"] = self.password
        form_data["ctl00$content$btnLogin"] = "Anmelden"  # Login button value

        # Step 3: Submit the login form
        response = None
        try:
            response = session.post(
                login_url,
                data=form_data,
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=SCRAPER_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            # Step 4: Read the answer. Three outcomes, not two.
            if WEB_LOGGED_IN_MARKER in response.text:
                _LOGGER.debug("WEB login successful.")
                return
            # Maintenance is checked on this answer too, not only on the page
            # fetched above: the window can open between the two requests, and
            # the portal serves the notice with HTTP 200 either way.
            notice = maintenance_notice(response.text)
            if notice:
                raise PortalMaintenanceError(notice)
            if WEB_LOGIN_FORM_MARKER in response.text:
                raise AuthError("Login failed: Invalid username or password.")
            # Neither logged in, nor the login form back, nor maintenance:
            # some other page. Saying "wrong password" about it counted a
            # portal hiccup towards the reauth prompt, and three of those in a
            # row take the integration down until somebody re-enters
            # credentials that were correct the whole time.
            raise UnknownAuthError(
                "Login failed: the portal answered with a page that is neither "
                "the logged-in view nor the login form."
            )
        except requests.exceptions.RequestException as exc:
            if response is not None and response.status_code == 403:
                self._activate_cooldown()
                raise ForbiddenError("Access forbidden during login.") from exc
            raise UnknownAuthError(f"Failed to submit the login form: {exc}") from exc
