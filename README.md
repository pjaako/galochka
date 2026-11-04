# galochka

galochka ("checkmark") is a LAN-only web tool for a small manufacturing company. It
walks a worker through a per-product checklist (text and reference photos) when a
delivery set is packed, records who confirmed each line with what evidence (photo,
quality points, serial number), requires a second check by another worker, and tracks
RMAs as intake, repair notes and outgoing list ("what came in goes out"). Workers log
in with a printed badge (QR code or short code). The UI is German. Stack: Python 3.13,
FastAPI, SQLite, vanilla JS.

## Relation to Cassandra

The sister app [Cassandra](https://github.com/pjaako/cassandra) is the single item
catalog and the reference-photo base. galochka syncs the item list from Cassandra's
`/api/items` (expert button and once a day) and shows Cassandra's reference photos
next to each checklist line, fetched over HTTP from `GALOCHKA_CASSANDRA_URL`. galochka
keeps no reference photos of its own, only evidence photos taken while packing,
receiving and checking. If Cassandra is down, packing still works, just without
reference photos.

## Requirements

- Debian 13 (trixie), Python 3.13, internet access during install (apt and PyPI).
- LAN only, plain HTTP, no TLS. Phones and tablets use Chrome or Safari; the camera
  is the system camera opened by a file input (no live viewfinder).
- A Dymo large address label printer (36x89 mm) connected to the office PC, with the
  Dymo driver installed. Phones do not print.
- A reachable Cassandra (default `http://cassandra`) is recommended, not
  required.

## Install

On a fresh Debian 13 machine (the production setup is an LXC named `galotschka.local`, the German spelling of galochka),
as a user who can `sudo`:

```
git clone https://github.com/pjaako/galochka.git galochka
cd galochka
sudo bash deploy/install.sh
```

The script installs system packages (`python3`, `python3-venv`, `python3-pip`, `git`,
`rsync`, `sqlite3`, `sudo`), creates the system user `galochka`, copies the checkout to
`/opt/galochka` (keeping `.git`), creates `/var/lib/galochka`, builds a venv and runs
`pip install -e .`, writes `/etc/galochka.env` with a random 12-character expert
password (only if the file does not exist yet), installs `/usr/local/bin/galochka-cli`
and the systemd unit `galochka`, and enables and restarts the service. It prints the
URL (`http://<ip>:8081`) and, on first install only, the generated expert password.
Write the password down. The script is idempotent.

Check it:

```
systemctl status galochka --no-pager
curl -s http://127.0.0.1:8081/health      # {"status":"ok","jobs_open":0}
```

## First steps

Do this once, in a browser on the office PC.

1. Open `http://galotschka.local:8081/experte` (or the printed IP) and log in with the
   expert password.
2. Tab **Artikel**: click "Artikel von Cassandra laden". The page shows the number of
   active items and the time of the last sync. If Cassandra is not reachable, use the
   CSV import instead (MRPeasy item export, UTF-8, as exported; items missing from
   the file are deactivated).
3. Tab **Vorlagen**: one template per product. Either import the MRPeasy BOM CSV
   ("BOM-Import", check the preview, then "Vorlage anlegen") or build a template by
   hand ("Neue Vorlage"). Per line you set the text, quantity, whether a photo and a
   serial number are required, and optional quality points.
4. Tab **Mitarbeiter**: create every worker, then "Ausweis drucken" for each. The
   badge page asks for the expert password again, shows the badge (name, QR code,
   short code) and prints one label per page. Print from the office PC to the Dymo,
   paper size 36x89 mm. Hand the badge to the worker.
5. Tab **Einstellungen**: "Zweite Kontrolle standardmäßig verlangen" (default on; a
   template can override it), "Abmeldung nach Inaktivität" in minutes (default 30,
   1 to 720), and the default quality points (default: sauber, unbeschädigt,
   funktion).
6. Tab **Aufträge**: create the jobs for the day (order number, template, number of
   cartons 1 to 20, optional customisations: omit lines or add extra items, each with
   a reason). Workers can also start jobs themselves.

## Daily use (workers)

- **Login**: open `http://galotschka.local:8081`, tap "Ausweis scannen" and photograph
  the QR code on the badge, or type the 6-character short code. The session ends with
  "Abmelden" or after the idle time from Einstellungen.
- **Aufträge**: list of open jobs ("Alle" or "Meine"). "Neuer Auftrag" starts an ad hoc
  delivery job (template and order number); "RMA annehmen" registers a return
  (template, RMA number from the other system, customer). Opening a job someone else
  works on asks for confirmation and takes it over.
- **Packen**: one line at a time with reference photos, quantity, quality toggles and
  serial fields. "In Ordnung" confirms (a photo is required where the line says so),
  "Fehlt" needs a reason, "Nicht in Ordnung" needs a reason and a photo,
  "Zusätzliches Teil" adds a part that is not on the list (reason and photo). The
  summary lists deviations in red. "Fertig gepackt" finishes the box.
- **Prüfen**: if the second check applies, a different worker opens the packed box
  in Aufträge ("Prüfen"), reviews the evidence and either "Freigeben" (photo of the
  closed box is mandatory) or "Zurück an Packer" with a reason. After release,
  "Etikett" prints the box label and "Versendet" marks the box as shipped.
- **RMA**: intake (every arriving part: Angekommen / Fehlt / Beschädigt, plus "Weiteres
  Teil gefunden"), then repair notes (Ersetzt / Einbehalten / Hinzugefügt), then
  "Rücksendung vorbereiten" generates the outgoing list (once), which is packed and
  checked like a delivery. There is no second check for intake.
- **Labels**: box labels and badges are Dymo 36x89 mm labels, printed from the office
  PC. The label page asks for the expert password when you are not logged in with a
  badge.

## Update

```
cd /opt/galochka && git pull && sudo bash deploy/install.sh
```

The script reuses the checkout in place, reinstalls the package, keeps
`/etc/galochka.env`, and restarts the service. Database migrations run on startup.

## Backup

```
sudo bash /opt/galochka/deploy/backup.sh /mnt/nas/galochka
```

Backs up the SQLite database (`sqlite3 .backup`, safe while the service runs) and the
`photos/` directory into `galochka-YYYY-MM-DD.tar.gz` in the target directory and keeps
the 8 newest archives there (one archive per day; a second run on the same day
overwrites it). The data directory is taken from `$GALOCHKA_DATA`, then
`/etc/galochka.env`, then `/var/lib/galochka`. Weekly cron entry (Sunday 03:00), as root:

```
0 3 * * 0 /opt/galochka/deploy/backup.sh /mnt/nas/galochka
```

Archives contain the badge tokens of active badges; keep them on storage that only the
admin can read.

### Restore

```
sudo systemctl stop galochka
sudo rm -f /var/lib/galochka/galochka.db-wal /var/lib/galochka/galochka.db-shm
sudo tar -xzf /mnt/nas/galochka/galochka-YYYY-MM-DD.tar.gz -C /var/lib/galochka
sudo chown -R galochka:galochka /var/lib/galochka
sudo systemctl start galochka
```

The `rm` clears WAL/SHM files left by the stopped service; the `chown` is needed
because the archive is made by root. Photos added after the backup are not removed by
the restore; they are just unreferenced.

## Configuration

`/etc/galochka.env` (mode 0640, root:galochka), read by the systemd unit. After editing:
`sudo systemctl restart galochka`.

| Variable | Default | Meaning |
|---|---|---|
| `GALOCHKA_DATA` | `./data` (install.sh writes `/var/lib/galochka`) | data directory: `galochka.db`, `photos/` |
| `GALOCHKA_PORT` | `8081` | HTTP port of `galochka serve` |
| `GALOCHKA_EXPERT_PASSWORD` | `experte` (install.sh writes a random one) | shared password of the expert area, sent as `X-Expert-Password` |
| `GALOCHKA_CASSANDRA_URL` | empty (install.sh writes `http://cassandra`) | base URL of Cassandra; empty = no sync, no reference photos, CSV import only |
| `GALOCHKA_AUTO_SYNC` | `1` | `0`, `false`, `no`, `off` or empty disables the daily item sync (first run 10 s after start, then every 24 h); not written by install.sh |

Use an ASCII password without spaces, quotes, `$` or `#`: the env file is also sourced
by a shell in `galochka-cli`.

Command line against production data (loads the env file, runs as user `galochka`):

```
sudo galochka-cli migrate
```

Running `.venv/bin/galochka` directly from a shell uses `./data`, not production.

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
GALOCHKA_DATA=./data .venv/bin/galochka serve --port 8081
```

Browser tests need Playwright and a Chromium build and skip themselves otherwise:

```
.venv/bin/pip install -e '.[browser]'
.venv/bin/playwright install chromium-headless-shell
.venv/bin/pytest tests/test_browser_packen.py
```

Without `GALOCHKA_CASSANDRA_URL` the dev server has no reference photos and no sync;
use the CSV import in the Artikel tab. Conventions: `docs/CONVENTIONS.md`. Design:
`docs/specs/2026-11-01-galochka-design.md`. Agents start at `CLAUDE.md`.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| "Cassandra ist nicht erreichbar" on the sync button | Cassandra down, wrong URL, DNS | check `GALOCHKA_CASSANDRA_URL`; the last catalog stays unchanged; use the CSV import meanwhile |
| Lines show an empty tile "Foto in Cassandra erfassen" | no reference photo in Cassandra, or Cassandra unreachable (answers are cached 10 min, failures 60 s) | packing works without photos; capture the photo in Cassandra |
| Expert password forgotten | | edit `GALOCHKA_EXPERT_PASSWORD` in `/etc/galochka.env`, `sudo systemctl restart galochka` |
| Worker lost the badge | | Mitarbeiter, "Neuer Ausweis" (old one stops working at once), print again |
| Label prints on two pages | paper size or margins wrong in the print dialog | paper 36x89 mm (Dymo large address), scale 100 %, no headers/footers; the page is `@page` 89x36 mm landscape with 2 mm margin |
| Everybody is sent to the login page | session expired (idle time) | log in again; raise the minutes in Einstellungen |

More in `docs/HANDOFF.md`.
