import csv
import io
import sqlite3
from dataclasses import dataclass

from galochka import repo
from galochka.cassandra_client import CassandraClient

REQUIRED_COLUMNS = ("Part No.", "Part description")


class ImportError_(ValueError):
    pass


@dataclass
class ImportRow:
    id: str
    name: str
    group_name: str
    location: str
    version: str
    drawing_revision: str


def parse_csv(data: bytes) -> list[ImportRow]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ImportError_(
            "CSV must be UTF-8 (export from MRPeasy, do not re-save in Excel)"
        ) from e
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ImportError_(f"missing required columns: {', '.join(missing)}")

        rows = []
        for row in reader:
            item_id = (row.get("Part No.") or "").strip()
            if not item_id:
                continue
            rows.append(
                ImportRow(
                    id=item_id,
                    name=(row.get("Part description") or "").strip(),
                    group_name=(row.get("Group name") or "").strip(),
                    location=(row.get("Default storage location (Main site)") or "").strip(),
                    version=(row.get("Parameters") or "").strip(),
                    drawing_revision=(row.get("Revision") or "").strip(),
                )
            )
    except csv.Error as e:
        raise ImportError_(f"unreadable CSV: {e}") from e
    return rows


def _version_sort_key(version: str):
    """Ascending order: numeric versions first (by value), non-numeric last (by string)."""
    try:
        return (0, float(version))
    except ValueError:
        return (1, version)


def _representative_version_key(version: str):
    """For picking the representative row: numeric versions outrank non-numeric ones."""
    try:
        return (1, float(version))
    except ValueError:
        return (0, version)


def _pick_representative(rows: list[ImportRow]) -> ImportRow:
    best = rows[0]
    best_key = _representative_version_key(best.version)
    for row in rows[1:]:
        key = _representative_version_key(row.version)
        if key >= best_key:
            best = row
            best_key = key
    return best


def _upsert_item(conn, existing, item_id, name, group_name, location, versions, drawing_revision, now, source) -> str:
    """Insert or update one item (no commit). Returns 'added', 'updated' or 'same'."""
    prev = existing.get(item_id)
    if prev is None:
        outcome = "added"
    elif (
        prev["name"] != name
        or prev["group_name"] != group_name
        or prev["location"] != location
        or prev["versions"] != versions
        or prev["drawing_revision"] != drawing_revision
    ):
        outcome = "updated"
    else:
        outcome = "same"

    conn.execute(
        """
        INSERT INTO items (id, name, group_name, location, versions, drawing_revision, active, updated_at, source)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            group_name=excluded.group_name,
            location=excluded.location,
            versions=excluded.versions,
            drawing_revision=excluded.drawing_revision,
            active=1,
            updated_at=excluded.updated_at,
            source=excluded.source
        """,
        (item_id, name, group_name, location, versions, drawing_revision, now, source),
    )
    return outcome


def _deactivate_missing(conn, keep_ids: set[str], now: str) -> int:
    """Deactivate active items not in keep_ids (no commit). Rows are never deleted."""
    stale = [
        row["id"] for row in conn.execute("SELECT id FROM items WHERE active=1") if row["id"] not in keep_ids
    ]
    conn.executemany("UPDATE items SET active=0, updated_at=? WHERE id=?", [(now, i) for i in stale])
    return len(stale)


def _total_active(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]


def import_items(conn: sqlite3.Connection, data: bytes, now: str) -> dict:
    """MRPeasy CSV import (fallback when Cassandra is not available).

    Items missing from the file are deactivated. The caller holds app.state.lock.
    """
    rows = parse_csv(data)
    if not rows:
        raise ImportError_("CSV contains no rows")

    grouped: dict[str, list[ImportRow]] = {}
    for row in rows:
        grouped.setdefault(row.id, []).append(row)

    existing = {row["id"]: row for row in conn.execute("SELECT * FROM items")}
    counts = {"added": 0, "updated": 0, "same": 0}
    try:
        for item_id, item_rows in grouped.items():
            versions = ", ".join(
                sorted({r.version for r in item_rows if r.version}, key=_version_sort_key)
            )
            rep = _pick_representative(item_rows)
            outcome = _upsert_item(
                conn, existing, item_id, rep.name, rep.group_name, rep.location,
                versions, rep.drawing_revision, now, "csv",
            )
            counts[outcome] += 1
        deactivated = _deactivate_missing(conn, set(grouped), now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "added": counts["added"],
        "updated": counts["updated"],
        "deactivated": deactivated,
        "total_active": _total_active(conn),
    }


def fetch_catalog(client: CassandraClient) -> list[dict]:
    """Collect every item from Cassandra. Network only: call it outside the lock.

    Raises CassandraUnavailable (also when the client is not configured).
    """
    return list(client.iter_all_items())


def apply_catalog(conn: sqlite3.Connection, items: list[dict], now: str) -> dict:
    """Upsert a fetched catalog (source='cassandra') and deactivate items it no longer has.

    An empty catalog changes nothing, not even items_synced_at: an empty answer from
    Cassandra must never wipe the local catalog. The caller holds app.state.lock.
    """
    existing = {row["id"]: row for row in conn.execute("SELECT * FROM items")}
    counts = {"added": 0, "updated": 0, "same": 0}
    seen: set[str] = set()
    deactivated = 0
    try:
        for item in items:
            item_id = str(item.get("item_id") or "").strip()
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            outcome = _upsert_item(
                conn,
                existing,
                item_id,
                str(item.get("name") or "").strip(),
                str(item.get("group_name") or "").strip(),
                str(item.get("location") or "").strip(),
                str(item.get("versions") or "").strip(),
                str(item.get("drawing_revision") or "").strip(),
                now,
                "cassandra",
            )
            counts[outcome] += 1
        if seen:
            deactivated = _deactivate_missing(conn, seen, now)
            repo.set_setting(conn, "items_synced_at", now, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "added": counts["added"],
        "updated": counts["updated"],
        "deactivated": deactivated,
        "total_active": _total_active(conn),
        "synced_at": repo.get_setting(conn, "items_synced_at", "") or None,
    }


def sync_items(conn: sqlite3.Connection, client: CassandraClient, now: str) -> dict:
    """fetch_catalog + apply_catalog in one call (no lock handling: for tests and scripts).

    The API layer calls the two halves separately so the HTTP part stays outside the lock.
    Raises CassandraUnavailable.
    """
    return apply_catalog(conn, fetch_catalog(client), now)
