-- Plaintext badge token, kept only so the badge can be printed again later
-- (GET /api/expert/workers/{id}/badge.svg regenerates the QR from it).
-- Security note: the DB file is readable only by the service user, and
-- token_hash stays the lookup key for logins. A revoked badge has its token
-- cleared to '' and its hash no longer matches an unrevoked row, so a leaked
-- or photographed old token is useless. Rows created before this migration
-- (none in practice) keep '' and cannot be reprinted: re-issue the badge.
ALTER TABLE badges ADD COLUMN token TEXT NOT NULL DEFAULT '';
