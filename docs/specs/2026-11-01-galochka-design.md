# galochka — packing checklists and RMA tracking: design spec and plan

Approved by the owner on 2026-11-01. The brainstorm answers are folded into the Decisions table.

## Context

Family company (Saarbrücken, German-speaking, <10 workers), a few delivery sets per day. Four problems:

1. Delivery sets leave incomplete (a part forgotten).
2. RMA sets come back incomplete; after repair nobody knows what must go back, or whether a part was lost in-house.
3. Parts are shipped dirty/scratched because nobody looks at them while packing.
4. When a complaint comes in, nobody can tell who packed/checked the set.

Solution: a LAN web app, **galochka** (Russian for "checkmark"), that walks a worker through a per-product checklist with text + reference photos, records who confirmed each line (badge login) with what evidence (photo, quality points, serial), and handles RMAs as intake → repair notes → outgoing with the rule "what came in goes out, adjusted by documented repair notes". Customised sets are supported as office-defined customisations and as on-the-spot worker deviations with reason.

The sister app **Cassandra** (`/home/user/cassandra`, same company, same owner) is the template for stack, UI style, deploy and docs. It has the MRPeasy item catalog and reference photos per part; galochka reuses those photos over HTTP. The repo `/home/user/galochka` is empty (LICENSE only); work goes to branch `claude/keen-rubin-h3uwav`.

## Decisions (owner, 2026-11-01; do not re-litigate)

| Topic | Decision |
|---|---|
| Relation to Cassandra | Separate app, same stack (Python 3.13, FastAPI, SQLite, vanilla JS, German `strings.de.json`). Cassandra is the single catalog and photo base: items are synced from Cassandra's `GET /api/items` (expert button + daily automatic sync); the MRPeasy items CSV import exists only as a fallback when Cassandra is not configured/reachable. Reference photos come from Cassandra only (`GALOCHKA_CASSANDRA_URL`, default `http://cassandra.local:8080`; apps run in different LXCs: `cassandra.local`, `galochka.local`); no galochka-own reference photos. A line without a Cassandra photo shows an empty tile and the hint "Foto in Cassandra erfassen". Evidence photos (per job line, box photo) are galochka-own. |
| Set templates | MRPeasy BOM CSV import (owner will commit a sample; importer mapping isolated for adaptation) + expert editing in the app. |
| Worker identity | Personal printed badge with QR (random token) + 6-char short code fallback. Scan = photo → server-side zxing decode (Cassandra `/api/qr` pattern). Session cookie until logout or idle expiry (setting `session_minutes`, default 30). |
| Expert area | Shared expert password from `/etc/galochka.env` (`X-Expert-Password` header, Cassandra pattern). Env-only, no change-password UI. |
| Deploy | Debian 13 LXC, `deploy/install.sh` idempotent pattern, systemd `galochka`, port 8081, data `/var/lib/galochka`, env `/etc/galochka.env`. Update = `git pull && sudo bash deploy/install.sh`. |
| Job start | Office creates jobs (queue) AND worker may start ad hoc (`ad_hoc=1`). |
| Customisation | Office: per job, lines withheld / extra with reason. Worker: may deviate with reason; deviations highlighted in record and reports. |
| Evidence | Photo per line (required/optional per template line). Quality points per line (configurable, defaults: sauber, unbeschädigt, Funktion geprüft). Serial per line (optional per template line). Worker-added extra line and `not_ok` need a photo; `missing` needs only a reason. |
| Second check | Default on; expert can disable globally or per template. Quick pass by a different worker: sees list + evidence, confirms whole set with one tick + photo of packed box, or rejects with note. |
| RMA | Identified by RMA number from another system (free text, unique among RMAs). Stages: intake (tick/photograph what arrived + extra items found) → repair notes (replaced / kept / added) → outgoing (generated list, confirmed like packing, second check applies). |
| Extras v1 | Printable box label (delivery and RMA) and worker badges on **Dymo large address labels (36×89 mm)** via `@page` CSS, printed from the office PC with the Dymo driver; no A4 packing slip. Serials per line, CSV export of the audit trail. |
| BOM depth | Top level only: each direct component is one line; a sub-assembly is one line; expert edits afterwards. |
| Working agreement | I am project owner: spec, briefs, reviews, decisions. Sonnet subagents write product code, one task each, reviewed before the next. I may make small fixes directly where that is cheaper than a rework round. Chat in English; UI German; code/docs English. |

## Architecture

```
galochka/
  galochka/                 Python package
    cli.py                  `galochka serve|migrate`
    config.py               Settings: GALOCHKA_DATA, GALOCHKA_PORT(8081), GALOCHKA_EXPERT_PASSWORD, GALOCHKA_CASSANDRA_URL
    db.py                   connect() (Row, FK on, WAL, busy_timeout 5000), migrate() — Cassandra pattern
    repo.py                 plain query functions (conn, ...); writes commit; ValueError→400, KeyError→404
    auth.py                 require_worker (cookie), require_expert (header), require_worker_or_expert; actor strings 'expert' | 'worker:<id>'
    api.py                  create_app(), pages, worker API
    api_expert.py           APIRouter(prefix="/api/expert", dependencies=[Depends(require_expert)])
    importer.py             parse_csv/import_items copied from Cassandra (CSV fallback); parse_bom with BOM_COLUMNS mapping dict + header-row detection; sync_items(conn, client, now) upserts from Cassandra (iterates GET /api/groups then GET /api/items?group=&limit=1000 because Cassandra has no offset paging)
    qr.py                   decode_qr (copied), badge regex ^galochka:badge:([A-Za-z0-9_-]{32})$, make_svg(text) via `qrcode` (runtime dep)
    images.py               normalize/save_photo copied from Cassandra (no crop), used for evidence photos only
    cassandra_client.py     one module for all Cassandra HTTP (urllib, 2 s timeout): item detail (photos) with positive cache 10 min, negative 60 s, stale-on-error, persisted in table cassandra_cache; items/groups listing for sync; runs outside the lock; thread pool prefetch for a job's lines (2 s budget). Cassandra's API will change (shared storage, recognition service) → keep every URL/shape in this module.
    audit.py                audit(conn, now, actor, action, entity, entity_id, detail)
    migrations/001_init.sql
    static/ anmelden.html auftraege.html packen.html pruefen.html rma.html experte.html druck.html
            app.js style.css strings.de.json fonts/ logo
  tests/  (conftest with data_dir, conn, make_image, client, seeded_client, expert headers, logged-in worker client)
  deploy/ install.sh galochka.service backup.sh galochka-cli
  docs/ specs/2026-11-01-galochka-design.md  CONVENTIONS.md  STATUS.md  HANDOFF.md  KNOWN-ISSUES.md
  README.md  CLAUDE.md  pyproject.toml  .gitignore
```

### Data model (001_init.sql)

- `items(id TEXT PK, name, group_name, location, versions, drawing_revision, active, updated_at, source CHECK IN ('cassandra','csv'))` — Cassandra columns so `import_items` copies verbatim; `source` records where the row last came from.
- `cassandra_cache(item_id PK, json, fetched_at, ok INTEGER)`.
- `workers(id, name, active, created_at)`.
- `badges(id, worker_id FK, token_hash TEXT UNIQUE (sha256), short_code TEXT, created_at, revoked_at NULL)`; partial unique index on `short_code WHERE revoked_at IS NULL`. Short code alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, 6 chars.
- `sessions(token PK, worker_id FK, created_at, last_seen_at)`; index on worker_id.
- `templates(id, product_item_id NULL FK, name, active DEFAULT 1, second_check INTEGER NULL (NULL = global setting), created_at, updated_at)`.
- `template_lines(id, template_id FK CASCADE, position, item_id NULL FK, label, instruction, qty DEFAULT 1, photo_required DEFAULT 1, serial_required DEFAULT 0, quality_points TEXT DEFAULT '[]' ('[]' = use setting at job creation), photo_item_id NULL)`; UNIQUE(template_id, position).
- `jobs(id, kind CHECK IN ('delivery','rma'), template_id FK RESTRICT, template_name, order_ref, customer, unit_no DEFAULT 1, unit_count DEFAULT 1, ad_hoc DEFAULT 0, status, created_by (actor), created_at, claimed_by NULL FK workers, claimed_at, stage_changed_at, note)`.
  - status CHECK IN ('open','packing','packed','checked','shipped','cancelled','intake','repair','outgoing').
  - delivery: open → packing (claim) → packed (finish) → checked (check ok) → shipped; reject: packed → packing; second check off: finish → checked. Expert PATCH: packed|checked|shipped → packing, cancelled → open, any → cancelled.
  - rma: intake → (finish) repair → (POST outgoing) outgoing → packed → checked → shipped; cancelled for nothing-to-return. Second check applies to outgoing only. Expert reopen outgoing → repair only if no outgoing confirmation exists (else 409).
  - partial unique index `order_ref WHERE kind='rma'`.
  - claim always succeeds and overwrites claimed_by (audited) — colleague can take over a half-packed box.
- `job_lines(id, job_id FK CASCADE, template_line_id NULL, stage CHECK IN ('pack','intake','outgoing'), position, item_id NULL, label, instruction, qty, photo_required, serial_required, quality_points JSON (resolved), source CHECK IN ('template','office','worker'), withheld DEFAULT 0, withheld_reason, withheld_by (actor), reason (for office/worker extras), source_line_id NULL FK job_lines (outgoing → intake line))`; UNIQUE(job_id, stage, position). Snapshot at job creation; template edits never touch existing jobs.
- `confirmations(id, job_line_id FK, worker_id FK, attempt INTEGER, result CHECK IN ('ok','missing','not_ok'), qty INTEGER, serials TEXT '[]', quality TEXT '{}' ({point: 'ok'|'not_ok'}), reason, photo_path, created_at)`; append-only; "current" = latest per line; index (job_line_id, id). Files `photos/jobs/<job_id>/<uuid>.jpg`.
  - Server rules: ok iff qty == line.qty; qty < line.qty → result missing + reason required; not_ok → reason + photo required; photo required when line.photo_required or line.source='worker' or result not_ok; serial_required → len(serials)==qty, all non-empty; worker extra = new job_line source='worker' + immediate ok confirmation with photo.
  - Office-withheld lines (withheld=1) are greyed, need no confirmation. Deviation (reports) = source='worker' OR latest result != 'ok'.
- `repair_notes(id, job_id FK, job_line_id NULL FK, action CHECK IN ('replaced','kept','added'), item_id NULL, label, qty DEFAULT 1, serial, reason, worker_id, created_at)`; CHECK ((action IN ('replaced','kept') AND job_line_id NOT NULL) OR (action='added' AND job_line_id IS NULL)); deletable by author while status='repair'.
- `checks(id, job_id FK, worker_id FK, result CHECK IN ('ok','rejected'), note (required when rejected), photo_path (required when ok), created_at)`; append-only; checker must differ from every confirmer of the current attempt → 403 `error.same_worker`.
- `settings(key, value)`: `second_check_default='1'`, `session_minutes='30'`, `default_quality_points='["sauber","unbeschädigt","funktion"]'`, `items_synced_at`.
- `audit(id, at, actor, action, entity, entity_id, detail JSON)`.
- `schema_migrations(name)`.

**Outgoing generation** (`repo.generate_outgoing(conn, job_id, now)`, pure, unit-tested, one-shot → 409 `error.outgoing_exists`): for each intake line with latest confirmed qty > 0: out_qty = qty − Σ kept.qty; if > 0 emit outgoing line (source_line_id = intake line, source inherited, flags from template, replaced notes' reasons visible via join). Each `added` note → line source='worker' with the note's reason. Intake `missing` → nothing. Report "in but not out" per RMA job not shipped/cancelled: intake qty − kept qty − latest outgoing confirmed qty, per source_line_id, > 0 → "lost in-house".

**Multi-unit orders**: one job = one box. Expert create takes `count` (1..20) → N jobs with same order_ref, unit_no 1..N, unit_count N; box label prints "Karton 2/3".

### Auth / session

- QR payload `galochka:badge:<token>` (32 url-safe chars); DB stores sha256. Unreadable image → 400 `error.bad_image`; unknown/revoked → 401 `error.badge_unknown`.
- Cookie `galochka_session=<token>; HttpOnly; SameSite=Lax; Path=/` (no Secure, plain HTTP). `require_worker` checks `last_seen_at + session_minutes`, bumps `last_seen_at`, else deletes the row → 401 `error.session_expired`; `C.api` on 401 on non-expert paths → redirect `/anmelden?next=<path>`.
- `POST /api/logout` deletes session + clears cookie. "Abmelden" + worker name always in nav; after finish/check offer "Fertig und abmelden".
- Revoke badge → `revoked_at` + delete the worker's sessions. Re-issue = revoke + new badge, worker id kept.
- Expert password: `hmac.compare_digest` against env. Expert actions on jobs are actor `'expert'`; expert never authors confirmations.
- Bootstrap: expert page → create worker → badge SVG fetched with `C.expertApi` → blob URL → `window.print()`.
- Print pages (office PC may have no badge): `require_worker_or_expert`; `druck.html` tries `C.api`, on 401 retries with `C.expertApi`. `/api/jobs/{id}/qr.svg` and `/api/jobs/{id}/label.svg` unauthenticated (job id is not a secret).

### Pages (FileResponse, no auth) and API

Pages: `/` (→ auftraege), `/anmelden`, `/auftraege`, `/packen/{job_id}`, `/pruefen/{job_id}`, `/rma/{job_id}`, `/experte`, `/druck/etikett/{job_id}` (delivery or RMA box label), `/druck/badge/{worker_id}`, `/health`.

| Method & path | Auth | Request → Response |
|---|---|---|
| POST /api/login/qr | – | multipart `image` → 200 `{worker:{id,name}}` + cookie; 400/401 |
| POST /api/login/code | – | `{code}` → same |
| POST /api/logout | cookie | → 204 |
| GET /api/me | cookie | → `{worker, expires_at}` |
| GET /api/jobs?status=&kind= | cookie | default statuses open,packing,intake,repair,outgoing,packed → `[{id,kind,status,order_ref,customer,template_name,unit_no,unit_count,ad_hoc,claimed_by_name,lines_total,lines_done,created_at}]` |
| POST /api/jobs | cookie | `{template_id,order_ref,customer}` → 201 job (ad_hoc=1, packing, claimed) |
| POST /api/rma | cookie | `{template_id,rma_no,customer}` → 201 job kind rma status intake; 409 duplicate |
| GET /api/jobs/{id} | cookie/expert | → job + `lines[]` (snapshot fields, `reference:{photos:[{url,source}]}`, `latest:{worker_name,result,qty,serials,quality,reason,photo_url,created_at,attempt}`) + `repair_notes[]` + `checks[]` + `second_check` |
| POST /api/jobs/{id}/claim | cookie | → 200 job |
| POST /api/jobs/{id}/lines | cookie | `{item_id?,label,qty,reason}` → 201 line (source worker, stage current) |
| POST /api/lines/{id}/confirm | cookie | multipart `result,qty,reason,serials(JSON),quality(JSON),image?` → 201; 400 `error.photo_required|serial_count|reason_required`; 409 wrong status |
| POST /api/jobs/{id}/finish | cookie | → 200 job; 409 `error.lines_open` + `{open_line_ids}` |
| POST /api/jobs/{id}/check | cookie | multipart `result,note,image` → 201; 403 `error.same_worker`; 409 |
| POST /api/jobs/{id}/ship | cookie/expert | from checked (or packed when check off) → 200 |
| POST /api/jobs/{id}/repair-notes | cookie | `{job_line_id?,action,item_id?,label,qty,serial,reason}` → 201 |
| DELETE /api/repair-notes/{id} | cookie (author) | → 204 |
| POST /api/jobs/{id}/outgoing | cookie | → 200 job with outgoing lines; 409 |
| GET /api/jobs/{id}/qr.svg | – | svg (QR `galochka:job:<id>`) |
| GET /api/items?q=&limit= | cookie/expert | `[{item_id,name,group_name}]` |
| GET /api/items/{id}/reference | cookie/expert | `{item_id,name,photos:[{url}],cassandra_url}`, never fails (Cassandra down → `photos: []`, `stale: true` when served from cache after an error) |
| GET /api/templates | cookie | active `[{id,name,product_item_id,line_count}]` |
| POST /api/expert/sync/items | expert | → `{added,updated,deactivated,total_active,synced_at}`; 502 `error.cassandra_unreachable`. Also run daily by a background thread started in the lifespan (skipped when no URL configured). |
| POST /api/expert/import/items | expert | multipart `file` (MRPeasy CSV, fallback) → `{added,updated,deactivated,total_active}` |
| POST /api/expert/import/bom?dry_run=1 | expert | multipart `file`, `product_item_id`, `name` → `{template_id?,lines:[{item_id,label,qty,known}],unknown_items[]}` |
| GET/POST /api/expert/templates; GET/PUT/DELETE /{id}; POST /{id}/duplicate | expert | PUT = full replace `{name,product_item_id,active,second_check,lines[]}`; DELETE → 409 if jobs exist (deactivate instead) |
| GET/POST /api/expert/workers; PATCH /{id} | expert | `{name,active}` → worker + `badge:{short_code,created_at}` |
| POST/DELETE /api/expert/workers/{id}/badge; GET …/badge.svg | expert | → `{short_code}` / 204 / svg (token only inside the SVG) |
| POST /api/expert/jobs | expert | `{kind,template_id,order_ref,customer,count,customisations:[{template_line_id,withheld,reason}|{item_id,label,qty,reason}],note}` → 201 `[jobs]` |
| PATCH /api/expert/jobs/{id} | expert | `{status,note}` reopen/cancel → 200 |
| GET/PUT /api/expert/settings | expert | `{second_check_default,session_minutes,default_quality_points[]}` |
| GET /api/expert/jobs?q=&serial=&worker_id=&status=&kind=&from=&to= | expert | list |
| GET /api/expert/jobs/{id}/history | expert | all confirmations + checks + audit rows |
| GET /api/expert/rma/open | expert | `[{job, lines:[{label,in_qty,kept_qty,out_qty,delta}]}]` |
| GET /api/expert/export.csv?from=&to= | expert | UTF-8 BOM, `;` separator (German Excel), one row per confirmation/check |

### Screens (German, strings.de.json)

1. **Anmelden**: big "Ausweis scannen" (file input capture, `--working` state 1–3 s), short-code field.
2. **Aufträge** (home): open jobs incl. "meine", buttons "Neuer Auftrag" (ad hoc: template + order no.), "RMA annehmen" (template + RMA no. + customer).
3. **Packen**: one line at a time; reference photos from Cassandra (`<img onerror>` → empty tile with hint "Foto in Cassandra erfassen"), label, instruction, qty, quality toggles, serial fields, camera; actions "In Ordnung", "Fehlt (Grund)", "Nicht in Ordnung (Grund + Foto)", "Zusätzliches Teil"; progress bar; summary with deviations in red; "Fertig gepackt". Worker name large in nav.
4. **Prüfen**: lines with confirmer + photos; "Freigeben" (box photo mandatory) / "Zurück an Packer (Grund)".
5. **RMA**: intake (same line UI: angekommen / fehlt / zusätzlich), repair notes (replaced / kept / added), "Rücksendung vorbereiten" → outgoing = Packen on generated lines, label print.
6. **Experte**: tabs Artikel (sync from Cassandra with last-sync time, CSV import fallback, BOM import with dry-run preview), Vorlagen, Aufträge (create with count + customisations, reopen, cancel), Mitarbeiter (create, badge print, revoke), Einstellungen, Berichte (search, history, offene RMAs), Export.
7. **Druck**: box label (delivery: order no., customer, set name, carton n/N, packed by / checked by + date, QR `galochka:job:<id>`; RMA: RMA no., customer, set name, QR), badge (name, QR, short code); `@page { size: 89mm 36mm; margin: 2mm }` landscape, one label per page, `window.print()`; opened from the office PC with the Dymo driver.

## Work breakdown (one Sonnet subagent per task, reviewed before the next; sequential on one branch)

Each brief = `docs/CONVENTIONS.md` + the spec sections for the task + acceptance list. Review = read diff, run `pytest -q`, run the acceptance checks; rework via numbered list. One commit per task, pushed to `claude/keen-rubin-h3uwav`.

- **T0 (me)**: `docs/specs/2026-11-01-galochka-design.md` (this plan expanded), `docs/CONVENTIONS.md`, `CLAUDE.md`, `docs/STATUS.md`. Rule into CLAUDE.md: plain HTTP → no getUserMedia/service workers/Clipboard API; only `<input type=file capture>`.
- **T1 Skeleton**: pyproject (qrcode runtime dep), config, db, 001_init.sql (full schema), audit helper, cli serve/migrate, create_app with health/pages/static/photos mounts/no-cache middleware, `experte.html` with login form + empty tab skeleton, all other pages as stubs with nav, app.js/style.css/fonts/logo copied+adapted, conftest, deploy files + `test_deploy.py`.
  - Accept: migrate twice no-op; tables/CHECKs present; FK on; `/health` 200; pages 200 text/html no-cache; `bash -n` all scripts + PASSWORD line test; i18n key-coverage test passes.
- **T2 Items + reference photos**: cassandra_client (detail with caches, groups/items listing), `sync_items`, daily sync thread, CSV importer fallback, `GET /api/items`, `GET /api/items/{id}/reference`, Artikel tab, images.py (evidence photos, tested here).
  - Accept: sync against a fake Cassandra (monkeypatched client) adds/updates/deactivates correctly and iterates all groups; sync with Cassandra unreachable → 502 and items untouched; CSV import fixture twice → 0 added second time; reference with Cassandra unreachable returns `photos: []` and the negative cache makes the 2nd call <100 ms; cache hit avoids an outbound call; `images.normalize` fixes EXIF orientation 6; expert routes 401 without header.
- **T3 Workers + badges + sessions**: workers CRUD, badge issue/revoke/SVG, login qr/code, logout, me, require_worker, idle expiry, Mitarbeiter tab, anmelden page, badge print page, audit rows.
  - Accept: badge SVG decodes via `qr.decode_qr`; login QR + code set HttpOnly cookie, `/api/me` ok; expiry (session_minutes=0) → 401 session_expired; revoke → 401; re-issue keeps worker id; audit rows for login/logout/revoke.
- **T4 Templates + BOM**: templates CRUD/duplicate, `parse_bom` per the BOM section (BOM_COLUMNS dict, real sample as fixture), dry-run import with qty-0 and unknown-item handling, Vorlagen tab (lines, reorder, flags, quality points, instruction).
  - Accept: PUT replace keeps ids of unchanged lines; dry run creates nothing, lists unknown items and the qty-0 row; import of `tests/fixtures/bom_BO00099.csv` creates 17 ordered lines (the two qty-0 rows skipped) with name "D00080 BSU A1207U PENGAUGE BOM" and product D00080; inactive template hidden from `/api/templates`; delete with jobs → 409; static i18n test.
- **T5a Jobs backend**: expert create (count, customisations), ad hoc create, claim, lines (worker extra), confirm with all rules, finish, ship, expert PATCH, audit.
  - Accept: count=3 → 3 jobs unit_no 1..3; withheld line needs no confirmation; worker extra without photo → 400; qty< without reason → 400; serial mismatch → 400; finish with open line → 409 listing ids; status table test; template edit after creation leaves job_lines unchanged.
- **T5b Packen + Aufträge UI**: pages, line-by-line flow, deviations, summary, ad hoc + RMA create dialogs (RMA create endpoint may be stubbed to T7a if not yet present → no, T5b covers delivery only).
  - Accept: i18n coverage; Playwright (skip-gated): login → claim → confirm 2 lines with fixture JPEG → finish → `packed`; progress counter; `C.downscaleImage` before upload; worker name in nav.
- **T6 Second check + box label**: check endpoint, reject path, attempt counter, settings global/per-template, Prüfen page, qr.svg, Dymo box-label print page (delivery), ship.
  - Accept: check by packer → 403; reject → packing, confirmations kept, attempt increments; ok without image → 400; template second_check=0 → finish lands in checked; label page renders with QR via unauthenticated qr.svg and has `@page { size: 89mm 36mm }`; ship from checked only.
- **T7a RMA backend**: `/api/rma`, intake confirm semantics, repair notes CRUD, `generate_outgoing`, reopen rules, open-RMA report query.
  - Accept: unit tests (intake 3, kept 1 → 2; missing → none; added → line with source_line_id NULL; replaced keeps qty; regenerate → 409; reopen with confirmed outgoing → 409; duplicate RMA → 409; lost-unit math).
- **T7b RMA UI + label**: rma page (intake, notes, outgoing hand-off to packen), RMA variant of the box label, RMA create dialog on Aufträge.
  - Accept: Playwright intake → note → outgoing → check; label contains RMA no.; i18n coverage; intake has no check step.
- **T8 Reports + export**: expert search, history, open RMAs, CSV export, Berichte/Export tabs.
  - Accept: search by serial finds job; history includes superseded confirmations; CSV header + one row per confirmation, BOM + `;`; open-RMA list excludes shipped/cancelled.
- **T9 Hardening**: whole-branch review (strong reviewer) + fix wave; lock-discipline grep (no urllib/Image.open inside `with lock`); README (install/update/backup/restore/dev); STATUS/HANDOFF/KNOWN-ISSUES (storage growth + prune proposal: setting `photo_retention_days`, `galochka prune` on a timer, later); full Playwright walkthrough with screenshots for the owner.

Order: T0 → T1 → T2 → T3 → T4 → T5a → T5b → T6 → T7a → T7b → T8 → T9. No parallel tasks (shared files: api.py, experte.html, strings.de.json).

## Environment risks (handled in design)

- Plain HTTP: file-input capture only; QR login is photo-then-upload.
- Shared tablets: HttpOnly cookie, idle expiry, name in nav, "different badge" rule uses confirmation authorship.
- Cassandra down: client outside lock, timeouts, caches, stale-on-error, prefetch pool; packing continues without reference photos; sync reports an error and keeps the last catalog.
- SQLite lock: read/normalize/write files outside the lock, only INSERT inside; unlink file on INSERT failure; busy_timeout.
- Storage: ~5 GB/year photos; backup keeps 8 tars → documented in KNOWN-ISSUES with the prune proposal; not in v1.
- BOM format may change with MRPeasy updates: `BOM_COLUMNS` dict + dry-run preview keep the mapping in one place.
- Printing: Dymo 36×89 mm labels via `@page` CSS from the office PC; phones do not print.

## BOM import (sample received 2026-11-01)

Sample: `bom_example.csv` (repo root, like Cassandra's `articles_example.csv`), also at `tests/fixtures/bom_BO00099.csv`. Format: UTF-8 with BOM, CRLF, quoted header, one BOM per file, flat (top level only), 19 lines, two of them with Quantity 0 (A00533, A01961). Columns: `BOM number, BOM name, Product number, Product name, Group number, Group name, Part No., Part description, Parameters, UoM, Quantity, Fixed quantity, Approximate cost, Notes`.

`importer.parse_bom(data: bytes) -> BomFile(bom_number, bom_name, product_item_id, product_name, rows: list[BomRow(item_id, label, version, uom, qty: float, notes, position)])`. Mapping in one dict `BOM_COLUMNS` (same tolerant header matching as `parse_csv`: strip BOM, case-insensitive, first matching alias). Rules: rows in file order become lines; `Part description` → label (expert edits later); `Notes` → instruction; `Parameters` (= version, as in Cassandra) appended to the label hint; `Quantity` parsed as float, stored as int when integral; **qty 0 rows are listed in the dry-run preview as "übersprungen (Menge 0)" and not created unless the expert ticks them**; unknown item ids (not in `items`) are still imported as lines with `item_id NULL` and listed under `unknown_items` (sync from Cassandra first fixes most). Template defaults from the file: name = `BOM name`, `product_item_id` = `Product number`; both editable in the preview. A second import of the same BOM number offers "replace lines of existing template" vs "create new template" (default: new).

## Verification

- `pytest -q` green after every task; each task adds tests (TestClient API tests, importer fixtures, deploy `bash -n`, static i18n coverage).
- Playwright (pre-installed Chromium) end-to-end in T5b/T7b/T9: badge login → claim → confirm → finish → second check by another badge → box label; RMA intake → note → outgoing → check; expert: sync items from a fake Cassandra, create template, job with withheld line, CSV export. Screenshots sent to the owner.
- Owner acceptance on the LXC: `sudo bash deploy/install.sh`, print one badge, pack one real set on a phone.
