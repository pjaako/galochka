import sqlite3


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
