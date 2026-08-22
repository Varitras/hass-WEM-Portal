# Changelog

All notable changes to this fork are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.12.0b5] – 2026-08-23

### Fixed

- **A malformed answer from the portal no longer reads as "nothing wrong".** A
  field the portal was expected to return as a list, but returned as something
  else, was quietly treated as empty - so a wrongly-typed `Errors` field showed
  as "no errors" and a bad statistics container as legitimately empty. Such an
  answer is now rejected, so the reading is discarded and retried instead of
  published as a false all-clear.
- **A transient failure to load the expert number entities no longer deletes
  them from the registry.** If the entity class could not be built, setup
  reported "no expert slots configured", and the cleanup that removes cleared
  slots then deleted every configured slot's entity - its history and restored
  state with it. A load failure now aborts the setup loudly, before the cleanup
  runs, so nothing is removed.
- **A heating schedule the app cannot render no longer sticks on the
  dashboard.** A week is kept on display as long as the schedule fetch is
  feeding it, but the check for whether it still is only asked whether the day
  carried any switching-time list, not whether that list holds a time the
  sensor can actually render. A week of unrenderable entries was held on the old
  plan forever; it now ages like any other row that shows nothing.
- **Shared services survive a refused unload during another account's unload.**
  Unloading an account is announced before its platforms come down; a second
  account unloading in that window saw the first as gone and released the
  domain-wide expert-write and holiday services. If the first account's unload
  was then refused, it stayed loaded but its services were gone, and every write
  and `set_holiday` call failed until a restart. A refused unload now restores
  the services the account still needs.

## [1.12.0b4] – 2026-08-22

### Fixed

- **In `both` mode, the web scrape no longer goes silently API-only after a
  restart.** The list of devices a cycle may poll was read from the API module
  cache or the live readings, but never from the scraper's own device id, which
  is stored apart from both. After a restart the readings are gone and the
  cache holds only the API device, so the scrape's own device was dropped from
  the filter, the scraper refused it, and the web half stopped running - and
  because it never ran, its id never came back to restore itself. The known
  devices are now the union of all three sources.
- **A holiday date written by hand is no longer undone by a poll running at
  the same time.** A single-date write carries the module's other dates along
  unchanged, read once the write holds the portal lock. They were read from the
  coordinator's published snapshot, which briefly lags the live data after the
  connection is re-established, so a write landing in that window sent a stale
  companion and reset a value another write had just stored. The companions are
  now read from the locked live data.
- **A cancelled expert write can no longer open a second portal session for the
  same account.** The service took the shared per-account lock on the event
  loop and released it if the awaiting call was cancelled - by a reload, an
  unload, or shutting Home Assistant down - while the worker thread was still
  driving the portal, leaving the next operation free to start beside it. The
  lock is now held by the worker for exactly as long as the work runs, the way
  the other expert paths already do.
- **A manual "re-scan parameters" is no longer lost when the entry reloads or
  is removed at the same moment.** The re-scan wrote the module cache to disk
  outside the lock the teardown waits on, so a write in flight could re-create a
  store that had just been deleted, or an older cycle's save could put the old
  timestamps back over the marks. It now goes through the same lock and guard
  the cycle's own saves use.
- **After a rate-limit refusal, statistics are retried up to 45 minutes
  sooner.** A statistics cycle that failed for every device shortens its next
  attempt, but the shortening was skipped when the failure was a refused login
  or a rejected request, leaving the full hour in place - so a 403, which also
  starts a 15-minute cooldown, could keep statistics locked for 45 minutes past
  the cooldown's end.
- **The web-scrape interval holds across daylight-saving changes.** The gate
  measured the time since the last scrape by subtracting two timezone-aware
  timestamps, which Python does as a naive wall-clock difference - so a DST
  change landed in the interval, scraping an hour early in spring and skipping
  an hour of cycles in autumn. It now compares absolute timestamps.
- **A poll cycle that runs out of time no longer starts one more request.** The
  deadline was checked just before a one-second courtesy pause rather than after
  it, so a cycle with under a second of budget left passed the check, spent the
  budget in the pause, and sent the request anyway.

## [1.12.0b3] – 2026-08-19

### Fixed

- **With more than one device, a reading the portal stops sending is aged on
  the right one.** The map that links an API reading to the scraped row showing
  the same value carried no device id, so a second device with a module and
  parameter of the same address looked up the first device's row, found nothing
  of its own, and kept its own dropped reading on display as current.
- **In `both` mode, a value the web scrape delivered this cycle is no longer
  blanked because the API left its counterpart out.** Both sources feed one row
  on their own schedules, so the ageing pass now leaves a row the scrape is
  still delivering to the scrape's own staleness handling instead of clearing a
  reading that arrived seconds ago.
- **A weekly programme that shares its row with the web scrape now shows its
  switching times.** When the value read had merged a programme into a scraped
  row, the schedule fetch still looked under the programme's own key: on a
  3.1.3.0 portal it did not recognise the programme at all, so its schedule was
  never fetched, and elsewhere it wrote the detail onto a second row no entity
  is built from - leaving the visible one on the raw plan.
- **An entity left showing `unknown` after its reading merged into another row
  is removed.** In `both` mode the first API cycle can build an entity under a
  key the web scrape then merges away; nothing took that entity down, so it sat
  on the dashboard as unavailable for the life of the session. It is removed
  once the merge retires its key - and only then, so a reading missing for a
  single cycle keeps its entity.

- **An expert parameter whose entity you disable is dropped from the poll for
  good, along with its failure count and any repair issue it raised.** The
  previous fix only recognised an entity whose initial adding was aborted;
  Home Assistant keeps the reference when you disable one later, so it went
  on being read from the portal every cycle.
- **A scrape that starts working later no longer leaves a second entity for
  the same reading behind.** In `both` mode before the first successful
  scrape, every cycle writes the API reading under its own key - and those
  rows were then accepted as merge targets themselves, because "is this a
  scraped row" was decided from the shape of the name. The reading went to
  both rows, so the same measurement appeared twice. Scraped rows carry no
  module, which is what tells them apart.
- **In `both` mode, a reading merged into a scraped row stops being shown as
  current when the portal leaves it out.** The ageing pass looked for the
  reading under the API parameter's own key - and a merged parameter has no
  row there any more, so it found nothing and moved on while the row that
  does carry the value kept showing the last answer indefinitely.
- **Two date writes at once can no longer undo one another.** A date write
  carries the module's other dates along unchanged, read from the stored
  reading - and that reading was updated only after the api lock had been
  released, so a second write queued behind the first read the value from
  before it and sent it back. Setting holiday begin and end in quick
  succession could leave the begin date as it was, with both calls reporting
  success.
- **A write that arrives right after a connection reset logs in again.** Polls
  restore the session; a write went straight to the portal, so a service call
  or an automation in that window failed until the next poll happened to run.
- **An expert parameter whose entity you disabled is no longer polled.** It
  published nothing but went on costing a login and a form read every cycle,
  and could raise a repair issue about a parameter nobody is looking at.
- **The expert lock is per account rather than per config entry**, so a legacy
  duplicate entry of the same account can no longer drive a second portal
  session in parallel with the first.
- **A store write already in flight is finished before the entry comes down**,
  so it can no longer land after a removal deleted those files or after a
  reload wrote new ones.
- **A weekly programme is only exempt from ageing while something really
  feeds it.** Three paths disagreed about what that means: any non-empty list
  of days counted as a delivered week even though a day without switching
  times renders to nothing; a programme whose value the device-level ageing
  had emptied still counted, although the day names are read out of that
  value; and a module the re-discovery dropped kept its exemption forever,
  because the fetch that would clear it walks the module list. All three now
  ask the same question, and the schedule read refuses an answer that does
  not pass it.
- **A setup that is cancelled is rolled back like one that fails.** Home
  Assistant cancels a setup task on shutdown and when setup takes too long,
  and a cancellation is not an `Exception` - so the rollback was skipped for
  the one ending that leaves the most behind: forwarded platforms, a
  coordinator with its timer armed, and two open HTTP sessions.
- **A poll finishing during an unload no longer writes to storage.** The gate
  asked only whether the store still holds this coordinator, which it does for
  the whole teardown - so a save could still start and land after the stores
  had been deleted or a reload had published new ones.
- **Unloading one of two entries of the same account keeps the shared
  auth-failure streak.** The count belongs to the account, and a legacy
  duplicate entry is still allowed to load; clearing it on unload took it out
  from under the entry that stays, pushing the reauth dialog back out of
  reach.
- **One device can no longer adopt or delete another device's entity during
  the unique_id migration.** The old id formats name neither a device nor a
  parameter - a bare key, a friendly name - so two devices with a parameter of
  the same name propose exactly the same one. Whichever the cycle walked first
  took the other's entity, or removed it. An id more than one reading answers
  to is now left alone, which is the only outcome that loses nothing.
- **A device status answer that carries no status is treated as a failed
  read.** A missing `ConnectionStatus` was read as the "unknown" state, which
  counts as a successful read of a device that is not online: the parameter
  read was skipped for that cycle and the fault sensors were published as "no
  errors" on no evidence at all.
- **No credentials are sent to a login page that has no login form.** A page
  can parse perfectly and still carry none of the ASP.NET fields a login is
  posted with; the password went out anyway and could only be refused, which
  then read as a wrong password. The scraper and the expert client already
  refused to do this.
- **A portal answer that announces itself as XML no longer costs the whole
  scrape.** An unreadable body was already allowed to fall back to a fresh
  login, but only when the parser refused it by its own error type - and the
  one answer that is plainly not the page, a document carrying an encoding
  declaration, is refused as an ordinary `ValueError` instead. That went
  straight past the fallback.
- **A failed API read now counts against the API interval.** In `both` mode
  the mobile API is read on its own interval rather than on every web cycle,
  but a cycle that FAILED left the interval unspent - and the coordinator's
  backoff, which was supposed to pace the retry, needs three failures in a row
  and is cleared by any success in between. A portal answering every other
  cycle with an error therefore put the API half back on the web interval.
- **A parameter that changed platform is no longer logged as missing once per
  cycle.** The entity of the platform it no longer is stays loaded until the
  next reload, and each cycle it warned "Can't find" about a reading that is
  right there. Now said once as a debug line, naming what the parameter has
  become; a reading that really is gone still warns.
- **The unavailable entity left behind by a parameter that changed platform
  is now removed even when it predates the current id format.** A parameter
  reclassified between releases - holiday begin and end went from switches to
  dates - leaves its old registry entry sitting unavailable beside the working
  one. That was already cleaned up, but only for entities registered under the
  current unique_id; one registered by an older release was searched for only
  on the platform the parameter is today, so it was found by neither half and
  stayed for good.
- **A login page served without its form is no longer reported as a wrong
  password on the expert path either.** The password has not been sent at
  that point - the missing fields are what it would be sent with. The web
  scraper already said so; the expert client still called it a credential
  problem.
- **An answer that is not HTML at all no longer costs the whole scrape.**
  The cheap session-reuse attempt is allowed to come back with "this is not
  the expert page" so a fresh login can follow - but a body the parser
  cannot read raised past that, and the fallback never ran.
- **A portal blocking this network during the energy statistics is reported
  as that.** The group loop deliberately let the refusal out so the
  coordinator could act on it; the device loop above caught it again, logged
  it as one device's statistics problem and tried the next device.
- **The expert service writes the parameter id you configured, not the one
  you typed.** Hex ids mean the same parameter in either case, so the check
  against your configured slots ignores case - and then the typed spelling
  was what went to the portal. The configured one came out of discovery and
  the portal has accepted it; that is the one that now travels.
- **A flow-rate sensor gets the icon its device class calls for.** The unit
  lookup matches case-insensitively, which covers "BAR" for "bar" but not
  "m3/h" for "m³/h" - a different character. That left the one unit the
  portal spells its own way without a device class, and the fallback icon
  "mdi:flash" was pinned on it, which overrides whatever the device class
  would have given it.
- **A portal that refuses this network stops the parameter discovery at
  once.** The log promised a budget of three refusals, and the code could
  never spend more than one: the first 403 pauses every request, so the
  second was refused before it was sent and left the counter where it was.
  What it said and what it did now match.
- **A cycle that runs out of time keeps what it discovered.** Discovery is
  stopped where it stands when a cycle exhausts its budget, and what it had
  found by then was only written to disk by a cycle that finished. An
  installation with enough modules to run out of time every cycle therefore
  never saved any of it and started from nothing after each restart -
  spending five seconds and a request per module all over again.
- **Two holiday dates written right after one another no longer undo each
  other.** A date write carries the module's other dates along unchanged,
  and that snapshot was taken before the write queued for the shared
  connection - so while the first write was still running, the second one
  had already read the value it was about to replace, and sent it back.
- **Removing the integration no longer leaves its cache behind after all.**
  A poll runs in a thread that cannot be cancelled, so one still running
  when the entry is removed finishes afterwards - and wrote the module cache
  and device id straight back into storage that had just been cleaned up,
  where they stayed for good.
- **Re-entering your password no longer leaves the failed logins that asked
  for it standing.** The count that escalates to a credentials prompt is
  kept on the account so it survives the reloads a failing setup causes -
  and nothing cleared it when the prompt was answered correctly. The very
  next login page the portal handed out was then the fourth in a row and
  asked for the same password again.
- **A weekly programme nothing is refreshing any more ages out like every
  other reading.** Programmes were exempt from the ageing that follows a
  module going silent, on the grounds that the hourly schedule fetch keeps
  them current. That fetch drops its own detail as soon as a due refresh
  fails - so where both had stopped, the plan from before the outage stood
  as the current one, without limit.
- **A module with nothing to report no longer costs the whole device its
  readings.** Such a module comes back as `"Values": null`, and a default
  for a missing key does not cover a key that is present and null - so the
  read raised, every cycle, for as long as the portal answered that way.
  Every list the portal sends is now read through one place that knows the
  difference.
- **An expert parameter with fractional steps takes the values it offers
  again.** The step is measured as the distance between two options, and a
  subtraction of two decimals carries their float error: a heating curve's
  0.05 was published as 0.04999999999999982. The write path then matched the
  value against the option list exactly, so most of what the entity could be
  set to came back as "not allowed" - naming a range that contains it.
- **An expert write interrupted by a reload no longer denies what it
  already did.** The abort gate is asked before every request, the read that
  confirms the write included, and said "stopped before the write reached
  the portal" in every case - including after the heating system had taken
  the value. It now says what happened at that point.
- **A flow-rate reading written with a decimal comma no longer breaks the
  update.** "m3/h" is the one unit read out of the value rather than the unit
  field, and it was the only one parsed with a bare conversion instead of the
  shared parser every other number goes through - so a scraped "0,55m3/h"
  raised, out of a platform mid-update, costing more than the one reading.
- **A lasting outage no longer leaves the last readings standing as
  current.** One failed cycle is tolerated on purpose - the portal answers
  one with "Unbekannter Fehler" now and then and the next one succeeds. But
  Home Assistant notifies entities on the refresh that fails first and on
  none after it, so the moment that tolerance ran out was never published:
  every entity of the account kept showing its pre-outage value, marked
  available, for as long as the outage lasted.
- **`both` mode no longer reads the mobile API at the web interval.** With
  the two intervals set apart - a five-minute scrape next to a half-hourly
  API read, say - the API was read on every scrape cycle rather than on its
  own: six times the requests that setting asks for, against a portal that
  counts 10,000 per 12 hours and IP. The scrape half was gated for exactly
  this; the API half was not.
- **A device you switched off is no longer asked for its parameter
  definitions.** The filter reached the readings and stopped there: the
  discovery in between was called without it. That discovery is the most
  expensive thing this integration does - five seconds of waiting and at
  least one request per module - and a disabled device paid all of it, on
  every install whose cache is incomplete and once a day after that. The
  portal counts requests per IP.
- **A parameter the portal names nothing for no longer costs the whole
  device its readings.** Where the portal sends no bounds, the plausible
  range is guessed from the parameter's name - and that name is optional.
  With none, the guess raised and took every reading of that device down
  with it for the cycle. Found by type-checking the module that builds the
  readings, which is now part of what CI checks.
- **Removing a duplicate entry of an account no longer wipes the other
  one's memory.** Old installations may still carry two entries of the same
  account, and both share what that account remembers - the rate-limit
  backoff, the authentication-failure count, the warnings meant to appear
  once. Removing either of them dropped all of it, so the entry that stayed
  went back to polling as though the portal had never refused anything.

## [1.12.0b2] – 2026-08-15

Eight repairs found by auditing 1.12.0b1 and then auditing the repairs. Three
of them are faults this pre-release introduced rather than inherited, and one
of those could take a control away for good: the platform of a parameter is
read from the value of each cycle, so a single odd answer moved a row to a
plain sensor - and the entity of the platform it had been was deleted with
nothing left to build it again. If you are running 1.12.0b1, this is the
reason to update.

### Fixed

- **Two accounts unloading at the same time no longer leave a service
  behind.** Each asked whether any other entry was still loaded, and Home
  Assistant only drops that mark once an unload has finished - so each saw
  the other as running and neither released the shared expert and holiday
  services. They stayed registered with nothing able to answer them.
- **A module the portal stops listing has its readings aged out too.** The
  pass that does the ageing walked the current module list and read a stamp
  kept inside each module's entry - so a module that dropped out of that list
  took its own stamp with it and was never visited again. Its readings were
  then the only ones nothing could touch: not refreshed, because a module
  without a description is skipped, and not aged, because the pass could not
  see them. They stood on the dashboard as current indefinitely. Readings the
  web scrape still feeds are unaffected, as before.
- **Switching the scraper's device off clears its repair issue.** The poll
  already skips a disabled device, so nothing was being attempted - but the
  report asked only whether the last attempts had failed, and only a scrape
  that works resets that count. The issue therefore stood for as long as the
  device stayed off, with nothing the user could do about it.
- **An expert parameter that keeps failing to read stops showing its last
  number.** Only a run of cycles in which *nothing at all* answered emptied
  those values, which left out the two cases where it matters most: a single
  configured parameter, where "all of them failed" is true every time one
  does and the rule therefore does not apply, and one broken id beside a
  working one, where the sibling's answer reset the count every cycle. Both
  raised a repair issue and went on displaying a restored number behind it.
  The value now goes when its own id has missed three reads in a row, once
  per run rather than every hour. A write the portal read back ends that run
  the same way a successful poll does - it is the stronger answer of the two,
  and until now the repair issue stayed up for a parameter that had just
  demonstrably worked.
- **A password changed while Home Assistant runs is noticed a cycle sooner,
  and costs far fewer refused logins.** A session that expires mid-cycle is
  renewed from inside whatever request noticed, so a rejected login surfaces
  in the middle of a partial read - where a handler whose job is to keep the
  poll going caught it. Two things followed: the re-authentication dialog
  stayed a cycle further away, because the count it needs is reset by any
  cycle that ends another way; and since the session flag is only checked
  once per cycle, every request after that one went out on the dead session
  and spent another refused login finding out. On an installation with
  several devices that is a login attempt per device and path, against a
  portal that counts requests per IP. The read-back after a write is the one
  place that still takes a refused login as an answer rather than an error:
  the write went through, and reporting it as failed would invite a retry
  while leaving the unconfirmed value on display as verified.
- **A reclassified parameter is no longer shown by the entity it left
  behind.** The daily re-discovery can decide that a parameter the portal
  used to describe as a date is a switch, and both entities are loaded until
  the next reload. Only the write path asked whether the row still belonged
  to the entity reading it - each of the five display paths reached into the
  coordinator's data directly, so the entity that was left over published the
  new platform's value as its own type: a holiday epoch as a switch that is
  on, a 0/1 as a date in 1970. The holiday service resolved its rows the same
  way and could send an epoch to a parameter that is no longer a date.
- **A reading that arrives after setup keeps its recorded history.** The
  migration from the old unique_id formats ran once, during setup - and the
  readings it has to reach are precisely the ones that are not there yet: a
  device unreachable at that moment, the parameter re-discovery that waits
  for the second cycle on purpose, the hourly statistics that appear minutes
  later. The half that builds the entities was given a listener for those,
  the half that migrates them was not, so a reading that showed up late got a
  brand new entity - with none of the history its old one carries, and
  nothing about the result looking wrong. Reclassifying a parameter while
  Home Assistant runs now also takes down the entity of the platform it no
  longer is, and builds the one it has become - both ways round, which
  matters because the platform is read from the value of that cycle: a
  holiday date answered once without a number is a plain sensor for one
  cycle and a date again on the next.

## [1.12.0b1] – 2026-08-12

The theme is identity and freshness: which circuit, which module and which
spelling a reading belongs to, and how long a value that stopped arriving may
still be presented as current. Beside it, three failures that were visible
only in the log - a rate-limit block, a web half that stopped delivering, an
expert parameter that will not read - are repair issues now, in your language.
Underneath, the integration was taken apart and put back together: transport,
statistics and the data model are their own modules, with structural guards
and a mutation run that keeps them that way. None of that is visible from the
outside, which is why this is a pre-release.

### Added
- **A rate-limit block now shows up in Repairs, in your language.** A 403
  cooldown pauses all polling for a long stretch - the one state a user
  notices and could previously explain only from the log. It is a repair
  issue while the block holds and clears itself with the next successful
  update.
- **A diagnostics download, written to be shareable.** Home Assistant's
  three-dot menu on the integration now offers a diagnostics report:
  coordinator health, readings and module counts. Credentials, configured
  expert ids and the scraped session are redacted by key; device ids are
  replaced by positional aliases (`device_1`) - they are dictionary keys,
  which redaction cannot reach. Attach it to bug reports instead of
  hand-picking log lines.

### Changed
- **The expert auto-poll reports a persistently unreadable parameter in
  Repairs, not as a notification.** Same three-strike rule, same two
  wordings (configured id vs. portal refusal) - but translatable, collected
  where Home Assistant gathers actionable problems, and taken back down
  automatically once the parameter reads again.
- **After a restart an expert parameter restores its value, never its range.**
  A stored range is a copy of a reading that no longer exists, and Home
  Assistant checks the published range before this integration is asked - so
  a stale restored range could block exactly the write that would have
  fetched the current one. Bounds and step now stay permissive until the
  portal has answered once. The price: after a restart the parameter is a
  typing box, not a slider, until the first read or write.

### Fixed
- **A device whose name contains "HasErrors" no longer turns all its sensors
  into diagnostics.** The diagnostic category was decided by searching the
  whole unique_id - which also carries the entry id and the device id - for
  one of the three status words. It is decided on the parameter id now, the
  same way the availability rule beside it already was.
- **Removing the integration removes its traces.** The module cache and the
  scraper device id stayed in `.storage` forever, the account's remembered
  state outlived the account, and a repair issue could outlive the entry
  that raised it. Removal now deletes both stores, the entry's issues and
  the account memory.
- **A cleared expert slot no longer leaves a dead number entity behind.**
  The registry entry of a slot that is no longer configured (or of every
  slot, once expert write is off) was never offered again and sat
  permanently unavailable. It is removed on the next reload; because the
  unique_id is stable, re-configuring the slot re-creates the entity under
  its old entity_id, so recorded history survives.
- **New bounds and options reach entities that already exist.** Rediscovery
  replaces the parameter descriptions once a day and every cycle delivers
  fresh metadata - but Number published its construction-time range forever
  (a value the device newly accepts was refused by Home Assistant before
  this integration was asked), and Select resolved against its
  construction-time options (a device already on a newly added option read
  as unknown). Both now take metadata from every coordinator update, before
  the value.
- **A module the portal stops answering for ages out - its siblings stay.**
  Freshness was tracked per device, so as long as module A kept answering,
  the readings of a module B missing from every answer were presented as
  current indefinitely - the only symptom was a number that never changed.
  Each module now carries its own freshness; after the same tolerance - half
  an hour, or two API intervals where those are longer - the silent module's
  readings go unknown, with one warning naming the module, while everything
  that answers is untouched.
- **A weekly programme whose refresh keeps failing shows the current raw
  plan, not last week's detail.** The schedule sensor prefers the fetched
  detail (`CircuitTimesDay`) over the raw value, and a failed refresh kept
  that stale detail on display over a newer plan the ordinary read had long
  delivered. A failed attempt of a due refresh now drops the stale detail;
  the existing raw-plan fallback takes over until a refresh succeeds again.
- **An answer outside the portal's own contract no longer detonates mid-code.**
  Valid JSON is not the same as the expected shape: `{"Parameters": null}`
  aborted the rest of a device's discovery with a TypeError and left the
  module with no retry timestamp; a device list without its `Devices` array
  surfaced as "unexpected error"; a value read answered with `null` failed
  with `'NoneType' object has no attribute 'get'` as its reason. Each answer
  form is now shape-checked where it arrives: the unreadable module is booked
  like a refusal, the device list raises a classified portal-side error (one
  malformed device row is skipped and logged, the rest of the account
  survives), and the null read fails with a reason a person can act on.
- **A German decimal reaches the write as the number it means.** The
  portal's own dialog accepts `1,5`; the `set_expert_parameter` action
  refused the same spelling, and an API string value like `21,5` stayed
  text where a scraped cell already read 21.5. Every portal number now goes
  through one shared parser, in both spellings.
- **The service dialog explains word values.** Home Assistant renders the
  translations, not `services.yaml` - so the hint that "Aus" works lived
  only where nobody saw it. Both translations now say it.
- **Two heating circuits no longer share one reading.** A parameter id
  identifies a parameter within its module, and two circuits are two
  modules of one type with one parameter catalogue - so the same id appears
  twice on a device. It was used bare, so the second circuit wrote its
  value into the first circuit's sensor and got no entity of its own. One
  circuit was publishing the other's temperature, the other was missing,
  and nothing said so.
- **The second circuit's weekly programme is read at all.** The hourly
  refresh was throttled per device and parameter id, without the module -
  so whichever circuit was fetched first blocked the other one, on that
  cycle and on every cycle after it.
- **A weekly programme keeps the week the portal reported.** The programme
  is fetched once an hour, the values every few minutes, and the value read
  rebuilt the row without the schedule - so the readable week survived
  roughly one cycle in twelve and the sensor fell back to the raw JSON in
  between.
- **A parameter the portal stops offering stops being shown as current.**
  Its last value used to stand unchanged for the rest of the session, with
  nothing in the log. It is now dropped when the portal's own parameter
  list no longer contains it, and the entity says so instead.
- **"Re-scan parameters" survives saving the settings form.** The request
  was kept in memory only, and saving the form reloads the entry - which
  rebuilt that memory from disk. The most natural next click undid it, as
  did any restart before the next update.
- **A rate-limit block is reported for as long as it holds.** The repair
  issue is now driven by the block itself rather than by whichever error
  happened to surface, so it appears while polling is paused and clears
  when it resumes.
- **An expert parameter id is one id however it is spelled.** The same
  hexadecimal id in upper and lower case counted as two - two slots, two
  entities, and a write that did not reach the configured one.
- **One unreadable row costs one row.** A module or parameter id the portal
  sent in a shape that cannot be used aborted the rest of that device;
  every remaining reading of the device was lost with it. And a device list
  in which no row at all can be read is now reported as the portal-side
  error it is, instead of being adopted as an empty account.
- **A reading that arrives later still gets its entity.** Entities were
  decided once, during setup. A device that was unreachable at that moment,
  a parameter found by the daily re-discovery, or statistics whose first
  attempt failed produced values that no entity ever showed - until the
  entry was reloaded by hand. They now appear on their own.
- **A dropdown writes the value that belongs to the option you picked.**
  The value was chosen by the position of the chosen name in a second list.
  Where the portal offers the same display name twice, picking one wrote
  the other one's value into the heating system while the entity showed
  what was clicked. Unresolvable cases now refuse the write and say why.
- **A control whose parameter is gone refuses to write.** The entity
  outlives the reading it was built from and keeps the address it was given
  at the time, so a click could still send a write for a parameter the
  portal no longer answers for.
- **A disabled expert entity no longer errors on every poll.** An entity
  switched off in the entity registry is still built and handed to the
  auto-poll, which then tried to publish state for something Home Assistant
  had never added.
- **A web half that stopped working says so.** In `both` mode a failing
  scrape is deliberately swallowed, so it cannot cost the readings the API
  half delivers - and with it went every trace that half the integration had
  stopped. Nothing reached the coordinator, each successful API cycle reset
  its counters, and on a fresh setup there were no scraped entities whose
  absence could be noticed. The log line asked the user to check the
  credentials, which the options form does not even contain. There is now a
  repair issue at the same threshold that stops presenting scraped values as
  current, naming the two things that actually help: check whether the
  portal's web page opens in a browser, or switch to API-only mode.
- **An expert parameter stops showing a value the poll can no longer
  confirm.** These entities restore their last value after a restart and are
  not coordinator readings, so none of the freshness rules elsewhere reached
  them. When the auto-poll produced nothing at all - a web login that
  failed, a session that broke - the per-parameter tally was deliberately
  left alone, because one outage says nothing about any single parameter.
  The result was that nothing happened at all: the dashboard kept a
  plausible number with nothing behind it, and the next attempt was an hour
  away. After two such cycles the values go to unknown; name and range stay.
- **The holiday service shows what the portal kept, not what it was asked
  for.** A write that returns without an error was accepted, which is not
  the same as stored: on this endpoint a range ending before it starts comes
  back as success and is discarded. That one pair is refused before it is
  sent, but the single date entity has been reading its value back since it
  turned out that check cannot cover everything. The service now does the
  same - one read for the whole device, at something used a few times a
  year. If the read-back fails, both dates go to unknown rather than
  claiming a holiday nobody confirmed.
- **A debug log no longer carries the whole installation.** Seven log calls
  handed over an entire data structure - the account's readings keyed by
  device id, or a device's module list - as a bare argument. Each was added
  while debugging something and then stayed, and a debug log is what people
  paste into an issue. The lines that name what is happening remain; for the
  data itself there is the diagnostics download, which is anonymised.
- **A scrape that arrives later still takes over its reading.** In `both`
  mode the first cycle often has no scrape yet - it is not due, or it
  failed. The merge then finds no scraped row for an API reading and points
  it at itself, correctly for that moment, but never looked again. A scrape
  arriving on a later cycle therefore produced a second entity for the same
  measurement, refreshing on a different schedule. The mapping is now
  rebuilt whenever the set of scraped rows changes, which costs no requests.
- **One unusable module id no longer costs the whole device its reading.**
  A `ModuleIndex` the portal sends as a list is dropped where the readings
  are built, but the freshness bookkeeping right after it used the same
  answer again without that guard. The failure landed inside the device's
  read, so values that had just been mapped correctly were reported as a
  failed read - the device counted as failed for that cycle and its
  readings began ageing towards unknown.
- **A control stops writing once its parameter belongs to another platform.**
  The daily re-discovery re-reads what the portal says a parameter is, and
  that decides whether it becomes a switch, a number or a date. When the
  answer changes, the entity for the new platform appears - and the old one
  stayed loaded and writeable until the next reload, still holding the
  address it was built with. The write gate asked whether the reading was
  still there, which it was; it now also asks whether it is still this
  entity's.
- **An expert parameter id is one parameter, however it is spelled.** Hex
  ids carry no meaning in their case, and the integration already knew that
  in two places - the duplicate check built its set that way, and the
  service checks its argument that way. Both then compared the raw spelling
  against it. Two slots differing only in case could pass the save as
  unrelated parameters, and a service call in another case wrote the value,
  got it confirmed, and left the entity showing the old one.
- **A password changed while Home Assistant runs is noticed.** The old
  session keeps working until it expires; the re-login that follows is then
  turned down, and that rejection left the integration still believing it
  was signed in. Every cycle after it skipped signing in and spent itself on
  refused requests, which the per-device handlers swallow - so nothing ever
  counted as an authentication failure and the re-authentication dialog
  never appeared. It stayed quietly dead until someone reloaded it by hand.
- **The two hourly portal limits no longer follow the wall clock.**
  Statistics and heating schedules are each asked for at most once an hour,
  and both measured that hour on a clock that can be corrected - NTP right
  after a boot being the reliable case. A correction forward made every
  stamp look old enough to fetch again. Weishaupt counts requests per IP,
  so a limit that drops open is exactly the traffic it exists to prevent.
  Both now read a clock that cannot jump.

## [1.11.0] – 2026-08-09

Same code as prerelease 1.11.0b4; only the version number changed.

### Security
- **A device id no longer travels in the message that asks you to report it.**
  When every device's parameter fetch fails, the reason Home Assistant shows
  ends with "open an issue at <tracker>" - so the whole string is written to be
  pasted somewhere public, and it carried the installation's device id
  verbatim. Only its last two digits remain, which is what tells two devices of
  one account apart and all the message needs it for.

### Added
- **The `set_expert_parameter` action now takes the word too, so an "off"
  position can finally be set.** A heating curve can be switched to *Aus*, a
  frost protection likewise — but that is not a point on the scale (the portal
  encodes it as `0` on one parameter and `-32768` on the next), so the number
  entity cannot offer it and Home Assistant's range check refuses it before
  this integration is asked. Passing the word the dialog shows goes around
  both; case does not matter. The value field is a text field now, which
  accepts numbers exactly as before.

### Fixed
- **A poll that runs out of time says so from every request, not just one.**
  A request is capped at what is left of the cycle, so timing out on that cap
  is the cycle stopping itself — which keeps the warm session — while an
  ordinary failure discards it. Only the login request told the two apart. In
  the session-reuse path an unclassified timeout was worse than untidy: that
  path retries with a full login, so a portal that had just failed to answer
  one request was sent two more.
- **A slot the portal has not been asked about yet accepts fine values.** The
  placeholder step was half a unit, and the heating curve is offered in
  hundredths — so four values in five could not be typed into a slot before
  its first successful read, and the write that would have fetched the real
  step was among them. The placeholder is now finer than any parameter needs,
  which costs nothing: until the real range arrives the entity is a box, where
  the value is typed rather than stepped.
- **A parameter set through the service shows its new value right away.** Both
  ways of writing an expert parameter end in a portal read-back, and the number
  entity applies it - but the `set_expert_parameter` action dropped the answer,
  so the entity for the very parameter that had just been set kept showing the
  old value until the next automatic read, which is off by default, or a
  restart.
- **A teardown during discovery ends the flow, for real this time.** The abort
  was translated into Home Assistant's own flow abort, but both callers wrap
  the discovery call in `except Exception` - and that abort reaches `Exception`
  through two base classes. So it was caught one frame above where it was
  raised and shown as "the search failed", the exact wording the translation
  exists to avoid. The previous test called the translating helper directly,
  which is precisely where the problem was not.
- **The single-failed-cycle tolerance reaches the sensors too.** They are most
  of the entities on an installation, and they were the ones it did not reach:
  the sensor platform overrides availability for its three diagnostic entities
  and spelled the cycle rule out again alongside. The rule now lives in one
  place that the override asks, and the test covers every platform rather than
  one.
- **Discovery offers the parameters that stand alone in their section.** The
  edit link carries a `readdata` flag, and it was read as "False means an
  aggregate entry with no value dialog" - so every parameter the portal shows
  alone under its own heading was skipped, among them *Betriebsart*,
  *Heizkennlinie*, *So/Wi Umschaltung* and *Reset*. Measured at the portal,
  the flag says something else: False where a parameter is alone in its
  section, True where several share one. It describes how the portal opens
  the dialog, not whether there is one - those parameters open the same dialog
  as any other and read and write like any other. They had to be typed in by
  hand until now.
- **One failed cycle no longer takes every entity of the account with it.** The
  portal answers a poll with "Unbekannter Fehler" now and then and the next one
  succeeds. Every entity went unavailable for that single cycle - half an hour
  of every graph at the default interval, plus a state change out and back for
  anything automating on it. The web scrape already tolerated failures before
  ageing its values; the API side tolerated none. One cycle is tolerated now,
  the second still reports the outage, and a device that stops answering has
  its readings aged out exactly as before.
- **A parameter the portal scales is read the way the portal shows it.** The
  edit form carries each allowed value twice: as the label it displays and as
  the string it wants posted back. For some parameters those differ - 1.5 is
  offered as `15` - and only the string was ever read. A parameter whose form
  says 1.0 to 30.0 in halves was therefore published as 10 to 300 in fives,
  and a value copied from the portal into Home Assistant was written to the
  heating system ten times too small. The label now decides what the parameter
  is; the string is still what goes back to the form, exactly as the form
  offered it. Two parts of this integration already read that label - the
  parameter list behind discovery and the scraped sensors - so they and the
  entity disagreed about the same parameter.

  Parameters whose two columns agree are unaffected, and a dropdown labelled
  with words rather than numbers still reads its values: for an enum the
  string is the only number there is.

  A special value among the numbers no longer drags the range off the scale
  either. The portal offers "Aus" as `0` on the heating curve but as `-32768`
  on the frost protection, so it cannot be placed on the scale by dividing -
  and reading the whole parameter by its value attributes instead published
  `-32768` as the minimum of a range that runs -20.0 to 17.5. Such a value now
  sits beside the scale rather than on it: the range is the numbers, and where
  the portal has the special value selected the state reads unknown, with the
  portal's own wording in `portal_value` to say which one.
- **Every expert parameter carries what the portal states beside it.** Two new
  attributes, both read from the dialog the integration already fetches, so
  they cost no request: `portal_value` is the wording shown for the current
  selection, and `factory_default` is the value the parameter left the factory
  with. A number entity cannot be coloured when it differs from its default -
  Home Assistant has no such option - but a dashboard card can compare against
  the attribute and do it. Both are text, because the same dialog reads "0.75"
  on one parameter and "Aus" or "Mittel" on the next.
- **A refused write corrects the range that refused it.** A slot stored before
  the fix above holds the scaled range, so the value the portal would accept
  is outside what the entity offers - and Home Assistant checks the published
  range before this integration is asked, which leaves the entity refusing the
  only values that would work. Nothing on its own path could break that
  circle: the hourly read is off by default, and a write was the one thing
  left. The refusal carries the form it was refused against now, so the first
  attempt after an upgrade puts the entity right even though it fails.
- **An expert parameter is a slider again once its range is known.** Until the
  portal has stated a range the entity publishes a placeholder spanning
  200000, and a slider over that is useless - so it was set to a typed box.
  The box then stayed after the real range had arrived, and its spinner arrows
  are worse than the slider they replaced: each click is a write of its own
  that waits on the portal, where a slider sends one value when the drag ends.
  The mode follows the bounds now instead of being fixed on the class.

## [1.11.0b3] – 2026-08-08

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
- **An expert slot's name can be cleared again, not only replaced.** Emptying
  the field and saving brought the old name straight back: an empty optional
  field is left out of the form data, and the save merges what was submitted
  over what was stored. The slot IDs were already written back explicitly for
  exactly this reason - the names now are too, and they are stored stripped.
- **A device that stops answering stops showing its last readings.** With
  more than one device, a cycle in which one answers and another does not is
  reported as successful - correctly, because failing it would take every
  other device's entities down too. The silent device kept publishing
  whatever it last returned, though, with nothing to say so: the rule that
  blanks a value the portal left out needs a reply to work from, and there is
  no reply here at all. A device that has not answered for half an hour now
  has its readings shown as unknown instead. Units, names and icons stay, so
  the entities keep their identity, and a single missed read changes nothing -
  the portal refuses requests routinely. Single-device installations are
  unaffected: there, a failed device already fails the cycle.
- **The expert auto-poll stops when an unload begins, not when it ends.** The
  teardown raises its flag before the platforms come down, because unloading
  them is the slow part - but the auto-poll only watched the second flag, set
  right at the end. A read that started just before a reload therefore kept
  navigating the portal through the whole gap between them. Every other write
  path already respected the earlier flag.
- **A rate-limit refusal during the statistics fetch is reported once, not
  once per group.** A 403 says something about the connection, not about the
  statistics group being read, but it was handled like any other group error -
  so the remaining groups were each asked, each refused by the cooldown check,
  and each logged. The cycle then closed by blaming "every group" for what was
  a single block. It now stops at the first refusal and reports it as one.
- **A device whose modules the portal refuses to describe recovers in an
  hour, not a day - and says so.** When the portal rejects a module's
  description, that module is kept rather than dropped, with an empty
  parameter list, and asked again later. How much later was the wrong way
  round: a module that already had parameters retried after an hour, while
  one that had none yet waited a full day. The second is the more urgent of
  the two - it has nothing to show at all - so a device whose every module
  was rejected stayed empty for a day over one refused request.

  Both cases retry within the hour now. A module that genuinely answers "I
  have no parameters" is unchanged and keeps the daily round: that is a real
  answer, and asking it hourly would spend twenty-four times the requests on
  modules that replied correctly the first time.

  The refusal is also visible now. A device left with no readable parameters
  because its descriptions were refused says exactly that, instead of the
  debug line it used to leave behind - so "no entities appeared for this
  device" has a stated cause rather than being something to guess at. The
  cycle still does not fail for it: the device may genuinely have nothing,
  and failing would put every other device into a backoff.
- **The web scrape keeps its schedule across a daylight-saving change.** The
  interval between scrapes was measured as the difference between two naive
  local timestamps, which are subtracted as if the clock never moved. So the
  hour the clock jumps landed straight in that difference: in spring it read
  an hour too long and the next scrape fired immediately, in autumn an hour
  too short and scraping paused for up to an hour beyond the configured
  interval. Both timestamps are timezone-aware now, so the arithmetic goes
  through UTC and the interval is the interval. Twice a year, and only the
  web scrape - the API poll was never affected.
- **A poll cycle that runs out of time now stops instead of running on
  unwatched.** Home Assistant abandons a cycle after 360 seconds, but that
  timeout cancels the *await* - it cannot cancel the worker thread behind it.
  The overrunning cycle therefore ran to completion: still holding the shared
  connection lock, still spending requests at a portal that counts them per
  IP, long after the result had been recorded as a failure and discarded. The
  next cycle then queued behind work nobody was waiting for any more.

  The worker now carries its own deadline, set when the cycle starts and
  checked before each request and before the scrape begins. It stops shortly
  before Home Assistant would give up, so the connection is free when the next
  cycle arrives. The cycle is reported as failed - a partial read is not
  booked as a success - and the readings gathered so far are kept for the next
  one to build on. Parameter discovery in particular resumes where it left
  off rather than restarting.

  Only automatic polling is affected. An on-demand write or service call has
  someone waiting on it and no timeout behind it, so neither gets a deadline.
- **An unreadable login page no longer costs the password.** If the portal
  answers the login page with an empty or unparseable body, the credentials
  are not sent. Previously the form came back empty, the login posted anyway -
  without the ASP.NET state the portal demands echoed back - and the request
  could only be refused. Same rule as the maintenance check: nothing is handed
  to a page that cannot process it.

  It is no longer reported as a credential problem either. Not sending the
  password was only half of it: the failure was still raised as an
  authentication error, so it counted towards re-authentication and three
  such portal hiccups in a row could ask for a password that was correct all
  along. A login page without its form fields is a portal-side problem and
  now says so.
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
- **An expert parameter no longer claims to be a percentage.** Every slot was
  published with the unit `%`, a range of 0 to 100 and a step of 1 - none of
  which the portal states. Its edit form carries a list of the values it
  accepts and no unit at all, so a flow temperature, a heating-curve slope
  and a delay were all shown, and recorded, as percentages, and a parameter
  the portal offers in halves could only be set to half of its values.

  The unit is gone, and the step now comes from the spacing of the values the
  portal offers. So do the minimum and maximum, which were read already - but
  only until a restart: they were not restored with the value, so a parameter
  whose real range is 200 to 800 came back sitting inside the assumed 0 to
  100, where it could not be set at all until the next successful read. With
  the hourly auto-poll switched off, that was never.

  A slot that has never been read publishes no opinion at all now. Home
  Assistant checks a call against the published range before this integration
  is asked, so the assumed 0 to 100 did not merely mislabel a parameter - it
  locked it, and the write that would have fetched the real range was exactly
  what it refused. Until the portal has said what it accepts, the range is
  deliberately far wider than any parameter and the step far finer, so
  nothing is excluded before it is known; the value is entered rather than
  dragged for the same reason. What the portal will not take is still caught,
  where it is actually known: the write checks against the form's own list of
  allowed values and names it in the error.
- **The same rule decides every login answer now, not only the one it was
  written for.** Staying on the login URL was the whole test on the web scrape
  and in the expert client, and a portal error page or an interstitial does
  that too while answering 200 - so anything of that kind counted as refused
  credentials, and three in a row reached the re-authentication prompt. Only
  the portal rendering its login form is evidence that credentials were seen
  and refused; anything else is a portal problem and is reported as one. Both
  paths carried their own copy of the check, and both now go by the markers
  the API login has used all along.
- **A request cut short by the poll deadline is reported as the deadline, not
  as a portal that stopped answering.** Each scrape request is capped at what
  is left of the cycle's budget, so the last one before the deadline times out
  ON that cap - and an ordinary timeout is the coordinator's second failure,
  which discards the warm session and forces a cold login. Which is what the
  deadline's own branch exists to avoid.
- **A web scrape that fails three times before it has ever succeeded says
  why.** Ageing the rows of a scrape that has given up iterates the keys of
  the last successful one, and before the first there are none - so the third
  failure raised a TypeError that replaced the real reason on its way out,
  whether that was maintenance, credentials or the network. Reachable in
  `both` mode, where the API half has already filled the device.
- **An expert slot restored from before the range fix no longer locks
  itself.** Home Assistant persists a number entity's minimum, maximum and
  step with or without a value, so a slot stored by an earlier version comes
  back carrying the assumed 0 to 100 although it was never read - and taking
  that back on the first start after the upgrade would reinstate exactly the
  lock the fix removes. Such a range is recognised by its missing value: a
  real one can only have been learnt by reading or writing, and either would
  have stored a value with it.
- **A teardown during parameter discovery ends the options flow with a
  sentence instead of a traceback.** The abort signal has to cross the broad
  handlers between the portal work and the flow, which is why it is not an
  ordinary exception - but that also carried it past the handler the options
  flow relies on, and Home Assistant's flow manager translates only its own
  abort. Discovery interrupted by a reload or an unload now closes the flow
  with a message in both catalogues. There is nothing to fall back to: the
  configuration being edited is going away.
- **The expert login stops at an unload too.** The two steps that follow the
  credentials - establishing the session context and the security-code
  dialog - opened with unguarded requests, so an unload during the credential
  exchange was answered with one more authenticated request at a portal that
  counts them. Every other step of that path already checked.

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
- **An expert number entity now waits for the portal and fails if the write
  did.** It used to start the write in the background and return at once, so
  every caller was told the write had succeeded whatever happened - an
  automation could carry on as if the heating had been set, with the actual
  outcome going only to the log and a notification. The domain service was
  changed to work this way in 1.10.0; the entity was not, so the two
  disagreed about the same write.

  Setting one now takes a few seconds, until the portal has confirmed the new
  value. Only the caller waits: polling, the other entities and the rest of
  Home Assistant are unaffected, and a second expert operation is still
  refused outright rather than queued. Failure notifications are gone - a
  failure is raised instead. The optional notification on SUCCESS is
  unchanged.

  That includes a write stopped because the integration was reloaded or
  unloaded underneath it. Nothing reaches the portal in that case, and the
  call now says so rather than returning as though the setting had been
  made - whoever asked is still waiting on the answer.
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
- **Both services state that they require an administrator.** The holiday
  service has been admin-only since it was written and said so nowhere; the
  expert service said it in the README but not in the description Home
  Assistant shows next to the action.

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
