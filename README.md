[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories)
[![buy me a coffee](https://img.shields.io/badge/If%20you%20like%20it-Buy%20me%20a%20coffee-yellow.svg?style=for-the-badge)](https://www.buymeacoffee.com/erikkastelec)
[![License](https://img.shields.io/github/license/Varitras/hass-WEM-Portal?style=for-the-badge)](LICENSE)

# hass-WEM-Portal

Brings a Weishaupt heating system into [Home Assistant](https://home-assistant.io/)
through the WEM Portal — readings, settings you can change, fault reporting and
energy statistics.

There is no local API. Everything goes through Weishaupt's cloud, using the two
interfaces the portal offers itself: the mobile app's API and the web frontend's
expert view.

This is a fork of
[erikkastelec/hass-WEM-Portal](https://github.com/erikkastelec/hass-WEM-Portal).

> **Disclaimer:** This is a personal hobby/test project, developed for my own
> setup and shared as-is. It is not an official integration, comes with no
> warranty, and no support or maintenance is guaranteed. Use at your own risk
> — especially the expert write feature, which changes settings on your real
> heating system. Always verify changes in the WEM Portal itself.
>
> Developed with AI assistance (Claude), with all testing, decisions, and
> validation done by me against my own installation.

---

## Contents

- [What you get](#what-you-get)
- [Installation](#installation)
- [Setup](#setup)
- [Choosing a mode](#choosing-a-mode)
- [Polling and the portal's rate limit](#polling-and-the-portals-rate-limit)
- [Options reference](#options-reference)
- [Services](#services)
- [Expert write access (web)](#expert-write-access-web)
- [Troubleshooting](#troubleshooting)

---

## What you get

Entities are created from whatever your installation actually reports, so the
exact list depends on your heat pump, its modules and the mode you choose.

| Entity type | What it is |
|---|---|
| **Sensor** | Every reading the portal offers: temperatures, pressures, speeds, operating hours, states. |
| **Number** | A setpoint you can change — room setpoint, hot-water temperature, and anything else the portal marks as writable with a numeric range. |
| **Select** | A parameter with a fixed list of choices, e.g. the operating mode or a hot-water push duration. |
| **Switch** | A genuine on/off parameter. |
| **Date** | Holiday begin and end. See [`set_holiday`](#wemportalset_holiday) — the portal only stores the pair, so writing one date alone does not work. |

### Diagnostic entities

Three per device, and they stay available even when everything else goes away —
they are what explains why:

- **Connection Status** — what the portal says about reaching the device.
- **Has Errors** — `Yes` / `No`. When a status read fails, this reports
  *unknown* rather than `No`: "no fault" because nothing is known is the one
  direction a fault sensor must never fail in.
- **Error Messages** — every active fault. The full list is in the `Errors`
  attribute; the state itself is capped by Home Assistant at 255 characters and
  says how many it had to drop.

### Weekly programmes

Heating and hot-water programmes become a sensor whose state is the readable
week, collapsing days that are alike:

```
MO 00:00-06:00 Komfort, 06:00-10:10 Normal, 10:10-13:30 Absenk,
13:30-24:00 Komfort; DI-SO 00:00-24:00 Komfort
```

The level names are the portal's own words, not a table in this repository, so
they follow your portal's language. Attributes carry the full detail:
`Schedule` (per day), `CircuitTimesDay` and `PossibleValues` (as the device
reports them) and `Raw_JSON`.

These are **read-only**. Change a programme in the WEM Portal app; the
integration re-reads it about once an hour.

### Energy statistics

Consumption and heat-output totals arrive as `total_increasing` energy sensors,
ready for the Energy Dashboard. They are fetched once an hour — that is the
portal's own granularity, and asking more often only spends requests.

---

## Installation

Requires **Home Assistant 2024.12.0** or newer.

### HACS (custom repository)

1. In [HACS](https://github.com/hacs/default), add
   `https://github.com/Varitras/hass-WEM-Portal` as a custom repository
   (category: Integration).
2. Install it and restart Home Assistant.
3. Continue with [Setup](#setup).

### Manual install

Copy everything from `custom_components/wemportal/` in this repository into
`<config directory>/custom_components/wemportal/`, then restart Home Assistant.

```bash
custom_components
└── wemportal
    ├── __init__.py
    ├── ...
    └── wemportalapi.py
```

---

## Setup

`Settings > Devices & Services > Add integration >` search for
**Weishaupt WEM Portal**.

| Field | Meaning |
|---|---|
| `username` | The email address you log into the WEM Portal with. |
| `password` | Your portal password. |
| `language` | Language for entity names: `en` or `de`. Defaults to `en`. |
| `mode` | Where readings come from — see [below](#choosing-a-mode). Defaults to `api`. |

The credentials are checked against **exactly the interface your mode needs**:
the mobile API and the web frontend are separate logins, and one working says
nothing about the other. The same check runs again if you change the mode
later, so a mode that cannot log in is refused instead of silently failing on
every update afterwards.

Everything else is under `CONFIGURE` once the integration is added.

---

## Choosing a mode

| Mode | Source | Use it when |
|---|---|---|
| `api` | Mobile API only | **Default, and the right answer for most people.** Covers every device on the account and everything the app shows. |
| `web` | Expert web page only | You want readings the app does not show and do not need the API ones. |
| `both` | Both | You want the union. Costs both request budgets. |

> **One device per account on the web path.** The scraper reads a single expert
> page and the portal decides which device that page shows, so with more than
> one device on the account its sensors are filed under the first device the
> mobile API reported — which need not be the one the page showed. The
> integration says so in the log once, and `api` mode covers every device
> correctly. Resolving it means driving the portal's device selector, which
> needs a multi-device account to develop against; upstream issue #43 has been
> open for that reason since 2022.

---

## Polling and the portal's rate limit

Weishaupt blocks an IP address that sends **too many requests in a 12-hour
window**. The block is per IP, not per account, and waiting is the only cure —
every retry during it makes it last longer.

The integration is built around that:

- A `403` from the portal pauses **all** outbound requests for 15 minutes.
- The setup and re-authentication dialogs refuse to send anything while that
  pause is active, and say so — deleting and re-adding the integration to "fix"
  a block is the one thing that reliably prolongs it.
- Repeated failures back off progressively instead of retrying at full speed.
- Parameter lists are discovered once and re-read daily, not every cycle.
- Weekly programmes refresh hourly, statistics hourly.

**What this means for your settings:** the intervals below are floors, not
recommendations. Halving an interval doubles the requests. If you run several
Home Assistant instances, or the WEM app on your phone, from the same
connection, they share the same budget.

---

## Options reference

`Settings > Devices & Services > WEM Portal > CONFIGURE`

### Polling

| Option | Default | Notes |
|---|---|---|
| `scan_interval` | 30 min | Web scraping. Below 15 min is not recommended; under 60 s is clamped to 60 s. |
| `api_scan_interval` | 5 min | Mobile API. Should not go below 3 min; under 60 s is clamped to 60 s. |
| `language` | `en` | `en` or `de`. |
| `mode` | `api` | Re-validated against the portal when changed. |

### Menu actions

- **`Search the portal for new API parameters`** — marks the cached parameter
  lists for a re-read on the next update. Which parameters a module has is
  discovered once and cached for 24 hours, so activating an input or output in
  the portal is otherwise not visible until the next day. Makes no portal
  request of its own. Concerns the mobile API only; the web scraper re-reads
  the whole page every cycle anyway.
- **`Discover expert parameters from the portal`** — see
  [Expert write access](#expert-write-access-web).

### Expert write access

All of these are described in [their own section](#expert-write-access-web):
`Expert write access via web`, ten name/ID slot pairs,
`Poll expert parameters periodically` + interval,
`Notify on successful expert write`, and two advanced navigation switches.

---

## Services

### `wemportal.set_holiday`

Sets a holiday period. **Use this rather than the two date entities
separately** — the portal accepts a single date write and then quietly
discards it; only the pair is stored.

Administrator only, like the expert service and for the same reason: it
changes a real setting on a heating system, and a service call is not covered
by the per-user entity permissions Home Assistant applies to the entities.

```yaml
action: wemportal.set_holiday
data:
  begin_entity: date.heating_circuit_1_holiday_begin
  begin: "2026-12-24"
  end_entity: date.heating_circuit_1_holiday_end
  end: "2027-01-02"
```

Both entities must belong to the same module — the portal addresses parameters
per module. A period ending before it starts is refused: the portal answers
such a pair with a success status and stores nothing.

Setting a date entity on its own still works and is safe. It writes both dates
in one request (keeping the other unchanged), then reads the value back and
shows what the portal actually kept — so a discarded write no longer appears
as a holiday that is not set.

### `wemportal.set_expert_parameter`

Writes one expert parameter. Requires the expert feature to be enabled and the
parameter to sit in one of the slots — see below.

```yaml
action: wemportal.set_expert_parameter
data:
  # Replace with YOUR own entityvalue. This one is a placeholder.
  entityvalue: "3A7F91C2E0B48D5619F2A0C7B4E83D105C2A"
  value: 30
```

Administrator only. Runs synchronously and **raises on failure**, so an
automation can tell whether the write succeeded. It takes a few seconds for the
portal navigation.

---

## Expert write access (web)

Some Fachmann/expert parameters — the heat pump's power limit
("Leistungsbegrenzung"), for instance — are only visible in the WEM Portal web
frontend and are **not exposed by the mobile API at all**. This optional
feature reads and writes them through the same web form the portal itself uses.

It is **disabled by default**. While disabled, no extra entities or services
exist and the integration behaves exactly as without it.

### How it works

- Reaching a parameter is a **minimal** web navigation: the Fachmann submenu,
  then the parameter's edit form. The session is cached in memory for up to 15
  minutes and reused — the login is the request the portal is most likely to
  reject.
- The new value is validated against the option list of **your device's own**
  edit form, so only values it actually accepts are sent.
- After writing, the form is read back to **verify** the value was applied.
  Unconfirmed writes raise an error.
- Only **one** expert operation runs per account at a time. A second write, or
  a write during the periodic read-back or a discovery, is refused rather than
  queued.
- A `403` on an **expert** request pauses only this feature, briefly — sensor
  polling keeps running. A 403 here is not proof of an IP-wide block; it can
  equally mean the portal did not accept that one request. A 403 seen by the
  **normal polling** is the real rate-limit signal and pauses everything.

### Step 1: find the parameter

Each writable parameter has a unique hex ID (`entityvalue`) that is **specific
to your installation** — treat it like a serial number and do not post it
publicly.

#### Option A: let the integration find them (recommended)

1. `Settings > Devices & Services > WEM Portal > CONFIGURE`
2. Choose **`Discover expert parameters from the portal`**.
3. Tick the modules to search (e.g. *Wärmepumpe*) and submit. One module at a
   time is gentler on the portal.
4. Back on the settings form, each slot's ID field is now a dropdown of what
   was found, labelled `group / name (current value)` — for example
   `Pumpe / Leistung Heizen (100 %)`.

A discovery only runs when you ask for it, never in the background, and the
module list is cached so re-opening the dialog does not hit the portal again.
If it cannot run, the form says which of four things happened: another expert
operation is in progress, portal access is backing off after a 403, the search
failed, or it ran and found nothing.

#### Option B: read the ID from the portal yourself

Use this if discovery does not find the parameter you want — the slot ID fields
accept a typed-in value as well as a picked one.

1. Log into [wemportal.com](https://www.wemportal.com) and navigate to the
   Fachmann page showing the parameter (e.g. `Fachmann > Wärmepumpe`).
2. Press `F12`, select the element picker and click the **pencil/edit icon**
   next to the parameter.
3. In the highlighted `<input>`, the `onclick` attribute contains
   `WwpsParameterDetails.aspx` followed by `entityvalue=<long hex string>` —
   for example `entityvalue=3A7F91C2E0B48D5619F2A0C7B4E83D105C2A`. **That is an
   illustrative example, not a real ID; yours will be different.**
4. Copy the hex string after `entityvalue=`.

Alternatively, open the parameter's edit dialog and copy `entityvalue=...`
straight from the request URL in the developer tools **Network** tab.

### Step 2: enable it

1. `Settings > Devices & Services > WEM Portal > CONFIGURE`
2. Enable `Expert write access via web`.
3. Fill in one or more of the ten slots. Each has a *name* (free text, becomes
   the entity's friendly name) and an *entityvalue*. Leave unused slots empty;
   a slot with an ID but no name gets a default one.
4. Save — the integration reloads.

Each filled slot becomes a writable `number` entity. Entities start **without a
value** unless periodic read-back is enabled — otherwise the value is only read
as part of a write. After a successful write (or the first periodic read), the
entity shows the verified value and its min/max tighten to the device's real
range.

Two limits worth knowing before you build on this:

- **Ten slots per account.** Only a parameter sitting in one of them can be
  read by the auto-poll or written by the service — the service is not a
  general write primitive for any parameter of the installation.
- **One expert account at a time.** The service is a single domain-wide
  registration with no account to target, so it refuses while more than one
  configured account has expert write enabled. Polling and every other feature
  keep working for all of them; only the service is affected, and it says so
  rather than guessing which heating system to change.

### Number entity vs. service

|  | Number entity | `set_expert_parameter` |
|---|---|---|
| Runs | Synchronously | Synchronously |
| Failure | Raises, so an automation sees it | Raises, so an automation sees it |
| Permission | Home Assistant's normal entity permissions | Administrator only |

Both wait for the portal to confirm the write, which takes a few seconds — the
call returns when the new value has been read back. Only the caller waits;
polling, the other entities and the rest of Home Assistant are unaffected.

Successful writes do **not** notify by default (that gets noisy when setting
several values). Enable **`Notify on successful expert write`** if you want a
confirmation on success too. Failures are not notified: they are raised, so
whoever asked for the write hears about it.

> **Note on permissions:** the service is admin-only, but the same parameter is
> also a number entity, and Home Assistant has no way for an integration to
> restrict one. Anyone allowed to control that entity can write the parameter —
> treat the entity's permissions as part of the setting.

### Periodic read-back (optional, off by default)

To have the entities reflect the portal's current values without a write,
enable **`Poll expert parameters periodically`** and set an interval in minutes
(default 60, **minimum 15**). All configured parameters are read in one shared
session, with a small random jitter so the timing is less regular.

> **Warning:** each read is a full Fachmann navigation. Polling too frequently
> can trigger a temporary 403 block, which pauses the expert feature until it
> clears. The 15-minute floor exists for this reason.

### Advanced options (only if you know what you are doing)

Two navigation steps are **skipped by default**, because the submenu alone
reaches the Fachmann level:

- **`Enable module select`** — runs the icon-menu module selection first. The
  **`Module menu index`** field then chooses the module (empty falls back to
  `6`, the heat pump on the reference install).
- **`Enable security-code step`** — runs the Fachmann security-code unlock.
  Only for a portal that requires the code per session.

> **Warning:** leave both off unless reads or writes actually fail without
> them. They add requests — and thus 403 exposure — and exist only as a
> fallback for unusual setups.

### Safety notes

- Writes go to your **real heating system**, identical to changing the value in
  the portal itself.
- Recommended first test: write the parameter's **current** value (e.g. 30 if
  the portal shows 30) and check the portal still shows it afterwards.
- This path is heavier than the mobile API. Every read or write is a web
  navigation. It runs only on explicit writes, or on the read-back timer if you
  enable it.

### About the entityvalue ID (background)

Derived by observation, not from vendor documentation — it may not hold for
every model.

- Part of the ID is an **address** (which category, which parameter) and part
  is **installation-specific**. The same parameter on another installation has
  a different ID. Never copy an ID from someone else: on your system it would
  address a *different* parameter, and writing to it could change something
  unintended.
- The ID also carries a **snapshot of the value** at the moment it was read.
  That part is not the address — the portal ignores it when opening the dialog,
  which is why a stored ID keeps working after the value changes.
- Do **not** "clean up" a stored ID by zeroing the value portion. The
  integration uses the full ID exactly as the portal does, and verifies the
  result by re-reading the form after every write.

---

## Troubleshooting

**Enable debug logging first:** `Settings > Devices & Services`, find WEM
Portal, click the three dots on the card, choose `Enable debug logging`.

| Symptom | Likely cause |
|---|---|
| Setup says the portal is refusing this network | The IP is rate-limited. Waiting is the only fix; retrying prolongs it. |
| A sensor shows *unknown* for a cycle | The portal answered without that parameter. Deliberate: a stale reading is not shown as current. |
| Web-only sensors go *unknown* together | Three scrapes in a row failed. They come back on the next successful one. |
| Asked to re-authenticate although the password is right | The portal served something other than a login form or a session. Only a re-rendered login form counts as wrong credentials, but a portal in an odd state can still get there. |
| A new parameter does not appear | Parameter lists are cached for 24 hours. Use `Search the portal for new API parameters`. |
| Expert discovery says another operation is running | The auto-poll or a write holds the per-account lock. Try again in a moment. |

When opening an issue, please include the debug log — **with your
`entityvalue` IDs and email address removed**.

---

## License

MIT — see [LICENSE](LICENSE).
