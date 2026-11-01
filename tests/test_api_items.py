import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import EXPERT
from galochka import api
from galochka.api import create_app
from galochka.cassandra_client import CassandraUnavailable
from galochka.config import Settings
from test_cassandra_client import ITEM_A, FakeCassandra, closed_port_url

BAD = {"X-Expert-Password": "wrong"}
FIXTURE = Path(__file__).parent / "fixtures" / "items_small.csv"


class FakeClient:
    """Stands in for app.state.cassandra on the sync path."""

    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def iter_all_items(self):
        if self.error:
            raise self.error
        yield from self.items


def _row(item_id, name="Part", group="G"):
    return {"item_id": item_id, "name": name, "group_name": group, "location": "", "versions": "",
            "drawing_revision": ""}


def _make_client(data_dir, cassandra_url="", **settings):
    app = create_app(Settings(data_dir=data_dir, expert_password="experte", auto_sync=False,
                              cassandra_url=cassandra_url, **settings))
    return TestClient(app)


@pytest.fixture
def configured(data_dir):
    """An app with a Cassandra URL set (the client is replaced per test)."""
    with _make_client(data_dir, "http://cassandra.invalid:8080") as c:
        yield c


@pytest.fixture
def unreachable(data_dir):
    with _make_client(data_dir, closed_port_url()) as c:
        yield c


@pytest.fixture
def seeded(client):
    resp = client.post("/api/expert/import/items", headers=EXPERT,
                       files={"file": ("items.csv", FIXTURE.read_bytes(), "text/csv")})
    assert resp.status_code == 200
    return client


def _audit(client):
    with client.app.state.lock:
        return client.app.state.conn.execute("SELECT * FROM audit ORDER BY id").fetchall()


# --- auth -----------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, BAD])
@pytest.mark.parametrize("path", ["/api/items", "/api/items/A01143/reference"])
def test_item_routes_need_worker_or_expert(client, path, headers):
    resp = client.get(path, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Die Sitzung ist abgelaufen. Bitte neu anmelden."}


@pytest.mark.parametrize("headers", [{}, BAD])
@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/expert/items/stats"), ("post", "/api/expert/sync/items"), ("post", "/api/expert/import/items")],
)
def test_expert_item_routes_need_expert_password(client, method, path, headers):
    resp = getattr(client, method)(path, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Nicht berechtigt."}


# --- GET /api/items ---------------------------------------------------------------------


def test_list_items_shape_and_order(seeded):
    resp = seeded.get("/api/items?limit=500", headers=EXPERT)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 11
    assert [i["item_id"] for i in items] == sorted(i["item_id"] for i in items)
    assert items[0] == {"item_id": "A01142", "name": items[0]["name"], "group_name": items[0]["group_name"]}
    assert set(items[0]) == {"item_id", "name", "group_name"}


def test_list_items_q_matches_id_or_name_case_insensitively(seeded):
    by_id = seeded.get("/api/items?q=a01143", headers=EXPERT).json()
    assert [i["item_id"] for i in by_id] == ["A01143"]
    by_name = seeded.get("/api/items?q=RULER", headers=EXPERT).json()
    assert "A01143" in [i["item_id"] for i in by_name]
    assert seeded.get("/api/items?q=gibt-es-nicht", headers=EXPERT).json() == []


def test_list_items_q_folds_umlauts(seeded):
    name = next(i["name"] for i in seeded.get("/api/items?q=A01633", headers=EXPERT).json())
    assert "ä" in name
    hits = seeded.get("/api/items", headers=EXPERT, params={"q": name.upper()}).json()
    assert [i["item_id"] for i in hits] == ["A01633"]


def test_list_items_q_treats_like_wildcards_literally(seeded):
    assert seeded.get("/api/items?q=%25", headers=EXPERT).json() == []
    assert seeded.get("/api/items?q=A_1", headers=EXPERT).json() == []


def test_list_items_limit_default_and_bounds(seeded):
    assert len(seeded.get("/api/items", headers=EXPERT).json()) == 11
    assert len(seeded.get("/api/items?limit=3", headers=EXPERT).json()) == 3
    assert seeded.get("/api/items?limit=0", headers=EXPERT).status_code == 422
    assert seeded.get("/api/items?limit=501", headers=EXPERT).status_code == 422


def test_list_items_default_limit_is_50(client):
    with client.app.state.lock:
        conn = client.app.state.conn
        conn.executemany(
            "INSERT INTO items (id, name, updated_at) VALUES (?, ?, 'x')", [(f"B{n:04d}", f"Part {n}") for n in range(80)]
        )
        conn.commit()
    assert len(client.get("/api/items", headers=EXPERT).json()) == 50
    assert len(client.get("/api/items?limit=500", headers=EXPERT).json()) == 80


def test_list_items_hides_inactive(seeded):
    seeded.post("/api/expert/import/items", headers=EXPERT, files={"file": ("a.csv", _csv_without(FIXTURE, "A01143"))})
    ids = [i["item_id"] for i in seeded.get("/api/items?limit=500", headers=EXPERT).json()]
    assert "A01143" not in ids and len(ids) == 10


def _csv_without(path, item_id):
    text = path.read_bytes().decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if not ln.startswith(f'"{item_id}"')]
    return "\r\n".join(lines).encode("utf-8")


# --- GET /api/items/{id}/reference ---------------------------------------------------------


def test_reference_with_cassandra_not_configured_is_200_and_empty(seeded):
    resp = seeded.get("/api/items/A01143/reference", headers=EXPERT)
    assert resp.status_code == 200
    assert resp.json() == {"item_id": "A01143", "name": "[Sticker] Ruler", "photos": [], "cassandra_url": "",
                           "stale": False}


def test_reference_with_cassandra_unreachable_is_200_and_negative_cache_is_fast(unreachable):
    first = unreachable.get("/api/items/A01143/reference", headers=EXPERT)
    assert first.status_code == 200
    assert first.json()["photos"] == [] and first.json()["item_id"] == "A01143"
    started = time.perf_counter()
    second = unreachable.get("/api/items/A01143/reference", headers=EXPERT)
    assert time.perf_counter() - started < 0.1
    assert second.status_code == 200 and second.json()["photos"] == []


def test_reference_name_falls_back_to_local_item_when_cassandra_is_down(data_dir):
    with _make_client(data_dir, closed_port_url()) as c:
        c.post("/api/expert/import/items", headers=EXPERT, files={"file": ("i.csv", FIXTURE.read_bytes())})
        assert c.get("/api/items/A01143/reference", headers=EXPERT).json()["name"] == "[Sticker] Ruler"


def test_reference_returns_absolute_photo_urls_from_cassandra(data_dir):
    fake = FakeCassandra()
    fake.items["A01143"] = ITEM_A
    try:
        with _make_client(data_dir, fake.url) as c:
            body = c.get("/api/items/A01143/reference", headers=EXPERT).json()
            assert body["cassandra_url"] == fake.url
            assert body["name"] == "[Sticker] Ruler" and body["stale"] is False
            assert body["photos"][0] == {"url": f"{fake.url}/photos/A01143/r1.jpg"}
            # unknown in Cassandra and locally: still 200
            unknown = c.get("/api/items/NOPE/reference", headers=EXPERT)
            assert unknown.status_code == 200
            assert unknown.json()["photos"] == [] and unknown.json()["name"] == ""
            # second call is served from the cache
            calls = len(fake.calls)
            c.get("/api/items/A01143/reference", headers=EXPERT)
            assert len(fake.calls) == calls
    finally:
        fake.stop()


def test_reference_for_item_unknown_locally_uses_cassandra_name(data_dir):
    fake = FakeCassandra()
    fake.items["A01143"] = ITEM_A
    try:
        with _make_client(data_dir, fake.url) as c:
            with c.app.state.lock:
                assert c.app.state.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
            assert c.get("/api/items/A01143/reference", headers=EXPERT).json()["name"] == "[Sticker] Ruler"
    finally:
        fake.stop()


# --- POST /api/expert/sync/items ----------------------------------------------------------------


def test_sync_409_when_not_configured(client):
    resp = client.post("/api/expert/sync/items", headers=EXPERT)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Cassandra ist nicht konfiguriert."}
    assert _audit(client) == []


def test_sync_502_when_unreachable_and_items_untouched(configured):
    configured.app.state.cassandra = FakeClient([_row("A1")])
    assert configured.post("/api/expert/sync/items", headers=EXPERT).status_code == 200
    configured.app.state.cassandra = FakeClient(error=CassandraUnavailable("down"))
    resp = configured.post("/api/expert/sync/items", headers=EXPERT)
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Cassandra ist nicht erreichbar."}
    assert [i["item_id"] for i in configured.get("/api/items", headers=EXPERT).json()] == ["A1"]
    assert len(_audit(configured)) == 1  # only the successful sync


def test_sync_502_with_a_real_unreachable_host(unreachable):
    assert unreachable.post("/api/expert/sync/items", headers=EXPERT).status_code == 502


def test_sync_200_adds_updates_deactivates_and_writes_audit(configured):
    configured.app.state.cassandra = FakeClient([_row("A1", "One"), _row("A2", "Two"), _row("A3", "Three")])
    first = configured.post("/api/expert/sync/items", headers=EXPERT)
    assert first.status_code == 200
    body = first.json()
    assert (body["added"], body["updated"], body["deactivated"], body["total_active"]) == (3, 0, 0, 3)
    assert body["synced_at"]

    configured.app.state.cassandra = FakeClient([_row("A1", "One"), _row("A2", "Two!"), _row("A4", "Four")])
    second = configured.post("/api/expert/sync/items", headers=EXPERT).json()
    assert (second["added"], second["updated"], second["deactivated"], second["total_active"]) == (1, 1, 1, 3)

    rows = _audit(configured)
    assert [(r["actor"], r["action"], r["entity"]) for r in rows] == [("expert", "items.sync", "items")] * 2
    assert '"added": 1' in rows[1]["detail"]


def test_sync_with_empty_answer_keeps_catalog(configured):
    configured.app.state.cassandra = FakeClient([_row("A1")])
    configured.post("/api/expert/sync/items", headers=EXPERT)
    configured.app.state.cassandra = FakeClient([])
    body = configured.post("/api/expert/sync/items", headers=EXPERT).json()
    assert (body["deactivated"], body["total_active"]) == (0, 1)


def test_sync_runs_cassandra_calls_outside_the_lock(configured):
    class LockSpy(FakeClient):
        def iter_all_items(self):
            assert not configured.app.state.lock.locked()
            yield _row("A1")

    configured.app.state.cassandra = LockSpy()
    assert configured.post("/api/expert/sync/items", headers=EXPERT).status_code == 200


# --- POST /api/expert/import/items, GET /api/expert/items/stats ------------------------------------


def test_import_csv_twice_second_run_adds_nothing(client):
    files = {"file": ("items.csv", FIXTURE.read_bytes(), "text/csv")}
    first = client.post("/api/expert/import/items", headers=EXPERT, files=files)
    assert first.status_code == 200
    assert first.json() == {"added": 11, "updated": 0, "deactivated": 0, "total_active": 11}
    second = client.post("/api/expert/import/items", headers=EXPERT, files=files)
    assert second.json() == {"added": 0, "updated": 0, "deactivated": 0, "total_active": 11}
    rows = _audit(client)
    assert [(r["actor"], r["action"]) for r in rows] == [("expert", "items.import")] * 2
    assert '"filename": "items.csv"' in rows[0]["detail"]


@pytest.mark.parametrize(
    "content",
    [b"", b"just some text", b'"Part No.","Part description"\r\n', b"\xff\xfe\x00bad", b'"Name","Foo"\r\n"a","b"\r\n'],
)
def test_import_bad_csv_is_400(client, content):
    resp = client.post("/api/expert/import/items", headers=EXPERT, files={"file": ("x.csv", content)})
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("Die CSV-Datei konnte nicht gelesen werden")
    assert _audit(client) == []


def test_import_without_file_is_422(client):
    assert client.post("/api/expert/import/items", headers=EXPERT).status_code == 422


def test_stats_before_and_after(configured):
    before = configured.get("/api/expert/items/stats", headers=EXPERT)
    assert before.json() == {"total_active": 0, "synced_at": None, "cassandra_url": "http://cassandra.invalid:8080"}
    configured.app.state.cassandra = FakeClient([_row("A1"), _row("A2")])
    synced_at = configured.post("/api/expert/sync/items", headers=EXPERT).json()["synced_at"]
    after = configured.get("/api/expert/items/stats", headers=EXPERT).json()
    assert after == {"total_active": 2, "synced_at": synced_at, "cassandra_url": "http://cassandra.invalid:8080"}


def test_stats_without_cassandra_url(client):
    assert client.get("/api/expert/items/stats", headers=EXPERT).json()["cassandra_url"] == ""


# --- daily sync thread -----------------------------------------------------------------------------


def _sync_threads():
    return [t for t in threading.enumerate() if t.name == "galochka-item-sync"]


def test_no_sync_thread_without_url_or_when_disabled(data_dir):
    with _make_client(data_dir, ""):
        assert _sync_threads() == []
    with _make_client(data_dir, "http://cassandra.invalid:8080"):
        assert _sync_threads() == []


def test_sync_thread_starts_with_url_and_stops_with_the_app(data_dir):
    app = create_app(Settings(data_dir=data_dir, cassandra_url="http://cassandra.invalid:8080", auto_sync=True))
    with TestClient(app):
        assert len(_sync_threads()) == 1
    deadline = time.time() + 2
    while _sync_threads() and time.time() < deadline:
        time.sleep(0.02)
    assert _sync_threads() == []


def test_sync_loop_syncs_then_repeats_and_swallows_errors(configured):
    class Flaky(FakeClient):
        def __init__(self):
            super().__init__([_row("A1")])
            self.runs = 0

        def iter_all_items(self):
            self.runs += 1
            if self.runs == 1:
                raise CassandraUnavailable("down")  # must not kill the loop
            return super().iter_all_items()

    flaky = Flaky()
    configured.app.state.cassandra = flaky
    stop = threading.Event()
    worker = threading.Thread(target=api._auto_sync_loop, args=(configured.app, stop, 0, 0.05), daemon=True)
    worker.start()
    deadline = time.time() + 3
    while flaky.runs < 3 and time.time() < deadline:
        time.sleep(0.02)
    stop.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert flaky.runs >= 3
    assert [i["item_id"] for i in configured.get("/api/items", headers=EXPERT).json()] == ["A1"]
    assert {r["actor"] for r in _audit(configured)} == {"system"}
