# Changelog

All notable changes to this fork are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.10.1] – 2026-07-18

### Fixed
- **Sensor icons are no longer all lightning bolts.** Every unit except `°C`
  received `mdi:flash`, and an explicitly set icon always beats the one Home
  Assistant derives from the device class - so correct classes (power, energy,
  duration, flow rate, temperature) were overridden on nearly every sensor.
  No icon is supplied where a device class exists; Home Assistant picks one
  that matches the rest of the interface.
- **Pressure sensors get a device class.** The portal reports `BAR` in
  uppercase while the Home Assistant constant is `bar`, so the lookup missed
  and the sensor had no class at all. Units are now matched
  case-insensitively.
- Percent and rpm sensors, which have no device class in Home Assistant, get
  `mdi:percent` and `mdi:fan` instead of a lightning bolt.
- Energy statistics sensors set their icon in a second place, so the same
  `kWh` quantity could appear with two different icons.

Values, units, history and long-term statistics are unaffected - this changes
only which icon is shown.

## [1.10.0] – 2026-07-18

### Upgrading from 1.9.0 - please read

- **The config entry is migrated** on first start. Take a backup of your Home
  Assistant configuration (at least `.storage`) beforehand.
- **Percent sensors lost the `power_factor` device class.** Values like power
  limit, pump speed or heating output are not a power factor, so the label was
  simply wrong. The icon changes; unit, state class, history and long-term
  statistics are unaffected.
- **`wemportal.set_expert_parameter` now runs synchronously and raises on
  failure** instead of returning immediately and only reporting via a
  notification. Automations can finally tell whether a write succeeded - but
  the action now takes a few seconds, and one that used to "succeed" silently
  may now surface a real error.

### Added
- **Discover expert (Fachmann) parameters from the options UI.** Pick which
  modules to search, then choose a parameter per slot from a dropdown labelled
  `group / name (current value)`; the same parameter cannot be picked twice.
  Entering an entityvalue by hand still works in the same field. Discovery
  runs only on demand, never in the background, and reports which of three
  things happened if it cannot run.

### Fixed
- **The full `entityvalue` no longer leaks on a 403.** The rejected request's
  URL was logged verbatim and embedded in the raised error, and the
  parameter-dialog URLs carry the installation-specific ID in their query
  string - so it reached the log, notifications and service errors alike.
- **A 403 on the expert path no longer pauses sensor polling.** Every 403 was
  treated as an IP-wide rate limit, but it can equally mean the portal simply
  rejected that one request. The expert path backs off on its own; a 403 seen
  by the normal polling still pauses everything, because that is the real
  rate-limit signal.
- **The expert path reuses its web session** instead of logging in for every
  operation - the login is the request the portal rejects most readily.
  Cookies are held in memory only, never written to disk.
- **Switching mode `web` → `both` no longer crashes**, and disabled devices
  are no longer polled - including a fully disabled installation, and the web
  scraper, which ignored the filter entirely.
- **A missing reading no longer reads as `0.0`.** It now makes the sensor
  unavailable instead of reporting a fabricated value that automations could
  act on.
- **A cycle in which every device fails is reported as failed** instead of
  silently serving stale values, so backoff and eventual re-authentication
  engage. The coordinator also counts its own update timeouts now.
- **Re-authentication can no longer switch accounts**, and the expert service
  resolves its target account per call, refusing when it is ambiguous.
- **The config entry version is actually bumped**, so the migration no longer
  re-runs on every startup, and entity unique_ids are migrated for every
  device rather than just the first.
- Numerous smaller fixes: HTTP sessions closed on unload, on a failed first
  refresh and after config-flow validation; bounded waiting for the shared API
  lock; statistics retried after 15 minutes rather than a full hour;
  `beautifulsoup4` declared in the manifest.

### Changed
- The `set_expert_parameter` action is translatable, and its entityvalue field
  carries an explicit "installation-specific - do not share publicly" warning.
- README rewritten around the two ways to obtain an entityvalue, with the
  manual route kept in full.

### Development
- The test suite grew from 15 to 110 tests, including end-to-end tests against
  a real Home Assistant instance. Every fix above is guarded by a regression
  test, and the tests themselves were verified by re-introducing the bugs they
  guard.

## [1.9.0] – 2026-07-08

### Fixed
- **Scraped sensors keep a stable device id (and history) across mode
  switches.** Web-scraped sensors are stored under a device id that becomes
  part of their entity `unique_id`. Previously that id depended on the mode:
  in `web` mode there is no API-discovered device, so scraped sensors fell
  back to a placeholder device (`0000`), while in `api`/`both` mode they
  attached to the real device id. Switching modes therefore re-created the
  scraped entities under a different id, orphaning the originals and losing
  their history. The scraper device id is now decided once - preferring the
  real API device id, falling back to the placeholder only for a pure-web
  install that has never seen the mobile API - and then persisted, so it
  stays constant across mode switches. Existing installations lock in
  whatever id they already use, so nobody loses history on upgrade.

### Code quality
- **Lint/consistency pass; no functional change to normal operation.**
  Removed unused imports, dead code (an unused device-id assignment) and
  whitespace noise; consolidated duplicate constants (`WEB_DEFAULT_URL`
  into `WEB_MAIN_URL`, `DEFAULT_CONF_MODE_VALUE` into `DEFAULT_MODE`);
  removed the unused `DEFAULT_NAME`. `scraper.py` now uses relative
  imports and the shared integration logger like every other module.
  Hoisted function-level `re`/`random` imports to module level (one sat
  inside a per-row parsing loop). The sensor platform now guards data
  access with `.get()` like the other platforms.
- **Error hints now point to this fork's issue tracker** instead of the
  upstream project's, and the data-gathering error text got its missing
  spaces back.
- **Modernized Home Assistant API usage:** the coordinator passes
  `config_entry` explicitly to the base class (the implicit variant is on
  HA's deprecation path) and uses `asyncio.timeout` instead of the
  third-party `async_timeout`; the switch platform uses the
  `SwitchDeviceClass` enum. Number entities now expose parsed numeric
  values as real floats instead of numeric strings.
- **`get_data()` split into three focused steps** (device status,
  parameter values, heating schedules) for readability; order, error
  handling and behaviour are unchanged.
- **Minimum Home Assistant version raised to 2024.11** (`hacs.json`): the
  explicit `config_entry` coordinator parameter used above only exists
  since 2024.11. The previous floor (2023.3) predates several APIs this
  integration already relied on.
- **Smaller style fixes.** The rate-limit cooldown check on the API object
  is now a public method (`check_cooldown`), matching its real use as the
  shared cooldown gate for the standalone expert writer. The options flow
  builds its prefill helper as a local function instead of a lambda stored
  on the flow instance. `config_validation`/`entity_registry` imports use
  the Home Assistant idiom (`cv` alias / `from ... import`).

### Security
- **The password field in the setup and re-authentication dialogs is now
  masked** (proper password input type) instead of rendering as clear text
  while typing.
- **A mistyped entityvalue no longer appears in full in error texts.** The
  "invalid entityvalue" error shown in notifications and logs now contains
  only the shortened form of the id - a nearly-correct id (e.g. one
  character off) previously ended up almost complete in exactly the texts
  people copy into issues and forums.
- **Internal ids derived from an entityvalue are now digests.** Entity
  unique_ids, persistent-notification ids and background-task names embed a
  truncated SHA-256 of the entityvalue instead of the raw
  installation-specific id, so shared `.storage` files or diagnostic dumps
  no longer contain it. Existing expert entities are migrated in place
  (entity id and history are preserved).
- **Authentication error messages no longer include the raw server response
  body.** They keep the HTTP status and the server's own status/message
  fields; a full response body (often an entire HTML error page) does not
  belong in UI messages and logs.
- **The account email is no longer logged at warning level** on failed API
  logins (warnings are what people paste into issues; debug logs keep it).
- **CI: the HACS validation action is pinned to a commit SHA** instead of a
  mutable branch reference.

### Added
- **Re-authentication support.** When the portal login keeps failing (e.g.
  after a password change), Home Assistant's re-authenticate prompt now
  opens a proper credentials dialog instead of failing with an unknown-step
  error that required deleting and re-adding the integration.

### Changed
- **The advanced module-menu-index option only accepts digits** (or empty
  for the default), so a typo is caught in the form instead of being sent
  to the portal as a postback argument.

## [1.8.5] – 2026-07-08

### Changed
- **Scan intervals now have a lower bound.** The web and API scan
  intervals in the options are clamped to a minimum (60 s web, 10 s API),
  like the expert poll interval already was. A stray tiny value such as
  `1` second would poll the portal continuously and reliably trigger the
  IP-wide 403 rate limit.
- **A single transient login failure no longer forces reauthentication.**
  The portal occasionally serves a login page mid-session; previously one
  such hiccup immediately put the integration into Home Assistant's
  reauth state, stopping all automatic retries until manual action. Auth
  errors now escalate to reauth only after 3 consecutive failures and are
  retried like other errors before that.
- **The language option is a closed choice (en/de) in the options too.**
  Previously the options dialog accepted any free-text language code,
  unlike the initial setup form.

### Fixed
- **Crash instead of a clear error when the API login hit a network
  failure.** A connection error/timeout during the login POST crashed the
  error handler itself (unbound `response` variable), surfacing as
  "Unexpected error" instead of the intended authentication error message.
- **A failed device refresh no longer discards the discovery cache.** The
  device/module list was cleared before the API call; if that call failed
  (e.g. a single 403), the in-memory parameter definitions were lost and
  the next successful cycle re-ran the slow, rate-limited full parameter
  discovery the cache exists to avoid. The new list now replaces the old
  one only after the call succeeded.
- **Missing request timeouts on the login paths.** The API login POST and
  the web login used for config-flow validation had no timeout, so a
  hanging server could block an executor thread indefinitely. They now
  use the same timeouts as the regular API/scraper requests.
- **Connection leak on error recovery.** When the coordinator re-created
  the API object after repeated errors, it closed the old API session but
  not the old instance's persistent web-scraper session, leaking one open
  connection towards the portal per recovery.
- **Minor robustness fixes:** device ids are normalized to strings before
  data lookups (latent KeyError with int ids); the expert write service
  strips whitespace around the passed entityvalue; retried API calls no
  longer report error details from the previous attempt's response; the
  unique-id migration triggers a light debounced refresh instead of a
  second full portal cycle right after startup.

## [1.8.4] – 2026-07-07

### Changed
- **Silent `except: pass` blocks now log at debug level.** The five
  best-effort cleanup/parse fallbacks (session close, cookie clear, hidden-
  field parsing) no longer swallow errors silently - they keep the same
  non-fatal behaviour but leave a debug-log trace, so a regression (e.g. a
  portal format change) is visible during troubleshooting. Resolves the
  static-analysis "try/except/pass" findings.

### Documentation
- **README: background on the entityvalue ID.** Explains that part of the ID
  is installation-specific (don't share/copy IDs) and that the embedded
  value snapshot is ignored for addressing - which is why a stored ID keeps
  working after the value changes, and why the ID must not be "normalized".

## [1.8.3] – 2026-07-07

### Fixed
- **A stored invalid slot ID (e.g. a leftover `0`) can now be cleared.**
  The slot fields used `default`, so clearing a field on save fell back to
  the stored value - making it impossible to delete an invalid entry: it
  could neither be saved (rejected as invalid) nor removed (reverted to the
  old value). The fields now use `suggested_value`, which prefills the
  current value but lets an emptied field stay empty, so a stray value can
  be deleted.

## [1.8.2] – 2026-07-07

### Fixed
- **Auto-poll skips invalid stored entityvalues instead of polling them.**
  A too-short or non-hex ID left in the config (e.g. a stray `0` from before
  the length check existed) is now skipped during periodic reads rather than
  triggering a portal request that hits an empty dialog and logs a
  misleading "reading 0 failed" warning. Active single reads/writes still
  reject such IDs with a clear error. The validity rule (hex + minimum
  length) is shared with the options-flow validation.

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
