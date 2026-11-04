# galochka — cold-start context for agents

Read this first in every new session. Then `docs/STATUS.md` (what is done, what is
next), `docs/specs/2026-11-01-galochka-design.md` (the approved design and task
plan), `docs/CONVENTIONS.md` (how the code is written) and `docs/KNOWN-ISSUES.md`.
If you deploy or maintain the server, read `docs/HANDOFF.md`.

## What this is

A LAN-only web tool for a 10-person family manufacturing company (German-speaking,
Saarbrücken). Workers assemble delivery sets and handle RMAs. galochka ("checkmark")
walks a worker through a per-product checklist with text and reference photos,
records who confirmed each line (badge login) with what evidence (photo, quality
points, serial), requires a second check by another worker, and tracks RMAs as
intake → repair notes → outgoing so that "what came in goes out". It answers the
owner's four problems: incomplete sets shipped, incomplete RMAs returned, dirty or
scratched parts, and no way to find who packed a set.

Sister app: **Cassandra** (`pjaako/cassandra`), same company, same stack. Cassandra is
the single item catalog and reference-photo base; galochka syncs items from it and
links to its photos. galochka keeps only its own evidence photos.

## Roles and working agreement

- The owner acts as sponsor and reviewer and tests on real devices.
- The lead agent is **project owner**: spec, task breakdown, subagent briefs, review,
  decisions. Implementation is delegated to cheaper subagents (Sonnet), one plan task
  per subagent, reviewed before the next task. The lead may make small fixes directly
  where that is cheaper than a rework round.
- Chat with the owner in English. UI strings are German and live only in
  `galochka/static/strings.de.json`. Code, comments, docs in English.
- Ask, don't assume: one focused question at a time.

## Key decisions (do not re-litigate without the owner)

See the Decisions table in the spec. In short: separate app, Cassandra conventions;
badge QR login + short code, HttpOnly session cookie, idle expiry; shared expert
password from the env file; office jobs and ad hoc jobs; office customisations and
worker deviations with reason; photo per line configurable per template line;
quality points and serials per line; second check default on, quick pass with box
photo; RMA number from another system; Dymo 36×89 mm labels and badges, no A4;
BOM import top level only; CSV export of the audit trail.

## Environment rules

- Debian 13 (trixie), Python 3.13. Production: an LXC reachable as `galotschka.local`,
  Cassandra as `cassandra` (port 80, resolved via the site search domain). Port 8081.
- **Plain HTTP on the LAN.** Browsers block `getUserMedia`, service workers and the
  Clipboard API on non-secure origins. The only camera path is
  `<input type="file" accept="image/*" capture="environment">` (Cassandra pattern).
  Never build a live viewfinder.
- Dev setup: `python3 -m venv .venv && .venv/bin/pip install -e .[dev]`, tests
  `pytest -q`, run `GALOCHKA_DATA=./data .venv/bin/galochka serve`.
- Git: `main` holds reviewed merges; day-to-day work is on `claude/keen-rubin-h3uwav`
  (one commit per task). Push after every completed task.
- Lock discipline: one SQLite connection shared under `app.state.lock`; image
  decoding, file writes and HTTP calls to Cassandra happen outside the lock.
