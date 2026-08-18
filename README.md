# Website Watch

Kleines lokales Tool, das einmal pro Lauf die RingerDB-API abruft, die Turnierdaten als Tabelle speichert und beim naechsten Lauf eine HTML-Datei mit markierten Aenderungen erzeugt.

Die aktuelle Konfiguration beobachtet zukuenftige Turniere und speichert Datum, Turniername, Ort, ausrichtenden Verein, Veranstalter, Online-Meldung von/bis, Altersklassen, Stilarten und den Anmeldelink.

## Einrichten

1. `config.example.json` nach `config.json` kopieren.
2. In `config.json` die API-`url`, die sichtbare `page_url` und bei Bedarf die `columns` anpassen.
3. Testlauf ausfuehren:

```powershell
python .\website_watch.py --config .\config.json
```

Beim ersten Lauf wird nur ein Schnappschuss gespeichert. Ab dem zweiten Lauf entsteht ein Report unter `runs\reports`.

## Taeglich automatisch ausfuehren

## GitHub Actions

Der Ordner enthaelt eine fertige GitHub-Action unter `.github/workflows/ringerdb-watch.yml`.

Vorgehen:

1. Ein neues Repository im gewuenschten GitHub-Account erstellen.
2. Den Inhalt dieses Ordners `Ringendb-watcher` in das Repository hochladen.
3. In GitHub unter `Settings` > `Secrets and variables` > `Actions` diese Secrets anlegen:

```text
RINGERDB_SMTP_HOST
RINGERDB_SMTP_PORT
RINGERDB_SMTP_USER
RINGERDB_SMTP_PASSWORD
RINGERDB_EMAIL_FROM
RINGERDB_EMAIL_TO
```

`RINGERDB_EMAIL_TO` kann eine Adresse oder mehrere kommagetrennte Adressen enthalten.

Lokal koennen mehrere Empfaenger in `config.json` als Liste eingetragen werden:

```json
"to": [
  "erste@example.de",
  "zweite@example.de"
]
```

Mit `retention_count` wird festgelegt, wie viele der neuesten Snapshots und Reports
jeweils gespeichert bleiben. Der Standardwert ist `2`.

Die Action laeuft taeglich um 20:00 Uhr deutscher Ortszeit (`Europe/Berlin`) und
kann zusaetzlich manuell ueber `Actions` > `RingerDB Watch` > `Run workflow`
gestartet werden. Die Zeitzone beruecksichtigt Sommer- und Winterzeit automatisch.

Bei Aenderungen verschickt das Skript den HTML-Report per E-Mail. Snapshots und Reports werden danach automatisch ins Repository committed, damit der naechste Lauf wieder gegen den letzten Stand vergleichen kann.

## Windows Aufgabenplanung

Beispiel fuer Windows Aufgabenplanung, taeglich um 08:00 Uhr:

```powershell
$script = "C:\pm\claude\Ringendb-watcher\website_watch.py"
$config = "C:\pm\claude\Ringendb-watcher\config.json"
$action = New-ScheduledTaskAction -Execute "python.exe" -Argument "`"$script`" --config `"$config`""
$trigger = New-ScheduledTaskTrigger -Daily -At 08:00
Register-ScheduledTask -TaskName "WebsiteWatch" -Action $action -Trigger $trigger -Description "Prueft taeglich Website-Aenderungen"
```

## Hinweise

- `columns` legt fest, welche API-Felder in der Vergleichstabelle landen.
- Verschachtelte Felder funktionieren mit Punktnotation, z. B. `id.value` oder `land.bezeichnung`.
- `detail_api_url` wird pro Turnier-ID genutzt, um Altersklassen und Stilarten nachzuladen.
- `registration_url_template` erzeugt den klickbaren Anmeldelink aus der Turnier-ID.
- Das Tool vergleicht die API-Daten, nicht Pixel oder Layout der Webseite.
