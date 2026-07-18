""" Constants for the WEM Portal Integration """
import logging
from typing import Final
from enum import IntEnum

class WemDataType(IntEnum):
    NUMBER_STEP_HALF = -1
    SELECT = 1
    SWITCH = 2
    NUMBER_STEP_ONE = 3
    PROGRAM = 6

_LOGGER = logging.getLogger("custom_components.wemportal")
DOMAIN: Final = "wemportal"
# Issue tracker of THIS fork - used in user-facing error hints, so problems
# with fork-specific behaviour land here and not at the upstream project.
GITHUB_PROJECT_URL: Final = "https://github.com/Varitras/hass-WEM-Portal/issues"
DEFAULT_TIMEOUT: Final = 360
WEB_MAIN_URL: Final = "https://www.wemportal.com/Web/Default.aspx"
# The portal origin, sent on every postback (confirmed via HAR) - both
# full and async postbacks include it. Async postbacks additionally
# include X-Requested-With: XMLHttpRequest, which the ASP.NET AJAX
# infrastructure commonly checks to recognize a legitimate AJAX callback
# rather than a plain form submission. Neither header was being sent
# before, which may explain why the Fachmann permission never actually
# took effect server-side despite every postback being accepted.
WEB_PORTAL_ORIGIN: Final = "https://www.wemportal.com"
# Accept-Language is identical across every request type (confirmed via
# HAR) and was never sent at all - notable given this project's own prior
# history of portal language-mismatch bugs. Accept differs by request
# shape: navigational requests (page loads, the full submenu postback)
# send the long browser-default value; async/XHR postbacks send "*/*".
WEB_ACCEPT_LANGUAGE: Final = "de-DE,de;q=0.9,en-DE;q=0.8,en;q=0.7,en-US;q=0.6"
WEB_ACCEPT_NAV: Final = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
WEB_ACCEPT_AJAX: Final = "*/*"
WEB_LOGIN_URL: Final = "https://www.wemportal.com/Web/Login.aspx"
CONF_SCAN_INTERVAL_API: Final = "api_scan_interval"
CONF_LANGUAGE: Final = "language"
CONF_MODE: Final = "mode"
DEFAULT_MODE: Final = "api"
AVAILABLE_MODES: Final = ["api", "web", "both"]
PLATFORMS = ["number", "select", "sensor", "switch"]

# Placeholder device id the web scraper falls back to when no real
# API-discovered device is known (a pure-web install that never ran the
# mobile API). The scraper itself has no device concept - it reads a web
# page - but entity unique_ids are "<entry>:<device_id>:<name>", so scraped
# sensors need a STABLE device id or their history breaks on mode switches.
# See WemPortalApi.resolve_scraper_device_id() for how this is locked in
# once and then persisted.
SCRAPER_FALLBACK_DEVICE_ID: Final = "0000"
DATA_GATHERING_ERROR: Final = (
    "An error occurred while gathering data. This issue should resolve by "
    f"itself. If this problem persists, open an issue at {GITHUB_PROJECT_URL}"
)

# Server-side status code returned by Statistics/Read for a statistics
# group that isn't valid for the queried module (ModuleType 7/Index 0).
# The refresh call lists such groups, but reading them is rejected with
# this code. It's an expected, harmless per-group condition - skipped
# quietly rather than logged as a warning on every startup.
WEM_INVALID_PARAMETER_STATUS: Final = 3001
DEFAULT_CONF_SCAN_INTERVAL_API_VALUE: Final = 300
DEFAULT_CONF_SCAN_INTERVAL_VALUE: Final = 1800
# Lower bounds enforced (clamped, like the expert poll interval) on the two
# scan intervals in the options flow. Both fields are plain positive-int
# seconds, so without a floor a stray tiny value (e.g. "1") would poll the
# portal continuously and reliably trigger the IP-wide 403 rate limit.
MIN_SCAN_INTERVAL_SECONDS: Final = 60  # web scraping interval floor
MIN_SCAN_INTERVAL_API_SECONDS: Final = 10  # mobile-API interval floor
DEFAULT_CONF_LANGUAGE_VALUE: Final = "en"
API_LOGIN_URL: Final = "https://www.wemportal.com/app/Account/Login"
API_DEVICE_READ_URL: Final = "https://www.wemportal.com/app/Device/Read"
API_EVENT_TYPE_READ_URL: Final = "https://www.wemportal.com/app/EventType/Read"
API_DATA_ACCESS_WRITE_URL: Final = "https://www.wemportal.com/app/DataAccess/Write"
API_DATA_ACCESS_READ_URL: Final = "https://www.wemportal.com/app/DataAccess/Read"
API_REFRESH_URL: Final = "https://www.wemportal.com/app/DataAccess/Refresh"
API_DEVICE_STATUS_READ_URL: Final = "https://www.wemportal.com/app/DeviceStatus/Read"
API_CIRCUIT_TIMES_REFRESH_URL: Final = "https://www.wemportal.com/app/CircuitTimes/Refresh"
API_CIRCUIT_TIMES_READ_URL: Final = "https://www.wemportal.com/app/CircuitTimes/Read"
API_STATISTICS_REFRESH_URL: Final = "https://www.wemportal.com/app/Statistics/Refresh"
API_STATISTICS_READ_URL: Final = "https://www.wemportal.com/app/Statistics/Read"

# How long to pause ALL outbound requests after the server responds with a
# 403 (rate limit / forbidden), before trying again. This is intentionally
# generous: a 403 means the server is already unhappy with our request
# rate, so backing off hard (rather than continuing to poll other
# endpoints in the same cycle) is the safer choice.
FORBIDDEN_COOLDOWN_SECONDS: Final = 15 * 60  # 15 minutes

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
API_REQUEST_TIMEOUT_SECONDS: Final = 10

# How many CONSECUTIVE AuthErrors the coordinator tolerates before
# escalating to ConfigEntryAuthFailed (HA's reauth flow, which stops all
# automatic retries until the user intervenes). The portal occasionally
# serves a transient login page, and treating a single such hiccup as
# "credentials are wrong" would needlessly take the integration down.
AUTH_ERROR_ESCALATION_THRESHOLD: Final = 3

# Heating schedules (CircuitTimes) rarely change - only when a user edits
# them directly in the WEM Portal app (this integration only ever shows
# them as read-only sensors). Refetching them every single coordinator
# cycle is unnecessary load; this caps how often they're refreshed.
CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS: Final = 3600  # 1 hour

# Energy statistics are daily aggregates - they don't need per-cycle
# refreshes. This caps how often they're refreshed.
STATISTICS_REFRESH_INTERVAL_SECONDS: Final = 3600  # 1 hour

# How long to wait before retrying when a statistics cycle failed for EVERY
# device. The rate-limit timestamp is deliberately set BEFORE the fetch (so a
# persistently failing portal can never be hammered), which would otherwise
# make a single failure cost a full refresh interval. Shortening the wait on
# failure keeps that protection while recovering sooner. Must stay well above
# the coordinator's scan interval so a failing portal is still approached at a
# calm pace.
STATISTICS_RETRY_INTERVAL_SECONDS: Final = 900  # 15 minutes

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

# How long a cached expert web session may be reused before we log in again.
#
# Every expert operation used to perform a FULL login, which is the one thing
# the portal reliably rejected (403 on Login.aspx) while the very same portal
# stayed reachable in a browser. The scraper has had cookie reuse for exactly
# this reason; the expert path now does too.
#
# The age cap is deliberate: a reuse attempt that fails costs two extra
# requests before falling back to a login, so we only try while the session is
# plausibly still alive. Kept in memory only - a session cookie is as good as
# a credential and has no business on disk.
EXPERT_SESSION_MAX_AGE_SECONDS: Final = 900  # 15 minutes

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
SERVICE_SET_EXPERT_PARAMETER: Final = "set_expert_parameter"

# --- Expert web navigation (Fachmann level) -----------------------------
# The Fachmann parameters (e.g. Leistungsbegrenzung) live behind a second
# authentication step and a stateful navigation sequence, reconstructed
# from a real browser HAR capture. These identify the ASP.NET postback
# targets/arguments of that sequence.
# (Formerly a second WEB_DEFAULT_URL constant existed with the identical
# value as WEB_MAIN_URL; consolidated into WEB_MAIN_URL.)
WEB_CODE_EXPERTS_URL: Final = (
    "https://www.wemportal.com/Web/UControls/Weishaupt/DataDisplay/CodeExpertsDetails.aspx"
)
# The portal's main pages don't use the standard __VIEWSTATE hidden field
# but a Telerik/ECN variant, __ECNPAGEVIEWSTATE. The dialog pages
# (CodeExpertsDetails, WwpsParameterDetails) do use plain __VIEWSTATE.
# Both names are treated as the page's state field so diagnostics and
# checks work across all navigation steps.
EXPERT_VIEWSTATE_FIELDS: Final = ("__ECNPAGEVIEWSTATE", "__VIEWSTATE")
# Telerik RadAjax async-postback marker. Real async postbacks (module
# select, timer polls, dialog saves) carry this field set to "true" in the
# body AND the X-MicrosoftAjax: Delta=true header. The submenu unlock is
# NOT an async postback - it's a classic full postback ending in a 302
# redirect - so it must omit both.
EXPERT_ASYNCPOST_FIELD: Final = "__ASYNCPOST"
# Skip the module-select postback (True by default). A live read proved
# the parameter dialog comes back fully populated WITHOUT selecting a
# module first, even though the heat pump is NOT the first menu entry -
# so the entityvalue in the dialog URL already addresses the device/
# module/parameter completely, and the former "module selected" session
# state is not needed. Skipping it removes one postback per operation
# (less load, less 403 exposure) and one point of failure. The module-
# select code is KEPT (see EXPERT_MODULE_MENU_TARGET / _establish_context)
# as a safety net for hypothetical other module layouts where a parameter
# might not resolve without it: flip this to False (or pass wem_debug.py
# without --skip-module-nav after inverting) to restore the module postback.
# The module is chosen by EXPERT_MODULE_ARG_HEATPUMP (icon-menu argument
# "6" = heat pump on the reference installation), overridable per install
# via CONF_EXPERT_MODULE_ARG.
EXPERT_SKIP_MODULE_NAV: Final = True
# The Fachmann security-code sub-sequence (dialog GET + code "11" POST +
# RAMMasterPage unlock callback, and the timer postback that feeds it) is
# DISABLED by default (True). It was proven unnecessary: a live read AND a
# live write both succeed with it skipped, because the submenu ClientState
# alone puts the session on the Fachmann level - exactly how the web
# scraper already reaches the expert view without any code. The code is
# deliberately KEPT (not deleted) as a safety net: should Weishaupt ever
# make the Fachmann level require the code again - e.g. if an account's
# permanent Fachmann unlock expires and the code becomes mandatory per
# session - flipping this back to False restores the full, HAR-verified
# unlock choreography without having to reconstruct it. Set to False (and
# via wem_debug.py --skip-security-code inverted) only to re-test that path.
EXPERT_SKIP_SECURITY_CODE: Final = True
# Submenu postback that opens the expert-code (Fachmann) dialog.
EXPERT_SUBMENU_TARGET: Final = "ctl00$SubMenuControl1$subMenu"
EXPERT_SUBMENU_ARG: Final = "3"
# RadMenu client state selecting the "Fachmann" entry (index 3). This
# JS-generated field is what tells the server which submenu item was
# clicked; it is NOT a server-rendered hidden input, so it must be
# supplied explicitly. Confirmed via HAR: the real submenu POST carries
# selectedItemIndex:3 with "Fachmann" selected:true, and only then does
# the reloaded page contain Fachmann-only parameters. Without it the
# postback lands on the plain user level (~146 KB) instead of the Fachmann
# level (~207 KB). The value codes 110/222/223/225/224 are deployment
# constants, not installation-specific; the installation line (index 1) is
# intentionally left blank here since its text is per-installation and does
# not affect which item is selected.
EXPERT_SUBMENU_CLIENTSTATE_FIELD: Final = "ctl00_SubMenuControl1_subMenu_ClientState"
EXPERT_SUBMENU_CLIENTSTATE_VALUE: Final = (
    '{"logEntries":[{"Type":3},'
    '{"Type":1,"Index":"0","Data":{"text":"Übersicht","value":"110"}},'
    '{"Type":1,"Index":"1","Data":{"text":"","value":""}},'
    '{"Type":1,"Index":"2","Data":{"text":"Benutzer","value":"222"}},'
    '{"Type":1,"Index":"3","Data":{"text":"Fachmann","value":"223","selected":true}},'
    '{"Type":1,"Index":"4","Data":{"text":"Statistik","value":"225"}},'
    '{"Type":1,"Index":"5","Data":{"text":"Datenlogger","value":"224"}}],'
    '"selectedItemIndex":"3"}'
)
# Save button inside a RadWindow dialog (Fachmann code + parameter write).
EXPERT_DIALOG_SAVE_TARGET: Final = "ctl00$DialogContent$BtnSave"
# Field carrying the Fachmann security code ("11", publicly known).
EXPERT_SECURITY_CODE_FIELD: Final = "ctl00$DialogContent$tbxSecurityCode"
EXPERT_SECURITY_CODE: Final = "11"
# Extra fields the code-experts dialog's async postback needs (from HAR).
# The dialog is a RadAjax async postback: besides __ASYNCPOST=true it
# needs the RadAjax control id, the ScriptManager target, and the dialog
# RadTabStrip client state (a JS-generated field the server accepts with
# this default). Portal-specific constants captured from the browser flow.
EXPERT_DIALOG_RADAJAX_ID: Final = "ctl00_RAMPDialogMaster"
EXPERT_DIALOG_TSM_FIELD: Final = "ctl00$TSMeControlNetDialog"
EXPERT_DIALOG_TSM_VALUE: Final = (
    "ctl00$ctl00$DialogContent$DivDialogPanel|ctl00$DialogContent$BtnSave"
)
# The dialog's OWN ScriptManager also needs its TSM version-blob hidden
# field (analogous to EXPERT_PAGE_TSM_ID_FIELD for the main page's
# ScriptManager) - confirmed present in the dialog's own response
# (window.__TsmHiddenField = $get('ctl00_TSMeControlNetDialog_TSM')) but
# never sent by our client. Verified identical (deployment-fixed, not
# session-specific) across four independent captured sessions.
EXPERT_DIALOG_TSM_ID_FIELD: Final = "ctl00_TSMeControlNetDialog_TSM"
EXPERT_DIALOG_TSM_ID_VALUE: Final = (
    ";;Telerik.Web.UI, Version=2020.1.114.45, Culture=neutral, "
    "PublicKeyToken=121fae78165ba3d4:de:40a36146-6362-49db-b4b5-57ab81f34dac:"
    "e330518b:16e4e7cd:f7645509:24ee1bba:33715776:88144a7a:1e771326:"
    "8e6f0d33:1f3a7489:6a6d718d:c128760b:19620875:874f8ea2:c172ae1e:"
    "f46195d3:9cdfc6e7:2003d0b8:c8618e41:e4f8f289:1a73651d:333f8d94:ed16cbdc"
)
EXPERT_DIALOG_RTS_STATE_FIELD: Final = "ctl00_DialogContent_RTSDialog_ClientState"
EXPERT_DIALOG_RTS_STATE_VALUE: Final = (
    '{"selectedIndexes":["0"],"logEntries":[],"scrollState":{}}'
)

# Extra fields the MAIN PAGE's async postbacks (module select, timer polls)
# need, distinct from the dialog's (see above). Confirmed via HAR: the
# security-code fix worked with only its 4 essential fields (no need to
# replicate the page's full _ClientState clutter), so the same minimal
# approach is tried here: __ASYNCPOST plus the ScriptManager field and its
# static TSM version blob (identical across module-select and timer-poll
# in the capture, i.e. tied to the page/session, not the specific postback).
EXPERT_PAGE_TSM_FIELD: Final = "ctl00$RSMeControlNetPage"
EXPERT_PAGE_TSM_ID_FIELD: Final = "ctl00_RSMeControlNetPage_TSM"
EXPERT_PAGE_TSM_VALUE: Final = (
    ";;Telerik.Web.UI, Version=2020.1.114.45, Culture=neutral, "
    "PublicKeyToken=121fae78165ba3d4:en-US:40a36146-6362-49db-b4b5-"
    "57ab81f34dac:16e4e7cd:33715776:f7645509:24ee1bba:6d43f6d9:e330518b:"
    "2003d0b8:c128760b:88144a7a:1e771326:c8618e41:1a73651d:333f8d94;"
    "System.Web.Extensions, Version=4.0.0.0, Culture=neutral, "
    "PublicKeyToken=31bf3856ad364e35:en-US:64455737-15dd-482f-b336-"
    "7074c5c53f91:76254418;Telerik.Web.UI, Version=2020.1.114.45, "
    "Culture=neutral, PublicKeyToken=121fae78165ba3d4:en-US:40a36146-6362-"
    "49db-b4b5-57ab81f34dac:f46195d3:854aa0a7:b2e06756:92fe8ea0:fa31b949:"
    "4877f69a:607498fe:4cacbc31:2a8622d7:19620875:874f8ea2:490a9d4e:"
    "bd8f85e4:c172ae1e:9cdfc6e7:e4f8f289:ed16cbdc;"
)
# ScriptManager panel prefix per event target - the value sent is always
# "ctl00$ctl00$<panel>|<event_target>" (confirmed via HAR for both targets).
EXPERT_PAGE_TSM_PANEL_BY_TARGET: Final = {
    "ctl00$rdMain$C$controlExtension$iconMenu$rmMenuLayer":
        "ctl00$ctl00$rdMain$C$controlExtension$ContentWithoutGridPanel",
    "ctl00$DeviceContextControl1$timerUpdateData":
        "ctl00$ctl00$DeviceContextControl1Panel",
}
# RadAjaxManager client-event callback the browser fires on the PARENT
# page whenever a RadWindow dialog (Fachmann unlock, parameter write)
# closes with a "refresh" signal. Confirmed via HAR: this is what actually
# registers state changes server-side - a plain page reload (what we did
# before) carries NO such signal and leaves the change inert. The dialog
# runs in its own independent ViewState/ScriptManager context, so this
# callback must use the PARENT page's own prior state, not the dialog's -
# specifically the state from the main-page timer postback that runs just
# before the security-code POST (byte-for-byte identical in the capture),
# not the earlier submenu reload.
EXPERT_RAM_MASTER_TARGET: Final = "ctl00$RAMMasterPage"
EXPERT_RAM_MASTER_RADAJAX_ID: Final = "ctl00_RAMMasterPage"
EXPERT_RAM_MASTER_TSM_VALUE: Final = "ctl00$RAMMasterPageSU|ctl00$RAMMasterPage"
# The "Function" value differs by which dialog just closed (observed:
# "columns" after the Fachmann-unlock dialog, "refreshdata" after a
# parameter write) - only the unlock case is needed for navigation.
EXPERT_RAM_MASTER_UNLOCK_ARGUMENT: Final = (
    '{"Sender":"1","Function":"columns","ValueType":"Int32","Value":"1","Arguments":[]}'
)
# The "Aktualisieren" (refresh) button's ClientState - confirmed via a
# structural field comparison against a real browser's RAMMasterPage
# postback: this field is NOT present as a hidden input anywhere on the
# page (the button's default state is a client-side constant the browser
# always knows, never server-rendered) but IS present in the real
# postback body. Missing it was the one remaining gap found (31/32
# fields already matched before this).
EXPERT_RAM_MASTER_REFRESH_BUTTON_FIELD: Final = (
    "ctl00_DeviceContextControl1_RefreshDeviceDataButton_ClientState"
)
EXPERT_RAM_MASTER_REFRESH_BUTTON_VALUE: Final = (
    '{"text":"Aktualisieren","value":"","checked":false,"target":"",'
    '"navigateUrl":"","commandName":"","commandArgument":"F003",'
    '"autoPostBack":true,"selectedToggleStateIndex":0,'
    '"validationGroup":null,"readOnly":false,"primary":false,"enabled":true}'
)
# The icon-menu control's own client state (confirmed via HAR:
# {"logEntries":[],"selectedItemIndex":"6"}). Live testing showed the
# module-select postback is accepted (real response, valid page state) but
# the parameter dialog still comes back empty afterwards - suggesting the
# server needs this control-level state, not just the postback event
# target/argument, to register "module N selected" in the session. Only
# relevant for the module-select postback, not the timer polls.
EXPERT_MODULE_ICONMENU_STATE_FIELD: Final = (
    "ctl00_rdMain_C_controlExtension_iconMenu_rmMenuLayer_ClientState"
)
EXPERT_MODULE_ICONMENU_STATE_TEMPLATE: Final = '{"logEntries":[],"selectedItemIndex":"%s"}'
# Icon-menu postback selecting a device module; ARG "6" = heat pump on the
# reference installation. Configurable via CONF_EXPERT_MODULE_ARG because
# the menu index can differ on other installations/module layouts.
EXPERT_MODULE_MENU_TARGET: Final = "ctl00$rdMain$C$controlExtension$iconMenu$rmMenuLayer"
EXPERT_MODULE_ARG_HEATPUMP: Final = "6"
CONF_EXPERT_MODULE_ARG: Final = "expert_module_arg"
# Cached Fachmann module list ([{index, value, label}]) from the last
# discovery, so the options-flow module picker renders instantly on repeat
# runs (with a "refresh" affordance). Installation-specific; stored in
# options like the slot ids.
CONF_EXPERT_MODULE_LIST: Final = "expert_module_list"
# Timer postback that pulls live values after navigating to a module.
EXPERT_TIMER_TARGET: Final = "ctl00$DeviceContextControl1$timerUpdateData"
# Live-value loading after a module select. The dialog can come back empty
# while values are still trickling in. Rather than firing a fixed batch of
# timer postbacks up front, expert_writer polls ON DEMAND: _fetch_form fires
# one timer postback only when the dialog is still empty, then retries -
# stopping the instant the dropdown is populated (early exit). The retry
# budget and pause are EXPERT_FORM_MAX_ATTEMPTS / EXPERT_FORM_RETRY_DELAY_
# SECONDS below, so the two former per-poll constants are no longer needed
# as a fixed loop. They are retained here only as documentation of the
# real browser's observed behaviour (a capture needed ~2 polls) and in case
# a fixed pre-poll is ever reintroduced; they are not read by the code.
EXPERT_TIMER_MAX_POLLS: Final = 4
EXPERT_TIMER_DELAY_SECONDS: Final = 3
EXPERT_TIMER_SETTLE_SECONDS: Final = 2
# How many times _fetch_form fetches the parameter dialog before giving up
# if it still comes back with an empty dropdown, and how long to wait
# between those attempts. Each empty attempt also fires one on-demand
# live-value timer postback (see EXPERT_TIMER_TARGET) before retrying.
EXPERT_FORM_MAX_ATTEMPTS: Final = 4
EXPERT_FORM_RETRY_DELAY_SECONDS: Final = 3

# Scraper Constants
MISSING_DATA_STRINGS: Final = ["--", "label ist null", "label ist null "]
BOOLEAN_OFF_STRINGS: Final = ["off", "aus"]
BOOLEAN_ON_STRINGS: Final = ["ein", "on"]
TEMPERATURE_KEYWORDS: Final = ["temperatur", "temperature", "temp"]
PERCENTAGE_KEYWORDS: Final = ["leistungsanforderung", "drehzahl", "power_requirement", "speed"]
ENERGY_POWER_KEYWORDS: Final = ["energie", "energy", "wärmemenge", "warmemenge", "leistung", "power"]
