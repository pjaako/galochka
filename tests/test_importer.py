import csv
import io
from pathlib import Path

import pytest

from galochka.cassandra_client import CassandraUnavailable
from galochka.importer import ImportError_, import_items, parse_csv, sync_items

FIXTURE = Path(__file__).parent / "fixtures" / "items_small.csv"
NOW = "2026-11-01T00:00:00+00:00"
LATER = "2026-11-02T00:00:00+00:00"


def _fixture_bytes():
    return FIXTURE.read_bytes()


def _rows_as_dicts():
    reader = csv.DictReader(io.StringIO(_fixture_bytes().decode("utf-8-sig")))
    return reader.fieldnames, list(reader)


def _write_csv(fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _csv_without_id(item_id):
    fieldnames, rows = _rows_as_dicts()
    return _write_csv(fieldnames, [r for r in rows if r["Part No."] != item_id])


def _csv_with_renamed_item(item_id, new_name):
    fieldnames, rows = _rows_as_dicts()
    for r in rows:
        if r["Part No."] == item_id:
            r["Part description"] = new_name
    return _write_csv(fieldnames, rows)


def _csv_with_versions(item_id, versions):
    fieldnames, rows = _rows_as_dicts()
    base = next(r for r in rows if r["Part No."] == item_id)
    rows = [r for r in rows if r["Part No."] != item_id]
    for ver in versions:
        new_row = dict(base)
        new_row["Parameters"] = ver
        new_row["Part description"] = f"{base['Part description']} [{ver}]"
        rows.append(new_row)
    return _write_csv(fieldnames, rows)


def _item(conn, item_id):
    return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def _setting(conn, key):
    return conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()["value"]


# --- parse_csv / import_items (copied from Cassandra) ----------------------------


def test_parse_csv_returns_all_rows():
    assert len(parse_csv(_fixture_bytes())) == 13


def test_parse_csv_preserves_umlaut():
    umlaut_row = next(r for r in parse_csv(_fixture_bytes()) if r.id == "A01633")
    assert "ä" in umlaut_row.name


def test_parse_csv_bom_stripped_from_header():
    assert parse_csv(_fixture_bytes())[0].id == "T00071"


def test_parse_csv_missing_required_column_raises():
    bad = _fixture_bytes().decode("utf-8-sig").replace("Part No.", "Nonsense").encode("utf-8")
    with pytest.raises(ImportError_, match="Part No."):
        parse_csv(bad)


def test_parse_csv_non_utf8_bytes_raises_import_error():
    data = b'"Part No.","Part description"\r\n"A0001","Caf\x92e widget"\r\n'
    with pytest.raises(ImportError_, match="UTF-8"):
        parse_csv(data)


def test_import_items_no_rows_raises_and_does_not_change_db(conn):
    import_items(conn, _fixture_bytes(), NOW)
    with pytest.raises(ImportError_, match="no rows"):
        import_items(conn, b'"Part No.","Part description"\r\n', NOW)
    assert conn.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0] == 11


def test_import_items_adds_all_new_with_source_csv(conn):
    result = import_items(conn, _fixture_bytes(), NOW)
    assert result == {"added": 11, "updated": 0, "deactivated": 0, "total_active": 11}
    assert {r["source"] for r in conn.execute("SELECT source FROM items")} == {"csv"}


def test_import_items_groups_versions_by_id(conn):
    import_items(conn, _fixture_bytes(), NOW)
    assert _item(conn, "A01143")["versions"] == "1.0, 2.0, 3.0"


def test_import_items_sets_drawing_revision_from_representative_row(conn):
    import_items(conn, _fixture_bytes(), NOW)
    assert _item(conn, "A01192")["drawing_revision"] == "2"
    assert _item(conn, "A01143")["drawing_revision"] == ""


def test_import_items_reimport_same_file_is_noop(conn):
    import_items(conn, _fixture_bytes(), NOW)
    result = import_items(conn, _fixture_bytes(), NOW)
    assert result == {"added": 0, "updated": 0, "deactivated": 0, "total_active": 11}


def test_import_items_removed_id_deactivates_but_keeps_row(conn):
    import_items(conn, _fixture_bytes(), NOW)
    result = import_items(conn, _csv_without_id("A01143"), NOW)
    assert result["deactivated"] == 1 and result["total_active"] == 10
    assert _item(conn, "A01143")["active"] == 0


def test_import_items_changed_name_counts_as_updated(conn):
    import_items(conn, _fixture_bytes(), NOW)
    result = import_items(conn, _csv_with_renamed_item("A01142", "Renamed Device"), NOW)
    assert result["updated"] == 1 and result["added"] == 0
    assert _item(conn, "A01142")["name"] == "Renamed Device"


def test_import_items_sorts_versions_numerically(conn):
    import_items(conn, _csv_with_versions("A01147", ["10.0", "2.0"]), NOW)
    assert _item(conn, "A01147")["versions"] == "2.0, 10.0"


def test_import_items_reactivates_existing_item(conn):
    import_items(conn, _fixture_bytes(), NOW)
    import_items(conn, _csv_without_id("A01143"), NOW)
    result = import_items(conn, _fixture_bytes(), NOW)
    assert result["updated"] == 0
    assert _item(conn, "A01143")["active"] == 1


# --- sync_items --------------------------------------------------------------------


class FakeClient:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def iter_all_items(self):
        if self.error:
            raise self.error
        yield from self.items


def _row(item_id, name="Part", group="G", **extra):
    return dict({"item_id": item_id, "name": name, "group_name": group, "location": "", "versions": "",
                 "drawing_revision": ""}, **extra)


def test_sync_adds_items_with_source_cassandra_and_sets_synced_at(conn):
    client = FakeClient([_row("A1", "One", location="R1", versions="1.0, 2.0", drawing_revision="3"), _row("A2")])
    result = sync_items(conn, client, NOW)
    assert result == {"added": 2, "updated": 0, "deactivated": 0, "total_active": 2, "synced_at": NOW}
    a1 = _item(conn, "A1")
    assert (a1["name"], a1["location"], a1["versions"], a1["drawing_revision"], a1["source"], a1["active"]) == (
        "One", "R1", "1.0, 2.0", "3", "cassandra", 1)
    assert _setting(conn, "items_synced_at") == NOW


def test_sync_updates_and_deactivates(conn):
    sync_items(conn, FakeClient([_row("A1", "One"), _row("A2", "Two"), _row("A3", "Three")]), NOW)
    result = sync_items(conn, FakeClient([_row("A1", "One"), _row("A2", "Two renamed"), _row("A4", "Four")]), LATER)
    assert result == {"added": 1, "updated": 1, "deactivated": 1, "total_active": 3, "synced_at": LATER}
    assert _item(conn, "A2")["name"] == "Two renamed"
    assert _item(conn, "A3")["active"] == 0  # kept, not deleted
    assert _setting(conn, "items_synced_at") == LATER


def test_sync_unchanged_catalog_is_noop(conn):
    client = FakeClient([_row("A1"), _row("A2")])
    sync_items(conn, client, NOW)
    result = sync_items(conn, client, LATER)
    assert (result["added"], result["updated"], result["deactivated"]) == (0, 0, 0)


def test_sync_dedupes_and_skips_rows_without_id(conn):
    result = sync_items(conn, FakeClient([_row("A1"), _row("A1", "Dup"), {"name": "no id"}, _row("")]), NOW)
    assert result["added"] == 1 and result["total_active"] == 1
    assert _item(conn, "A1")["name"] == "Part"


def test_sync_reactivates_item_that_returns(conn):
    sync_items(conn, FakeClient([_row("A1"), _row("A2")]), NOW)
    sync_items(conn, FakeClient([_row("A1")]), NOW)
    assert _item(conn, "A2")["active"] == 0
    sync_items(conn, FakeClient([_row("A1"), _row("A2")]), LATER)
    assert _item(conn, "A2")["active"] == 1


def test_empty_answer_does_not_deactivate_or_touch_synced_at(conn):
    sync_items(conn, FakeClient([_row("A1"), _row("A2")]), NOW)
    result = sync_items(conn, FakeClient([]), LATER)
    assert result == {"added": 0, "updated": 0, "deactivated": 0, "total_active": 2, "synced_at": NOW}
    assert _item(conn, "A1")["active"] == 1
    assert _setting(conn, "items_synced_at") == NOW


def test_sync_never_synced_empty_answer_has_no_synced_at(conn):
    assert sync_items(conn, FakeClient([]), NOW)["synced_at"] is None


def test_unavailable_propagates_and_leaves_items_untouched(conn):
    sync_items(conn, FakeClient([_row("A1")]), NOW)
    with pytest.raises(CassandraUnavailable):
        sync_items(conn, FakeClient(error=CassandraUnavailable("down")), LATER)
    assert _item(conn, "A1")["active"] == 1
    assert _setting(conn, "items_synced_at") == NOW


def test_csv_then_sync_switches_source_and_deactivates_csv_only_items(conn):
    import_items(conn, _fixture_bytes(), NOW)
    result = sync_items(conn, FakeClient([_row("A01143", "[Sticker] Ruler")]), LATER)
    assert result["total_active"] == 1 and result["deactivated"] == 10
    assert _item(conn, "A01143")["source"] == "cassandra"
