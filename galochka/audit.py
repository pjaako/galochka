import json
import sqlite3


def audit(
    conn: sqlite3.Connection,
    now: str,
    actor: str,
    action: str,
    entity: str,
    entity_id: int | str,
    detail: dict | None = None,
) -> int:
    """Append one row to the audit trail and commit. Returns the audit row id.

    actor is 'expert' or 'worker:<id>' (or 'system'); detail is stored as JSON
    text. The caller holds app.state.lock.
    """
    cur = conn.execute(
        "INSERT INTO audit (at, actor, action, entity, entity_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (now, actor, action, entity, str(entity_id), json.dumps(detail or {}, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid
