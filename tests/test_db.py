import json
import sqlite3

import pytest

from galochka import audit, db, repo

TABLES = {
    "items",
    "cassandra_cache",
    "workers",
    "badges",
    "sessions",
    "templates",
    "template_lines",
    "jobs",
    "job_lines",
    "confirmations",
    "repair_notes",
    "checks",
    "settings",
    "audit",
    "schema_migrations",
}

NOW = "2026-11-01T00:00:00+00:00"


def _tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _worker(conn, name="Anna"):
    return conn.execute("INSERT INTO workers (name, created_at) VALUES (?, ?)", (name, NOW)).lastrowid


def _template(conn, name="Set A"):
    return conn.execute(
        "INSERT INTO templates (name, created_at, updated_at) VALUES (?, ?, ?)", (name, NOW, NOW)
    ).lastrowid


def _job(conn, template_id, kind="delivery", status="open", order_ref="A-1"):
    return conn.execute(
        "INSERT INTO jobs (kind, template_id, order_ref, status, created_by, created_at, stage_changed_at) "
        "VALUES (?, ?, ?, ?, 'expert', ?, ?)",
        (kind, template_id, order_ref, status, NOW, NOW),
    ).lastrowid


def _line(conn, job_id, position=1, stage="pack", source="template"):
    return conn.execute(
        "INSERT INTO job_lines (job_id, stage, position, label, source) VALUES (?, ?, ?, 'x', ?)",
        (job_id, stage, position, source),
    ).lastrowid


def test_migrate_creates_all_tables(conn):
    assert TABLES <= _tables(conn)


def test_migrate_twice_is_a_noop(conn):
    before = [tuple(r) for r in conn.execute("SELECT * FROM schema_migrations")]
    db.migrate(conn)
    after = [tuple(r) for r in conn.execute("SELECT * FROM schema_migrations")]
    assert before == after == [("001_init.sql",)]
    # seed rows were not duplicated
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 4


def test_foreign_keys_pragma_on(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_seed_settings(conn):
    assert repo.get_setting(conn, "second_check_default") == "1"
    assert repo.get_setting(conn, "session_minutes") == "30"
    assert json.loads(repo.get_setting(conn, "default_quality_points")) == ["sauber", "unbeschädigt", "funktion"]
    assert repo.get_setting(conn, "items_synced_at") == ""
    assert repo.get_setting(conn, "nope") is None
    assert repo.get_setting(conn, "nope", "dflt") == "dflt"


def test_set_setting_roundtrip_and_rollback(conn):
    repo.set_setting(conn, "session_minutes", "45")
    assert repo.get_setting(conn, "session_minutes") == "45"
    repo.set_setting(conn, "session_minutes", "50", commit=False)
    conn.rollback()
    assert repo.get_setting(conn, "session_minutes") == "45"


def test_items_source_check(conn):
    conn.execute("INSERT INTO items (id, name, updated_at, source) VALUES ('A1', 'x', ?, 'csv')", (NOW,))
    # default source
    conn.execute("INSERT INTO items (id, name, updated_at) VALUES ('A2', 'x', ?)", (NOW,))
    assert conn.execute("SELECT source FROM items WHERE id='A2'").fetchone()[0] == "cassandra"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO items (id, name, updated_at, source) VALUES ('A3', 'x', ?, 'other')", (NOW,))


def test_jobs_status_and_kind_checks(conn):
    tid = _template(conn)
    for status in ("open", "packing", "packed", "checked", "shipped", "cancelled", "intake", "repair", "outgoing"):
        _job(conn, tid, status=status, order_ref=f"o-{status}")
    with pytest.raises(sqlite3.IntegrityError):
        _job(conn, tid, status="bogus")
    with pytest.raises(sqlite3.IntegrityError):
        _job(conn, tid, kind="other")


def test_jobs_rma_order_ref_is_unique_only_for_rma(conn):
    tid = _template(conn)
    _job(conn, tid, kind="delivery", order_ref="X")
    _job(conn, tid, kind="delivery", order_ref="X")  # multi-unit orders share order_ref
    _job(conn, tid, kind="rma", status="intake", order_ref="X")  # not blocked by the delivery rows
    with pytest.raises(sqlite3.IntegrityError):
        _job(conn, tid, kind="rma", status="intake", order_ref="X")


def test_template_cannot_be_deleted_while_jobs_exist(conn):
    tid = _template(conn)
    _job(conn, tid)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM templates WHERE id=?", (tid,))


def test_job_lines_checks_and_unique_position(conn):
    tid = _template(conn)
    jid = _job(conn, tid)
    _line(conn, jid, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _line(conn, jid, 1)  # same (job, stage, position)
    _line(conn, jid, 1, stage="outgoing")  # other stage is fine
    with pytest.raises(sqlite3.IntegrityError):
        _line(conn, jid, 2, stage="bogus")
    with pytest.raises(sqlite3.IntegrityError):
        _line(conn, jid, 3, source="bogus")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE job_lines SET withheld=2 WHERE job_id=?", (jid,))


def test_template_lines_unique_position_and_cascade(conn):
    tid = _template(conn)
    conn.execute("INSERT INTO template_lines (template_id, position, label) VALUES (?, 1, 'a')", (tid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO template_lines (template_id, position, label) VALUES (?, 1, 'b')", (tid,))
    conn.execute("DELETE FROM templates WHERE id=?", (tid,))
    assert conn.execute("SELECT COUNT(*) FROM template_lines").fetchone()[0] == 0


def test_templates_second_check_values(conn):
    for value in (None, 0, 1):
        conn.execute(
            "INSERT INTO templates (name, second_check, created_at, updated_at) VALUES ('t', ?, ?, ?)",
            (value, NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO templates (name, second_check, created_at, updated_at) VALUES ('t', 2, ?, ?)", (NOW, NOW)
        )


def test_confirmations_result_check(conn):
    wid = _worker(conn)
    line = _line(conn, _job(conn, _template(conn)))
    for result in ("ok", "missing", "not_ok"):
        conn.execute(
            "INSERT INTO confirmations (job_line_id, worker_id, result, created_at) VALUES (?, ?, ?, ?)",
            (line, wid, result, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO confirmations (job_line_id, worker_id, result, created_at) VALUES (?, ?, 'maybe', ?)",
            (line, wid, NOW),
        )


def test_confirmations_cascade_with_job(conn):
    wid = _worker(conn)
    jid = _job(conn, _template(conn))
    line = _line(conn, jid)
    conn.execute(
        "INSERT INTO confirmations (job_line_id, worker_id, result, created_at) VALUES (?, ?, 'ok', ?)",
        (line, wid, NOW),
    )
    conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
    assert conn.execute("SELECT COUNT(*) FROM job_lines").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_repair_notes_action_and_line_rule(conn):
    wid = _worker(conn)
    jid = _job(conn, _template(conn), kind="rma", status="repair", order_ref="R1")
    line = _line(conn, jid, stage="intake")
    sql = "INSERT INTO repair_notes (job_id, job_line_id, action, worker_id, created_at) VALUES (?, ?, ?, ?, ?)"
    conn.execute(sql, (jid, line, "replaced", wid, NOW))
    conn.execute(sql, (jid, line, "kept", wid, NOW))
    conn.execute(sql, (jid, None, "added", wid, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (jid, None, "replaced", wid, NOW))  # needs a line
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (jid, None, "kept", wid, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (jid, line, "added", wid, NOW))  # must not have a line
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (jid, line, "lost", wid, NOW))


def test_checks_rules(conn):
    wid = _worker(conn)
    jid = _job(conn, _template(conn))
    sql = "INSERT INTO checks (job_id, worker_id, result, note, photo_path, created_at) VALUES (?, ?, ?, ?, ?, ?)"
    conn.execute(sql, (jid, wid, "ok", "", "jobs/1/a.jpg", NOW))
    conn.execute(sql, (jid, wid, "rejected", "Teil fehlt", "", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (jid, wid, "ok", "", "", NOW))  # approval needs the box photo
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (jid, wid, "rejected", "", "", NOW))  # rejection needs a note
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (jid, wid, "later", "x", "x", NOW))


def test_badge_short_code_unique_only_while_active(conn):
    wid = _worker(conn)
    sql = "INSERT INTO badges (worker_id, token_hash, short_code, created_at, revoked_at) VALUES (?, ?, ?, ?, ?)"
    conn.execute(sql, (wid, "h1", "ABC234", NOW, None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (wid, "h2", "ABC234", NOW, None))  # second active badge with the same code
    conn.execute(sql, (wid, "h3", "ABC234", NOW, NOW))  # revoked rows may repeat a code
    conn.execute(sql, (wid, "h4", "ABC234", NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (wid, "h1", "ZZZ999", NOW, None))  # token_hash is unique


def test_sessions_cascade_with_worker(conn):
    wid = _worker(conn)
    conn.execute("INSERT INTO sessions (token, worker_id, created_at, last_seen_at) VALUES ('tok', ?, ?, ?)", (wid, NOW, NOW))
    conn.execute("DELETE FROM workers WHERE id=?", (wid,))
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_foreign_key_violation_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (kind, template_id, status, created_by, created_at, stage_changed_at) "
            "VALUES ('delivery', 999, 'open', 'expert', ?, ?)",
            (NOW, NOW),
        )


def test_audit_helper_inserts_and_commits(conn):
    first = audit.audit(conn, NOW, "expert", "settings.update", "settings", "x", {"k": "ä"})
    second = audit.audit(conn, NOW, "worker:3", "login", "worker", 3, None)
    assert second == first + 1
    rows = conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
    assert rows[0]["actor"] == "expert"
    assert json.loads(rows[0]["detail"]) == {"k": "ä"}
    assert rows[1]["entity_id"] == "3"
    assert json.loads(rows[1]["detail"]) == {}
    assert not conn.in_transaction
