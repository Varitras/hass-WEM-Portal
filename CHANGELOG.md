# Changelog

All notable changes to this fork are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.10.0b6] – 2026-07-18

Pre-release. Stops the expert path from logging in for every operation.

### Fixed
- **The expert (Fachmann) path now reuses its web session instead of logging
  in every time.** Each expert operation - opening the discovery dialog,
  running discovery, every write, every auto-poll - performed a full login.
  That is the request the portal rejects most readily: a 403 on `Login.aspx`
  was observed while the same portal answered a browser normally (including
  in a fresh incognito session) and while the scraper, which has reused its
  cookies for exactly this reason, kept working.

  Cookies are cached in memory only - never written to disk, since a live
  session cookie is credential-equivalent - and reused for at most 15
  minutes. A dead session falls back to a full login. A 403 during a reuse
  attempt is not retried with a login, so a rejection cannot turn into a
  second rejected request.

## [1.10.0b5] – 2026-07-18

Pre-release. Addresses the discovery finding a live test produced: the
search ran without being blocked, but returned no parameters.

### Fixed
- **Expert discovery now reads the module page from the postback response.**
  Selecting a module is an async postback whose response already contains the
  re-rendered module panel. That response was discarded in favour of a plain
  `GET Default.aspx`, which live-testing showed returning no readable
  parameters. The postback response is now used whenever it carries rows -
  it is the server's direct answer to "show me this module", and it saves a
  request. The previous `GET` remains as a fallback.
- Discovery logs how many parameters each source yielded, so a remaining
  failure identifies itself instead of needing another round of guessing.

## [1.10.0b4] – 2026-07-18

Pre-release. Corrects how a 403 on the expert path is interpreted.

### Fixed
- **A 403 on the expert (Fachmann) path no longer pauses sensor polling.**
  Every 403 was treated as an IP-wide rate limit and paused the entire
  integration for 15 minutes - but a 403 can equally mean the portal simply
  did not accept that one request. This was confirmed in practice: the portal
  was reachable in a browser while the integration considered itself blocked.
  The expert path now backs off on its own for 5 minutes and leaves polling
  alone. A 403 seen by the API or scraper still pauses everything, including
  the expert path, because that is the actual rate-limit signal.
- **A 403 now names the request that was rejected.** The previous message
  covered more than a dozen possible call sites without saying which one,
  which made every diagnosis guesswork.

### Changed
- The options form now shows the exact remaining backoff time and the
  rejected request, instead of a vague "try again later".

## [1.10.0b3] – 2026-07-18

Pre-release, based on first feedback from 1.10.0b2.

### Fixed
- **A failed parameter discovery is now reported instead of silently
  producing an empty dropdown.** Three cases are distinguished: portal access
  paused after a 403 (no request was even sent), the search failed, and the
  search ran but found nothing. Previously all three looked identical to the
  user - an empty list with no explanation.
- **A missing reading is no longer logged as an invalid value.** The portal
  regularly reports no current value for a parameter, which correctly makes
  the sensor unavailable; logging that at warning level made a normal
  condition look like a defect. It is now debug. Values that genuinely cannot
  be interpreted are still warned about.

### Changed
- The module-list reload option now states that it is rarely needed and that
  an extra portal login right after the first one can trigger a 403 block.

## [1.10.0b2] – 2026-07-18

Pre-release. Identical to 1.10.0b1 apart from the manifest key order, which
hassfest requires to be `domain`, `name`, then alphabetical. No functional
change - 1.10.0b1 runs fine, it only failed the linter.

## [1.10.0b1] – 2026-07-18

Pre-release. The expert-parameter discovery is new and has not yet been
verified against a live portal - please report what the module list and the
slot dropdowns actually show.

### Added
- **Discover expert (Fachmann) parameters from the options UI.** A new
  options menu can search selected modules on the portal and list the
  available expert parameters; each of the ten slots is now a dropdown of
  discovered parameters (a parameter can't be picked twice). Manual entry of
  an entityvalue still works. Discovery runs only on demand.

### Fixed
- **Switching mode `web` → `both` no longer crashes.** A web-only install
  persists a placeholder scraper device id (`0000`); after a switch to
  `both` it existed as a data key without matching API modules, so the API
  refresh raised `KeyError` - which its own error handler then re-raised.
  Scraper-only devices are now skipped by the API and statistics paths.
- **Missing values no longer read as a real `0.0`.** A momentarily missing
  value (`--`) now becomes unavailable (`None`) for every sensor, not just
  energy/power - a briefly missing temperature no longer reports 0 °C and
  can no longer trigger automations on a fabricated reading.
- **A cycle in which every device's data fetch fails is now reported as a
  failed update** (so backoff and eventual re-auth engage), instead of being
  marked successful while serving stale values. A partial success (at least
  one device refreshed) still counts as success.
- **Disabled devices are no longer polled.** The coordinator's disabled-
  device lookup now uses the same device identifier the entities register
  (`<entry_id>:<device_id>`); previously it looked up a bare id that never
  matched, so a disabled device kept being polled.
- **The expert-write service now targets the correct account** when more
  than one config entry exists: the target account is resolved per call (and
  the call is refused when it is ambiguous) instead of being fixed to the
  first-loaded entry, and the shared service is only removed once no
  expert-enabled entry remains loaded.
- **Re-authentication can no longer silently switch accounts.** The entered
  username must match the entry's existing account (case-insensitive); only
  the password is updated.
- **A 403 during an expert (Fachmann) operation now engages the shared
  cooldown**, so the API and scraper paths back off too - previously the
  expert client could only *check* the cooldown, never set it, so an expert
  403 kept the rest of the integration hitting a rate-limited portal.
- **Concurrent expert operations can no longer collide.** A shared per-account
  lock serialises entity writes, the service, and the auto-poll read, so two
  operations can't target the same heating parameter or open parallel portal
  sessions at once; a second concurrent operation is rejected.
- **The auto-poll now uses the current API instance** after a session
  recovery (it previously kept a reference to the discarded instance and its
  stale cooldown state), and its initial background read is now cancelled on
  unload instead of running on after the entry is gone.
- **`beautifulsoup4` is now declared as a requirement** in the manifest (the
  web-login path imports it); it previously worked only because Home
  Assistant happens to ship the library.
- **Config-entry migration now bumps the entry version**, so an old (v1)
  entry is no longer treated as migration-pending on every startup.
- **A rejected API write is no longer reported as success.** The write now
  checks the portal's response `Status` (the portal can answer HTTP 200 with
  `Status != 0`, as the login does) and raises instead of optimistically
  showing the new value.
- **`unique_id` migration now runs for every device**, not just the first, so
  additional devices' old ids and history are migrated too.
- **The full installation-specific entityvalue is no longer written to the
  debug log** (only a shortened form), and the empty-form HTML snippet dump is
  smaller.
- **API writes and the poll cycle no longer run concurrently on the same
  session/state.** A shared lock serialises a `change_value` write against a
  `fetch_data` poll cycle, so they can't interleave and corrupt the HTTP
  session or the in-memory data.
- **HTTP sessions are now closed on unload and after config-flow validation**,
  instead of leaving open connections behind on every reload/setup attempt.

### Changed
- **A failed energy-statistics cycle is retried after 15 minutes instead of a
  full hour.** The rate-limit timestamp is still set before the fetch (so a
  portal that keeps failing is never asked more often than that), but a cycle
  that failed for every device no longer costs a whole refresh interval.
- **The `wemportal.set_expert_parameter` service now runs synchronously and
  raises on failure** instead of returning immediately and only reporting via
  a notification, so automations can tell whether the write actually
  succeeded. The write still takes a few seconds (portal navigation); the
  number-entity slider keeps its background behaviour.
- The manifest now declares `integration_type: hub`.
- **Percent sensors no longer report the `power_factor` device class.** Values
  like power limit, heating/cooling output, pump speed and power demand are
  not a power factor (cos phi), so the label was simply wrong. They keep their
  `%` unit and `measurement` state class, so history and long-term statistics
  are unaffected - only the icon and any `device_class`-based filtering
  change. Operating-hour sensors keep `duration` / `total_increasing`.
- **Development only:** the test suite gained end-to-end tests that run
  against a real Home Assistant instance (entry setup/unload, the schema
  migration, the config/options/reauth flows and the expert service). They
  are marked `e2e` and deselected in the default run; CI runs the full suite
  with `-m ""` against the current Home Assistant release.
- **Development only:** the API data mapper and the web scraper's page
  parsing are now covered by tests (both were untested). These pin down
  which platform each portal parameter becomes, how values and units are
  parsed, and that malformed input costs only the affected data point.

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
