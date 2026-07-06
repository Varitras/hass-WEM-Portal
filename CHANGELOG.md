# Changelog

All notable changes to this fork are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [1.7.19] – 2026-07-06

### Fixed
- **Expert navigation: add Referer headers matching the real browser.**
  Direct verification (searching response dumps for Fachmann-only values
  like "Schaltdifferenz") showed the Fachmann permission never actually
  took effect server-side, despite every postback being accepted without
  error. The real browser sends a Referer header on every request in the
  chain; `curl_cffi` does not do this automatically, and none were being
  sent. Added Referer headers matching the HAR exactly: main-page
  postbacks reference the main page; the security-code dialog's POST
  references its own GET URL (a same-page form submit); the write POST
  references the exact URL of the last successfully fetched parameter
  dialog (including its real cache-buster), not a fresh one.

## [1.7.18] – 2026-07-06

### Fixed
- **Expert navigation: replace fake page reload with the real unlock
  callback.** Full response dumps revealed that reloading the main page
  after the Fachmann security-code dialog closes had zero functional
  effect - the unlock was never registered server-side, despite the
  security-code POST itself looking successful. The real browser never
  reloads the page here; instead, the closing dialog fires a
  RadAjaxManager client callback on the parent page
  (`ctl00$RAMMasterPage`) that actually registers the unlock. The same
  mechanism reappears after a parameter write, confirming it's a generic
  "dialog closed" callback rather than a one-off. Since the security-code
  dialog runs in its own independent ViewState context, this callback now
  correctly carries forward the parent page's own prior state rather than
  the dialog's response.

## [1.7.17] – 2026-07-06

### Fixed
- **Expert navigation: register module selection via icon-menu client
  state.** All navigation postbacks are now accepted by the server (real,
  growing responses, valid page state throughout), yet the parameter
  dialog still came back empty after module select and the timer polls.
  The module-select postback's event target/argument tells the server
  which control fired, but the icon-menu control's own client state
  (`selectedItemIndex`) appears to be what actually persists "module N
  selected" into the session. Added, matching the configured module
  argument.

### Changed
- **Reduced timer polls from 8 to 4.** A real browser capture needed only
  2 polls before the dialog came back populated on the first genuine
  attempt; 4 keeps a safety margin while roughly halving the request
  count for this step.

## [1.7.16] – 2026-07-06

### Fixed
- **Expert navigation: add ScriptManager fields to module/timer
  postbacks.** A hybrid test confirmed the Fachmann unlock alone doesn't
  populate the parameter dialog - module selection is required. The
  module-select and timer-poll postbacks were still rejected because they
  also need a `ctl00$RSMeControlNetPage` field (pattern
  `ctl00$ctl00$<panel>|<event_target>`) plus its static TSM version blob,
  in addition to `__ASYNCPOST=true`. Added for the two known targets only;
  other postbacks are unaffected.

## [1.7.15] – 2026-07-06

### Fixed
- **Expert navigation: complete the Fachmann-unlock async postback.** The
  security-code postback is now sent as a full Telerik async postback
  (`__ASYNCPOST=true` plus the RadAjax control id, ScriptManager target and
  dialog client state), which the server previously rejected when only the
  header was sent.

### Changed
- **Expert navigation: try the Fachmann unlock alone, then fetch the dialog
  directly** (`EXPERT_SKIP_MODULE_NAV`). The module-select and timer-poll
  postbacks depend on many client-generated fields that a non-browser HTTP
  client can't reproduce; skipping them and fetching the parameter dialog
  right after the unlock is both simpler and more robust when it suffices.
  The full postback chain is retained behind the switch.

## [1.7.14] – 2026-07-06

### Fixed
- **Expert navigation: correct postback shapes.** The navigation postbacks
  were all modeled as Telerik async postbacks, which the server rejected.
  In fact the submenu (Fachmann) unlock is a classic full postback ending
  in a 302 redirect - no `__ASYNCPOST` field, no `X-MicrosoftAjax` header -
  while the module-select and timer postbacks are async postbacks that need
  `__ASYNCPOST=true` in the body in addition to the header. `_postback` now
  distinguishes the two shapes, and the main page is reloaded after the
  unlock so the async postbacks start from the post-unlock state with a
  fresh `__ECNPAGEVIEWSTATE`.

## [1.7.13] – 2026-07-06

### Fixed
- **Expert navigation: recognize `__ECNPAGEVIEWSTATE` as the page state
  field.** The portal's main pages don't use the standard `__VIEWSTATE`
  hidden field but a Telerik/ECN variant, `__ECNPAGEVIEWSTATE`. The
  presence checks looked only for `__VIEWSTATE` and therefore reported
  the page state as missing during navigation. The checks and diagnostics
  now accept either field (main pages use the ECN variant, dialog pages
  use plain `__VIEWSTATE`).

## [1.7.12] – 2026-07-06

### Fixed
- **Expert navigation: parse Telerik async-postback (delta) responses.**
  The Fachmann navigation steps (submenu, module select, timer polls)
  return a Telerik/MS-Ajax delta response, not HTML. The hidden-field
  extractor only understood HTML, so it read no `__VIEWSTATE` from those
  responses and silently forwarded an empty one into the next postback,
  breaking the navigation chain without raising - leaving the parameter
  dialog with an empty value dropdown. The extractor now parses the delta
  format as well, falling back to HTML otherwise.
- **Quietly skip statistics groups the module rejects (status 3001).**
  On every startup the statistics discovery logged a warning for a group
  that `Statistics/Refresh` lists but `Statistics/Read` rejects with
  status 3001 ("invalid parameter") for the fixed module. The group was
  already skipped; it is now skipped at debug level, while any other
  error is still logged as a warning. The server-side status code is now
  attached to the raised error so callers can react to specific codes
  without parsing the message text.

### Added
- Step-by-step debug logging across the expert navigation (main page,
  Fachmann unlock, module select, each timer poll, and per-postback
  response size / delta-vs-HTML / VIEWSTATE presence), so a remaining
  failure can be pinpointed to the exact step.

## [1.7.11] – 2026-07-06

Completes the expert write feature (making it actually work against the
live portal) and a full-construct review pass.

### Added
- **Expert write now reproduces the full Fachmann navigation.** The first
  live tests returned an empty value dropdown; a browser HAR capture
  showed that reaching a Fachmann parameter is a stateful multi-step
  sequence, not a single dialog fetch. The client now reproduces it: load
  the main page, unlock the Fachmann level via the security-code dialog
  (code `11`, publicly known), select the target module, and poll the
  live-value timer until values arrive - only then is the value dropdown
  populated (proven in the capture: first dialog fetch 0 options, second
  91).
- **Configurable module menu index** (`expert_module_arg`, default `6` =
  heat pump on the reference installation) for other module layouts.
- **German translations** (`de.json`); config and options were previously
  English-only even under a German HA locale. Readable, localizable names
  for the two expert number entities.

### Changed
- **Conservative expert-write timing (reliability over speed).** More
  generous live-value polling plus a settle pause, and `_fetch_form()`
  now retries on an empty dropdown instead of failing immediately - it
  stops as soon as values are present, so the generous budget costs
  nothing when the server is quick. A write takes ~1 min.
- **Expert writes run as background tasks.** A write takes longer than a
  frontend service call will wait, which surfaced as a UI timeout even
  though the write completed. Both the number entity and the service now
  detach the work and report the outcome via a persistent notification
  plus the log; a second concurrent write is rejected.
- **Expert number entities follow the `has_entity_name` convention** like
  the other platforms (readable, translatable names instead of the raw
  technical slug).
- Sharpened the `language` option label (it sets the language of the
  portal-derived sensor/parameter names, not the integration UI), and
  added the missing `language`/`mode` labels on the first-time setup
  dialog.

### Fixed
- **403 cooldown now survives an api re-instantiation.** The coordinator
  re-creates the `WemPortalApi` on repeated errors but reset the cooldown
  and never updated `hass.data['api']` - so an active rate-limit cooldown
  could be silently dropped (resuming requests against a server that just
  said back off), and the expert writer could end up on a different api
  object with an independent cooldown. The state is now carried into the
  new instance and `hass.data['api']` is updated, keeping the cooldown
  integration-wide.

### Meta
- Declared the minimum Home Assistant version (`2023.3.0`) in `hacs.json`
  (required by `async_create_background_task`).

## [1.7.10] – 2026-07-06

### Fixed
- **Expert write access: empty value dropdown on first live test**
  ("no numeric options found"). Root cause: the edit dialog resolves
  device data from the server-side session context - a fresh login
  session has no active installation selected yet (a browser gets this
  implicitly by navigating the portal). The client now loads the portal
  main page once after login, establishing the installation context
  (+1 request per write operation).
- **Sharper error messages:** login page instead of the dialog → clear
  auth error (content and redirect detection); empty dropdown → specific
  context diagnosis including a debug-level response snippet when debug
  logging is enabled.
- **Clean error surfacing:** the number entity and the service now report
  failures as `HomeAssistantError` - the frontend shows the actual error
  text instead of an "Unexpected exception" traceback in the log.

Note: starting with this version, every delivered test build gets its own
version number so the installed build is always unambiguous.

## [1.7.9] – 2026-07-05

New feature: **expert write access via the web
portal** for Fachmann parameters the mobile API does not expose at all
(proven from the cached module data: the API delivers only 18 user-level
parameters, while the Fachmann view shows 100+ values - e.g. the heat
pump's power limit, "Leistungsbegrenzung").

### Added
- **New standalone module `expert_writer.py`** - fully separate from
  scraper/API/coordinator (those files are untouched; `number.py` only
  minimally hooked in). Own short-lived session per operation, invoked
  explicitly only - **no periodic polling**. The global 403 cooldown
  applies here as well.
- **Option "Expert write access (web)"** in the configure dialog -
  **OFF by default**. While disabled: no service, no entities, behavior
  identical to 1.7.8.
- **Service `wemportal.set_expert_parameter`** (entityvalue + value):
  fetch form → validate the value against the device's live option list
  (the real allowed range, never bypassed) → "Senden" postback → re-read
  and **verify**. Unconfirmed writes raise `ParameterWriteError`.
- **Two optional number entities** (`wp_leistungsbegrenzung_heizen`/
  `_kuehlen`) via entityvalue fields in the options. The value updates
  only on writes (verified post-write state) and is restored after
  restarts (RestoreNumber). Min/max tighten to the device's real range
  after the first successful write.

## [1.7.8] – 2026-07-05

Hardening and efficiency work on the web scraper. Focus: fail fast on a
slow server, gap-free rate-limit protection, less connection-setup
overhead.

### Added
- **30s timeout on all scraper requests:** a hanging/slow WEM server used
  to block the update cycle until the coordinator-wide timeout (360 s).
  Now the individual request fails after 30 s and the existing
  retry/backoff takes over much earlier.
- **403 cooldown now also covers the scraping path:** the global cooldown
  (in the API path since 1.7.7) is now checked before every scraping run,
  and a 403 from the web frontend activates it too. Background:
  Weishaupt's rate limit applies per IP/account, not per endpoint - a 403
  from either side therefore pauses both. Applies to 403 only; all other
  errors remain handled per-path as before (scraper error ≠ API pause and
  vice versa).
- **Scraper connection reused across cycles:** the scraper instance
  (including its TCP connection/TLS session) now persists instead of
  being rebuilt every cycle - saving a full connection handshake per
  cycle. After auth errors or a 403 it is deliberately discarded and
  cleanly closed so recovery starts on a fresh connection.

### Changed
- Code hygiene in the scraper: `ICON_MAPPER` once at module level instead
  of per table row; dead else-branch removed in `parse_expert_page`;
  login error handling untangled (own errors are no longer re-wrapped by
  the network-error handler).

### Unchanged (deliberately)
- The 2-second pause after the login POST stays in place (risk/benefit of
  removing it unclear, so left untouched).

Covered by 4 new tests (instance reuse across 3 cycles, 403 → cooldown +
scraper discard + fail-fast without network activity, auth error →
scraper discard, timeout on all HTTP calls) plus the full existing suite
(9 test files).

## [1.7.7] – 2026-07-05

Further, deliberately conservative measures to reduce load on Weishaupt's
server. All changes are strictly additive/more cautious -
nothing ever polls more frequently or aggressively than before, only
less.

### Added
- **403 vs. 401 handled separately:** a 403 (rate limit/block) no longer
  automatically triggers a fresh login attempt (an extra request at
  exactly the wrong moment). Instead, a 30-minute cooldown is activated:
  **all** further requests (including to entirely different endpoints)
  fail immediately until then, with no network access at all. A 401
  (expired session) behaves as before (one retry with a fresh login).
- **Heating schedules (CircuitTimes) are cached:** refetched only every
  4 hours per heating circuit instead of on every single update cycle -
  they only change when edited manually in the Weishaupt app (not
  editable via HA anyway).
- **Statistics interval widened to 4 hours** (previously 1 hour) - these
  are daily aggregates that don't change hourly.

Covered by 4 new, targeted tests (no retry on 403, cooldown blocks
follow-up requests without any network access, 401 behaves unchanged,
CircuitTimes skipped on the second cycle).

## [1.7.6] – 2026-07-05

Full codebase review across all files, focused on uncaught
exceptions and robustness gaps. No functional behavior change for normal
operation - purely hardening against edge and error cases.

### Fixed
- **`number.py`/`select.py`/`switch.py`:** the same crash risk from
  direct key access (`ParameterID`, `icon`, `ModuleIndex`, `ModuleType`,
  `min_value`/`max_value`/`step`) that was already fixed in `sensor.py`
  is now also fixed in the three sibling files. A single unexpected data
  record can no longer crash the entire platform setup for all devices.
- **`number.py`:** numeric validation tightened - `NumberEntity` has no
  valid text state, so a numeric value is now always enforced, not just
  when a unit happens to be present (same error class as the power-sensor
  crash from 1.7.5, closed preventively here).
- **`mapper.py::process_api_values`:** now processes each data point with
  its own error isolation - a single malformed value no longer aborts
  processing for the rest of the entire device update. Also fixed a
  missing `_LOGGER` import (which would itself have caused a
  `NameError`).
- **`__init__.py`:** `migrate_unique_ids()` no longer crashes on empty
  `coordinator.data`; a corrupted discovery cache file no longer prevents
  startup entirely; the migration itself is now fault-tolerant; minor
  defensive `.get()`/default fixes.
- **`coordinator.py`:** catch-all exception handler added (covers e.g.
  `asyncio.TimeoutError` on very large installations that genuinely
  exceed the timeout); the old HTTP session is now cleanly closed on
  re-instantiation instead of just discarded.
- **`wemportalapi.py`:** `get_parameters()` now also catches `ValueError`
  (broken JSON), not just `KeyError`; `assert` replaced with an explicit
  check; `api_login()`/`web_login()` now catch the broader
  `RequestException` instead of only `HTTPError` (consistent with
  `make_api_call`); fixed a latent `NameError` in `web_login()` (access
  to `response` before it was guaranteed to be assigned);
  `fetch_webscraping_data()` now applies the same backoff to plain
  network errors as to other failure modes.

Verified with 7 test suites (including new, targeted tests for each of
the points above).

## [1.7.5] – 2026-07-05

### Fixed
- **Crash on numeric sensors caused by text values:** `sanitize_value()`
  returned text (`"Off"`/`"On"`) instead of a number for recognized
  boolean values whenever the current reading had no unit attached (e.g.
  a plain `"Aus"` without a number). For sensors that are fundamentally
  numeric (`device_class: power`, `state_class: measurement`) whose unit
  (e.g. `"kW"`) was preserved from a previous cycle (see the value-gap
  protection from 1.7.0), this combined into: real unit + non-numeric
  text value - Home Assistant strictly rejects that and the sensor could
  not be created at all
  (`ValueError: [...] has the non-numeric value: 'Off'`). Affected e.g.
  `sensor.warmepumpe_soll_leistung` and `sensor.warmepumpe_ist_leistung`.
  `sanitize_value()` now always returns `0.0`/`1.0` for boolean values,
  never text - as in the original code before this branching was
  introduced. Additionally, as a second safety layer: `sensor.py` now
  also uses `device_class`/`state_class` (not just the current cycle's
  unit) to detect that a numeric value is required, and
  `device_class`/`state_class` are now set **before** value validation so
  this safety net is active from the very first entity creation.

## [1.7.4] – 2026-07-05

### Fixed
- **`fuzzywuzzy` performance warning in the log:** added
  `python-Levenshtein` to the requirements so the fast C extension for
  fuzzy matching in `select.py` is installed automatically instead of
  falling back to the slower pure-Python implementation. Purely
  cosmetic/performance - functionality was not affected.

## [1.7.3] – 2026-07-05

### Fixed
- **SELECT options failed to match due to an API language mismatch:**
  even after the 1.7.1 fix, "Value Off not found in options [...] (names:
  ['Aus', ...])" kept occurring. Cause this time: the **live value read
  from the API** came back as `"Off"` (English) while the option list
  derived from the parameter definition (`EnumValues`) contained `"Aus"`
  (German) - independent of the sanitize_value fix from 1.7.1. The
  existing fuzzy string matching could not bridge this, since `"Off"` and
  `"Aus"` share no letters and the similarity score stayed far below the
  threshold of 75. `select.py` now explicitly recognizes German/English
  on/off synonyms (`"Off"`↔`"Aus"`, `"On"`↔`"Ein"`) and matches them
  against the actually present option list regardless of language.
  Covered by tests for both directions plus a sanity check against false
  positives.

## [1.7.2] – 2026-07-05

### Fixed
- **Empty error messages on failed API calls:** `get_response_details()`
  checked `if response:` - for `requests.Response` that is always `False`
  for status codes ≥ 400 (`Response.__bool__`), i.e. exactly when an
  error is present. As a result, the error description sent by the server
  (status/message) was never read, and log messages like "Server returned
  status code: and message:" stayed empty. Affects only the diagnostic
  quality of the logs, not functionality itself (the existing resilience
  mechanism - individual failed statistics groups are skipped, everything
  else continues normally - was not affected).

## [1.7.1] – 2026-07-05

### Fixed
- **Regression from 1.7.0:** SELECT entities where an option is literally
  named `"Aus"` (e.g. "Hot Water Push" with options
  `Aus, 5, 10, ..., 240` minutes) failed with
  `Value Off not found in options [...]`. The cause: the new, shared
  `sanitize_value()` (see 1.7.0) was applied to **all** EnumValues
  parameters, not just true SWITCH booleans - rewriting `"Aus"` to
  `"Off"`, which then no longer matched the entity's own option list.
  Normalization now runs only for parameters with `DataType == SWITCH`;
  other enum values (SELECT) keep their original string. Covered by a
  regression test (reproduces the exact reported case).

## [1.7.0] – 2026-07-05

### Added
- Discovery cache: device/module/parameter definitions are now persisted
  across Home Assistant restarts. The slow, rate-limited parameter
  discovery (`get_parameters()`, ~5 s per module) only runs when
  something is actually missing (fresh install, new module).
- Session/cookie reuse for web scraping: before a full login handshake,
  the session from the last successful scrape is tried first. Reduces
  requests per cycle.
- `RestoreSensor` for all sensors: the unit (`unit_of_measurement`) is
  restored from the last known state after a restart if it is briefly
  missing right after startup.
- Additional, purely additive backoff safety margin in the coordinator
  after repeated failures (scaled, capped at 6 h) - existing
  sleep/rate-limiting times in `wemportalapi.py` remain unchanged.

### Changed
- Consolidated `sanitize_value()` from `mapper.py` and `scraper.py` into
  a single shared implementation in `utils.py`.

### Fixed
- **Locale-dependent switch bug:** switches with the value `"Ein"`/`"On"`
  (capitalized, depending on portal language or API vs. scraping path)
  were incorrectly shown as "off". Detection extended to
  `1`, `1.0`, `"On"`, `"on"`, `"Ein"`, `"ein"`.
- **Header merge bug in `make_api_call()`:** call-specific headers (e.g.
  in `get_statistics()`) previously replaced the default headers entirely
  instead of extending them, losing `Host`/`User-Agent`/`Accept`.
- **False "Unknown" gaps:** a single missing scrape/API value overwrote
  the last known value with `None` or `0.0`. The last known value is now
  kept until a new valid value arrives.
- **Crash risk during entity setup:** `values["platform"]` (direct key
  access) in `sensor.py`/`number.py`/`select.py`/`switch.py` could abort
  the entire platform setup for all devices on a single unexpected data
  record. Now `.get("platform")` with clean skipping of individual broken
  entries.

### Removed
- Unused constant `REFRESH_WAIT_TIME` (dead code).

---

## [1.6.0] – upstream (erikkastelec/hass-WEM-Portal)
Base version of this fork. See the
[original repo](https://github.com/erikkastelec/hass-WEM-Portal) for
earlier history.
