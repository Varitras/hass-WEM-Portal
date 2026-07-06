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
GITHUB_PROJECT_URL: Final = "https://github.com/erikkastelec/hass-WEM-Portal/issues"
DEFAULT_NAME: Final = "Weishaupt WEM Portal"
DEFAULT_TIMEOUT: Final = 360
WEB_MAIN_URL: Final = "https://www.wemportal.com/Web/Default.aspx"
WEB_LOGIN_URL: Final = "https://www.wemportal.com/Web/Login.aspx"
CONF_SCAN_INTERVAL_API: Final = "api_scan_interval"
CONF_LANGUAGE: Final = "language"
CONF_MODE: Final = "mode"
DEFAULT_MODE: Final = "api"
AVAILABLE_MODES: Final = ["api", "web", "both"]
PLATFORMS = ["number", "select", "sensor", "switch"]
DATA_GATHERING_ERROR: Final = "An error occurred while gathering data.This issue should resolve by itself. If this problem persists,open an issue at https://github.com/erikkastelec/hass-WEM-Portal/issues"

# Server-side status code returned by Statistics/Read for a statistics
# group that isn't valid for the queried module (ModuleType 7/Index 0).
# The refresh call lists such groups, but reading them is rejected with
# this code. It's an expected, harmless per-group condition - skipped
# quietly rather than logged as a warning on every startup.
WEM_INVALID_PARAMETER_STATUS: Final = 3001
DEFAULT_CONF_SCAN_INTERVAL_API_VALUE: Final = 300
DEFAULT_CONF_SCAN_INTERVAL_VALUE: Final = 1800
DEFAULT_CONF_LANGUAGE_VALUE: Final = "en"
DEFAULT_CONF_MODE_VALUE: Final = "api"
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
FORBIDDEN_COOLDOWN_SECONDS: Final = 30 * 60  # 30 minutes

# Per-request timeout for the web scraper's HTTP calls. Without one, a
# slow/hanging WEM Portal response would block the executor thread until
# the coordinator-wide DEFAULT_TIMEOUT (360s) fires; failing the single
# request after this many seconds instead lets the existing retry/backoff
# logic take over much sooner.
SCRAPER_REQUEST_TIMEOUT_SECONDS: Final = 30

# Heating schedules (CircuitTimes) rarely change - only when a user edits
# them directly in the WEM Portal app (this integration only ever shows
# them as read-only sensors). Refetching them every single coordinator
# cycle is unnecessary load; this caps how often they're refreshed.
CIRCUIT_TIMES_REFRESH_INTERVAL_SECONDS: Final = 4 * 3600  # 4 hours

# Energy statistics are daily aggregates - they don't need hourly
# refreshes. Widening this beyond the original 1 hour further reduces
# steady-state load without any meaningful loss of freshness.
STATISTICS_REFRESH_INTERVAL_SECONDS: Final = 4 * 3600  # 4 hours

# Expert write access (web) - disabled by default. Only when enabled are
# the wemportal.set_expert_parameter service and (if entityvalues are
# configured) the two Leistungsbegrenzung number entities registered.
CONF_EXPERT_WRITE: Final = "expert_write_enabled"
CONF_EXPERT_ENTITY_HEATING: Final = "expert_entityvalue_heating"
CONF_EXPERT_ENTITY_COOLING: Final = "expert_entityvalue_cooling"
SERVICE_SET_EXPERT_PARAMETER: Final = "set_expert_parameter"

# --- Expert web navigation (Fachmann level) -----------------------------
# The Fachmann parameters (e.g. Leistungsbegrenzung) live behind a second
# authentication step and a stateful navigation sequence, reconstructed
# from a real browser HAR capture. These identify the ASP.NET postback
# targets/arguments of that sequence.
WEB_DEFAULT_URL: Final = "https://www.wemportal.com/Web/Default.aspx"
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
# Hybrid test switch: skip the fragile module-select + timer-poll postback
# chain and, after the Fachmann unlock, fetch the parameter dialog
# directly. Live test showed the dialog stays empty without module
# selection, so the module postback is required - now False again since
# _postback() adds the minimal ScriptManager fields these postbacks need.
EXPERT_SKIP_MODULE_NAV: Final = False
# Submenu postback that opens the expert-code (Fachmann) dialog.
EXPERT_SUBMENU_TARGET: Final = "ctl00$SubMenuControl1$subMenu"
EXPERT_SUBMENU_ARG: Final = "3"
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
# Timer postback that pulls live values after navigating to a module.
EXPERT_TIMER_TARGET: Final = "ctl00$DeviceContextControl1$timerUpdateData"
# Waiting for the portal to load live values after selecting a module.
# The browser polls repeatedly with no explicit "done" signal. We favor
# reliability over speed: poll generously and only stop early once the
# parameter dialog actually returns a populated value list (checked by the
# caller). EXPERT_TIMER_MAX_POLLS caps the total attempts,
# EXPERT_TIMER_DELAY_SECONDS is the pause between polls, and
# EXPERT_TIMER_SETTLE_SECONDS is an extra settle pause after the polls
# before the first dialog fetch. These are deliberately conservative:
# a few extra seconds per (rare, on-demand) write is a fair price for it
# working every time. A real browser capture needed only 2 polls; 4 keeps
# a safety margin while halving the previous request count (less load).
EXPERT_TIMER_MAX_POLLS: Final = 4
EXPERT_TIMER_DELAY_SECONDS: Final = 3
EXPERT_TIMER_SETTLE_SECONDS: Final = 2
# How many times to retry fetching the parameter dialog if it still comes
# back with an empty dropdown (values not fully loaded yet), and how long
# to wait between those retries.
EXPERT_FORM_MAX_ATTEMPTS: Final = 4
EXPERT_FORM_RETRY_DELAY_SECONDS: Final = 3

# Scraper Constants
MISSING_DATA_STRINGS: Final = ["--", "label ist null", "label ist null "]
BOOLEAN_OFF_STRINGS: Final = ["off", "aus"]
BOOLEAN_ON_STRINGS: Final = ["ein", "on"]
TEMPERATURE_KEYWORDS: Final = ["temperatur", "temperature", "temp"]
PERCENTAGE_KEYWORDS: Final = ["leistungsanforderung", "drehzahl", "power_requirement", "speed"]
ENERGY_POWER_KEYWORDS: Final = ["energie", "energy", "wärmemenge", "warmemenge", "leistung", "power"]
