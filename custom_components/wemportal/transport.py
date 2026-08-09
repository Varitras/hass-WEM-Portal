"""The transport half of the portal client (Umbau P9, K8).

Everything here is about GETTING a request to the portal and surviving its
answers: the shared HTTP session, the retry-and-recover ladder around
make_api_call, and the two 403 backoffs. Nothing in this module knows what
a device, a module or a reading is - the import guard test pins that.

Mixin rather than a separate object on purpose: the transport state (session,
lock, cooldowns, deadline) lives on the ONE WemPortalApi instance and is
shared with the domain half, so the split is a move of code, not a change of
object shape.
"""

from typing import Final
import logging

import time

import requests

from .const import (
    API_LOCK_TIMEOUT_SECONDS,
    API_REQUEST_TIMEOUT_SECONDS,
    DATA_GATHERING_ERROR,
)
from .exceptions import (
    ExpiredSessionError,
    ForbiddenError,
    WemPortalError,
)

_LOGGER = logging.getLogger(__name__)

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
    """Session, retry, recovery and cooldown - the wire side of the api."""

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
        self.webscraping_cookie = {}
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
                response_data = response.json()
                _LOGGER.debug(response_data)
                # Status we get back from server
                server_status = response_data["Status"]
                server_message = response_data["Message"]
            except (KeyError, ValueError):
                pass
        return server_status, server_message

    def _send(self, url, headers, data):
        """GET when the call carries no body, POST when it does."""
        if not data:
            _LOGGER.debug("Sending GET request to %s with headers: %s", url, headers)
            return self.session.get(
                url, headers=headers, timeout=API_REQUEST_TIMEOUT_SECONDS
            )
        _LOGGER.debug(
            "Sending POST request to %s with headers: %s and data: %s",
            url,
            headers,
            data,
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
            # Same idea, different budget: stop a poll cycle that is out of
            # time before spending another request on it. Inside the attempt
            # loop on purpose, so a retry cannot carry a cycle past the
            # deadline the first attempt was still inside of.
            self.check_deadline()

            time.sleep(1)  # Wait 1 sec between requests to be graceful to the API.
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

        return response
