import hashlib
import json
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


# --- templates ----------------------------------------------------------------------------

MAX_NAME = 120
MAX_INSTRUCTION = 500
MAX_QTY = 999
MAX_QUALITY_POINTS = 10
MAX_POINT_LENGTH = 60
DUPLICATE_SUFFIX = " (Kopie)"


def clean_line(raw: dict) -> dict:
    """Validate and normalize one template line (pure; no database access).

    Returns {id, item_id, label, instruction, qty, photo_required, serial_required,
    quality_points, photo_item_id}. The label may still be '': the caller fills it from the
    item name (resolve in create/replace) and rejects it when there is no item either.
    Raises ValueError with a short English reason.
    """

    def text(key: str, limit: int) -> str:
        value = raw.get(key)
        value = "" if value is None else value
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        value = value.strip()
        if len(value) > limit:
            raise ValueError(f"{key} is longer than {limit} characters")
        return value

    def optional_id(key: str) -> str | None:
        return text(key, 80) or None

    qty = raw.get("qty", 1)
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
        raise ValueError(f"qty must be an integer from 1 to {MAX_QTY}")
    flags = {}
    for key, default in (("photo_required", True), ("serial_required", False)):
        value = raw.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        flags[key] = value
    points = raw.get("quality_points") or []
    if not isinstance(points, list) or len(points) > MAX_QUALITY_POINTS:
        raise ValueError(f"at most {MAX_QUALITY_POINTS} quality points")
    cleaned = []
    for point in points:
        if not isinstance(point, str) or not point.strip() or len(point.strip()) > MAX_POINT_LENGTH:
            raise ValueError(f"quality points must be non-empty strings of at most {MAX_POINT_LENGTH} characters")
        cleaned.append(point.strip())
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("quality points must be unique")
    line_id = raw.get("id")
    if line_id is not None and (isinstance(line_id, bool) or not isinstance(line_id, int)):
        raise ValueError("id must be an integer")
    return {
        "id": line_id,
        "item_id": optional_id("item_id"),
        "label": text("label", MAX_NAME),
        "instruction": text("instruction", MAX_INSTRUCTION),
        "qty": qty,
        "photo_required": flags["photo_required"],
        "serial_required": flags["serial_required"],
        "quality_points": cleaned,
        "photo_item_id": optional_id("photo_item_id"),
    }


def clean_template_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"name must be 1 to {MAX_NAME} characters")
    return name


def item_names(conn: sqlite3.Connection, item_ids) -> dict[str, str]:
    """{item_id: name} for the ids that exist (active or not)."""
    ids = sorted({i for i in item_ids if i})
    found: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT id, name FROM items WHERE id IN ({marks})", chunk):
            found[row["id"]] = row["name"]
    return found


def _require_product(conn: sqlite3.Connection, product_item_id: str | None) -> str | None:
    product_item_id = (product_item_id or "").strip() or None
    if product_item_id is not None and product_item_id not in item_names(conn, [product_item_id]):
        raise ValueError(f"unknown_item:{product_item_id}")
    return product_item_id


def _resolve_lines(conn: sqlite3.Connection, lines: list[dict]) -> list[dict]:
    """clean_line each line, check item references and default empty labels to the item name.

    Raises ValueError: a reason from clean_line, 'unknown_item:<id>' or 'label_required'.
    """
    cleaned = [clean_line(line) for line in lines]
    names = item_names(conn, [i for c in cleaned for i in (c["item_id"], c["photo_item_id"])])
    for line in cleaned:
        for key in ("item_id", "photo_item_id"):
            if line[key] is not None and line[key] not in names:
                raise ValueError(f"unknown_item:{line[key]}")
        if not line["label"]:
            if line["item_id"] is None or not names[line["item_id"]].strip():
                raise ValueError("label_required")
            line["label"] = names[line["item_id"]].strip()[:MAX_NAME]
    return cleaned


def _line_values(line: dict) -> tuple:
    return (
        line["item_id"],
        line["label"],
        line["instruction"],
        line["qty"],
        1 if line["photo_required"] else 0,
        1 if line["serial_required"] else 0,
        json.dumps(line["quality_points"], ensure_ascii=False),
        line["photo_item_id"],
    )


def _tri(value: bool | None) -> int | None:
    return None if value is None else (1 if value else 0)


def create_template(
    conn: sqlite3.Connection,
    name: str,
    product_item_id: str | None,
    second_check: bool | None,
    lines: list[dict],
    now: str,
    commit: bool = True,
) -> int:
    """Insert a template with its lines (positions 1..n in list order). Returns the id.

    Raises ValueError (see _resolve_lines; also a bad name). Line ids in the payload are ignored.
    """
    name = clean_template_name(name)
    product_item_id = _require_product(conn, product_item_id)
    cleaned = _resolve_lines(conn, lines)
    cur = conn.execute(
        "INSERT INTO templates (product_item_id, name, active, second_check, created_at, updated_at)"
        " VALUES (?, ?, 1, ?, ?, ?)",
        (product_item_id, name, _tri(second_check), now, now),
    )
    template_id = cur.lastrowid
    for position, line in enumerate(cleaned, start=1):
        conn.execute(
            "INSERT INTO template_lines (template_id, position, item_id, label, instruction, qty,"
            " photo_required, serial_required, quality_points, photo_item_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (template_id, position, *_line_values(line)),
        )
    if commit:
        conn.commit()
    return template_id


def _template_line(row: sqlite3.Row) -> dict:
    try:
        points = json.loads(row["quality_points"])
    except ValueError:
        points = []
    return {
        "id": row["id"],
        "position": row["position"],
        "item_id": row["item_id"],
        "item_name": row["item_name"] or "",
        "label": row["label"],
        "instruction": row["instruction"],
        "qty": row["qty"],
        "photo_required": bool(row["photo_required"]),
        "serial_required": bool(row["serial_required"]),
        "quality_points": points if isinstance(points, list) else [],
        "photo_item_id": row["photo_item_id"],
    }


def _template_head(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "product_item_id": row["product_item_id"],
        "product_name": row["product_name"] or "",
        "active": bool(row["active"]),
        "second_check": None if row["second_check"] is None else bool(row["second_check"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_TEMPLATE_SELECT = """
    SELECT t.*, p.name AS product_name,
           (SELECT COUNT(*) FROM template_lines l WHERE l.template_id = t.id) AS line_count
    FROM templates t LEFT JOIN items p ON p.id = t.product_item_id
"""


def get_template(conn: sqlite3.Connection, template_id: int) -> dict | None:
    """The template with `lines` ordered by position (item_name joined, '' without item), or None."""
    row = conn.execute(_TEMPLATE_SELECT + " WHERE t.id = ?", (template_id,)).fetchone()
    if row is None:
        return None
    template = _template_head(row)
    template["line_count"] = row["line_count"]
    template["lines"] = [
        _template_line(r)
        for r in conn.execute(
            "SELECT l.*, i.name AS item_name FROM template_lines l LEFT JOIN items i ON i.id = l.item_id"
            " WHERE l.template_id = ? ORDER BY l.position",
            (template_id,),
        )
    ]
    return template


def list_templates(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    """Templates by name (without lines) with `line_count`."""
    where = " WHERE t.active = 1" if active_only else ""
    rows = conn.execute(_TEMPLATE_SELECT + where + " ORDER BY t.name COLLATE NOCASE, t.id").fetchall()
    result = []
    for row in rows:
        head = _template_head(row)
        head["line_count"] = row["line_count"]
        result.append(head)
    return result


def replace_template(
    conn: sqlite3.Connection,
    template_id: int,
    name: str,
    product_item_id: str | None,
    active: bool,
    second_check: bool | None,
    lines: list[dict],
    now: str,
    commit: bool = True,
) -> None:
    """Full replace of a template and its lines.

    A payload line whose `id` is a line of this template is updated in place (its id is
    kept); other payload lines are inserted; lines missing from the payload are deleted.
    Positions are 1..n in payload order. Raises KeyError (unknown template) or ValueError.
    """
    if conn.execute("SELECT 1 FROM templates WHERE id=?", (template_id,)).fetchone() is None:
        raise KeyError(template_id)
    name = clean_template_name(name)
    product_item_id = _require_product(conn, product_item_id)
    cleaned = _resolve_lines(conn, lines)

    existing = {r["id"] for r in conn.execute("SELECT id FROM template_lines WHERE template_id=?", (template_id,))}
    keep: set[int] = set()
    for line in cleaned:
        if line["id"] in existing and line["id"] not in keep:
            keep.add(line["id"])
        else:
            line["id"] = None  # unknown, foreign or repeated id: insert as a new line
    # Park every position on a unique negative value so reordering cannot hit UNIQUE(template_id, position).
    conn.execute("UPDATE template_lines SET position = -id WHERE template_id=?", (template_id,))
    for stale in existing - keep:
        conn.execute("DELETE FROM template_lines WHERE id=?", (stale,))
    for position, line in enumerate(cleaned, start=1):
        if line["id"] is None:
            conn.execute(
                "INSERT INTO template_lines (template_id, position, item_id, label, instruction, qty,"
                " photo_required, serial_required, quality_points, photo_item_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (template_id, position, *_line_values(line)),
            )
        else:
            conn.execute(
                "UPDATE template_lines SET position=?, item_id=?, label=?, instruction=?, qty=?,"
                " photo_required=?, serial_required=?, quality_points=?, photo_item_id=? WHERE id=?",
                (position, *_line_values(line), line["id"]),
            )
    conn.execute(
        "UPDATE templates SET name=?, product_item_id=?, active=?, second_check=?, updated_at=? WHERE id=?",
        (name, product_item_id, 1 if active else 0, _tri(second_check), now, template_id),
    )
    if commit:
        conn.commit()


def duplicate_template(conn: sqlite3.Connection, template_id: int, now: str, commit: bool = True) -> int:
    """Copy a template (active, name + ' (Kopie)', same lines). Returns the new id. KeyError if unknown."""
    source = get_template(conn, template_id)
    if source is None:
        raise KeyError(template_id)
    name = source["name"][: MAX_NAME - len(DUPLICATE_SUFFIX)] + DUPLICATE_SUFFIX
    lines = [dict(line, id=None) for line in source["lines"]]
    return create_template(
        conn, name, source["product_item_id"], source["second_check"], lines, now, commit=commit
    )


def template_has_jobs(conn: sqlite3.Connection, template_id: int) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE template_id=? LIMIT 1", (template_id,)).fetchone() is not None


def delete_template(conn: sqlite3.Connection, template_id: int, commit: bool = True) -> None:
    """Delete a template and its lines. KeyError if unknown; ValueError('template_in_use') if a job uses it."""
    if conn.execute("SELECT 1 FROM templates WHERE id=?", (template_id,)).fetchone() is None:
        raise KeyError(template_id)
    if template_has_jobs(conn, template_id):
        raise ValueError("template_in_use")
    conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
    if commit:
        conn.commit()


def find_template_for_bom(conn: sqlite3.Connection, name: str, product_item_id: str | None) -> int | None:
    """An existing template that a BOM import would duplicate: same name, else same product."""
    row = conn.execute(
        "SELECT id FROM templates WHERE name = ? OR (? IS NOT NULL AND product_item_id = ?)"
        " ORDER BY (name = ?) DESC, id DESC LIMIT 1",
        (name, product_item_id, product_item_id, name),
    ).fetchone()
    return None if row is None else row["id"]
