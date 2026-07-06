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
# Submenu postback that opens the expert-code (Fachmann) dialog.
EXPERT_SUBMENU_TARGET: Final = "ctl00$SubMenuControl1$subMenu"
EXPERT_SUBMENU_ARG: Final = "3"
# Save button inside a RadWindow dialog (Fachmann code + parameter write).
EXPERT_DIALOG_SAVE_TARGET: Final = "ctl00$DialogContent$BtnSave"
# Field carrying the Fachmann security code ("11", publicly known).
EXPERT_SECURITY_CODE_FIELD: Final = "ctl00$DialogContent$tbxSecurityCode"
EXPERT_SECURITY_CODE: Final = "11"
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
# working every time.
EXPERT_TIMER_MAX_POLLS: Final = 8
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
