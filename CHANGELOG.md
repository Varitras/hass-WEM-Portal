# Changelog

All notable changes to this fork are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.8.1] – 2026-07-07

### Changed
- **README clarifies the example entityvalue.** The hex ID shown in the
  "how to find your entityvalue" steps and the service example is now
  explicitly marked as an illustrative placeholder, not a real/copyable ID.
- **Consistent wording for the option toggles.** The advanced module-select
  and security-code toggles now follow the same structure as the other
  options (short description + default + a "Note:" caveat), while keeping
  their leading warning symbol to flag them as the riskier advanced
  options.

### Fixed
- **Short slot IDs like `0` are no longer accepted.** The options-flow
  entityvalue validation now requires a minimum length in addition to being
  hex, so a stray entry like `0` or `abc` is rejected in the form instead
  of being saved as a valid ID (which would only cause a failing portal
  request later). Real IDs (long hex strings) are unaffected; empty stays
  allowed.
- **Saving the options without changes no longer reloads the integration.**
  The options dialog previously always triggered a full reload (and a fresh
  portal login) on save, even when nothing changed - e.g. saving without
  edits, or typing only whitespace into an already-empty slot-ID field
  (whitespace is stripped to empty). The flow now detects an unchanged
  save and closes without reloading, which also avoids needless requests
  against the portal's 403 rate limit.

## [1.8.0] – 2026-07-07

### Added
- **Option to notify on successful expert writes** (`Notify on successful
  expert write`), off by default. A successful write no longer posts a
  persistent notification unless this is enabled, which avoids notification
  noise when setting several values. Failed writes always notify, and
  successes are still written to the log regardless.
- **Expert slot IDs are validated when saving the options** (hex-only): a
  typo'd entityvalue is flagged directly in the form instead of failing
  cryptically on the first read/write. Entered values are preserved on the
  error redisplay.
- **Persistent auto-poll failures now surface**: if reading a configured
  parameter fails 3 times in a row (usually a mistyped entityvalue), one
  notification per id is raised; it resets on the next successful read.

### Changed
- **Dropped the `fuzzywuzzy` and `python-Levenshtein` dependencies.** The
  select platform's last-resort fuzzy option matching now uses Python's
  standard-library `difflib` (same 0.75 similarity cutoff), removing two
  external requirements - including one that needs C compilation and could
  fail to install on some architectures. `fuzzywuzzy` was also deprecated
  (renamed to `thefuzz` upstream).
- **Centralized the per-device `DeviceInfo`** into a single
  `build_device_info()` helper in `utils.py`, replacing the block that was
  duplicated across the number, select, sensor, and switch platforms. All
  four sub-devices now report a consistent model.
- **`DeviceInfo` is now imported from `homeassistant.helpers.device_registry`**
  (the current location) instead of the legacy `homeassistant.helpers.entity`
  re-export, in all five entity platform modules - future-proofing against
  the eventual removal of the old import path.
- **Installation-specific entityvalue IDs are shortened** in log messages
  and notification texts (first 6 characters + ellipsis), so copying logs
  into issues or forums no longer leaks the full id. Debug-level logs keep
  the full id for troubleshooting.
- **Rate-limit cooldown errors now show the remaining time in minutes**
  instead of raw seconds.

### Fixed
- **Empty parameter values no longer crash numeric sensors** (matches
  upstream issue #141). When the portal sends an empty string for a
  parameter, `sanitize_value()` now returns `None` (the sensor shows as
  "unavailable") instead of passing the empty string through, which on a
  numeric sensor raised "could not convert string to float: ''" during
  entity setup. A fabricated `0.0` is deliberately avoided so a sensor
  briefly without a reading doesn't report a false zero. (The sensor
  platform already had a second guard for this; the value source is now
  correct too.)
- **Auto-poll no longer collides with a running write**: a poll cycle is
  skipped while a write is in flight, and a poll result that arrives during
  a write is discarded - previously a stale pre-write value could briefly
  overwrite the freshly verified one.

## [1.7.0] – 2026-07-07

First release of this fork, based on upstream
[erikkastelec/hass-WEM-Portal](https://github.com/erikkastelec/hass-WEM-Portal)
1.6.0. Focus areas: fewer and gentler requests to Weishaupt's servers,
broad robustness hardening, and a new optional expert (Fachmann) read/write
feature for parameters the mobile API does not expose.

### Added

- **Expert (Fachmann) read/write access via the web portal.** Many
  Fachmann parameters (e.g. the heat pump's power limit,
  "Leistungsbegrenzung") are only available in the web frontend and are not
  exposed by the mobile API. A new, self-contained module reaches them
  through the same web form the portal uses, in a minimal three-step
  navigation (log in → switch to the Fachmann submenu → fetch the
  parameter dialog).
  - **Disabled by default.** While off, no extra entities or services
    exist and behaviour is unchanged.
  - **Ten configurable parameter slots**, each with a free-text name and an
    `entityvalue` ID, become writable `number` entities.
  - **Service `wemportal.set_expert_parameter`** (entityvalue + value):
    fetches the form, validates the value against the device's own live
    option list (its real allowed range, never bypassed), submits, then
    re-reads and verifies. Unconfirmed writes raise an error.
  - **Writes run as background tasks** (a write takes roughly 5-15 s) and
    report the outcome via a persistent notification and the log; a second
    concurrent write is rejected.
  - **Optional periodic read-back** (off by default) reads all configured
    parameters in one shared session at a configurable interval (default
    60 min, minimum 15), with a small random jitter so the pattern is less
    regular. A warning in the UI and README notes the 403 risk.
  - **Advanced fallback toggles** (off by default) to re-enable a module
    selection step and a security-code step for unusual portal/module
    layouts.
  - Values are restored across restarts (`RestoreNumber`).
- **Discovery cache:** device/module/parameter definitions persist across
  restarts, so the slow, rate-limited parameter discovery only runs when
  something is actually missing.
- **Session/cookie reuse for web scraping:** the previous session is tried
  before a full login handshake, reducing requests per cycle.
- **Scraper connection reused across cycles** instead of rebuilt each time;
  discarded and cleanly closed after auth errors or a 403.
- **`RestoreSensor` for all sensors:** the unit of measurement is restored
  from the last known state if briefly missing right after startup.
- **30 s timeout on all scraper requests**, so a hanging server fails fast
  and hands over to the existing retry/backoff much earlier.
- **Additional, purely additive coordinator backoff** after repeated
  failures (scaled, capped), on top of the existing rate-limit-aware
  pacing.
- German translations (`de.json`) for the config and options UI.

### Changed

- **403 handling (rate limit) is now a global cooldown.** A 403 activates
  a 15-minute cooldown during which all further requests fail immediately
  without network access, instead of triggering an immediate re-login at
  the worst moment. The cooldown covers both the API and the web-scraping
  paths, since Weishaupt's rate limit applies per IP/account, and it is
  preserved when the API object is re-instantiated after repeated errors.
- **Heating schedules (CircuitTimes) are cached** and refetched at most
  once an hour per circuit (they change only when edited in the Weishaupt
  app).
- **Statistics are refreshed at most once an hour** (daily aggregates that
  don't change every cycle).
- Consolidated `sanitize_value()` into a single shared implementation.

### Fixed

- **Locale-dependent switch state:** switches reporting `"Ein"`/`"On"`
  (depending on portal language or API vs. scraping) could show as "off".
  Detection now covers the German and English on/off spellings.
- **SELECT options failing to match across languages:** a live value like
  `"Off"` no longer fails against a German option list (`"Aus"`, …);
  German/English on/off synonyms are matched regardless of language.
- **False "Unknown" gaps:** a single missing scrape/API value no longer
  overwrites the last known value with `None`/`0.0`; the last value is kept
  until a new valid one arrives.
- **Numeric sensors crashing on text values:** boolean normalisation now
  always yields a number (never text) for numeric sensors, so a unit
  carried over from a previous cycle can't combine with a text value into
  an invalid state that Home Assistant rejects.
- **Robustness across entity setup and data handling:** malformed
  individual data records are isolated (via `.get()` and per-record error
  handling) so one bad record can't abort platform setup for a whole
  device; several latent crash paths in `__init__.py`, `coordinator.py`,
  `wemportalapi.py`, `mapper.py` and the entity platforms were closed.
- **Header merge in `make_api_call()`:** call-specific headers now extend
  the default headers instead of replacing them.
- **Empty server error messages:** error details returned by the server on
  failed API calls are now read and logged instead of coming out blank.

### Removed

- Unused dead code (e.g. the `REFRESH_WAIT_TIME` constant).

---

## [1.6.0] – upstream (erikkastelec/hass-WEM-Portal)

Base version of this fork. See the
[original repo](https://github.com/erikkastelec/hass-WEM-Portal) for
earlier history.
