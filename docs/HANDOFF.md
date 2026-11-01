# Handoff: deploying and maintaining galochka

Written 2026-11-01 at the end of task T8 (T9 hardening still open), for the agent or
person who deploys galochka and keeps it running. Read this file, then `CLAUDE.md`,
`docs/STATUS.md` and `docs/KNOWN-ISSUES.md`. The README is the operator manual; the spec
(`docs/specs/2026-11-01-galochka-design.md`) is the authority on behaviour.

## 1. What you are taking over

A LAN-only web app for a 10-person manufacturing company in Saarbrücken. Workers pack
delivery sets against a per-product checklist, a second person checks the box, and RMAs
are tracked intake, repair notes, outgoing. Every confirmation records the worker (badge
login), photo, quality points and serials, so "who packed this set" has an answer.
FastAPI, SQLite, photos on disk. No cloud calls, no TLS, no user accounts; workers have
badges, the expert area has one shared password.

State at handoff: 668 tests green, 2 skipped (see STATUS.md), branch
`claude/keen-rubin-h3uwav` (day-to-day work) and `main` (reviewed merges). Never deployed
to a real machine. The owner still has to run the acceptance test on the LXC (section 2).

| Thing | Where |
|---|---|
| App code and venv | `/opt/galochka` (root-owned git checkout, `.venv` inside) |
| Data | `/var/lib/galochka`: `galochka.db` (+ `-wal`/`-shm`), `photos/jobs/<job_id>/<uuid>.jpg` |
| Config | `/etc/galochka.env` (0640 root:galochka) |
| Service | systemd `galochka`, user `galochka`, port 8081, `ExecStart=… galochka serve --host 0.0.0.0` |
| CLI | `/usr/local/bin/galochka-cli` (`migrate`, `serve`) |
| Sister app | Cassandra in another LXC, `http://cassandra.local:8080` |

Two LXCs: `galochka.local` (this app, 8081) and `cassandra.local` (Cassandra, 8080).
galochka calls Cassandra over HTTP only (item list, reference photos; 2 s timeout, caches
in table `cassandra_cache`); Cassandra never calls galochka. All Cassandra URLs live in
`galochka/cassandra_client.py` (the only place to touch when Cassandra's API changes).

State is only `galochka.db` and `photos/`. Everything else can be rebuilt by
`install.sh`.

## 2. First deployment (Debian 13 LXC)

Needs: root, internet during install (apt, PyPI), a few hundred MB disk plus room for
photos (about 5 GB per year is the estimate).

1. Create the LXC (Debian 13) and give it the DNS name `galochka.local` (router/DNS
   entry or mDNS; phones must resolve it). Check that it can reach `cassandra.local:8080`:
   `curl -s http://cassandra.local:8080/health`.
2. Clone and install:
   ```
   git clone https://github.com/pjaako/galochka.git galochka && cd galochka
   sudo bash deploy/install.sh
   ```
   Read the script first, it has not run on a real machine yet. It prints the URL and the
   generated expert password (first install only). Hand the password to the owner.
3. Verify:
   ```
   systemctl status galochka --no-pager
   curl -s http://127.0.0.1:8081/health        # {"status":"ok","jobs_open":0}
   journalctl -u galochka -n 50 --no-pager
   ```
   If `hostname -I` printed an address the phones cannot reach, use `galochka.local`.
4. Set `GALOCHKA_CASSANDRA_URL` in `/etc/galochka.env` if Cassandra is elsewhere
   (install.sh wrote `http://cassandra.local:8080`), then `sudo systemctl restart galochka`.
5. Browser, `http://galochka.local:8081/experte`: log in, Artikel, "Artikel von Cassandra
   laden" (about 1500 to 3000 active items expected; CSV import is the fallback).
6. Office PC: install the Dymo label software/driver and load Dymo large address labels
   (36x89 mm). The browser must see the printer.
7. Vorlagen: import one BOM CSV (preview first) or build a template. Mitarbeiter: create
   the owner as a test worker and a second worker. "Ausweis drucken" (needs the expert
   password again), print one badge to the Dymo. Check: exactly one label, landscape,
   QR readable. If it prints on two pages see section 7.
8. Set up the weekly backup cron (README has the line) and test one backup and restore.

Acceptance with the owner (real phone, plain HTTP): log in with the printed badge (scan
and short code), create a job for the test template in the expert Aufträge tab, pack it
on the phone (photo on a required line, one "Fehlt" with reason, one extra part), finish,
log in as the second worker and release it with a box photo, print the box label, mark
it shipped. Then one RMA: "RMA annehmen", intake, a repair note, "Rücksendung
vorbereiten", pack, check. Then Berichte: find the job, open the history, download the
CSV export in Excel.

## 3. Routine operations

| Task | Command |
|---|---|
| Update | `cd /opt/galochka && sudo git pull && sudo bash deploy/install.sh` |
| Logs | `journalctl -u galochka -f` |
| Restart | `sudo systemctl restart galochka` |
| Health | `curl -s http://galochka.local:8081/health` (`jobs_open` = jobs not shipped/cancelled) |
| Migrate by hand | `sudo galochka-cli migrate` (also runs at every start) |
| Backup | `sudo bash /opt/galochka/deploy/backup.sh /mnt/nas/galochka` (keeps 8 archives) |
| Restore | README, section "Restore" |
| Disk use of photos | `du -sh /var/lib/galochka/photos` and `du -sh /var/lib/galochka/photos/jobs/* \| sort -h \| tail` |
| Expert password | edit `/etc/galochka.env`, restart (ASCII, no spaces, quotes, `$`, `#`) |

Never run `/opt/galochka/.venv/bin/galochka` from a shell for production work: without
the env file it silently uses `./data`. Use `galochka-cli` (it runs as user `galochka`,
so files you pass must be readable by that user).

Backups: one archive per day (same-day reruns overwrite), 8 kept. They hold the whole
`photos/` tree, so the archive size grows with it. They also contain the plaintext badge
tokens (migration 002 keeps them so a badge can be reprinted): store them access-restricted.

Update checks: migrations (`galochka/migrations/NNN_*.sql`) apply automatically at start;
after an update look at `journalctl -u galochka -n 30` and `/health`. Sessions survive a
restart (they are in the database).

## 4. What the people do

- **Worker (packer).** Logs in on a phone or tablet by photographing the badge QR or
  typing the short code. In Aufträge picks a job (or starts an ad hoc one with template
  and order number), confirms line by line: "In Ordnung" (photo where required, quality
  toggles all answered, serial numbers when the line asks), "Fehlt" (how many are there,
  reason), "Nicht in Ordnung" (reason and photo), "Zusätzliches Teil" (reason and photo).
  Then "Fertig gepackt". It refuses while a line is open. Another worker may take a
  half-packed box over (confirmation dialog); both names then appear as packers.
- **Second checker.** A different person, logged in with their own badge, opens the packed
  box via "Prüfen", sees every line with confirmer and evidence photos, and either
  "Freigeben" (photo of the closed box is mandatory) or "Zurück an Packer" (reason, at
  least 3 characters). A rejection reopens all lines as a new attempt; confirmations are
  kept and visible in the history. When the second check is off (global setting or
  template), "Fertig gepackt" lands directly in `checked`. After release anybody can
  print the label ("Etikett") and press "Versendet".
- **Repair technician.** Takes in a return with "RMA annehmen" (template = expected
  parts, RMA number from the other system, customer; duplicate RMA number is refused).
  Intake: per line Angekommen / Fehlt (count) / Beschädigt (reason, photo), plus "Weiteres
  Teil gefunden". "Eingang abschließen" moves the job to repair. There the technician
  records notes per line: Ersetzt (part swapped, quantity unchanged on return), Einbehalten
  (kept in the house, reduces the return quantity) and Hinzugefügt (new part goes out).
  The page previews the return list. "Rücksendung vorbereiten" generates the outgoing list
  once: arrived quantity minus kept, plus added parts; lines that did not arrive produce
  nothing. The outgoing stage is packed and checked like a delivery.
- **Expert / office.** Works on `/experte` with the shared password: Artikel (sync, CSV),
  Vorlagen (BOM import, editing), Aufträge (create with 1 to 20 cartons and
  customisations, cancel, reopen, labels), Mitarbeiter (create, print/re-issue/revoke
  badge, deactivate), Einstellungen, Berichte (search by order, customer, serial, worker,
  date; full history of a job; offene RMAs with arrived/kept/returned/difference and
  days open), Export (CSV of confirmations, checks and repair notes). Prints badges and
  box labels on the Dymo.

## 5. Settled decisions (do not reopen without the owner)

- Separate app on Cassandra's stack and conventions; Cassandra is the only item catalog and
  reference-photo source (no galochka reference photos); CSV item import is a fallback.
- Plain HTTP on the LAN: no `getUserMedia`, service workers or Clipboard API; the camera is
  `<input type=file accept="image/*" capture="environment">` only.
- Identity: printed badge with QR (`galochka:badge:<32-char token>`) plus 6-character short
  code; HttpOnly session cookie, idle expiry (`session_minutes`, default 30). Expert area:
  one shared password from the env file, header `X-Expert-Password`, no change-password UI.
- Jobs come from the office (queue) and workers can start ad hoc jobs. Office customisations
  and worker deviations both need a reason and are highlighted in reports.
- Evidence per line: photo (required or optional per template line), quality points,
  serials. Worker-added extra line and `not_ok` need a photo; `missing` needs a reason.
- Second check on by default (global setting, per-template override); the checker must not
  be one of the packers of the current attempt; release needs a box photo.
- RMA: number from another system, unique; intake, repair notes, one-shot outgoing list;
  second check applies to outgoing only.
- BOM import: top level only, one line per direct component; `BOM_COLUMNS` in
  `importer.py` is the single mapping.
- Dymo 36x89 mm labels and badges via `@page` CSS from the office PC; no A4 packing slip.
- Out of scope in v1: user accounts, HTTPS, MRPeasy write-back, languages besides German,
  photo pruning, galochka's own reference photos.

## 6. How to work on the code

- `CLAUDE.md` is the cold-start file. `docs/CONVENTIONS.md` is how the code is written
  (layers, error codes, lock discipline, i18n, tests, deploy). `docs/specs/…-design.md` has the
  data model, API table and the T0 to T9 task plan.
- Process: the lead agent specs, delegates one plan task to a Sonnet subagent, reviews
  (reads the diff, runs `pytest -q`, runs the acceptance list), then the next task. One
  commit per task, pushed to `claude/keen-rubin-h3uwav`.
- Tests: `.venv/bin/pytest -q`. Browser tests (Playwright, `tests/test_browser_*.py`) skip
  without Chromium; install with `pip install -e '.[browser]'` and
  `playwright install chromium-headless-shell`.
- UI strings only in `galochka/static/strings.de.json`; a test checks key coverage.
- Lock discipline: one SQLite connection under `app.state.lock`; image decoding, file
  writes and HTTP calls to Cassandra happen outside the lock.
- Migrations: add `galochka/migrations/NNN_name.sql` with the next number. Never edit an
  applied file (001 to 003 exist). `db.migrate` records applied names in `schema_migrations`.
- Dev: `GALOCHKA_DATA=./data .venv/bin/galochka serve --port 8081` (port 8080 is taken on the
  owner's dev PC). Set `GALOCHKA_AUTO_SYNC=0` if you point it at a real Cassandra and do
  not want the sync thread.

## 7. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Sync button: "Cassandra ist nicht erreichbar" (HTTP 502) | Cassandra down, wrong `GALOCHKA_CASSANDRA_URL`, DNS | `curl http://cassandra.local:8080/api/groups` from the galochka LXC; the catalog is left as it was; CSV import as fallback; the daily sync logs a warning and retries in 24 h |
| Sync says "nicht konfiguriert" (409) | URL empty in the env file | set it, restart |
| Empty reference tile | no photo in Cassandra, or Cassandra down | not a blocker; answers cached 10 min (failures 60 s, stale answers served on error) |
| Sync shows the warning "unvollständige Liste" | a Cassandra group (or the ungrouped listing) has more than 1000 items; Cassandra has no offset paging. The sync still upserts what it got and deactivates nothing | split the group in MRPeasy, or add paging to Cassandra's `/api/items` (backlog) |
| Everybody lands on the login page | idle expiry | each authenticated request refreshes the clock; after `session_minutes` without a request the next request gets 401 `session_expired` and the page redirects to `/anmelden?next=…`. Already stored confirmations stay; the action that hit the 401 was not saved and must be repeated after login. Raise the minutes in Einstellungen |
| Badge scan "Ausweis oder Kurzcode unbekannt oder gesperrt" | revoked/re-issued badge, wrong code | Mitarbeiter, "Neuer Ausweis"; revoking or re-issuing logs the worker out at once |
| "Die Prüfung muss eine andere Person durchführen" (403) | the checker confirmed a line in the current attempt (current stage) | another worker checks. After a rejection only the people who re-confirm lines in the new attempt are excluded |
| A box cannot be checked at all | only one worker exists, or second check unwanted | create a second worker; or switch the second check off in Einstellungen or on the template (affects jobs finished afterwards) |
| "Die Rücksendeliste wurde schon erstellt" | the outgoing list is one-shot per RMA | the expert can send the RMA back to repair only while no outgoing line is confirmed: `PATCH /api/expert/jobs/<id>` with `{"status":"repair"}` and the expert password header (no button in the UI). This deletes the unconfirmed outgoing lines so the list can be generated again. With a confirmed outgoing line: 409 "Die Rücksendung wurde schon bestätigt" |
| "Es gibt nichts zurückzusenden" | everything missing or kept | expert cancels the RMA in Aufträge |
| Packed/checked/shipped job must be changed | | expert, Aufträge, "Wieder öffnen" (delivery back to packing, RMA back to outgoing); cancelled jobs reopen to open/intake |
| BOM preview skips rows "übersprungen (Menge 0)" | MRPeasy BOM rows with quantity 0 | tick "Zeilen mit Menge 0 trotzdem übernehmen" if they are wanted |
| BOM lines without article | Part No. not in the catalog yet | sync from Cassandra first, then import again; unknown ones become label-only lines |
| CSV import/BOM: "muss UTF-8 sein" | file re-saved by Excel | export again from MRPeasy, do not open and save in Excel |
| CSV export looks wrong in Excel | opened via import wizard with wrong settings | open the file by double click: it is UTF-8 with BOM and `;` separated, CRLF; times are UTC; default range is the last 90 days |
| Item import deactivated most items | a filtered export was imported (absent items are deactivated) | import the full export, or sync from Cassandra |
| Label prints on two pages or cut off | print dialog paper size | paper 36x89 mm (Dymo large address, landscape), scale 100 %, headers/footers off. Page CSS is `@page { size: 89mm 36mm; margin: 2mm }`, one label per page |
| Badge print page: "Experten-Passwort nötig" | the office PC has no worker session | enter the expert password (kept in the tab's sessionStorage) |
| Phone cannot reach the server | DNS or firewall | try the IP; `ss -ltn \| grep 8081`; open TCP 8081 in the LAN |
| Service restarts in a loop | bad env file, port in use, unreadable data dir | `journalctl -u galochka -n 100`; `chown -R galochka:galochka /var/lib/galochka` |
| Disk fills up | photos, about 5 GB per year, plus 8 backup copies | see backlog (pruning); check `du` as in section 3 |

## 8. Backlog (nothing here blocks operation; see KNOWN-ISSUES.md)

- **Photo retention.** Proposed in the spec, not built: setting `photo_retention_days` and a
  `galochka prune` command on a systemd timer. Must keep audit rows and null/mark the
  photo path rather than leave dangling references. Backups grow with `photos/`.
- **Rate limit on code login.** `POST /api/login/code` has no throttling (6 characters from
  a 32-letter alphabet). Fine on the LAN; add a per-IP delay if the app ever leaves it.
- **Cassandra paging.** Groups with more than 1000 items are truncated. The sync detects
  this, warns and skips deactivation, so nothing is lost, but new items beyond the limit
  are not imported. Needs offset paging (or a `since` parameter) in Cassandra's `/api/items`.
- Unauthenticated read endpoints by design: `/photos/*` (uuid file names) and
  `/api/jobs/{id}/qr.svg`, plus all HTML pages (data comes from authenticated APIs). Fine on
  the LAN; revisit before any wider exposure.
- The expert password travels in a header over plain HTTP and is stored in
  `sessionStorage`; there is no UI to change it.
- `galochka-cli` does not forward `GALOCHKA_AUTO_SYNC` (only data, port, password, URL).
- The UI has no button for RMA outgoing back to repair (API only, section 7).
- `install.sh` and `backup.sh` have never run on a real machine; the badge and label
  printing on the actual Dymo driver is untested (print layout verified as one PDF page in
  Chromium only).
- Missing from v1 per the spec: T9 items (whole-branch review, fix wave, lock-discipline grep,
  full Playwright walkthrough with screenshots for the owner).
