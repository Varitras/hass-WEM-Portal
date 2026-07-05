# Changelog

Alle nennenswerten Änderungen an diesem Fork werden hier dokumentiert.
Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.0.0/),
Versionierung an [Semantic Versioning](https://semver.org/lang/de/).

## [1.7.6] – 2026-07-05

Vollständiger Codebase-Review über alle Dateien hinweg (auf Wunsch), Fokus
auf nicht abgefangene Exceptions und Robustheitslücken. Keine funktionale
Verhaltensänderung für den Normalbetrieb – ausschließlich Absicherung
gegen Rand- und Fehlerfälle.

### Behoben
- **`number.py`/`select.py`/`switch.py`:** dieselbe Absturzgefahr durch
  direkten Key-Zugriff (`ParameterID`, `icon`, `ModuleIndex`, `ModuleType`,
  `min_value`/`max_value`/`step`), die in `sensor.py` schon länger behoben
  war, jetzt auch in den drei Geschwisterdateien behoben. Ein einzelner
  unerwarteter Datensatz kann nicht mehr das Setup der kompletten Plattform
  für alle Geräte crashen lassen.
- **`number.py`:** numerische Validierung verschärft – `NumberEntity` hat
  keinen gültigen Text-Zustand, daher wird jetzt immer auf eine Zahl
  geprüft, nicht nur wenn zufällig eine Einheit vorhanden ist (dieselbe
  Fehlerklasse wie beim Leistungs-Sensor-Crash aus 1.7.5, hier präventiv
  geschlossen).
- **`mapper.py::process_api_values`:** verarbeitet jetzt jeden Datenpunkt
  einzeln abgesichert – ein einzelner fehlerhafter Wert bricht nicht mehr
  die Verarbeitung für den Rest des kompletten Geräte-Updates ab. Dabei
  auch einen fehlenden `_LOGGER`-Import behoben (hätte selbst zu einem
  `NameError` geführt).
- **`__init__.py`:** `migrate_unique_ids()` crasht nicht mehr bei leerem
  `coordinator.data`; ein beschädigtes Discovery-Cache-File verhindert
  nicht mehr den kompletten Start; die Migration selbst ist jetzt
  fehlertolerant abgesichert; kleinere defensive `.get()`/Default-Fixes.
- **`coordinator.py`:** catch-all Exception-Handler ergänzt (fängt u. a.
  `asyncio.TimeoutError` bei sehr großen Anlagen ab, die den Timeout
  genuine überschreiten); alte HTTP-Session wird bei Re-Instanziierung
  jetzt sauber geschlossen statt nur verworfen.
- **`wemportalapi.py`:** `get_parameters()` fängt jetzt auch `ValueError`
  (kaputtes JSON) statt nur `KeyError` ab; `assert` durch expliziten Check
  ersetzt; `api_login()`/`web_login()` fangen jetzt die breitere
  `RequestException` statt nur `HTTPError` ab (konsistent zu
  `make_api_call`); dabei einen latenten `NameError` in `web_login()`
  behoben (Zugriff auf `response`, bevor sicher war, dass es zugewiesen
  wurde); `fetch_webscraping_data()` behandelt reine Netzwerkfehler jetzt
  mit demselben Backoff wie andere Fehlerarten.

Mit 7 Testsuiten (inkl. neuer, gezielter Tests für jeden der obigen
Punkte) verifiziert.

## [1.7.5] – 2026-07-05

### Behoben
- **Absturz bei numerischen Sensoren durch Text-Werte:** `sanitize_value()`
  lieferte für erkannte Boolean-Werte Text (`"Off"`/`"On"`) statt einer
  Zahl, wenn dem aktuellen Messwert keine Einheit anhing (z. B. reines
  `"Aus"` ohne Zahl). Bei Sensoren, die grundsätzlich numerisch sind
  (`device_class: power`, `state_class: measurement`) und deren Einheit
  (z. B. `"kW"`) aus einem vorherigen Zyklus beibehalten wurde (siehe
  Wert-Lücken-Schutz aus 1.7.0), führte das zu: reale Einheit + nicht-
  numerischer Text-Wert – Home Assistant lehnt das strikt ab und der
  Sensor konnte gar nicht erst angelegt werden
  (`ValueError: [...] has the non-numeric value: 'Off'`). Betroffen u. a.
  `sensor.warmepumpe_soll_leistung` und `sensor.warmepumpe_ist_leistung`.
  `sanitize_value()` liefert jetzt für Boolean-Werte immer `0.0`/`1.0`,
  nie Text – wie im Original-Code, bevor diese Fallunterscheidung
  eingeführt wurde. Zusätzlich als zweite Sicherheitsebene: `sensor.py`
  erkennt jetzt auch anhand von `device_class`/`state_class` (nicht nur
  der Einheit des aktuellen Zyklus), dass ein numerischer Wert
  erforderlich ist, und `device_class`/`state_class` werden jetzt **vor**
  der Wert-Validierung gesetzt, damit dieses Sicherheitsnetz auch beim
  allerersten Anlegen der Entität greift.

## [1.7.4] – 2026-07-05

### Behoben
- **`fuzzywuzzy`-Performance-Warnung im Log:** `python-Levenshtein` zu
  den Requirements hinzugefügt, damit die schnelle C-Erweiterung für das
  Fuzzy-Matching in `select.py` automatisch mitinstalliert wird, statt
  auf die langsamere reine Python-Implementierung zurückzufallen. Rein
  kosmetisch/Performance – die Funktionalität war davon nicht betroffen.

## [1.7.3] – 2026-07-05

### Behoben
- **SELECT-Optionen passten wegen Sprach-Mismatch der API nicht:** Auch
  nach dem 1.7.1-Fix trat "Value Off not found in options [...] (names:
  ['Aus', ...])" weiterhin auf. Ursache diesmal: Der **live von der API
  gelesene Wert** kam als `"Off"` (Englisch) zurück, während die aus der
  Parameter-Definition (`EnumValues`) ermittelte Optionsliste `"Aus"`
  (Deutsch) enthielt – unabhängig vom sanitize_value-Fix aus 1.7.1. Das
  bestehende Fuzzy-String-Matching konnte das nicht auffangen, da `"Off"`
  und `"Aus"` keine gemeinsamen Buchstaben haben und der Ähnlichkeits-Score
  weit unter dem Schwellwert von 75 lag. `select.py` erkennt jetzt
  explizit deutsch/englische On/Off-Synonyme (`"Off"`↔`"Aus"`,
  `"On"`↔`"Ein"`) und matcht sie unabhängig von der jeweiligen Sprache
  gegen die tatsächlich vorhandene Optionsliste. Mit Tests für beide
  Richtungen sowie einem Sanity-Check gegen False-Positives abgesichert.

## [1.7.2] – 2026-07-05

### Behoben
- **Leere Fehlermeldungen bei fehlgeschlagenen API-Calls:** `get_response_details()`
  prüfte `if response:` – bei `requests.Response` ist das bei Statuscodes
  ≥ 400 immer `False` (`Response.__bool__`), also genau dann, wenn ein
  Fehler vorliegt. Dadurch wurde die vom Server mitgeschickte
  Fehlerbeschreibung (Status/Message) nie ausgelesen, Log-Meldungen wie
  „Server returned status code: and message:" blieben leer. Betrifft nur
  die Diagnose-Qualität der Logs, nicht die Funktion selbst (der
  bestehende Resilienz-Mechanismus – einzelne fehlgeschlagene
  Statistik-Gruppen werden übersprungen, alles andere läuft normal weiter
  – war davon nicht betroffen).

## [1.7.1] – 2026-07-05

### Behoben
- **Regression aus 1.7.0:** SELECT-Entitäten, bei denen eine Option
  wörtlich `"Aus"` heißt (z. B. "Hot Water Push" mit den Optionen
  `Aus, 5, 10, ..., 240` Minuten), schlugen fehl mit
  `Value Off not found in options [...]`. Ursache war, dass die neue,
  gemeinsame `sanitize_value()` (siehe 1.7.0) auf **alle**
  EnumValues-Parameter angewendet wurde, nicht nur auf echte
  SWITCH-Booleans – dabei wurde `"Aus"` zu `"Off"` umgeschrieben und
  passte danach nicht mehr zur eigenen Optionsliste. Die Normalisierung
  läuft jetzt nur noch für Parameter mit `DataType == SWITCH`; andere
  Enum-Werte (SELECT) behalten ihren Original-String. Mit
  Regressionstest abgesichert (reproduziert exakt den gemeldeten Fall).

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
