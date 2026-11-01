# Known issues and deferred minors (as of 2026-11-01)

None yet. Items deferred during reviews are collected here.

## Deferred by design
- Photo storage growth (≈5 GB/year at per-line photos) and backup archive size: a
  `photo_retention_days` setting plus a `galochka prune` command on a systemd timer
  are proposed in the spec (T9), not implemented in v1.
