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

# Scraper Constants
MISSING_DATA_STRINGS: Final = ["--", "label ist null", "label ist null "]
BOOLEAN_OFF_STRINGS: Final = ["off", "aus"]
BOOLEAN_ON_STRINGS: Final = ["ein", "on"]
TEMPERATURE_KEYWORDS: Final = ["temperatur", "temperature", "temp"]
PERCENTAGE_KEYWORDS: Final = ["leistungsanforderung", "drehzahl", "power_requirement", "speed"]
ENERGY_POWER_KEYWORDS: Final = ["energie", "energy", "wärmemenge", "warmemenge", "leistung", "power"]
