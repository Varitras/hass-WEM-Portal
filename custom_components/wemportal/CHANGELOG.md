# Changelog

Alle nennenswerten Änderungen an diesem Fork werden hier dokumentiert.
Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.0.0/),
Versionierung an [Semantic Versioning](https://semver.org/lang/de/).

## [1.7.0] – 2026-07-05

### Hinzugefügt
- Discovery-Cache: Geräte-/Modul-/Parameter-Definitionen werden jetzt über
  Home-Assistant-Neustarts hinweg persistiert. Die langsame, gedrosselte
  Parameter-Discovery (`get_parameters()`, ~5 Sek. pro Modul) läuft nur
  noch, wenn tatsächlich etwas fehlt (Neuinstallation, neues Modul).
- Session-/Cookie-Wiederverwendung beim Web-Scraping: Vor einem vollen
  Login-Handshake wird zuerst versucht, die Session aus dem letzten
  erfolgreichen Scrape weiterzunutzen. Reduziert Requests pro Zyklus.
- `RestoreSensor` für alle Sensoren: die Einheit (`unit_of_measurement`)
  wird nach einem Neustart aus dem letzten bekannten Zustand wiederhergestellt,
  falls sie direkt nach dem Neustart kurzzeitig fehlt.
- Zusätzliche, rein additive Backoff-Sicherheitsmarge im Coordinator nach
  wiederholten Fehlschlägen (skaliert, gedeckelt bei 6 Std.) – bestehende
  Sleep-/Rate-Limiting-Zeiten in `wemportalapi.py` bleiben unverändert.

### Geändert
- `sanitize_value()` aus `mapper.py` und `scraper.py` zu einer einzigen,
  gemeinsamen Implementierung in `utils.py` konsolidiert.

### Behoben
- **Sprachabhängiger Switch-Bug:** Schalter mit dem Wert `"Ein"`/`"On"`
  (Großschreibung, je nach Portal-Sprache oder API- vs. Scraping-Pfad)
  wurden fälschlich als „aus" angezeigt. Erkennung erweitert auf
  `1`, `1.0`, `"On"`, `"on"`, `"Ein"`, `"ein"`.
- **Header-Merge-Bug in `make_api_call()`:** Aufruf-spezifische Header
  (z. B. bei `get_statistics()`) ersetzten bisher die Standard-Header
  komplett statt sie zu ergänzen, wodurch `Host`/`User-Agent`/`Accept`
  verloren gingen.
- **Falsche „Unknown"-Lücken:** Ein einzelner fehlender Scrape-/API-Wert
  überschrieb den letzten bekannten Wert mit `None` bzw. `0.0`. Der letzte
  bekannte Wert wird jetzt beibehalten, bis ein neuer gültiger Wert vorliegt.
- **Absturzrisiko bei Entity-Setup:** `values["platform"]` (direkter
  Key-Zugriff) in `sensor.py`/`number.py`/`select.py`/`switch.py` hätte bei
  einem einzigen unerwarteten Datensatz das komplette Setup der jeweiligen
  Plattform für alle Geräte abbrechen lassen. Jetzt `.get("platform")` mit
  sauberem Überspringen einzelner fehlerhafter Einträge.

### Entfernt
- Ungenutzte Konstante `REFRESH_WAIT_TIME` (toter Code).

---

## [1.6.0] – upstream (erikkastelec/hass-WEM-Portal)
Ausgangsversion dieses Forks. Siehe [Original-Repo](https://github.com/erikkastelec/hass-WEM-Portal)
für die vorherige Historie.
