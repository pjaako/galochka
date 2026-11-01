-- Plaintext badge token, kept only so the badge can be printed again later
-- (GET /api/expert/workers/{id}/badge.svg regenerates the QR from it).
-- Security note: the token sits in the DB file, which is protected by the data
-- directory mode 0750 (galochka:galochka, set by install.sh) and the service's
-- UMask=0027 (files it creates are not world-readable); backups are written
-- with umask 077. token_hash stays the lookup key for logins. A revoked badge
-- has its token cleared to '' and its hash no longer matches an unrevoked row,
-- so a leaked or photographed old token is useless. Rows created before this migration
-- (none in practice) keep '' and cannot be reprinted: re-issue the badge.
ALTER TABLE badges ADD COLUMN token TEXT NOT NULL DEFAULT '';
