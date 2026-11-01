# Known issues and deferred minors (as of 2026-11-01)

Items deferred during reviews are collected here.

## Behaviour
- Cassandra's `/api/items` has no offset paging: a group with more than 1000 items is
  imported incompletely. The sync warns and never deactivates items in that case; the
  fix belongs in Cassandra (paging), see HANDOFF backlog.
- No rate limit on `POST /api/login/code` (6 chars from a 32-letter alphabet, LAN only).
  Acceptable for now; add a per-IP delay if the app ever leaves the LAN.

## Deferred by design
- Photo storage growth (≈5 GB/year at per-line photos) and backup archive size: a
  `photo_retention_days` setting plus a `galochka prune` command on a systemd timer
  are proposed in the spec (T9), not implemented in v1.
