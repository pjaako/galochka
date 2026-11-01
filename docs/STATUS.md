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

## Next
- T3 Workers + badges + sessions.
