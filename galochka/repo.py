import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: str, commit: bool = True) -> None:
    """Upsert one setting. Pass commit=False to batch several settings into one transaction."""
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    if commit:
        conn.commit()


def search_items(conn: sqlite3.Connection, q: str, limit: int) -> list[sqlite3.Row]:
    """Active items ordered by id whose id or name contains q (case-insensitive, Unicode-aware).

    The match runs in Python because SQLite's LOWER()/LIKE only fold ASCII ("ü" vs "Ü").
    """
    rows = conn.execute("SELECT id, name, group_name FROM items WHERE active=1 ORDER BY id").fetchall()
    needle = q.strip().casefold()
    if needle:
        rows = [r for r in rows if needle in r["id"].casefold() or needle in r["name"].casefold()]
    return rows[:limit]


def get_item_name(conn: sqlite3.Connection, item_id: str) -> str | None:
    row = conn.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
    return None if row is None else row["name"]


# --- workers ------------------------------------------------------------------------------

_SHORT_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_SHORT_CODE_LENGTH = 6
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")

# One worker with the columns of its current (unrevoked) badge; badge_* are NULL without one.
_WORKER_SELECT = """
    SELECT w.id, w.name, w.active, w.created_at,
           b.short_code AS badge_short_code, b.created_at AS badge_created_at
    FROM workers w
    LEFT JOIN badges b ON b.id = (
        SELECT MAX(id) FROM badges WHERE worker_id = w.id AND revoked_at IS NULL
    )
"""


def create_worker(conn: sqlite3.Connection, name: str, now: str, commit: bool = True) -> int:
    cur = conn.execute("INSERT INTO workers (name, active, created_at) VALUES (?, 1, ?)", (name, now))
    if commit:
        conn.commit()
    return cur.lastrowid


def list_workers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All workers (active first, then by name) with the short code of their current badge."""
    return conn.execute(_WORKER_SELECT + " ORDER BY w.active DESC, w.name COLLATE NOCASE, w.id").fetchall()


def get_worker(conn: sqlite3.Connection, worker_id: int) -> sqlite3.Row | None:
    return conn.execute(_WORKER_SELECT + " WHERE w.id = ?", (worker_id,)).fetchone()


def update_worker(
    conn: sqlite3.Connection,
    worker_id: int,
    name: str | None = None,
    active: bool | None = None,
    commit: bool = True,
) -> None:
    """Rename and/or (de)activate a worker. Deactivating also deletes the worker's sessions.

    Raises KeyError for an unknown worker.
    """
    if conn.execute("SELECT 1 FROM workers WHERE id=?", (worker_id,)).fetchone() is None:
        raise KeyError(worker_id)
    if name is not None:
        conn.execute("UPDATE workers SET name=? WHERE id=?", (name, worker_id))
    if active is not None:
        conn.execute("UPDATE workers SET active=? WHERE id=?", (1 if active else 0, worker_id))
        if not active:
            delete_worker_sessions(conn, worker_id, commit=False)
    if commit:
        conn.commit()


# --- badges -------------------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    # token_urlsafe(24) is always 32 characters of [A-Za-z0-9_-]; validate anyway so a
    # change of the generator can never silently produce badges the QR regex rejects.
    while True:
        token = secrets.token_urlsafe(24)
        if _TOKEN_RE.match(token):
            return token


def _new_short_code(conn: sqlite3.Connection) -> str:
    while True:
        code = "".join(secrets.choice(_SHORT_CODE_ALPHABET) for _ in range(_SHORT_CODE_LENGTH))
        taken = conn.execute(
            "SELECT 1 FROM badges WHERE short_code=? AND revoked_at IS NULL", (code,)
        ).fetchone()
        if taken is None:
            return code


def issue_badge(
    conn: sqlite3.Connection, worker_id: int, now: str, commit: bool = True
) -> tuple[int, str, str]:
    """Create a badge for the worker. Returns (badge_id, token, short_code).

    The caller revokes the old badge first when re-issuing (revoke_badges). The token is
    stored hashed (login lookup) and in plaintext (reprinting, see migration 002).
    Raises KeyError for an unknown worker.
    """
    if conn.execute("SELECT 1 FROM workers WHERE id=?", (worker_id,)).fetchone() is None:
        raise KeyError(worker_id)
    token = _new_token()
    short_code = _new_short_code(conn)
    cur = conn.execute(
        "INSERT INTO badges (worker_id, token_hash, short_code, created_at, token) VALUES (?, ?, ?, ?, ?)",
        (worker_id, hash_token(token), short_code, now, token),
    )
    if commit:
        conn.commit()
    return cur.lastrowid, token, short_code


def revoke_badges(conn: sqlite3.Connection, worker_id: int, now: str, commit: bool = True) -> int:
    """Revoke every unrevoked badge of the worker and delete the worker's sessions.

    Returns the number of revoked badges. The plaintext token is cleared too.
    """
    cur = conn.execute(
        "UPDATE badges SET revoked_at=?, token='' WHERE worker_id=? AND revoked_at IS NULL",
        (now, worker_id),
    )
    delete_worker_sessions(conn, worker_id, commit=False)
    if commit:
        conn.commit()
    return cur.rowcount


def get_active_badge(conn: sqlite3.Connection, worker_id: int) -> sqlite3.Row | None:
    """The worker's unrevoked badge (id, short_code, created_at, token), or None."""
    return conn.execute(
        "SELECT id, short_code, created_at, token FROM badges "
        "WHERE worker_id=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
        (worker_id,),
    ).fetchone()


_BADGE_LOGIN_SELECT = """
    SELECT b.id AS badge_id, w.id AS worker_id, w.name AS name
    FROM badges b JOIN workers w ON w.id = b.worker_id
    WHERE b.revoked_at IS NULL AND w.active = 1
"""


def find_badge_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """Unrevoked badge of an active worker for a scanned token (badge_id, worker_id, name)."""
    return conn.execute(_BADGE_LOGIN_SELECT + " AND b.token_hash = ?", (hash_token(token),)).fetchone()


def find_badge_by_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    """Same for a typed short code (compared upper-case)."""
    return conn.execute(_BADGE_LOGIN_SELECT + " AND b.short_code = ?", (code.strip().upper(),)).fetchone()


# --- sessions -----------------------------------------------------------------------------


def create_session(conn: sqlite3.Connection, worker_id: int, now: str, commit: bool = True) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token, worker_id, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
        (token, worker_id, now, now),
    )
    if commit:
        conn.commit()
    return token


def get_session_worker(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """Session joined with its worker: id, name, active (of the worker) and last_seen_at."""
    return conn.execute(
        "SELECT w.id AS id, w.name AS name, w.active AS active, s.last_seen_at AS last_seen_at "
        "FROM sessions s JOIN workers w ON w.id = s.worker_id WHERE s.token = ?",
        (token,),
    ).fetchone()


def touch_session(conn: sqlite3.Connection, token: str, now: str, commit: bool = True) -> None:
    conn.execute("UPDATE sessions SET last_seen_at=? WHERE token=?", (now, token))
    if commit:
        conn.commit()


def delete_session(conn: sqlite3.Connection, token: str, commit: bool = True) -> None:
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    if commit:
        conn.commit()


def delete_worker_sessions(conn: sqlite3.Connection, worker_id: int, commit: bool = True) -> None:
    conn.execute("DELETE FROM sessions WHERE worker_id=?", (worker_id,))
    if commit:
        conn.commit()


def session_minutes(conn: sqlite3.Connection) -> int:
    """Idle expiry in minutes (setting session_minutes, 30 when missing or garbled)."""
    try:
        return int(get_setting(conn, "session_minutes", "30"))
    except (TypeError, ValueError):
        return 30


def delete_expired_sessions(conn: sqlite3.Connection, now: str, minutes: int, commit: bool = True) -> int:
    """Delete sessions idle for longer than `minutes`. Returns the number deleted.

    Timestamps are ISO-8601 UTC with the same format, so they compare as strings.
    """
    cutoff = (datetime.fromisoformat(now) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM sessions WHERE last_seen_at < ?", (cutoff,))
    if commit:
        conn.commit()
    return cur.rowcount
