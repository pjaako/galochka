import json
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from galochka.cassandra_client import CassandraClient, CassandraUnavailable

NOW = "2026-11-01T10:00:00+00:00"


def later(seconds: float) -> str:
    return (datetime.fromisoformat(NOW) + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()


ITEM_A = {
    "item_id": "A01143",
    "name": "[Sticker] Ruler",
    "photos": [
        {"id": 1, "source": "query", "url": "/photos/A01143/q1.jpg"},
        {"id": 2, "source": "reference", "url": "/photos/A01143/r1.jpg"},
        {"id": 3, "source": "reference", "url": "/photos/A01143/r2.jpg"},
    ],
}


class FakeCassandra:
    """A tiny Cassandra on a free local port: serves canned JSON, counts calls, can fail or delay."""

    def __init__(self):
        self.groups: list[str] = []
        self.list_rows: dict[str, list[dict]] = {}  # group ("" = ungrouped call) -> rows
        self.items: dict[str, dict] = {}
        self.fail = False
        self.delay = 0.0
        self.calls: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.calls.append(self.path)
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.fail:
                    return self._send(500, {"detail": "boom"})
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/groups":
                    return self._send(200, outer.groups)
                if parsed.path == "/api/items":
                    return self._send(200, outer.list_rows.get(query.get("group", [""])[0], []))
                if parsed.path.startswith("/api/items/"):
                    item = outer.items.get(urllib.parse.unquote(parsed.path[len("/api/items/"):]))
                    if item is None:
                        return self._send(404, {"detail": "Nicht gefunden."})
                    return self._send(200, item)
                self._send(404, {"detail": "Nicht gefunden."})

            def _send(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def cassandra():
    fake = FakeCassandra()
    fake.items["A01143"] = ITEM_A
    yield fake
    fake.stop()


@pytest.fixture
def lock():
    return threading.Lock()


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def cache_rows(conn):
    return conn.execute("SELECT item_id, ok FROM cassandra_cache").fetchall()


# --- fetch_item / listing -----------------------------------------------------


def test_fetch_item_200(cassandra):
    item = CassandraClient(cassandra.url).fetch_item("A01143")
    assert item["name"] == "[Sticker] Ruler"
    assert cassandra.calls == ["/api/items/A01143"]


def test_fetch_item_404_is_none(cassandra):
    assert CassandraClient(cassandra.url).fetch_item("NOPE") is None


def test_fetch_item_server_error_raises(cassandra):
    cassandra.fail = True
    with pytest.raises(CassandraUnavailable):
        CassandraClient(cassandra.url).fetch_item("A01143")


def test_fetch_item_connection_refused_raises():
    with pytest.raises(CassandraUnavailable):
        CassandraClient(closed_port_url()).fetch_item("A01143")


def test_fetch_item_garbage_json_raises(cassandra):
    cassandra.items["BAD"] = ["not", "an", "object"]
    with pytest.raises(CassandraUnavailable):
        CassandraClient(cassandra.url).fetch_item("BAD")


def test_fetch_item_quotes_the_id(cassandra):
    CassandraClient(cassandra.url).fetch_item("A/1 x")
    assert cassandra.calls == ["/api/items/A%2F1%20x"]


def test_not_configured_means_unavailable_without_network():
    client = CassandraClient("")
    for call in (lambda: client.fetch_item("A1"), client.list_groups, lambda: client.list_items("g")):
        with pytest.raises(CassandraUnavailable):
            call()
    with pytest.raises(CassandraUnavailable):
        list(client.iter_all_items())


def test_trailing_slash_in_base_url_is_ignored(cassandra):
    assert CassandraClient(cassandra.url + "/").fetch_item("A01143")["item_id"] == "A01143"


def test_iter_all_items_walks_groups_dedupes_and_adds_ungrouped(cassandra):
    cassandra.groups = ["Accessories", "Items for A1041 & co"]
    cassandra.list_rows = {
        "Accessories": [
            {"item_id": "T00071", "name": "Pulsar", "group_name": "Accessories"},
            {"item_id": "A00001", "name": "Dup", "group_name": "Accessories"},
        ],
        "Items for A1041 & co": [
            {"item_id": "A00001", "name": "Dup", "group_name": "Items for A1041 & co"},
            {"item_id": "A01143", "name": "Ruler", "group_name": "Items for A1041 & co"},
        ],
        # the ungrouped call returns everything; only rows outside the known groups are new
        "": [
            {"item_id": "T00071", "name": "Pulsar", "group_name": "Accessories"},
            {"item_id": "Z00009", "name": "No group", "group_name": ""},
        ],
    }
    ids = [row["item_id"] for row in CassandraClient(cassandra.url).iter_all_items()]
    assert ids == ["T00071", "A00001", "A01143", "Z00009"]
    assert "/api/items?limit=1000&group=Items+for+A1041+%26+co" in cassandra.calls
    assert cassandra.calls.count("/api/items?limit=1000") == 1


def test_iter_all_items_propagates_unavailable(cassandra):
    cassandra.fail = True
    with pytest.raises(CassandraUnavailable):
        list(CassandraClient(cassandra.url).iter_all_items())


# --- reference_photos and its cache ---------------------------------------------


def test_reference_photos_shape_order_and_absolute_urls(cassandra, conn, lock):
    result = CassandraClient(cassandra.url).reference_photos(conn, lock, "A01143", NOW)
    assert result == {
        "item_id": "A01143",
        "name": "[Sticker] Ruler",
        "photos": [
            {"url": f"{cassandra.url}/photos/A01143/r1.jpg"},
            {"url": f"{cassandra.url}/photos/A01143/r2.jpg"},
            {"url": f"{cassandra.url}/photos/A01143/q1.jpg"},
        ],
        "cassandra_url": cassandra.url,
        "stale": False,
    }


def test_positive_cache_hit_avoids_second_call(cassandra, conn, lock):
    client = CassandraClient(cassandra.url)
    first = client.reference_photos(conn, lock, "A01143", NOW)
    second = client.reference_photos(conn, lock, "A01143", later(599))
    assert second == first
    assert len(cassandra.calls) == 1
    assert [tuple(r) for r in cache_rows(conn)] == [("A01143", 1)]


def test_positive_cache_expires_after_ten_minutes(cassandra, conn, lock):
    client = CassandraClient(cassandra.url)
    client.reference_photos(conn, lock, "A01143", NOW)
    cassandra.items["A01143"] = dict(ITEM_A, name="Renamed")
    assert client.reference_photos(conn, lock, "A01143", later(601))["name"] == "Renamed"
    assert len(cassandra.calls) == 2


def test_cache_survives_a_new_client_instance(cassandra, conn, lock):
    CassandraClient(cassandra.url).reference_photos(conn, lock, "A01143", NOW)
    CassandraClient(cassandra.url).reference_photos(conn, lock, "A01143", later(5))
    assert len(cassandra.calls) == 1


def test_unknown_item_is_cached_as_empty(cassandra, conn, lock):
    client = CassandraClient(cassandra.url)
    for offset in (0, 30):
        result = client.reference_photos(conn, lock, "NOPE", later(offset))
        assert result["photos"] == [] and result["name"] == "" and result["stale"] is False
    assert len(cassandra.calls) == 1


def test_negative_cache_second_call_is_fast_and_makes_no_request(cassandra, conn, lock):
    cassandra.fail = True
    client = CassandraClient(cassandra.url)
    first = client.reference_photos(conn, lock, "A01143", NOW)
    assert first["photos"] == [] and first["stale"] is False
    started = time.perf_counter()
    second = client.reference_photos(conn, lock, "A01143", later(10))
    assert time.perf_counter() - started < 0.1
    assert second["photos"] == []
    assert len(cassandra.calls) == 1
    assert [tuple(r) for r in cache_rows(conn)] == [("A01143", 0)]


def test_negative_cache_with_unreachable_host_is_fast_on_second_call(conn, lock):
    client = CassandraClient(closed_port_url())
    assert client.reference_photos(conn, lock, "A01143", NOW)["photos"] == []
    started = time.perf_counter()
    assert client.reference_photos(conn, lock, "A01143", later(1))["photos"] == []
    assert time.perf_counter() - started < 0.1


def test_negative_cache_expires_after_60_seconds_and_recovers(cassandra, conn, lock):
    cassandra.fail = True
    client = CassandraClient(cassandra.url)
    client.reference_photos(conn, lock, "A01143", NOW)
    cassandra.fail = False
    assert client.reference_photos(conn, lock, "A01143", later(59))["photos"] == []
    assert len(cassandra.calls) == 1
    result = client.reference_photos(conn, lock, "A01143", later(61))
    assert len(result["photos"]) == 3 and result["stale"] is False
    assert [tuple(r) for r in cache_rows(conn)] == [("A01143", 1)]


def test_stale_json_served_when_refetch_fails(cassandra, conn, lock):
    client = CassandraClient(cassandra.url)
    fresh = client.reference_photos(conn, lock, "A01143", NOW)
    cassandra.fail = True
    stale = client.reference_photos(conn, lock, "A01143", later(700))
    assert stale["stale"] is True
    assert stale["photos"] == fresh["photos"] and stale["name"] == fresh["name"]
    # still stale and still no new request inside the 60 s negative window
    calls = len(cassandra.calls)
    assert client.reference_photos(conn, lock, "A01143", later(730))["stale"] is True
    assert len(cassandra.calls) == calls
    # and the stale data survives a second failed refetch
    assert client.reference_photos(conn, lock, "A01143", later(800))["photos"] == fresh["photos"]
    # Cassandra is back: fresh again
    cassandra.fail = False
    assert client.reference_photos(conn, lock, "A01143", later(900))["stale"] is False


def test_not_configured_reference_photos_is_empty_and_touches_nothing(conn, lock):
    result = CassandraClient("").reference_photos(conn, lock, "A01143", NOW)
    assert result == {"item_id": "A01143", "name": "", "photos": [], "cassandra_url": "", "stale": False}
    assert cache_rows(conn) == []


def test_http_call_happens_outside_the_lock(cassandra, conn, lock):
    class SpyClient(CassandraClient):
        def fetch_item(self, item_id):
            assert not lock.locked()
            return super().fetch_item(item_id)

    SpyClient(cassandra.url).reference_photos(conn, lock, "A01143", NOW)


# --- prefetch ---------------------------------------------------------------------


def test_prefetch_fills_the_cache(cassandra, conn, lock):
    for n in range(6):
        cassandra.items[f"P{n}"] = dict(ITEM_A, item_id=f"P{n}", name=f"Part {n}")
    client = CassandraClient(cassandra.url)
    client.prefetch(conn, lock, [f"P{n}" for n in range(6)] + ["P0"], NOW, budget_seconds=5)
    assert sorted(r["item_id"] for r in cache_rows(conn)) == [f"P{n}" for n in range(6)]
    calls = len(cassandra.calls)
    assert calls == 6  # the duplicate id is fetched once
    assert client.reference_photos(conn, lock, "P3", later(1))["name"] == "Part 3"
    assert len(cassandra.calls) == calls


def test_prefetch_returns_within_budget_when_cassandra_is_slow(cassandra, conn, lock):
    cassandra.delay = 0.6
    started = time.perf_counter()
    CassandraClient(cassandra.url, timeout=5).prefetch(
        conn, lock, [f"S{n}" for n in range(12)], NOW, budget_seconds=0.2
    )
    assert time.perf_counter() - started < 0.5
    # let the workers that are still in flight finish before the fixtures close the DB
    time.sleep(0.8)


def test_prefetch_never_raises(conn, lock):
    CassandraClient(closed_port_url()).prefetch(conn, lock, ["A1", "A2"], NOW, budget_seconds=1)
    CassandraClient("").prefetch(conn, lock, ["A1"], NOW)
    CassandraClient("http://x").prefetch(conn, lock, [], NOW)
    CassandraClient(closed_port_url()).prefetch(conn, lock, ["A1"], "not a timestamp", budget_seconds=1)
