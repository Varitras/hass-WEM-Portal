# Changelog

All notable changes to this fork are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

Findings from several rounds of auditing 1.11.0b2, plus a re-audit of the
fixes themselves. Mostly correctness and safety: who may write a heating
parameter, what counts as a successful update, and which failures are allowed
to ask the user for new credentials.

### Security
- **The expert SERVICE now requires an administrator.** It was registered with
  no permission check at all, so any authenticated Home Assistant user could
  call it and change a heating setting. The opt-in option and the
  installation-specific parameter id were obscurity, not access control.

  This covers the service and nothing else. The same parameter is also a
  number entity, and Home Assistant has no way for an integration to restrict
  one - entity access is decided per user in Home Assistant itself. Anyone
  allowed to control that entity can still write the parameter, so treat the
  entity's permissions as part of the setting, not the service call as the
  only door.
- **The service only accepts parameters configured in the integration's
  options.** Without that it was a generic write primitive for any parameter
  of the installation, including ones never exposed to Home Assistant.
- **Portal URLs are stripped before they reach a log.** A rejected request
  used to be logged verbatim, publishing the installation-specific parameter
  id from the query string and, on cookieless sessions, a session id from the
  path. Neither the account nor those ids appear in the log any more.

### Added
- **Holiday begin and end are dates now, not switches.** The portal types them
  the same way it types a real on/off parameter, and the only thing telling
  the two apart - whether it declared any bounds - was being filled in with a
  guess. So they became toggles, and toggling one would have written a
  holiday starting on the 1st of January 1970 to the heating system. They are
  date entities on a new `date` platform, reading and writing the whole-day
  encoding the portal actually uses. The switch entity each of them left
  behind is removed on the next start.
- **Every device reports its own availability.** The coordinator only knew
  whether a CYCLE succeeded, so on a multi-device installation a device that
  had been offline for days still presented its last reading as current. Its
  entities now go unavailable, while the connection-status, error and
  error-message sensors stay available to explain why.
- **A relabelled portal row is reported.** Scraped sensors are keyed by their
  portal labels, so a wording change at the portal silently produces a new
  entity and leaves the history behind on the old one. Nothing can prevent
  that, but it is now visible in the log instead of being noticed weeks later.
- **Config entries carry a normalised account id.** Adding the same account
  twice with different capitalisation created a second entry polling the same
  installation. Existing entries are given the id on their next start.
- **A service sets a holiday as one range: `wemportal.set_holiday`.** Home
  Assistant sets one entity at a time, and a holiday is not one value. The
  portal takes begin and end only together, and only when the range they
  describe makes sense - a write carrying one of them alone is refused, and a
  pair whose begin falls after its end is reported as successful and stores
  nothing. So whichever of the two date entities is set first describes half a
  range the portal may discard, and which half that is depends on the order
  the user happens to click in. The service takes both dates and both entities
  and puts them on the wire in one request. Both must belong to the same
  module, and a range that ends before it starts is refused rather than
  reported as a setting that never happened. Like the expert service, it
  requires an administrator: a service call is not covered by the per-user
  entity permissions Home Assistant applies to the entities themselves.
- **A weekly programme is readable.** Heating and hot water programmes arrive
  as a JSON object with three fixed windows per day, and the sensor's state
  was the single word "Programmed" - the times could only be got at by
  parsing the raw string in a template. The state is the week itself now,
  with consecutive days that match collapsed into one range, and a `Schedule`
  attribute lists every day with the windows it actually uses. Where the
  portal marks a window with a letter, the attribute carries it. A week too
  long for a Home Assistant state falls back to the bare word, and the
  attribute still holds every window.

  Where the device's own view of the programme is available it is used
  instead, and it is the complete one. The value read delivers the programmed
  windows and nothing else, so the stretches between them - the base level,
  which is most of a normal day - simply do not appear. The schedule fetch
  returns every stretch, and the level names with it, in the portal's own
  words: a Monday reads `00:00-06:00 Komfort, 06:00-10:10 Normal,
  10:10-13:30 Absenk, 13:30-24:00 Komfort` rather than three windows with a
  three-hour hole in the middle. Nothing about the levels is interpreted
  here; the portal ships their names alongside the programme.

### Fixed
- **An unreadable login page no longer costs the password.** If the portal
  answers the login page with an empty or unparseable body, the credentials
  are not sent. Previously the form came back empty, the login posted anyway -
  without the ASP.NET state the portal demands echoed back - and the request
  could only be refused. Same rule as the maintenance check: nothing is handed
  to a page that cannot process it.
- **A weekly programme is no longer blanked by the value read on the portals
  that actually have one.** Schedules are exempt from the "stop showing what
  the portal did not send" rule, because they are fetched on their own path
  with their own throttle. That exemption asked for the declared parameter
  type, and a 3.1.3.0 portal types every programme as an ordinary switch with
  the schedule as JSON in the value - so it applied to nobody with such a
  portal, and a cycle that answered without the programme took the readable
  week with it. It is now recognised by what the value is, the same way the
  schedule fetch already recognises it. Real switches are still cleared.
- **Readings from a web scrape that stopped working stop being presented as
  current.** When the scrape failed, the values from the last one that worked
  stayed in place and the sensors stayed available, so a number from hours ago
  was shown as the present reading with nothing saying otherwise. After three
  consecutive failures they now show as unknown instead. Three rather than
  one: a single failed scrape is ordinary, and the second is where the cached
  session is discarded and a full login retried - only the third says the
  portal is not delivering. Counted in failures rather than in elapsed time
  because each failure adds a growing pause of its own, so the same multiple
  of the scan interval means a different thing on every installation. Units
  and names are kept, so entities do not change identity.
- **A date the portal accepted but did not store is no longer displayed as
  set.** A write that returns without an error means the request was accepted,
  not that the value was kept: a holiday range that ends before it starts is
  answered with a success status and silently discarded. The entity published
  the written day on the strength of that answer, so Home Assistant showed a
  holiday that did not exist until the next poll took it away again, minutes
  later and with no explanation. It now reads the value back and shows what
  the portal actually holds. The `set_holiday` service, which writes both
  dates at once, refuses such a range up front and is unchanged. A read-back
  that itself fails is logged, not raised - the write did happen.
- **A device whose every statistics group failed no longer counts as read.**
  Statistics are fetched once an hour, and a cycle that failed for every
  device retries after a much shorter interval instead. Errors on individual
  groups were swallowed one at a time, so a device where all of them failed
  still returned normally and counted towards that hour - the readings then
  waited the full interval in the one case where waiting is most clearly
  wrong. A group the portal reports as not applicable to the module is still
  not a failure: retrying sooner cannot produce a reading that does not exist.
- **A parameter the portal did not answer for is no longer logged as a fault.**
  An empty reading is a normal condition - the portal regularly omits a
  parameter, and the integration deliberately blanks one it left out so a stale
  reading is not published as current. The sensor platform already treated that
  as expected; number and select reported it as an invalid value, so the
  integration's own bookkeeping arrived in the log as a warning. Select even
  attached the parameter's full option list - 49 entries, to say there was no
  value. A value that is present but genuinely unusable still warns.
- **The relabelled-rows warning names the likeliest cause and stops promising
  entities that cannot exist yet.** Changing the display language in the
  portal's own account settings relabels every scraped row at once, which the
  message did not mention - so a switch someone made themselves read like a
  fault in the integration. It also claimed the new labels "become NEW
  entities", but entities are created once during setup: nothing new appears
  at that moment, the existing sensors simply lose their row and show as
  unknown until the labels come back. Only a restart creates entities from the
  new labels, and that is when the history stays with the old ones.
- **Expert parameter discovery takes the same lock as everything else on that
  path.** Only one expert portal operation per account may run at a time - the
  entity write and the scheduled read both take a shared lock, but discovery,
  the heaviest of the three and the only one started by hand, took none. It
  could open a second portal session beside a running read or write. Starting
  it while another operation holds the lock now says so instead of queueing a
  second session.
- **A scheduled expert read stops when its configuration is unloaded.** The
  read fetches several parameters on one session, and cancelling it cancels the
  waiting, not the worker thread - so an unload halfway through kept navigating
  the portal with the credentials of an entry being torn down. The cooperative
  stop the write path already had now covers the read as well.
- **Changing the mode is checked against the connection it switches to.** The
  mobile API and the web portal are separate logins, so one working says
  nothing about the other - which is why the initial setup validates exactly
  the transport the chosen mode will use. Switching mode afterwards skipped
  that check, so an entry could be moved to a connection its credentials do not
  work on: the dialog reported success and every update failed. A save that
  leaves the mode alone still costs no portal request.
- **Setting up or re-authenticating no longer sends requests into an active
  rate-limit block.** The cooldown after a 403 was checked on every polling
  request but not on the logins themselves, and the config and re-authentication
  flows call those directly - which is exactly where somebody lands after
  deleting and re-adding the integration to "fix" a blockade. Every attempt
  extended the very block it was trying to escape. It now fails immediately,
  without a request, and says so.
- **A refused login PAGE is reported as a refusal.** The 403 handling covered
  the request that submits the credentials, but the one before it - fetching
  the login page, and therefore the first request to meet a blocked IP - was
  reported as "could not load the page". That reads like a network hiccup,
  invites an immediate retry and started no cooldown at all.
- **A portal page that is neither the login form nor a session is no longer
  counted as a wrong password.** Anything answered with HTTP 200 that did not
  contain the logout button was treated as rejected credentials, and three of
  those in a row ask the user to re-enter a password that was correct the whole
  time. A rejection now has to look like one: the portal rendering its login
  form again. Planned maintenance is also recognised on this second answer, not
  only on the page fetched before it - the window can open between the two.
- **A module whose description cannot be read is no longer asked again every
  cycle.** A rejected request and an empty description are both recorded with a
  timestamp, which is what bounds the retry. An answer in an unexpected shape -
  an HTML error page, a truncated payload - was skipped with only a log line
  and no timestamp, so nothing held it back and the request repeated on every
  update, without limit. It is now booked like the other two, and a module that
  already had a working parameter list keeps it.
- **The "re-read parameter lists" option now also covers the modules it exists
  for.** A module the portal refuses to describe keeps an empty list, so it is
  retried once a day instead of never. The button tested that stored list for
  truthiness rather than for presence, and therefore skipped exactly those
  modules - the only ones for which waiting a day is the wrong answer.
- **A disabled device is no longer polled once per restart.** The device filter
  asked "do we know any devices?" of the readings collected this session, which
  are empty until the first fetch runs - so every restart looked like a fresh
  install and sent no filter at all. It now asks the persisted module cache,
  which knows the same devices and survives the restart.
- **Planned maintenance is recognised on the web path as well.** It was only
  detected during a full login, so a reused session ran into the maintenance
  page unchecked.
- **Network problems are no longer counted as wrong credentials.** A timeout,
  a DNS failure or a 5xx from the portal was reported as an authentication
  error, so three portal outages in a row could ask for a password that was
  correct. The re-authentication counter is now genuinely consecutive: any
  other kind of failure in between resets it.
- **An account with several devices is told what the web path does with
  them.** The scraper reads a single expert page and the portal decides
  which device that page shows, while its sensors are filed under the first
  device the mobile API reported. Those need not be the same one, and
  nothing said so. They still are - resolving it means driving the portal's
  device selector, which needs a multi-device account to develop against -
  but the log now names the device the sensors were filed under, and the
  README says `api` mode covers every device correctly.
- **A refused write says what the portal answered.** Home Assistant shows a
  failed service call as the exception text and nothing else, and that text
  was "Error changing parameter X value" - while the portal's own reply sat
  one exception deeper, where only debug logging would have shown it. A
  rejected holiday date read exactly that.
- **A reused web session that lands on the wrong page no longer costs the
  whole scrape.** The fast path reuses a cached session and posts to select
  the Expert tab, and that postback carries state the portal can refuse - in
  which case the answer is still HTTP 200, just the main page instead of the
  expert view, with no redirect and no error status for the existing checks
  to catch. The parse then found nothing and the cycle ended there, although
  the fresh login it falls back to would have worked.
- **An expert page with no readings now says what it was.** "Contained no
  readable panels" is true of two unrelated problems and names neither: a
  page that is not the expert view at all, and the expert view with markup
  the selectors no longer match. The log now reports the size, the title and
  how many panel containers were found - zero means the wrong page, one or
  more means the page changed.
- **A portal that says "stop" is no longer answered with a login.** The
  expert path reuses a cached web session and falls back to a full login
  when it turns out to be stale. Only a rate-limit answer was recognised as
  something other than staleness, so announced maintenance and a server
  error both sent the two requests of a login handshake immediately after
  the portal said it was unavailable. The web scraper has treated all three
  as answers for a while; the expert path does now too.
- **A parameter added in the portal is found without reinstalling.** The
  discovered parameter list of a module was cached forever, so activating an
  input or output on a module the integration already knew produced
  something it would never see - no error, no log line, and no way to force a
  re-scan short of removing and re-adding the integration. A new MODULE was
  always found; a new parameter on an existing one never was. The list is
  re-read once a day now, and `Configure` has a `Search the portal for new
  API parameters` entry for the moment right after something changed. A
  re-read that fails keeps the parameters it already had - only a module that
  never had any is dropped. The web path was never affected: it re-reads the
  whole page every cycle.
- **A device with nothing to read is no longer asked anyway.** The portal
  rejects a value read that names no modules with 400 Bad Request, and the
  integration built exactly that request whenever parameter discovery had
  produced nothing for a device - every cycle, for as long as it stayed that
  way. Two requests spent per cycle on an answer that cannot come, and a
  generic "an error occurred while gathering data" in the log. It now says
  which of the two situations it is: a device that has no modules at all is
  simply empty and not a failure, while a device whose discovery produced no
  parameters is reported as one.
- **An API reading the portal did not send is no longer shown as current.**
  The data a cycle writes into is only rebuilt once per session, and each
  cycle writes only what came back, so a parameter the portal left out kept
  its previous value for the rest of the session - with nothing in the log
  saying anything was missing. Holiday begin and end are included on
  purpose: the portal stops sending them once no holiday is set, and last
  year's date standing as current is the same mistake somewhere less
  obvious. Heating schedules are not, because they are fetched on their own
  path that keeps them fresh, and a module the portal did not answer for at
  all is left alone.
- **A scraped reading the portal no longer has is no longer shown as
  current.** The web path deliberately carried the previous value over
  whenever a scrape came back without one, on the assumption that it was
  "still very likely accurate". It is not: the portal renders "--" for a
  value it does not currently have, and a setpoint was observed reading 50.5
  degrees for three hours while the portal and the heat pump both showed
  nothing. Only reloading the integration cleared it. A gap is the truthful
  record of an hour without a reading; a flat line at the last value is what
  automations act on. A row that stops being scraped altogether is cleared
  for the same reason. The unit is still carried over - a "--" row has none,
  and Home Assistant objects when a unit changes.
- **A bad expert poll is no longer blamed on the configured parameter
  IDs.** When every configured parameter failed to read in the same cycle,
  each of them was counted as a broken ID, and after three cycles the user
  got a notification per parameter telling them to fix settings that were
  correct. That now counts as one failed batch. A single configured
  parameter keeps being reported, because with only one there is nothing to
  compare it against - and the message no longer claims to know whether the
  ID or the portal is at fault.
- **A heating schedule that fails to load is no longer re-fetched every
  cycle.** The hourly interval was recorded only after a SUCCESS, so a
  schedule that kept failing never engaged it: every update spent two more
  requests on it, at a portal that was already failing. The attempt is what
  counts now, and a failed one is retried after fifteen minutes rather than
  a full hour.
- **A writeable dropdown whose options the portal left out is a plain sensor
  now.** It had two different outcomes depending on how the options were
  missing: an empty field produced a dropdown with nothing to choose from,
  while an explicit null - which is what the portal actually sends - raised
  while building the option list and was logged as an "unexpected error" once
  per value per cycle. Both show the value as a sensor now. A control with no
  options cannot be operated, so presenting one was never right.
- **A request that never reached the portal no longer reports itself as a
  server answer.** A timed-out read was logged as "Server returned status
  code:  and message: " - two empty fields, because there was no response to
  read them from - which sends anyone looking at that line to the portal for
  a fault on this side of the connection.
- **A slow answer no longer fails the whole cycle on the first try.** The
  per-request timeout is 12 seconds instead of 10, and the two reads that
  carry readings retry once if they did not reach the portal at all.
  Statistics and heating schedules deliberately do not: they are the bulk of
  an hourly cycle, optional, and an hour stale at worst, so retrying those as
  well would push a bad cycle past the timeout covering the whole update. The
  request that starts a measurement is left out for a different reason - it
  is not safe to repeat when only its answer was lost.
- **Re-authentication is actually reachable during startup.** The counter
  lived on the coordinator, and a failed first refresh makes Home Assistant
  build a new one - so a password changed while Home Assistant was off left
  the entry retrying forever instead of asking for a new one.
- **A rate-limited session reuse no longer triggers an immediate login.** The
  403 was swallowed and answered with two more requests - the opposite of
  backing off. For the same reason the scrape backoff is no longer skipped on
  the first cycle after the integration recovers from repeated errors.
- **An error page is no longer parsed as a successful scrape.** The session
  reuse path never checked the status code, so the sensors took on whatever
  fell out of a 500.
- **An empty API answer is no longer counted as a refreshed device**, and a
  write answered with a page instead of a result is no longer reported as a
  completed change.
- **A device that was offline at startup is discovered once it returns.**
  Parameter discovery gated on a status field written only during the initial
  device fetch, so the device stayed empty for the rest of the session and
  only a reload fixed it.
- **An unreachable device no longer fails the whole cycle.** That took every
  other entity down with it, discarded a web scrape that had already
  succeeded, and started a backoff of up to six hours, so the device coming
  back was noticed late.
- **A missing switch reading is reported as unknown, not as off.** Any
  automation watching the switch saw a real state change.
- **An empty API reading no longer overwrites a web reading** collected in the
  same cycle, and a statistics group with no value is skipped instead of
  being reported as zero - which the Energy Dashboard reads as a meter reset.
- **In `both` mode with more than one device, a reading no longer appears
  twice.** The API value was merged into its scraped counterpart only on
  single-device installations; elsewhere both survived as separate entities
  whose values drifted apart.
- **Configuration is validated the way it will actually be used.** A setup
  using the mobile API could be accepted on the strength of a web login and
  then fail on every single update.
- **Adding an account that is already configured says so**, instead of
  reporting an unknown error.
- **The service no longer restricts values to 0-100 in steps of one.** Expert
  parameters include temperatures, times and curves, and half steps that the
  data model itself defines were rejected before they reached the portal.
- **Scan intervals stored by an older release are held to the current
  minimum**, instead of being used exactly as they were saved.
- **A write in progress is stopped when the integration is unloaded** as far
  as that is possible: a request already on the wire cannot be aborted, but
  the write is now abandoned at every step before it - including directly
  before the request that changes the parameter. The service call is covered
  as well, not just the entity.
- **Re-authentication actually reloads the integration.** It reported success
  while doing nothing whenever the entry was unchanged - re-entering the same
  password - or when the failed setup it was meant to repair meant there was
  nothing listening for the update in the first place.
- **Saving options applies them.** Every option is read during setup, and the
  reload that used to happen as a side effect of the same mechanism.
- **A server error on the web path is no longer reported as a wrong
  password.** A 4xx/5xx while loading the expert page was turned into an
  authentication failure, so three portal outages in a row could ask for
  credentials that were correct.
- **A page that contains no readings is no longer counted as a scrape.** An
  error or placeholder page served with HTTP 200 simply parses to nothing,
  which reset the retry counter and left the previous values on display
  looking current.
- **A write is only reported as done when the portal confirms it.** Anything
  other than an explicit success - an error object, an empty answer, a page
  instead of a result - was taken for a completed write, so the entity showed
  the requested value until the next poll quietly replaced it. The portal's
  own reason now appears in the error.
- **A refused measurement refresh is not read as a fresh one.** The portal
  answers a rejection with HTTP 200 and a non-zero status; unnoticed, the
  following read fell back to the previous measurement and its values were
  booked as new.
- **Recovering from repeated errors no longer lifts the portal's rate
  limits.** The recovery rebuilt its connection object from scratch, which
  also reset the hourly limits on statistics and heating schedules - so the
  next cycle refetched both immediately, against a portal that had just been
  failing. It now resets the connection and nothing else.
- **The scrape backoff survives the internal recovery.** Rebuilding the API
  connection after repeated errors discarded the backoff those very errors
  had just earned.
- **Holiday begin and end are written together.** The portal marks both
  writeable and reads them back correctly, but written one at a time each
  write comes back with an internal error and no job id - while an ordinary
  setpoint on the same account and the same endpoint is accepted. A holiday
  is a range, and the portal appears to want the whole of it, so a write now
  carries the module's other date parameters along at their current value in
  the same request. Whether that is enough is measured, not assumed: if the
  portal refuses the pair as well, the parameters are read-only in practice
  and will be presented as such.
- **A blocked IP is named as one during setup and re-authentication.** When
  the portal refuses requests from a network - which it does past its request
  limit, per IP, for twelve hours - the setup flow reported "Failed to
  connect". That reads like a network fault and invites an immediate retry,
  against the very IP being refused, so every attempt made the situation last
  longer. It is exactly what leads to deleting and re-adding the integration
  to "fix" a blockade. Both flows now say what is actually happening and that
  the credentials are not the problem. The options flow has said this
  properly for a while; these two had not.
- **A reading the integration cannot interpret is reported once, not every
  cycle.** The portal occasionally sends a word this integration does not
  know - a pump speed reading "Stop" on an installation whose portal writes
  "Aus" and "off" everywhere else. That is a legitimate answer, not a fault,
  and it arrives on every cycle for as long as the condition lasts, so the
  warning repeated indefinitely and buried everything else. It is said once
  per sensor and value now, names the word, and asks for it to be reported -
  so it can be added to the vocabulary on evidence rather than on a guess
  about somebody else's heat pump. The sensor still shows unknown rather than
  a fabricated number.
- **Two scraped rows that produce one sensor are reported.** The parser
  assigns into its output by key, which overwrites without a word, so one
  reading ends up showing another's value - a plausible number from the wrong
  place. Two ways to get there remain: the same row name twice in one panel,
  or two panels carrying the same heading, where every row of one circuit
  lands on another's and it looks like a missing circuit rather than a
  collision. Both are now named in the log, once, with the panel and row they
  came from. Deliberately reported and not resolved: making the key unique
  would mint new entities and leave the old ones behind, for a collision
  nobody has been observed to have yet.
- **The fault message no longer drops what it cannot show.** Several active
  faults were joined into one string and sliced at 255 characters, silently -
  so the second fault disappeared while the first still read like the whole
  story. Home Assistant does refuse a longer state, so the state still has to
  fit, but it now keeps whole messages and says how many it left out, and an
  `Errors` attribute carries every one of them regardless. A single message
  too long on its own is cut and marked rather than dropped.
- **A status nobody could read is no longer published as the current one.**
  The connection-status, has-errors and error-message sensors are written
  only by a successful device-status read. When one failed they went on
  showing the previous answer as current - and for the fault sensor that
  means reporting "No" because nothing is known rather than because nothing
  is wrong, which is the one direction it must never fail in. They report
  unknown now until the next successful read. The entities stay available,
  because unknown is the honest answer and hiding them would remove the very
  things that explain the situation, and parameter discovery is unaffected: a
  failed status read says nothing about whether the device is there.
- **A heating circuit the portal refuses to describe is no longer lost for
  the session.** A module whose parameter description came back rejected was
  deleted from the cache, and only the once-per-session device read could
  bring it back - so it stayed missing until the integration was reloaded.
  Whether an installation showed one heating circuit or two came down to what
  the portal happened to answer in the second Home Assistant started, which
  is exactly how the reports describe it: sometimes the first only,
  sometimes both. The module is kept with an empty parameter list and a
  timestamp now, so it is asked again on the normal daily interval and
  nothing is ever permanently thrown away. A module that has been described
  as empty is still not polled, so this costs one description request per day
  and nothing per cycle.
- **The heating-schedule fetch runs again.** It only ever looked at
  parameters the portal declares as DataType 6. A current portal types every
  weekly programme as DataType 2 - the same type as an ordinary switch - with
  a JSON object in the value, so on those installations the fetch never ran
  at all. Not failing, never entered, which is why no log ever mentioned it.
  A programme is now recognised by what its value is, and a plain switch is
  still left alone. This is the only path that asks the device for its
  schedule rather than reading the portal's stored copy, so the
  `CircuitTimesDay` and `PossibleValues` attributes it provides come back
  with it.
- **The schedule fetch no longer replaces the programme it enriches.** It
  wrote the fixed word "Active" into the same row the value read fills, so on
  an installation where both paths run, a readable week was replaced by a
  placeholder once an hour until the next cycle put it back. It only adds its
  own attributes now; a row that no value read ever delivered still gets the
  placeholder, because there the fetch is the only source there is.
- **A second write no longer sends the first one back.** Each platform
  updated its own displayed value after a write, but not the coordinator's
  copy - which stays as the last poll left it, minutes ago. The date platform
  reads that copy to build the companion values it sends with a write, so two
  writes inside one poll interval put a superseded value on the wire and
  asked the portal to undo the first. The coordinator's copy is brought up to
  date as soon as the portal accepts a write, and left alone when it refuses
  one.
- **A failed update says what failed.** When every device's parameter fetch
  failed, Home Assistant was handed "all API parameter fetches failed this
  cycle; see the warnings above" - and it shows that message and nothing
  else, so the actual reason (a request that timed out, a refused refresh, a
  read that came back empty) had to be picked out of the log and matched up
  by timestamp. The reason now travels with the failure and names the device
  it belongs to; with several devices failing, every one of them is listed.

### Removed
- **The `beautifulsoup4` dependency.** It was installed for three lines: the
  hidden fields of the web login form. `lxml` is already required and already
  parses the far more involved expert page, so it reads those three lines too
  and the integration is down to two dependencies.
- **`strings.json`.** Home Assistant reads a custom integration's translations
  from `translations/` only - `strings.json` is the source file core
  integrations hand to their translation pipeline, and there is no such
  pipeline here. Nothing loaded it, and nothing compared it against the files
  that are loaded, so it drifted: by the time it went it was two keys and one
  text behind. A test now checks that the English and German catalogues carry
  the same keys, which is the check that was missing all along.

### Changed
- **The minimum supported Home Assistant version is 2024.12.0.** The options
  flow relies on an attribute that does not exist in 2024.11, so the declared
  minimum was wrong rather than merely conservative.
- Home Assistant reloads the integration through exactly one path now. Doing
  it from the configuration flow as well is deprecated as of 2026.6 and
  rejected from 2026.12.
- YAML configuration is explicitly declared unsupported, so a stray
  `wemportal:` block is reported instead of being ignored.
- Sensor icons follow the device class where Home Assistant provides one,
  rather than every entity showing the same generic icon.
- **A reused web session that missed the expert page is no longer reported as
  an error.** The scraper answers such a page by logging in fresh and loses no
  readings, but the diagnostic in front of that fallback went out as a warning
  for every caller - so a self-healing normal case filled the Home Assistant
  error log, sixteen entries in sixteen hours on one installation. On that
  path it is a debug message now. After a full login, where nothing else can
  recover, it stays a warning.

## [1.11.0b2] – 2026-07-28

Cross-checked against a third-party reverse-engineered API reference. Most of
it confirmed what this integration already does; these are the differences
that were worth acting on.

### Added
- **Devices are named by their reported type.** `Device/Read` reports whether
  a device is a heat pump or a combi boiler, which was ignored - every device
  showed the generic "WEM Portal" as its model.

### Fixed
- **Energy statistics pick the newest entry by its date, not by its position
  in the list.** The API happens to return the newest day last, so the code
  took the last element and never looked at the date it carried. That is an
  assumption about ordering rather than a check; a differently sorted
  response would silently yield the wrong day's reading.
- **`DataAccess/Read` now passes the `JobID` returned by `DataAccess/Refresh`.**
  Reading without it works - the server falls back to the most recent job -
  but two overlapping refreshes could then return the other one's values.

### Changed
- The minimum API scan interval is 60 seconds instead of 10, and the options
  form now states the recommended value (>= 180 s). The floor is only a
  guard against a stray tiny value; an existing configuration keeps running
  at whatever it is set to.

## [1.11.0b1] – 2026-07-28

### Added
- **Planned portal maintenance is recognised as such.** During announced
  downtime the portal serves a fully working login form whose backend is
  down, so the login "failed" like a wrong password - and after three cycles
  Home Assistant asked for credentials that were correct all along.

  The maintenance notice is now detected before the credentials are
  submitted, reported as its own error, and kept out of the re-authentication
  counter. The announced window appears in the log. The password is no longer
  sent to a page that cannot process it.

  Detection matches the portal's dedicated notice container, not its wording,
  which is localised and changes per announcement. Based on a single observed
  maintenance page: if a future announcement uses different markup, detection
  simply does not fire and the previous behaviour applies.

## [1.10.2] – 2026-07-19

### Fixed
- **Pressure sensors no longer log a warning on every reading.** 1.10.1 made
  the device-class lookup case-insensitive so that the portal's `BAR` is
  recognised as pressure - but the unit itself still reached the entity as
  `BAR`, and Home Assistant only accepts `bar` for that device class. The
  unit is now normalised along with the lookup.

  Note: the three pressure sensors change their unit from `BAR` to `bar`.
  Home Assistant may raise a repair notice about the changed unit for their
  long-term statistics; the readings themselves are identical.

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
