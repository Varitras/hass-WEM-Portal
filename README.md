[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)
[![buy me a coffee](https://img.shields.io/badge/If%20you%20like%20it-Buy%20me%20a%20coffee-yellow.svg?style=for-the-badge)](https://www.buymeacoffee.com/erikkastelec)
[![License](https://img.shields.io/github/license/Varitras/hass-WEM-Portal?style=for-the-badge)](LICENSE)

# hass-WEM-Portal

Custom component for retrieving sensor information from the Weishaupt WEM Portal.  
The component uses web scraping as well as the Weishaupt mobile API to get the
sensor data from the Weishaupt WEM Portal (Expert view) and makes it available
in [Home Assistant](https://home-assistant.io/).

This is a fork of
[erikkastelec/hass-WEM-Portal](https://github.com/erikkastelec/hass-WEM-Portal).

## Installation

### HACS (custom repository)

- In [HACS](https://github.com/hacs/default), add
  `https://github.com/Varitras/hass-WEM-Portal` as a custom repository
  (category: Integration), then install it.
- Activate the component by configuring it via the UI as described in the
  [Configuration](#configuration) section below.

### Manual install

Create a directory called `wemportal` in the `<config directory>/custom_components/`
directory on your Home Assistant instance. Install this component by copying all
files in `/custom_components/wemportal/` from this repo into the new
`<config directory>/custom_components/wemportal/` directory you just created.

This is how your custom_components directory should look:

```bash
custom_components
├── wemportal
│   ├── __init__.py
│   ├── ...
│   └── wemportalapi.py  
```

## Configuration

The integration is configured in the Home Assistant frontend: go to
`Settings > Devices & Services`, click `Add integration`, and search for
`Weishaupt WEM Portal`.

After adding the integration, click `CONFIGURE` to edit the default settings.
Please read what each setting does below.

Configuration variables during initial setup:

- `username`: Email address used for logging into the WEM Portal
- `password`: Password used for logging into the WEM Portal
- `language`: Preferred language for entity names. `en` for English or `de`
  for German (defaults to `en`).
- `mode`: Data-fetching mode. Defaults to `api` (data available through the
  mobile API). Option `web` gets only the data on the website, while `both`
  queries website and API and provides all available data from both sources.

Optional settings (available via `CONFIGURE` after setup):

- `scan_interval`: Update frequency of web scraping in seconds (defaults to
  30 min). Setting it below 15 min is not recommended.
- `api_scan_interval`: Update frequency for API data fetching in seconds
  (defaults to 5 min, should not be lower than 3 min).

## Expert write access (web)

Some Fachmann/expert parameters (e.g. the heat pump's power limit,
"Leistungsbegrenzung") are only visible in the WEM Portal web frontend and
are **not exposed by the mobile API at all**. This optional feature can
read and write such parameters through the same web form the portal itself
uses.

It is **disabled by default**. While disabled, no extra entities or
services exist and the integration behaves exactly as before.

### How it works

- Reaching an expert parameter is a **minimal** web navigation: log in,
  switch to the Fachmann submenu, then fetch the parameter's edit form.
- Writing happens **on demand** on a short-lived web session.
- The new value is validated against the option list of the device's own
  edit form, so only values your heat pump actually accepts can be sent.
- After writing, the form is read back to **verify** the device accepted
  the value; unconfirmed writes raise an error.
- Optionally, the configured parameters can also be **read back on a
  timer** (see *Periodic read-back* below) - off by default.
- A rate-limit response (403) from the server pauses this feature together
  with the rest of the integration.

### Finding the entityvalue ID of a parameter

Each writable parameter has a unique hex ID (`entityvalue`) that is
**specific to your installation** - treat it like a serial number and do
not post it publicly. To find it:

1. Log into [wemportal.com](https://www.wemportal.com) in your browser and
   navigate to the Fachmann page showing the parameter (e.g.
   `Fachmann > Wärmepumpe`).
2. Press `F12` (developer tools), select the element picker (arrow icon)
   and click the **pencil/edit icon** next to the parameter.
3. In the highlighted `<input>` element, look at the `onclick` attribute:
   it contains a URL fragment like `WwpsParameterDetails.aspx` followed by
   `entityvalue=3A7F91C2E0B48D5619F2A0C7B4E83D105C2A`.
4. Copy the hex string after `entityvalue=` - that is the ID.

Alternatively, open the parameter's edit dialog and copy the
`entityvalue=...` value straight from the request URL in the developer
tools **Network** tab.

### Enabling the feature

1. Go to `Settings > Devices & Services > WEM Portal > CONFIGURE`.
2. Enable `Expert write access via web`.
3. Fill in one or more of the **ten generic expert-parameter slots**. Each
   slot has a *name* (free text - becomes the entity's friendly name) and
   an *entityvalue* (the hex ID from the step above). Leave unused slots
   empty. A slot with an ID but no name gets a default name.
4. Save - the integration reloads.

Each filled slot becomes a writable `number` entity you can add to a
dashboard. Entities start **without a value** unless periodic read-back is
enabled - otherwise the value is only read as part of a write. After a
successful write (or the first periodic read), the entity shows the
verified value and its min/max tighten to the device's real allowed range.

A write runs as a **background task**: setting the number or calling the
service returns immediately, and the result is reported via a **persistent
notification** and the log once finished. A second write is rejected while
one is still running.

### Periodic read-back (optional, off by default)

If you want the entities to reflect the portal's current values without a
write, enable **`Poll expert parameters periodically`** and set a **poll
interval in minutes** (default 60, minimum 15). All configured parameters
are then read in **one shared session** at that interval, with a small
random jitter so the timing is less regular.

> **Warning:** each read is a full Fachmann navigation. Polling too
> frequently can trigger a **temporary IP block (403)** from the portal,
> which pauses the whole integration until it clears. Keep the interval
> generous; the 15-minute floor is enforced for this reason.

Independent of the number entities, the service
`wemportal.set_expert_parameter` can write any expert parameter directly:

```yaml
action: wemportal.set_expert_parameter
data:
  entityvalue: "3A7F91C2E0B48D5619F2A0C7B4E83D105C2A"  # your ID
  value: 30
```

### Advanced options (only if you know what you are doing)

Two optional navigation steps - a **module select** and a **security-code
step** - are **skipped by default** because the submenu alone reaches the
Fachmann level. They can be re-enabled from the options dialog in case a
different portal or module layout needs them:

- **`Enable module select`** (default off) - runs the icon-menu module
  selection before fetching the parameter dialog. When enabled, the
  **`Module menu index`** field chooses the module (empty falls back to
  `6`, the heat pump on the reference install).
- **`Enable security-code step`** (default off) - runs the Fachmann
  security-code unlock. Enable only for a portal that requires the code
  per session.

> **Warning:** leave both off unless reads/writes actually fail without
> them. They add requests (and thus 403 exposure) and exist only as a
> fallback for unusual setups.

### Safety notes

- Writes go to your **real heating system**, identical to changing the
  value in the portal itself.
- Recommended first test: write the parameter's **current** value (e.g.
  30 if the portal shows 30) and check the portal still shows the same
  value afterwards, before making real changes.
- **This path is heavier than the mobile API.** Every read or write is a
  fresh web login plus navigation. It runs only on explicit, on-demand
  writes - or, if you enable it, on the periodic read-back timer. Both
  respect the same 403 cooldown as the rest of the integration. If you
  enable periodic read-back, keep the interval generous (the minimum is
  15 minutes) so you don't provoke a temporary block.

## Troubleshooting

Please set logging for the custom component to debug:

Go to `Settings > Devices & Services`, find WEM Portal, click the three
dots on the card, and choose `Enable debug logging`.
