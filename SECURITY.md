# Security Policy

This is a personal, best-effort hobby/test fork of the WEM Portal integration,
provided **as-is** with no warranty (see the README disclaimer). There is no
guaranteed response time, but security reports are welcome and taken seriously.

## Supported versions

Only the latest release is maintained. Please reproduce issues on the newest
version before reporting.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**, not in a public issue:

- Preferred: open a private report via GitHub Security Advisories
  ("Security" tab → "Report a vulnerability") on
  <https://github.com/Varitras/hass-WEM-Portal>.

When reporting, please include the affected version, a description, and steps
to reproduce if possible.

## Scope notes

- Portal credentials are stored by Home Assistant in its own config-entry
  storage (in clear text, like every HA integration); protect your Home
  Assistant instance and backups accordingly.
- Installation-specific `entityvalue` IDs are sensitive. Please redact them
  (and any real credentials) from logs, screenshots, and reports.
