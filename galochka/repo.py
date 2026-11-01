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


# --- jobs ---------------------------------------------------------------------------------

JOB_STATUSES = ("open", "packing", "packed", "checked", "shipped", "cancelled", "intake", "repair", "outgoing")
ACTIVE_JOB_STATUSES = ("open", "packing", "intake", "repair", "outgoing", "packed")
MAX_ORDER_REF = 40
MAX_CUSTOMER = 80
MAX_REASON = 200
MIN_REASON = 3
MAX_SERIAL = 64
MAX_UNITS = 20
MAX_CUSTOMISATIONS = 50
_BUILTIN_QUALITY_POINTS = ["sauber", "unbeschädigt", "funktion"]

# Statuses in which lines can be confirmed or added, per job kind. The stage of the line
# must equal current_stage(job).
_CONFIRMABLE = {"delivery": ("packing",), "rma": ("intake", "outgoing")}


class LinesOpen(ValueError):
    """finish_job found lines without a confirmation in the current attempt."""

    def __init__(self, line_ids: list[int]):
        super().__init__("lines_open")
        self.line_ids = line_ids


def default_quality_points(conn: sqlite3.Connection) -> list[str]:
    """settings.default_quality_points as a list (the built-in three when missing or garbled)."""
    try:
        points = json.loads(get_setting(conn, "default_quality_points", "[]") or "[]")
    except ValueError:
        points = None
    if not isinstance(points, list) or not all(isinstance(p, str) for p in points):
        return list(_BUILTIN_QUALITY_POINTS)
    return points


def current_stage(job) -> str:
    """Which line stage a job is working on: 'pack' (delivery), 'intake' or 'outgoing' (rma)."""
    if job["kind"] == "delivery":
        return "pack"
    if job["status"] in ("outgoing", "packed", "checked", "shipped"):
        return "outgoing"
    return "intake"


# Same rule as current_stage, for SQL (alias j = jobs).
_STAGE_SQL = (
    "(CASE WHEN j.kind = 'delivery' THEN 'pack'"
    " WHEN j.status IN ('outgoing','packed','checked','shipped') THEN 'outgoing' ELSE 'intake' END)"
)
_ATTEMPT_SQL = "(1 + (SELECT COUNT(*) FROM checks k WHERE k.job_id = j.id AND k.result = 'rejected'))"
# Intake lines are done once confirmed, whatever the attempt: attempts count rejected
# second checks, which only concern the packed/outgoing box.
_DONE_SQL = (
    "EXISTS (SELECT 1 FROM confirmations c WHERE c.job_line_id = l.id"
    f" AND (l.stage = 'intake' OR c.attempt = {_ATTEMPT_SQL}))"
)


def _job_select(extra: str = "") -> str:
    return f"""
    SELECT j.*, w.name AS claimed_by_name, {_STAGE_SQL} AS stage, {_ATTEMPT_SQL} AS attempt,
           (SELECT COUNT(*) FROM job_lines l
             WHERE l.job_id = j.id AND l.stage = {_STAGE_SQL} AND l.withheld = 0) AS lines_total,
           (SELECT COUNT(*) FROM job_lines l
             WHERE l.job_id = j.id AND l.stage = {_STAGE_SQL} AND l.withheld = 0 AND {_DONE_SQL}) AS lines_done
           {extra}
    FROM jobs j LEFT JOIN workers w ON w.id = j.claimed_by
"""


_JOB_SELECT = _job_select()
# Report columns: when it shipped, how many lines of the current stage deviate (latest result not ok, or a
# line a worker added), and who approved the second check (only while checked or shipped, like the label).
_REPORT_SELECT = _job_select(f""", CASE WHEN j.status = 'shipped' THEN j.stage_changed_at END AS shipped_at,
           (SELECT COUNT(*) FROM job_lines l
             WHERE l.job_id = j.id AND l.stage = {_STAGE_SQL}
               AND (l.source = 'worker' OR COALESCE((SELECT c.result FROM confirmations c
                    WHERE c.job_line_id = l.id ORDER BY c.id DESC LIMIT 1), 'ok') <> 'ok')) AS deviations,
           CASE WHEN j.status IN ('checked', 'shipped') THEN
             (SELECT cw.name FROM checks k JOIN workers cw ON cw.id = k.worker_id
               WHERE k.job_id = j.id AND k.result = 'ok' ORDER BY k.id DESC LIMIT 1) END AS checked_by_name""")


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """The job with claimed_by_name, stage, attempt, lines_total and lines_done, or None."""
    return conn.execute(_JOB_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()


def list_jobs(
    conn: sqlite3.Connection, statuses, kind: str | None = None, claimed_by: int | None = None
) -> list[sqlite3.Row]:
    """Jobs in the given statuses (oldest first), optionally of one kind / claimed by one worker."""
    statuses = list(statuses)
    where = [f"j.status IN ({','.join('?' * len(statuses))})"]
    args: list = list(statuses)
    if kind is not None:
        where.append("j.kind = ?")
        args.append(kind)
    if claimed_by is not None:
        where.append("j.claimed_by = ?")
        args.append(claimed_by)
    return conn.execute(_JOB_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY j.id", args).fetchall()


def job_to_dict(row) -> dict:
    """JSON shape of a job (row from get_job / list_jobs)."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "stage": row["stage"],
        "order_ref": row["order_ref"],
        "customer": row["customer"],
        "template_id": row["template_id"],
        "template_name": row["template_name"],
        "unit_no": row["unit_no"],
        "unit_count": row["unit_count"],
        "ad_hoc": bool(row["ad_hoc"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "claimed_by": row["claimed_by"],
        "claimed_by_name": row["claimed_by_name"],
        "claimed_at": row["claimed_at"],
        "stage_changed_at": row["stage_changed_at"],
        "note": row["note"],
        "lines_total": row["lines_total"],
        "lines_done": row["lines_done"],
    }


def _casefold(value):
    return value.casefold() if isinstance(value, str) else value


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_jobs(
    conn: sqlite3.Connection,
    statuses,
    kind: str | None = None,
    q: str | None = None,
    serial: str | None = None,
    worker_id: int | None = None,
    created_from: str | None = None,
    created_before: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Jobs for the reports, newest first, with shipped_at, deviations and checked_by_name.

    q: case-insensitive substring of order_ref, customer or template_name. serial: exact (case-insensitive)
    match of one serial of a confirmation or the serial of a repair note. worker_id: the worker confirmed a
    line, checked, wrote a repair note, created or claimed the job. created_from (inclusive) and
    created_before (exclusive) are ISO strings compared with jobs.created_at.
    """
    conn.create_function("casefold", 1, _casefold, deterministic=True)
    statuses = list(statuses)
    where = [f"j.status IN ({','.join('?' * len(statuses))})"]
    args: list = list(statuses)
    if kind is not None:
        where.append("j.kind = ?")
        args.append(kind)
    q = (q or "").strip()
    if q:
        pattern = "%" + _like_escape(q.casefold()) + "%"
        where.append(
            "(casefold(j.order_ref) LIKE ? ESCAPE '\\' OR casefold(j.customer) LIKE ? ESCAPE '\\'"
            " OR casefold(j.template_name) LIKE ? ESCAPE '\\')"
        )
        args += [pattern] * 3
    serial = (serial or "").strip()
    if serial:
        where.append(
            "(EXISTS (SELECT 1 FROM confirmations c JOIN job_lines l ON l.id = c.job_line_id"
            "         WHERE l.job_id = j.id AND json_valid(c.serials)"
            "           AND EXISTS (SELECT 1 FROM json_each(c.serials) s WHERE casefold(s.value) = ?))"
            " OR EXISTS (SELECT 1 FROM repair_notes n WHERE n.job_id = j.id AND casefold(n.serial) = ?))"
        )
        args += [serial.casefold()] * 2
    if worker_id is not None:
        where.append(
            "(j.claimed_by = ? OR j.created_by = ?"
            " OR EXISTS (SELECT 1 FROM confirmations c JOIN job_lines l ON l.id = c.job_line_id"
            "            WHERE l.job_id = j.id AND c.worker_id = ?)"
            " OR EXISTS (SELECT 1 FROM checks k WHERE k.job_id = j.id AND k.worker_id = ?)"
            " OR EXISTS (SELECT 1 FROM repair_notes n WHERE n.job_id = j.id AND n.worker_id = ?))"
        )
        args += [worker_id, f"worker:{worker_id}", worker_id, worker_id, worker_id]
    if created_from:
        where.append("j.created_at >= ?")
        args.append(created_from)
    if created_before:
        where.append("j.created_at < ?")
        args.append(created_before)
    args += [limit, offset]
    return conn.execute(
        _REPORT_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY j.id DESC LIMIT ? OFFSET ?", args
    ).fetchall()


def job_report_dict(row) -> dict:
    """job_to_dict plus the report columns (row from search_jobs / get_report_job)."""
    data = job_to_dict(row)
    data["shipped_at"] = row["shipped_at"]
    data["deviations"] = row["deviations"]
    data["checked_by_name"] = row["checked_by_name"]
    return data


def get_report_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(_REPORT_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()


def days_open(created_at: str, now: str) -> int:
    """Whole days between two ISO timestamps (0 when unparsable or in the future)."""
    try:
        delta = datetime.fromisoformat(now) - datetime.fromisoformat(created_at)
    except ValueError:
        return 0
    return max(0, delta.days)


def _clean_reason(value) -> str:
    reason = value.strip() if isinstance(value, str) else ""
    if len(reason) < MIN_REASON:
        raise ValueError("reason_required")
    if len(reason) > MAX_REASON:
        raise ValueError("invalid_request")
    return reason


def _clean_extra(conn: sqlite3.Connection, raw: dict) -> dict:
    """Validate an extra line ({item_id?, label, qty, reason}); returns clean values with the label resolved."""
    item_id = raw.get("item_id")
    item_id = item_id.strip() if isinstance(item_id, str) else item_id
    item_id = item_id or None
    if item_id is not None and (not isinstance(item_id, str) or len(item_id) > 80):
        raise ValueError("invalid_request")
    label = raw.get("label")
    label = "" if label is None else label
    if not isinstance(label, str) or len(label.strip()) > MAX_NAME:
        raise ValueError("invalid_request")
    label = label.strip()
    qty = raw.get("qty", 1)
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
        raise ValueError("bad_qty")
    reason = _clean_reason(raw.get("reason"))
    if item_id is not None:
        names = item_names(conn, [item_id])
        if item_id not in names:
            raise ValueError(f"unknown_item:{item_id}")
        label = label or names[item_id].strip()[:MAX_NAME]
    if not label:
        raise ValueError("label_required")
    return {"item_id": item_id, "label": label, "qty": qty, "reason": reason}


def _clean_customisations(conn: sqlite3.Connection, template: dict, customisations) -> tuple[dict, list[dict]]:
    """Split office customisations into ({template_line_id: reason} to withhold, [extra lines])."""
    if customisations is None:
        customisations = []
    if not isinstance(customisations, list) or len(customisations) > MAX_CUSTOMISATIONS:
        raise ValueError("invalid_request")
    line_ids = {line["id"] for line in template["lines"]}
    withheld: dict[int, str] = {}
    extras: list[dict] = []
    for raw in customisations:
        if not isinstance(raw, dict):
            raise ValueError("invalid_request")
        if raw.get("template_line_id") is not None:
            line_id = raw["template_line_id"]
            if isinstance(line_id, bool) or not isinstance(line_id, int) or line_id not in line_ids:
                raise ValueError("unknown_template_line")
            if raw.get("withheld") is not True:
                raise ValueError("invalid_request")
            withheld.setdefault(line_id, _clean_reason(raw.get("reason")))
        else:
            extras.append(_clean_extra(conn, raw))
    return withheld, extras


def _insert_line(conn: sqlite3.Connection, job_id: int, stage: str, position: int, line: dict) -> int:
    cur = conn.execute(
        "INSERT INTO job_lines (job_id, template_line_id, stage, position, item_id, label, instruction, qty,"
        " photo_required, serial_required, quality_points, photo_item_id, source, withheld, withheld_reason,"
        " withheld_by, reason, source_line_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id, line.get("template_line_id"), stage, position, line.get("item_id"), line["label"],
            line.get("instruction", ""), line["qty"], 1 if line.get("photo_required", True) else 0,
            1 if line.get("serial_required") else 0, json.dumps(line["quality_points"], ensure_ascii=False),
            line.get("photo_item_id"), line.get("source", "template"), 1 if line.get("withheld") else 0,
            line.get("withheld_reason", ""), line.get("withheld_by", ""), line.get("reason", ""),
            line.get("source_line_id"),
        ),
    )
    return cur.lastrowid


def create_jobs(
    conn: sqlite3.Connection,
    kind: str,
    template_id: int,
    order_ref: str,
    customer: str,
    count: int,
    customisations: list[dict] | None,
    created_by: str,
    now: str,
    ad_hoc: bool = False,
    claimed_by: int | None = None,
    note: str = "",
    commit: bool = True,
) -> list[int]:
    """Create `count` jobs (one per box) from a template; returns their ids, unit_no 1..count.

    Each job gets its own snapshot of the template lines (stage 'pack' for delivery,
    'intake' for rma), so later template edits never touch it. Empty quality_points
    resolve to settings.default_quality_points, an empty label to the item name.
    Customisations (office): {template_line_id, withheld: true, reason} marks that snapshot
    line withheld; {item_id?, label, qty, reason} appends a line with source 'office'.
    Status: delivery 'open' ('packing' and claimed when ad_hoc with a worker), rma 'intake'
    (claimed when a worker is given).

    Raises KeyError (unknown template) or ValueError with a code: template_inactive,
    template_empty, bad_count, reason_required, unknown_template_line, unknown_item:<id>,
    label_required, bad_qty, invalid_request, rma_duplicate. Validation happens before the first write.
    """
    if kind not in ("delivery", "rma"):
        raise ValueError("invalid_request")
    order_ref = (order_ref or "").strip()
    customer = (customer or "").strip()
    if not 1 <= len(order_ref) <= MAX_ORDER_REF or len(customer) > MAX_CUSTOMER:
        raise ValueError("invalid_request")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_UNITS or (
        kind == "rma" and count != 1
    ):
        raise ValueError("bad_count")
    template = get_template(conn, template_id)
    if template is None:
        raise KeyError(template_id)
    if not template["active"]:
        raise ValueError("template_inactive")
    if not template["lines"]:
        raise ValueError("template_empty")
    withheld, extras = _clean_customisations(conn, template, customisations)
    default_points = default_quality_points(conn)

    stage = "pack" if kind == "delivery" else "intake"
    # A worker who starts a delivery ad hoc claims it; a worker who accepts an RMA always does.
    claimed = claimed_by is not None and (ad_hoc or kind == "rma")
    status = ("packing" if claimed else "open") if kind == "delivery" else "intake"
    snapshot = []
    for line in template["lines"]:
        item = dict(line)
        item["template_line_id"] = line["id"]
        item["label"] = line["label"].strip() or line["item_name"].strip() or line["item_id"] or ""
        item["quality_points"] = line["quality_points"] or list(default_points)
        if line["id"] in withheld:
            item.update(withheld=True, withheld_reason=withheld[line["id"]], withheld_by=created_by)
        snapshot.append(item)
    for extra in extras:
        snapshot.append(
            {
                **extra,
                "source": "office",
                "photo_required": True,
                "serial_required": False,
                "quality_points": list(default_points),
                "photo_item_id": extra["item_id"],
            }
        )

    ids = []
    for unit_no in range(1, count + 1):
        try:
            cur = conn.execute(
                "INSERT INTO jobs (kind, template_id, template_name, order_ref, customer, unit_no, unit_count,"
                " ad_hoc, status, created_by, created_at, claimed_by, claimed_at, stage_changed_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind, template_id, template["name"], order_ref, customer, unit_no, count,
                    1 if ad_hoc else 0, status, created_by, now,
                    claimed_by if claimed else None, now if claimed else "", now, (note or "").strip(),
                ),
            )
        except sqlite3.IntegrityError as e:
            if kind == "rma" and "order_ref" in str(e):
                raise ValueError("rma_duplicate") from e
            raise
        job_id = cur.lastrowid
        for position, line in enumerate(snapshot, start=1):
            _insert_line(conn, job_id, stage, position, line)
        ids.append(job_id)
    if commit:
        conn.commit()
    return ids


# --- job lines and confirmations -----------------------------------------------------------


def get_line(conn: sqlite3.Connection, line_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM job_lines WHERE id = ?", (line_id,)).fetchone()


def _load_json(value, default):
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return default
    return data if isinstance(data, type(default)) else default


def _photo_url(path: str) -> str | None:
    return f"/photos/{path}" if path else None


_LINE_LATEST_SELECT = """
    SELECT l.*, c.id AS c_id, c.worker_id AS c_worker_id, w.name AS c_worker_name, c.attempt AS c_attempt,
           c.result AS c_result, c.qty AS c_qty, c.serials AS c_serials, c.quality AS c_quality,
           c.reason AS c_reason, c.photo_path AS c_photo_path, c.created_at AS c_created_at
    FROM job_lines l
    LEFT JOIN confirmations c ON c.id = (SELECT MAX(id) FROM confirmations WHERE job_line_id = l.id)
    LEFT JOIN workers w ON w.id = c.worker_id
"""


def _latest_of(row) -> dict | None:
    if row["c_id"] is None:
        return None
    return {
        "id": row["c_id"],
        "line_id": row["id"],
        "worker_id": row["c_worker_id"],
        "worker_name": row["c_worker_name"],
        "attempt": row["c_attempt"],
        "result": row["c_result"],
        "qty": row["c_qty"],
        "serials": _load_json(row["c_serials"], []),
        "quality": _load_json(row["c_quality"], {}),
        "reason": row["c_reason"],
        "photo_url": _photo_url(row["c_photo_path"]),
        "created_at": row["c_created_at"],
    }


def get_job_lines(conn: sqlite3.Connection, job_id: int, stage: str | None = None) -> list[tuple]:
    """[(line row, latest confirmation dict or None)] in position order, for one stage (None = all)."""
    where, args = "l.job_id = ?", [job_id]
    if stage is not None:
        where += " AND l.stage = ?"
        args.append(stage)
    rows = conn.execute(f"{_LINE_LATEST_SELECT} WHERE {where} ORDER BY l.stage, l.position", args).fetchall()
    return [(row, _latest_of(row)) for row in rows]


def get_line_with_latest(conn: sqlite3.Connection, line_id: int) -> tuple | None:
    row = conn.execute(_LINE_LATEST_SELECT + " WHERE l.id = ?", (line_id,)).fetchone()
    return None if row is None else (row, _latest_of(row))


def line_to_dict(row, latest: dict | None = None) -> dict:
    """JSON shape of a job line (without the reference photos, which the API adds)."""
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "stage": row["stage"],
        "position": row["position"],
        "item_id": row["item_id"],
        "photo_item_id": row["photo_item_id"],
        "label": row["label"],
        "instruction": row["instruction"],
        "qty": row["qty"],
        "photo_required": bool(row["photo_required"]),
        "serial_required": bool(row["serial_required"]),
        "quality_points": _load_json(row["quality_points"], []),
        "source": row["source"],
        "withheld": bool(row["withheld"]),
        "withheld_reason": row["withheld_reason"],
        "withheld_by": row["withheld_by"],
        "reason": row["reason"],
        "source_line_id": row["source_line_id"],
        "latest": latest,
    }


_CONFIRMATION_SELECT = (
    "SELECT c.*, w.name AS worker_name FROM confirmations c JOIN workers w ON w.id = c.worker_id"
)


def _confirmation_dict(row) -> dict:
    return {
        "id": row["id"],
        "line_id": row["job_line_id"],
        "worker_id": row["worker_id"],
        "worker_name": row["worker_name"],
        "attempt": row["attempt"],
        "result": row["result"],
        "qty": row["qty"],
        "serials": _load_json(row["serials"], []),
        "quality": _load_json(row["quality"], {}),
        "reason": row["reason"],
        "photo_url": _photo_url(row["photo_path"]),
        "created_at": row["created_at"],
    }


def get_confirmation(conn: sqlite3.Connection, confirmation_id: int) -> dict | None:
    row = conn.execute(_CONFIRMATION_SELECT + " WHERE c.id = ?", (confirmation_id,)).fetchone()
    return None if row is None else _confirmation_dict(row)


def current_attempt(conn: sqlite3.Connection, job_id: int) -> int:
    """1 + the number of rejected second checks of the job."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM checks WHERE job_id = ? AND result = 'rejected'", (job_id,)
    ).fetchone()
    return 1 + row["n"]


def open_line_ids(conn: sqlite3.Connection, job_id: int, stage: str, attempt: int) -> list[int]:
    """Ids (in position order) of the non-withheld lines of `stage` without a confirmation in `attempt`.

    Intake lines ignore the attempt: any confirmation counts.
    """
    rows = conn.execute(
        "SELECT l.id FROM job_lines l WHERE l.job_id = ? AND l.stage = ? AND l.withheld = 0"
        " AND NOT EXISTS (SELECT 1 FROM confirmations c WHERE c.job_line_id = l.id"
        "                 AND (l.stage = 'intake' OR c.attempt = ?))"
        " ORDER BY l.position",
        (job_id, stage, attempt),
    ).fetchall()
    return [r["id"] for r in rows]


def lines_blocker(job, stage: str) -> str | None:
    """'wrong_status' unless lines of `stage` can be confirmed or added in the job's current status."""
    if job["status"] not in _CONFIRMABLE[job["kind"]] or stage != current_stage(job):
        return "wrong_status"
    return None


def validate_confirmation(line, result, qty, serials, quality, reason, has_photo) -> str | None:
    """The server rules for one confirmation. Pure; returns None when fine, else the error key.

    `line` is a job_lines row (or dict with the same keys). Keys returned: error.line_withheld,
    error.bad_qty, error.reason_required, error.photo_required, error.serial_count,
    error.quality_not_ok, error.quality_incomplete, error.invalid_request.

    Quality points: when qty > 0 (ok, not_ok, partial missing) every point of the line must be
    answered 'ok' or 'not_ok'; a missing line with qty 0 needs no answers.
    """
    if line["withheld"]:
        return "error.line_withheld"
    if result not in ("ok", "missing", "not_ok"):
        return "error.invalid_request"
    if isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= line["qty"]:
        return "error.bad_qty"
    reason = reason.strip() if isinstance(reason, str) else None
    if reason is None or len(reason) > MAX_REASON:
        return "error.invalid_request"
    # ok <=> everything is there; a shortfall is 'missing' (with a reason); not_ok needs all pieces too.
    if qty < line["qty"]:
        if result != "missing":
            return "error.bad_qty"
        if len(reason) < MIN_REASON:
            return "error.reason_required"
    elif result == "missing":
        return "error.bad_qty"
    if result == "not_ok" and len(reason) < MIN_REASON:
        return "error.reason_required"

    if not isinstance(serials, list) or not all(isinstance(s, str) for s in serials):
        return "error.invalid_request"
    if any(not s.strip() or len(s) > MAX_SERIAL for s in serials):
        return "error.serial_count"
    if len(serials) > qty or (line["serial_required"] and len(serials) != qty):
        return "error.serial_count"

    if not isinstance(quality, dict):
        return "error.invalid_request"
    points = _load_json(line["quality_points"], [])
    if any(k not in points or v not in ("ok", "not_ok") for k, v in quality.items()):
        return "error.invalid_request"
    if result == "ok" and "not_ok" in quality.values():
        return "error.quality_not_ok"
    if qty > 0 and any(p not in quality for p in points):
        return "error.quality_incomplete"

    needs_photo = bool(line["photo_required"]) or line["source"] == "worker" or result == "not_ok"
    if needs_photo and not (result == "missing" and qty == 0) and not has_photo:
        return "error.photo_required"
    return None


def add_confirmation(
    conn: sqlite3.Connection,
    line_id: int,
    worker_id: int,
    attempt: int,
    result: str,
    qty: int,
    serials: list[str],
    quality: dict,
    reason: str,
    photo_path: str,
    now: str,
    commit: bool = True,
) -> int:
    """Append a confirmation of a line (the previous one stays; the highest id is the current one).

    Re-checks the job status and the server rules (validate_confirmation, has_photo = photo_path
    is set). Raises KeyError (unknown line) or ValueError with a code: wrong_status or the key of
    validate_confirmation without its 'error.' prefix.
    """
    line = get_line(conn, line_id)
    if line is None:
        raise KeyError(line_id)
    job = get_job(conn, line["job_id"])
    if lines_blocker(job, line["stage"]):
        raise ValueError("wrong_status")
    error = validate_confirmation(line, result, qty, serials, quality, reason, bool(photo_path))
    if error:
        raise ValueError(error.removeprefix("error."))
    cur = conn.execute(
        "INSERT INTO confirmations (job_line_id, worker_id, attempt, result, qty, serials, quality, reason,"
        " photo_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            line_id, worker_id, attempt, result, qty,
            json.dumps([s.strip() for s in serials], ensure_ascii=False),
            json.dumps(quality, ensure_ascii=False), reason.strip(), photo_path or "", now,
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def add_worker_line(
    conn: sqlite3.Connection,
    job_id: int,
    stage: str,
    item_id: str | None,
    label: str,
    qty: int,
    reason: str,
    now: str,
    commit: bool = True,
) -> int:
    """A part the worker found that the list does not know (source 'worker', photo required).

    Appended after the last position of the stage. Only while the job is being packed
    (delivery packing, rma intake or outgoing) and for the job's current stage. Raises KeyError
    (unknown job) or ValueError: wrong_status, reason_required, bad_qty, label_required,
    unknown_item:<id>, invalid_request.
    """
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if lines_blocker(job, stage):
        raise ValueError("wrong_status")
    extra = _clean_extra(conn, {"item_id": item_id, "label": label, "qty": qty, "reason": reason})
    position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM job_lines WHERE job_id = ? AND stage = ?",
        (job_id, stage),
    ).fetchone()["p"]
    line_id = _insert_line(
        conn,
        job_id,
        stage,
        position,
        {
            **extra,
            "source": "worker",
            "photo_required": True,
            "serial_required": False,
            "quality_points": default_quality_points(conn),
            "photo_item_id": extra["item_id"],
        },
    )
    if commit:
        conn.commit()
    return line_id


def delete_worker_line(conn: sqlite3.Connection, line_id: int, commit: bool = True) -> sqlite3.Row:
    """Remove a mistakenly added worker line (source 'worker', never confirmed, job still being packed).

    Returns the deleted line row (for the audit). Raises KeyError (unknown line) or
    ValueError('wrong_status') when the line is not worker-added, already has a confirmation,
    or the job is not in a confirmable status for the line's stage.
    """
    line = get_line(conn, line_id)
    if line is None:
        raise KeyError(line_id)
    job = get_job(conn, line["job_id"])
    if line["source"] != "worker" or lines_blocker(job, line["stage"]):
        raise ValueError("wrong_status")
    if conn.execute("SELECT 1 FROM confirmations WHERE job_line_id = ? LIMIT 1", (line_id,)).fetchone():
        raise ValueError("wrong_status")
    conn.execute("DELETE FROM job_lines WHERE id = ?", (line_id,))
    if commit:
        conn.commit()
    return line


# --- job workflow --------------------------------------------------------------------------


def claim_job(conn: sqlite3.Connection, job_id: int, worker_id: int, now: str, commit: bool = True) -> int | None:
    """The worker takes the job (a colleague's claim is overwritten).

    Delivery: open -> packing. RMA: allowed in intake, repair and outgoing; the status stays.
    Returns the previous claimed_by (worker id or None) for the audit row. Raises KeyError or
    ValueError('wrong_status') in any other status.
    """
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] == "delivery":
        if job["status"] not in ("open", "packing"):
            raise ValueError("wrong_status")
        conn.execute(
            "UPDATE jobs SET claimed_by = ?, claimed_at = ?, status = 'packing',"
            " stage_changed_at = CASE WHEN status = 'packing' THEN stage_changed_at ELSE ? END WHERE id = ?",
            (worker_id, now, now, job_id),
        )
    else:
        if job["status"] not in ("intake", "repair", "outgoing"):
            raise ValueError("wrong_status")
        conn.execute("UPDATE jobs SET claimed_by = ?, claimed_at = ? WHERE id = ?", (worker_id, now, job_id))
    if commit:
        conn.commit()
    return job["claimed_by"]


def second_check_applies(conn: sqlite3.Connection, job) -> bool:
    """Does a finished box need a second check? template.second_check, else the global default.

    For an rma this is only asked about the outgoing box (intake has no check).
    """
    row = conn.execute("SELECT second_check FROM templates WHERE id = ?", (job["template_id"],)).fetchone()
    if row is not None and row["second_check"] is not None:
        return bool(row["second_check"])
    return get_setting(conn, "second_check_default", "1") == "1"


def finish_job(conn: sqlite3.Connection, job_id: int, now: str, commit: bool = True) -> str:
    """The worker is done: delivery packing -> packed (second check) or checked; rma intake -> repair,
    rma outgoing -> packed or checked. Returns the new status.

    Raises KeyError, ValueError('wrong_status') or LinesOpen (carries the open line ids).
    """
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] == "intake" and job["kind"] == "rma":
        new_status = "repair"
    elif job["status"] == "packing" or (job["status"] == "outgoing" and job["kind"] == "rma"):
        new_status = "packed" if second_check_applies(conn, job) else "checked"
    else:
        raise ValueError("wrong_status")
    open_ids = open_line_ids(conn, job_id, current_stage(job), current_attempt(conn, job_id))
    if open_ids:
        raise LinesOpen(open_ids)
    conn.execute("UPDATE jobs SET status = ?, stage_changed_at = ? WHERE id = ?", (new_status, now, job_id))
    if commit:
        conn.commit()
    return new_status


def ship_job(conn: sqlite3.Connection, job_id: int, now: str, commit: bool = True) -> None:
    """checked -> shipped. KeyError, or ValueError('wrong_status') from any other status."""
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] != "checked":
        raise ValueError("wrong_status")
    conn.execute("UPDATE jobs SET status = 'shipped', stage_changed_at = ? WHERE id = ?", (now, job_id))
    if commit:
        conn.commit()


def current_confirmers(conn: sqlite3.Connection, job) -> list[sqlite3.Row]:
    """Workers (id, name) who confirmed lines of the current stage in the current attempt, first one first."""
    return conn.execute(
        "SELECT w.id AS id, w.name AS name, MIN(c.id) AS first_id FROM confirmations c"
        " JOIN job_lines l ON l.id = c.job_line_id JOIN workers w ON w.id = c.worker_id"
        " WHERE l.job_id = ? AND l.stage = ? AND c.attempt = ? GROUP BY w.id ORDER BY first_id",
        (job["id"], current_stage(job), job["attempt"]),
    ).fetchall()


def packer_names(conn: sqlite3.Connection, job) -> list[str]:
    """Distinct names of the workers who packed the current attempt ("gepackt von")."""
    return [r["name"] for r in current_confirmers(conn, job)]


def check_blocker(
    conn: sqlite3.Connection, job_id: int, worker_id: int, result: str, note: str, has_photo: bool
) -> str | None:
    """The rule check of a second check, no write. Returns None or an error code.

    Codes: wrong_status (job not packed), same_worker (the checker packed the current attempt),
    invalid_request (unknown result), photo_required (ok without box photo), reason_required (rejected
    with a note under 3 characters). Raises KeyError for an unknown job.
    """
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] != "packed":
        return "wrong_status"
    if any(r["id"] == worker_id for r in current_confirmers(conn, job)):
        return "same_worker"
    if result not in ("ok", "rejected"):
        return "invalid_request"
    if result == "ok" and not has_photo:
        return "photo_required"
    if result == "rejected":
        note = (note or "").strip()
        if len(note) < MIN_REASON:
            return "reason_required"
        if len(note) > MAX_REASON:
            return "invalid_request"
    return None


def add_check(
    conn: sqlite3.Connection,
    job_id: int,
    worker_id: int,
    result: str,
    note: str,
    photo_path: str,
    now: str,
    commit: bool = True,
) -> int:
    """Second check of a packed box (append-only row in `checks`). Returns the check id.

    ok -> status checked (box photo required). rejected -> back to packing (delivery) or outgoing
    (rma), stage_changed_at = now; the confirmations stay, current_attempt() goes up by one so all
    non-withheld lines are open again. Raises KeyError or ValueError with a code of check_blocker.
    """
    error = check_blocker(conn, job_id, worker_id, result, note, bool(photo_path))
    if error:
        raise ValueError(error)
    job = get_job(conn, job_id)
    cur = conn.execute(
        "INSERT INTO checks (job_id, worker_id, result, note, photo_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, worker_id, result, (note or "").strip(), photo_path or "", now),
    )
    if result == "ok":
        new_status = "checked"
    else:
        new_status = "packing" if job["kind"] == "delivery" else "outgoing"
    conn.execute("UPDATE jobs SET status = ?, stage_changed_at = ? WHERE id = ?", (new_status, now, job_id))
    if commit:
        conn.commit()
    return cur.lastrowid


def _check_dict(row) -> dict:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "worker_id": row["worker_id"],
        "worker_name": row["worker_name"],
        "result": row["result"],
        "note": row["note"],
        "photo_url": _photo_url(row["photo_path"]),
        "created_at": row["created_at"],
        "attempt": row["attempt"],
    }


_CHECK_SELECT = (
    "SELECT k.*, w.name AS worker_name,"
    " 1 + (SELECT COUNT(*) FROM checks r WHERE r.job_id = k.job_id AND r.result = 'rejected' AND r.id < k.id)"
    " AS attempt FROM checks k JOIN workers w ON w.id = k.worker_id"
)


def get_check(conn: sqlite3.Connection, check_id: int) -> dict | None:
    row = conn.execute(_CHECK_SELECT + " WHERE k.id = ?", (check_id,)).fetchone()
    return None if row is None else _check_dict(row)


def list_checks(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """All second checks of the job, newest first. `attempt` = the packing attempt the check judged."""
    rows = conn.execute(_CHECK_SELECT + " WHERE k.job_id = ? ORDER BY k.id DESC", (job_id,)).fetchall()
    return [_check_dict(r) for r in rows]


def label_data(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """What the box label prints, or None for an unknown job.

    checked_by / checked_at come from the approving check and only while the job is checked or shipped
    (an expert reopening wipes the claim); None means "ohne Prüfung" (second check off).
    """
    job = get_job(conn, job_id)
    if job is None:
        return None
    checked_by = checked_at = None
    if job["status"] in ("checked", "shipped"):
        row = conn.execute(_CHECK_SELECT + " WHERE k.job_id = ? AND k.result = 'ok' ORDER BY k.id DESC LIMIT 1",
                           (job_id,)).fetchone()
        if row is not None:
            checked_by, checked_at = row["worker_name"], row["created_at"]
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "order_ref": job["order_ref"],
        "customer": job["customer"],
        "template_name": job["template_name"],
        "unit_no": job["unit_no"],
        "unit_count": job["unit_count"],
        "packed_by": packer_names(conn, job),
        "checked_by": checked_by,
        "checked_at": checked_at,
        "shipped_at": job["stage_changed_at"] if job["status"] == "shipped" else None,
    }


def has_outgoing_confirmation(conn: sqlite3.Connection, job_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM confirmations c JOIN job_lines l ON l.id = c.job_line_id"
        " WHERE l.job_id = ? AND l.stage = 'outgoing' LIMIT 1",
        (job_id,),
    ).fetchone() is not None


def set_job_status(
    conn: sqlite3.Connection, job_id: int, status: str, note: str, now: str, commit: bool = True
) -> str:
    """Expert override of the status. Returns the old status.

    Allowed: packed|checked|shipped -> packing (delivery) / outgoing (rma), cancelled -> open
    (delivery) / intake (rma), any status except shipped and cancelled -> cancelled, rma outgoing ->
    repair when no outgoing line was confirmed yet. Going back to repair (or reopening a cancelled
    rma to intake) deletes the generated, still unconfirmed outgoing lines so generate_outgoing can
    run again; a cancelled rma with confirmed outgoing lines cannot be reopened. A non-empty note is
    appended to the job note.
    Raises KeyError, ValueError('outgoing_confirmed') or ValueError('bad_transition').
    """
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    old, kind = job["status"], job["kind"]
    done = ("packed", "checked", "shipped")
    if status == "cancelled":
        allowed = old not in ("shipped", "cancelled")
    elif status == "packing":
        allowed = kind == "delivery" and old in done
    elif status == "outgoing":
        allowed = kind == "rma" and old in done
    elif status == "open":
        allowed = kind == "delivery" and old == "cancelled"
    elif status == "intake":
        allowed = kind == "rma" and old == "cancelled"
        if allowed and has_outgoing_confirmation(conn, job_id):
            allowed = False
    elif status == "repair":
        allowed = kind == "rma" and old == "outgoing"
        if allowed and has_outgoing_confirmation(conn, job_id):
            raise ValueError("outgoing_confirmed")
    else:
        allowed = False
    if not allowed:
        raise ValueError("bad_transition")
    if kind == "rma" and status in ("repair", "intake"):
        conn.execute("DELETE FROM job_lines WHERE job_id = ? AND stage = 'outgoing'", (job_id,))
    note = (note or "").strip()
    new_note = (job["note"] + "\n" + note).strip() if note else job["note"]
    conn.execute(
        "UPDATE jobs SET status = ?, stage_changed_at = ?, note = ? WHERE id = ?", (status, now, new_note, job_id)
    )
    if commit:
        conn.commit()
    return old


# --- rma: repair notes, outgoing generation, balance ----------------------------------------

REPAIR_ACTIONS = ("replaced", "kept", "added")
# Statuses of an rma that is still in the house (not shipped, not cancelled).
OPEN_RMA_STATUSES = ("intake", "repair", "outgoing", "packed", "checked")


def _confirmed_qty(conn: sqlite3.Connection, line_id: int) -> int:
    """Qty of the latest confirmation of a line (0 when it has none)."""
    row = conn.execute(
        "SELECT qty FROM confirmations WHERE job_line_id = ? ORDER BY id DESC LIMIT 1", (line_id,)
    ).fetchone()
    return row["qty"] if row else 0


def _note_dict(row) -> dict:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "action": row["action"],
        "job_line_id": row["job_line_id"],
        "line_label": row["line_label"],
        "item_id": row["item_id"],
        "label": row["label"],
        "qty": row["qty"],
        "serial": row["serial"],
        "reason": row["reason"],
        "worker_id": row["worker_id"],
        "worker_name": row["worker_name"],
        "created_at": row["created_at"],
    }


_NOTE_SELECT = (
    "SELECT n.*, l.label AS line_label, w.name AS worker_name FROM repair_notes n"
    " LEFT JOIN job_lines l ON l.id = n.job_line_id JOIN workers w ON w.id = n.worker_id"
)


def get_repair_note(conn: sqlite3.Connection, note_id: int) -> dict | None:
    row = conn.execute(_NOTE_SELECT + " WHERE n.id = ?", (note_id,)).fetchone()
    return None if row is None else _note_dict(row)


def list_repair_notes(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """The repair notes of a job, oldest first, each with the label of its intake line (line_label)."""
    rows = conn.execute(_NOTE_SELECT + " WHERE n.job_id = ? ORDER BY n.id", (job_id,)).fetchall()
    return [_note_dict(r) for r in rows]


def add_repair_note(
    conn: sqlite3.Connection,
    job_id: int,
    worker_id: int,
    action: str,
    job_line_id: int | None,
    item_id: str | None,
    label: str,
    qty: int,
    serial: str,
    reason: str,
    now: str,
    commit: bool = True,
) -> int:
    """Record what happened to a part during repair (rma status 'repair'). Returns the note id.

    replaced / kept refer to an intake line of this job with a confirmed qty > 0: qty 1..confirmed;
    kept needs a reason (customer agreement) and Σ kept qty per line must stay <= confirmed qty;
    replaced keeps the qty in the outgoing list (reason optional). item_id / label default to those
    of the intake line. added (a part that was not in the box, no line) needs label or item_id,
    qty 1..999 and a reason. serial is optional (<= 64 characters).

    Raises KeyError (unknown job) or ValueError with a code: wrong_status, invalid_request, bad_line,
    bad_qty, reason_required, label_required, unknown_item:<id>.
    """
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if job["kind"] != "rma" or job["status"] != "repair":
        raise ValueError("wrong_status")
    if action not in REPAIR_ACTIONS:
        raise ValueError("invalid_request")
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise ValueError("bad_qty")
    serial = serial.strip() if isinstance(serial, str) else None
    reason = reason.strip() if isinstance(reason, str) else None
    label = label.strip() if isinstance(label, str) else None
    item_id = item_id.strip() if isinstance(item_id, str) else item_id
    item_id = item_id or None
    if (
        serial is None or len(serial) > MAX_SERIAL or reason is None or len(reason) > MAX_REASON
        or label is None or len(label) > MAX_NAME or (item_id is not None and (not isinstance(item_id, str)
                                                                             or len(item_id) > 80))
    ):
        raise ValueError("invalid_request")
    item_name = None
    if item_id is not None:
        item_name = item_names(conn, [item_id]).get(item_id)
        if item_name is None:
            raise ValueError(f"unknown_item:{item_id}")

    if action == "added":
        if job_line_id is not None:
            raise ValueError("bad_line")
        if not 1 <= qty <= MAX_QTY:
            raise ValueError("bad_qty")
        if len(reason) < MIN_REASON:
            raise ValueError("reason_required")
        label = label or (item_name or "").strip()[:MAX_NAME]
        if not label:
            raise ValueError("label_required")
    else:
        line = None
        if isinstance(job_line_id, int) and not isinstance(job_line_id, bool):
            line = get_line(conn, job_line_id)
        if line is None or line["job_id"] != job_id or line["stage"] != "intake" or line["withheld"]:
            raise ValueError("bad_line")
        confirmed = _confirmed_qty(conn, job_line_id)
        if confirmed < 1:
            raise ValueError("bad_line")
        if not 1 <= qty <= confirmed:
            raise ValueError("bad_qty")
        if action == "kept":
            if len(reason) < MIN_REASON:
                raise ValueError("reason_required")
            kept = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) AS n FROM repair_notes WHERE job_line_id = ? AND action = 'kept'",
                (job_line_id,),
            ).fetchone()["n"]
            if kept + qty > confirmed:
                raise ValueError("bad_qty")
        if item_id is None:
            item_id = line["item_id"]
        label = label or (item_name or "").strip()[:MAX_NAME] or line["label"]
    cur = conn.execute(
        "INSERT INTO repair_notes (job_id, job_line_id, action, item_id, label, qty, serial, reason, worker_id,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, job_line_id, action, item_id, label, qty, serial, reason, worker_id, now),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def delete_repair_note(conn: sqlite3.Connection, note_id: int, worker_id: int, commit: bool = True) -> dict:
    """Remove a repair note; only its author, and only while the rma is in 'repair'. Returns the note.

    Raises KeyError (unknown note) or ValueError: not_author, wrong_status.
    """
    note = get_repair_note(conn, note_id)
    if note is None:
        raise KeyError(note_id)
    if note["worker_id"] != worker_id:
        raise ValueError("not_author")
    if get_job(conn, note["job_id"])["status"] != "repair":
        raise ValueError("wrong_status")
    conn.execute("DELETE FROM repair_notes WHERE id = ?", (note_id,))
    if commit:
        conn.commit()
    return note


def generate_outgoing(conn: sqlite3.Connection, job_id: int, now: str, commit: bool = True) -> list[int]:
    """Build the outgoing list of an rma in 'repair' ("what came in goes out") and move it to 'outgoing'.

    For every intake line with a latest confirmed qty > 0: out_qty = qty - Σ kept qty; if > 0 an
    outgoing line (source_line_id = the intake line; source, item, label, instruction, photo /
    serial flags and quality points copied; reason = the reasons of its 'replaced' notes as
    "ersetzt: <reason>"). Withheld lines and lines that did not arrive give nothing. Every 'added'
    note becomes an outgoing line with source 'worker', the note's reason, photo required and the
    default quality points. Positions 1..n: intake order, then added notes. Returns the new line ids.

    One-shot. Raises KeyError or ValueError: outgoing_exists (outgoing lines exist already),
    wrong_status (not rma / not 'repair'), outgoing_empty (nothing to send back).
    """
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if conn.execute("SELECT 1 FROM job_lines WHERE job_id = ? AND stage = 'outgoing' LIMIT 1", (job_id,)).fetchone():
        raise ValueError("outgoing_exists")
    if job["kind"] != "rma" or job["status"] != "repair":
        raise ValueError("wrong_status")
    notes = list_repair_notes(conn, job_id)
    out_lines: list[dict] = []
    for row, latest in get_job_lines(conn, job_id, "intake"):
        confirmed = latest["qty"] if latest else 0
        if row["withheld"] or confirmed < 1:
            continue
        mine = [n for n in notes if n["job_line_id"] == row["id"]]
        out_qty = confirmed - sum(n["qty"] for n in mine if n["action"] == "kept")
        if out_qty < 1:
            continue
        replaced = [f"ersetzt: {n['reason']}" if n["reason"] else "ersetzt" for n in mine if n["action"] == "replaced"]
        out_lines.append(
            {
                "item_id": row["item_id"],
                "photo_item_id": row["photo_item_id"],
                "label": row["label"],
                "instruction": row["instruction"],
                "qty": out_qty,
                "photo_required": bool(row["photo_required"]),
                "serial_required": bool(row["serial_required"]),
                "quality_points": _load_json(row["quality_points"], []),
                "source": row["source"],
                "reason": "; ".join(replaced),
                "source_line_id": row["id"],
            }
        )
    default_points = default_quality_points(conn)
    for note in notes:
        if note["action"] == "added":
            out_lines.append(
                {
                    "item_id": note["item_id"],
                    "photo_item_id": note["item_id"],
                    "label": note["label"],
                    "qty": note["qty"],
                    "photo_required": True,
                    "serial_required": False,
                    "quality_points": list(default_points),
                    "source": "worker",
                    "reason": note["reason"],
                }
            )
    if not out_lines:
        raise ValueError("outgoing_empty")
    ids = [_insert_line(conn, job_id, "outgoing", position, line) for position, line in enumerate(out_lines, 1)]
    conn.execute("UPDATE jobs SET status = 'outgoing', stage_changed_at = ? WHERE id = ?", (now, job_id))
    if commit:
        conn.commit()
    return ids


_BALANCE_SELECT = """
    SELECT l.id AS intake_line_id, l.item_id, l.label,
           (SELECT c.qty FROM confirmations c WHERE c.job_line_id = l.id ORDER BY c.id DESC LIMIT 1) AS in_qty,
           (SELECT COALESCE(SUM(n.qty), 0) FROM repair_notes n
             WHERE n.job_line_id = l.id AND n.action = 'kept') AS kept_qty,
           (SELECT COALESCE(SUM(COALESCE((SELECT c2.qty FROM confirmations c2 WHERE c2.job_line_id = o.id
                                           ORDER BY c2.id DESC LIMIT 1), 0)), 0)
              FROM job_lines o WHERE o.source_line_id = l.id AND o.stage = 'outgoing') AS out_qty
    FROM job_lines l WHERE l.job_id = ? AND l.stage = 'intake' AND l.withheld = 0 ORDER BY l.position
"""


def rma_balance(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """Per intake line that arrived (confirmed qty > 0): what came in, what stays, what went out.

    [{intake_line_id, item_id, label, in_qty, kept_qty, out_qty, delta}]; out_qty = latest confirmed
    qty of the outgoing line(s) made from it (0 while there is none or it is unconfirmed);
    delta = in_qty - kept_qty - out_qty. delta > 0 means "in the house, not returned (yet)".
    """
    rows = conn.execute(_BALANCE_SELECT, (job_id,)).fetchall()
    return [
        {
            "intake_line_id": r["intake_line_id"],
            "item_id": r["item_id"],
            "label": r["label"],
            "in_qty": r["in_qty"],
            "kept_qty": r["kept_qty"],
            "out_qty": r["out_qty"],
            "delta": r["in_qty"] - r["kept_qty"] - r["out_qty"],
        }
        for r in rows
        if (r["in_qty"] or 0) > 0
    ]


def open_rma_balances(conn: sqlite3.Connection, now: str | None = None) -> list[dict]:
    """[{job: job dict, lines: rma_balance}] of every rma not shipped or cancelled, oldest first.

    With `now`, each job also carries days_open (whole days since it was created).
    """
    out = []
    for job in list_jobs(conn, OPEN_RMA_STATUSES, kind="rma"):
        data = job_to_dict(job)
        if now is not None:
            data["days_open"] = days_open(job["created_at"], now)
        out.append({"job": data, "lines": rma_balance(conn, job["id"])})
    return out


# --- reports: history and export ------------------------------------------------------------


def _audit_actor_names(conn: sqlite3.Connection, actors) -> dict[str, str]:
    """{'worker:<id>': name} for the worker actors in `actors` (unknown or non-worker actors are left out)."""
    ids = {a.split(":", 1)[1] for a in actors if a.startswith("worker:") and a.split(":", 1)[1].isdigit()}
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT id, name FROM workers WHERE id IN ({','.join('?' * len(ids))})", [int(i) for i in ids]
    ).fetchall()
    return {f"worker:{r['id']}": r["name"] for r in rows}


def job_history(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Everything recorded about one job, or None for an unknown job.

    {job, lines: [{line, confirmations: [all, oldest first]}] (all stages, intake before outgoing),
     repair_notes (oldest first), checks (oldest first), audit: [{at, actor, actor_name, action, detail}]}.
    Superseded confirmations (earlier attempts, corrections) are included: the table is append-only.
    """
    job = get_report_job(conn, job_id)
    if job is None:
        return None
    by_line: dict[int, list[dict]] = {}
    for row in conn.execute(
        _CONFIRMATION_SELECT + " JOIN job_lines l ON l.id = c.job_line_id WHERE l.job_id = ? ORDER BY c.id",
        (job_id,),
    ).fetchall():
        by_line.setdefault(row["job_line_id"], []).append(_confirmation_dict(row))
    lines = [
        {"line": line_to_dict(row, latest), "confirmations": by_line.get(row["id"], [])}
        for row, latest in get_job_lines(conn, job_id)
    ]
    audit_rows = conn.execute(
        "SELECT * FROM audit WHERE entity = 'job' AND entity_id = ? ORDER BY id", (str(job_id),)
    ).fetchall()
    names = _audit_actor_names(conn, {r["actor"] for r in audit_rows})
    return {
        "job": job_report_dict(job),
        "lines": lines,
        "repair_notes": list_repair_notes(conn, job_id),
        "checks": list(reversed(list_checks(conn, job_id))),
        "audit": [
            {
                "at": r["at"],
                "actor": r["actor"],
                "actor_name": names.get(r["actor"]),
                "action": r["action"],
                "detail": _load_json(r["detail"], {}),
            }
            for r in audit_rows
        ],
    }


_EXPORT_JOB_COLUMNS = (
    "j.id AS job_id, j.kind AS job_kind, j.status AS job_status, j.order_ref, j.customer, j.template_name,"
    " j.unit_no, j.unit_count"
)


def export_events(
    conn: sqlite3.Connection, created_from: str, created_before: str, kind: str | None = None
) -> list[dict]:
    """Every confirmation, second check and repair note with created_at in [created_from, created_before).

    Oldest first. Each dict has `event` ('confirmation' | 'check' | 'note'), the job columns (job_id,
    job_kind, job_status, order_ref, customer, template_name, unit_no, unit_count) and the event row
    (confirmation: line stage/position/item_id/label/line_qty, worker_name, attempt, result, qty, serials,
    quality, reason, photo_path; check: result, note, attempt; note: action, label, qty, serial, reason).
    """
    kind_sql, kind_args = (" AND j.kind = ?", [kind]) if kind else ("", [])
    window = " AND {t}.created_at >= ? AND {t}.created_at < ?"
    args = [created_from, created_before] + kind_args
    events: list[dict] = []
    for row in conn.execute(
        f"SELECT c.*, w.name AS worker_name, l.stage AS line_stage, l.position AS line_position,"
        f" l.item_id AS line_item_id, l.label AS line_label, l.qty AS line_qty, {_EXPORT_JOB_COLUMNS}"
        " FROM confirmations c JOIN job_lines l ON l.id = c.job_line_id JOIN jobs j ON j.id = l.job_id"
        " JOIN workers w ON w.id = c.worker_id WHERE 1 = 1" + window.format(t="c") + kind_sql,
        args,
    ):
        events.append({"event": "confirmation", **dict(row)})
    for row in conn.execute(
        f"SELECT k.*, w.name AS worker_name,"
        " 1 + (SELECT COUNT(*) FROM checks r WHERE r.job_id = k.job_id AND r.result = 'rejected' AND r.id < k.id)"
        f" AS attempt, {_EXPORT_JOB_COLUMNS}"
        " FROM checks k JOIN jobs j ON j.id = k.job_id JOIN workers w ON w.id = k.worker_id"
        " WHERE 1 = 1" + window.format(t="k") + kind_sql,
        args,
    ):
        events.append({"event": "check", **dict(row)})
    for row in conn.execute(
        f"SELECT n.*, w.name AS worker_name, l.stage AS line_stage, l.position AS line_position,"
        f" l.item_id AS line_item_id, l.label AS line_label, l.qty AS line_qty, {_EXPORT_JOB_COLUMNS}"
        " FROM repair_notes n JOIN jobs j ON j.id = n.job_id JOIN workers w ON w.id = n.worker_id"
        " LEFT JOIN job_lines l ON l.id = n.job_line_id WHERE 1 = 1" + window.format(t="n") + kind_sql,
        args,
    ):
        events.append({"event": "note", **dict(row)})
    rank = {"confirmation": 0, "check": 1, "note": 2}
    events.sort(key=lambda e: (e["created_at"], rank[e["event"]], e["id"]))
    return events
