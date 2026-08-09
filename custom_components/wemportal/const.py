"""Constants for the WEM Portal Integration"""

from enum import IntEnum
from typing import Final


class WemDataType(IntEnum):
    NUMBER_STEP_HALF = -1
    SELECT = 1
    SWITCH = 2
    NUMBER_STEP_ONE = 3
    PROGRAM = 6


DOMAIN: Final = "wemportal"
# Issue tracker of THIS fork - used in user-facing error hints, so problems
# with fork-specific behaviour land here and not at the upstream project.
GITHUB_PROJECT_URL: Final = "https://github.com/Varitras/hass-WEM-Portal/issues"
DEFAULT_TIMEOUT: Final = 360

# How long an on-demand operation waits for the shared API lock.
#
# asyncio.timeout cancels the AWAIT, not the executor thread, so a poll that
# overran DEFAULT_TIMEOUT keeps running - and keeps the lock. An unbounded
# acquire parked the next operation behind it with no feedback at all.
#
# Long enough that a routine collision (a user's write during a running poll)
# queues and succeeds rather than failing, but deliberately BELOW
# DEFAULT_TIMEOUT: a waiter must give up before the coordinator abandons the
# await it belongs to, otherwise every cycle leaves another parked worker
# behind.
API_LOCK_TIMEOUT_SECONDS: Final = DEFAULT_TIMEOUT - 30


WEB_MAIN_URL: Final = "https://www.wemportal.com/Web/Default.aspx"
WEB_LOGIN_URL: Final = "https://www.wemportal.com/Web/Login.aspx"


# How the answer to a submitted login form is read.
#
# The logout button only exists once a session is established, so its presence
# is proof the login worked. Its ABSENCE proves nothing on its own: the portal
# answers HTTP 200 for a rejected login, for a maintenance page, and for the
# odd error page, and treating all of those as "wrong password" is how three
# portal hiccups in a row ask for credentials that were correct.
#
# The password field is the second half of the answer: it means the portal
# rendered the login form again, which is what a genuine rejection looks like.
# Named after the field the login POST itself sends, so the two cannot drift.
WEB_LOGGED_IN_MARKER: Final = "ctl00_btnLogout"
WEB_LOGIN_FORM_MARKER: Final = "ctl00$content$tbxPassword"
# Both are read by wemportalapi.web_login and by the scraper's own login, which
# classify the same answer the same way: a session, a refusal, or neither.
CONF_SCAN_INTERVAL_API: Final = "api_scan_interval"
CONF_LANGUAGE: Final = "language"
CONF_MODE: Final = "mode"
DEFAULT_MODE: Final = "api"
PLATFORMS = ["date", "number", "select", "sensor", "switch"]


DATA_GATHERING_ERROR: Final = (
    "An error occurred while gathering data. This issue should resolve by "
    f"itself. If this problem persists, open an issue at {GITHUB_PROJECT_URL}"
)

DEFAULT_CONF_SCAN_INTERVAL_API_VALUE: Final = 300
DEFAULT_CONF_SCAN_INTERVAL_VALUE: Final = 1800
# Lower bounds enforced (clamped, like the expert poll interval) on the two
# scan intervals in the options flow. Both fields are plain positive-int
# seconds, so without a floor a stray tiny value (e.g. "1") would poll the
# portal continuously and reliably trigger the IP-wide 403 rate limit.
MIN_SCAN_INTERVAL_SECONDS: Final = 60  # web scraping interval floor
# Raised from 10s: the API is Weishaupt's own app backend, and polling it
# too often risks a temporary block. This is still only a nonsense guard
# (matching the web floor) - the actual recommendation lives in the
# options form, so a deliberate choice is not overridden.
MIN_SCAN_INTERVAL_API_SECONDS: Final = 60  # mobile-API interval floor
DEFAULT_CONF_LANGUAGE_VALUE: Final = "en"


# Per-request timeout for the web scraper's HTTP calls. Without one, a
# slow/hanging WEM Portal response would block the executor thread until
# the coordinator-wide DEFAULT_TIMEOUT (360s) fires; failing the single
# request after this many seconds instead lets the existing retry/backoff
# logic take over much sooner.
SCRAPER_REQUEST_TIMEOUT_SECONDS: Final = 30

# Per-request timeout for the mobile-API HTTP calls (login included).
# make_api_call() already used this value inline; the login POST previously
# had no timeout at all, so a hanging server could block the executor
# thread indefinitely (the coordinator's async timeout only abandons the
# await - the thread itself would stay stuck).
#
# Raised from 10s after a live log showed a /DataAccess/Read give up at
# exactly 10.0s and fail the whole cycle. The portal is occasionally just
# slow, and the budget has room: even if EVERY request of an hourly cycle
# ran into this timeout, the worst case stays at roughly 74% of the
# coordinator's DEFAULT_TIMEOUT.
API_REQUEST_TIMEOUT_SECONDS: Final = 12


# How many CONSECUTIVE AuthErrors the coordinator tolerates before
# escalating to ConfigEntryAuthFailed (HA's reauth flow, which stops all
# automatic retries until the user intervenes). The portal occasionally
# serves a transient login page, and treating a single such hiccup as
# "credentials are wrong" would needlessly take the integration down.
AUTH_ERROR_ESCALATION_THRESHOLD: Final = 3


# Expert write access (web) - disabled by default. Only when enabled are
# the wemportal.set_expert_parameter service and the configured expert
# number entities registered.
CONF_EXPERT_WRITE: Final = "expert_write_enabled"
# Ten generic expert-parameter slots. Each slot has a free-text name (used
# as the entity's friendly name / slug source) and an entityvalue hex ID
# (from the portal's parameter edit dialog). Empty slots are ignored.
EXPERT_SLOT_COUNT: Final = 10

# Minimum length for a slot entityvalue ID in the options-flow validation.
# Real entityvalues are long hex strings (the known ones are 36 chars); this
# floor rejects obvious stray entries like "0" or "abc" while staying well
# below 36 so a slightly different length on another installation still
# validates. Format is additionally checked to be hex.
MIN_EXPERT_ENTITYVALUE_LENGTH: Final = 16
CONF_EXPERT_SLOT_NAME_TEMPLATE: Final = "expert_slot_%d_name"
CONF_EXPERT_SLOT_ID_TEMPLATE: Final = "expert_slot_%d_id"
# Optional periodic read-back of the configured expert parameters. OFF by
# default: each read is a full Fachmann navigation, so frequent polling
# raises the risk of a temporary IP block (403) from the portal.
CONF_EXPERT_AUTO_POLL: Final = "expert_auto_poll_enabled"
CONF_EXPERT_POLL_INTERVAL: Final = "expert_poll_interval_minutes"
# Whether a successful expert write posts a persistent notification.
# OFF by default (a notification on every write gets noisy, especially when
# setting several values). Failures always notify regardless, and successes
# are always logged either way - this only controls the success popup.
CONF_EXPERT_NOTIFY_ON_SUCCESS: Final = "expert_notify_on_success"
# Advanced/expert-only toggles for the two navigation steps that are
# skipped by default (both proven unnecessary on the reference install).
# Exposed in the options UI - OFF by default - so a user on a different
# portal/module layout can re-enable them WITHOUT editing code, but with a
# clear "only if you know what you're doing" warning. When unset, the code
# falls back to the EXPERT_SKIP_* module constants below.
CONF_EXPERT_ENABLE_MODULE_NAV: Final = "expert_enable_module_nav"
CONF_EXPERT_ENABLE_SECURITY_CODE: Final = "expert_enable_security_code"
# Default poll interval when auto-poll is enabled (minutes). Conservative
# by design; the options UI also warns about the 403 risk.
DEFAULT_EXPERT_POLL_INTERVAL_MINUTES: Final = 60
# Lower bound enforced on the configured interval, so a mistaken tiny value
# can't hammer the portal.
MIN_EXPERT_POLL_INTERVAL_MINUTES: Final = 15


CONF_EXPERT_MODULE_ARG: Final = "expert_module_arg"
# Cached Fachmann module list ([{index, value, label}]) from the last
# discovery, so the options-flow module picker renders instantly on repeat
# runs (with a "refresh" affordance). Installation-specific; stored in
# options like the slot ids.
CONF_EXPERT_MODULE_LIST: Final = "expert_module_list"

BOOLEAN_OFF_STRINGS: Final = ["off", "aus"]
BOOLEAN_ON_STRINGS: Final = ["ein", "on"]
