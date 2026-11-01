import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    migrations_dir = Path(__file__).parent / "migrations"

    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        script = f"BEGIN;\n{sql}\nINSERT INTO schema_migrations(name) VALUES ('{path.name}');\nCOMMIT;"
        try:
            conn.executescript(script)
        except Exception:
            conn.rollback()
            raise
