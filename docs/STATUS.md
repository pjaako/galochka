# Status

Updated: 2026-11-01

## Done
- 2026-11-01: brainstorm with the owner (four rounds of questions), design reviewed by
  a planning agent, spec approved: `docs/specs/2026-11-01-galochka-design.md`.
  Conventions survey of Cassandra: `docs/CONVENTIONS.md`. BOM sample received:
  `bom_example.csv`. Task plan T0–T9 in the spec.
- T0: repo scaffolding docs (this file, CLAUDE.md, spec, conventions, fixtures).

- T1 (2026-11-01, Sonnet, reviewed): skeleton. Package, full schema `001_init.sql`,
  settings API, pages as stubs, experte.html with login + tabs + working Einstellungen,
  app.js/style.css/fonts, deploy scripts, tests. Suite: 77 passed, 2 skipped (sqlite3 CLI).

- T2 (2026-11-01, Sonnet, reviewed): Cassandra client (`cassandra_client.py`, the only
  module knowing Cassandra's URLs; cached reference photos, stale-on-error, prefetch),
  item sync from Cassandra (button + daily thread) with CSV import as fallback,
  `GET /api/items`, `GET /api/items/{id}/reference`, Artikel tab. Known limit: Cassandra
  has no offset paging, a group with >1000 items is truncated. Suite: 175 passed, 2 skipped.

- T3 (2026-11-01, Sonnet, reviewed): workers, badges (QR `galochka:badge:<token>`,
  sha256 lookup, plaintext kept only for printing, cleared on revoke), short-code login,
  HttpOnly session cookie with idle expiry, Anmelden page, Mitarbeiter tab, Dymo badge
  print page. Migration 002. Suite: 234 passed, 2 skipped.

- T4 (2026-11-01, Sonnet, reviewed): templates CRUD with id-preserving full replace,
  duplicate, delete guarded by jobs (409), `GET /api/templates`; BOM importer
  (`BOM_COLUMNS`, real sample as fixture, dry-run preview, qty-0 rows optional,
  unknown items kept as label-only lines, new/replace modes); Vorlagen tab editor and
  BOM import UI. Suite: 296 passed, 2 skipped.

- T5a (2026-11-01, Sonnet, reviewed + one rework round): jobs backend. Expert job
  creation with count and customisations (withheld / office extra), ad hoc jobs, claim,
  worker extra lines (+ delete while unconfirmed), confirmations with all server rules
  (qty, reason, photo, serials, all quality points answered), finish (second check
  decided per template/global), ship, expert status PATCH, audit rows. Evidence photos
  normalised and written outside the lock. Migration 003 (job_lines.photo_item_id).
  Suite: 495 passed, 2 skipped.

- T5b (2026-11-01, Sonnet, reviewed): Aufträge page (cards, Alle/Meine, ad hoc job),
  Packen screen (one line at a time, reference photo, quality toggles, serials, camera,
  In Ordnung / Fehlt / Nicht in Ordnung, extra lines, summary, finish), expert Aufträge
  tab (create with cartons and customisations, cancel, reopen; new
  `GET /api/expert/jobs`). Playwright tests run for real against the pre-installed
  Chromium (`/opt/pw-browsers`). Suite: 506 passed, 2 skipped.

- T6 (2026-11-01, Sonnet, reviewed): second check (`POST /api/jobs/{id}/check`: box
  photo on release, reason on rejection, different worker than the packers, rejection
  reopens all lines as attempt n+1), Prüfen page with evidence review, job QR,
  `GET /api/jobs/{id}/label`, Dymo 36×89 mm box label print page (print verified as one
  page via PDF in the browser test). Suite: 534 passed, 2 skipped.

- T7a (2026-11-01, Sonnet, reviewed): RMA backend. `POST /api/rma`, intake via the
  normal confirmations, repair notes (replaced / kept / added, author-only delete),
  `generate_outgoing` (what came in minus kept, plus added; one-shot, empty → 409 and the
  expert cancels), outgoing stage reusing confirm/finish/check/ship, expert reopen rules,
  `GET /api/expert/rma/open` with the in/kept/out/delta balance. Suite: 631 passed, 2 skipped.

## Next
- T7b RMA UI + label.
