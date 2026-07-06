[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)
[![buy me a coffee](https://img.shields.io/badge/If%20you%20like%20it-Buy%20me%20a%20coffee-yellow.svg?style=for-the-badge)](https://www.buymeacoffee.com/erikkastelec)
[![License](https://img.shields.io/github/license/toreamun/amshan-homeassistant?style=for-the-badge)](LICENSE)

# hass-WEM-Portal

Custom component for retrieving sensor information from Weishaupt WEM Portal.  
Component uses webscraping, as well as Weishaupt mobile API, to get all the sensor data from the Weishaupt WEM Portal (
Expert view) and makes it available in [Home Assistant](https://home-assistant.io/).

## Installation

### HACS (preferred method)

- In [HACS](https://github.com/hacs/default) Store search for erikkastelec/hass-WEM-Portal and install it
- Activate the component by configuring it via UI as described in [Configuration](#configuration) section below.

### Manual install

Create a directory called `wemportal` in the `<config directory>/custom_components/` directory on your Home Assistant
instance. Install this component by copying all files in `/custom_components/wemportal/` folder from this repo into the
new `<config directory>/custom_components/wemportal/` directory you just created.

This is how your custom_components directory should look like:

```bash
custom_components
├── wemportal
│   ├── __init__.py
│   ├── ...
│   ├── ...
│   ├── ...
│   └── wemportalapi.py  
```

## Configuration

Integration must be configured in Home Assistant frontend: Go to `Settings > Devices&Services `, click on ` Add integration ` button and search for `Weishaupt WEM Portal`.

After Adding the integration, you can click `CONFIGURE` button to edit the default settings. Make sure to read what each setting does below.

Configuration variables during initial setup:

- `username`: Email address used for logging into WEM Portal
- `password`: Password used for logging into WEM Portal
- `language`: Defines preferred language for entity names. Select `en` for English translation or `de` for German. (defaults to en)
- `mode`: Defines the mode of data fetching. Defaults to `api`, which gets the data available through the mobile API. Option `web` gets only the data on the website, while option `both` queries website and api and provides all the available data from both sources.

Optional settings (available by clicking the `CONFIGURE` button after setup):

- `scan_interval`: Defines update frequency of web scraping in seconds (defaults to 30 min). Setting update frequency below 15 min is not recommended.
- `api_scan_interval`: Defines update frequency for API data fetching in seconds (defaults to 5 min, should not be lower than 3 min).

## Expert write access (web)

Some Fachmann/expert parameters (e.g. the heat pump's power limit,
"Leistungsbegrenzung") are only visible in the WEM Portal web frontend and
are **not exposed by the mobile API at all**. This optional feature can
read and write such parameters through the same web form the portal itself
uses.

It is **disabled by default**. While disabled, no extra entities or
services exist and the integration behaves exactly as before.

### How it works

- Writing happens **on demand only** on a short-lived web session
  (a handful of requests per write, no periodic polling at all).
- The new value is validated against the option list of the device's own
  edit form, so only values your heat pump actually accepts can be sent.
- After writing, the form is read back to **verify** the device accepted
  the value; unconfirmed writes raise an error.
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
   it contains a URL fragment like
   `WwpsParameterDetails.aspx` followed by
   `entityvalue=6400000000000000000000000000000000FF`.
4. Copy the hex string after `entityvalue=` - that is the ID.

### Enabling the feature

1. Go to `Settings > Devices & Services > WEM Portal > CONFIGURE`.
2. Enable `Expert write access via web`.
3. Optionally paste your entityvalue IDs into the
   `Leistungsbegrenzung Heizen` / `Kuehlen` fields (either or both).
4. Save - the integration reloads.

If IDs were entered, number entities appear (e.g.
`number.wp_leistungsbegrenzung_heizen`). They start **without a value** -
this is expected, since the value is only read as part of a write
operation (by design, to avoid any extra polling). After the first
successful write, the entity shows the verified value and its min/max
tighten to the device's real allowed range.

A write takes roughly a minute (it reproduces the full Fachmann
navigation and waits for the portal to load live values). It runs as a
**background task**: setting the number or calling the service returns
immediately, and the result is reported via a **persistent
notification** and the log once finished. The entity value updates once
the write is verified. A second write is rejected while one is still
running.

Independent of the number entities, the service
`wemportal.set_expert_parameter` can write any expert parameter directly:

```yaml
action: wemportal.set_expert_parameter
data:
  entityvalue: "6400000000000000000000000000000000FF"  # your ID
  value: 30
```

### Safety notes

- Writes go to your **real heating system**, identical to changing the
  value in the portal itself.
- Recommended first test: write the parameter's **current** value (e.g.
  30 if the portal shows 30) and check the portal still shows the same
  value afterwards, before making real changes.
- **This path is heavier than the mobile API.** Reaching a Fachmann
  parameter reproduces the full browser navigation: unlock the Fachmann
  level (security code `11`, publicly known), select the heat pump module,
  and poll for live values before reading/writing - roughly a dozen
  requests per write, taking about a minute. It runs only on explicit,
  on-demand writes (never periodically), as a background task, and
  respects the same 403 cooldown as the rest of the integration, but you
  should not automate it to fire frequently.


## Troubleshooting
Please set your logging for the custom_component to debug:

Go to `Settings > Devices&Services `, find WEM Portal and click on `three dots` at the bottom of the card. Click on `Enable debug logging`.
