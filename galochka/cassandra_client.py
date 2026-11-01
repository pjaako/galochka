"""The only module that knows Cassandra's URLs and JSON shapes.

Cassandra's API will change (shared storage, recognition service); keep every
path and field name here. All HTTP happens outside app.state.lock: the cache
helpers take the lock themselves, only around their DB access.
"""

import concurrent.futures
import http.client
import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("galochka")

POSITIVE_TTL_SECONDS = 600
NEGATIVE_TTL_SECONDS = 60
# Cassandra's /api/items accepts at most 1000 rows per call and has no offset paging.
_ITEMS_LIMIT = 1000


class CassandraUnavailable(Exception):
    """Cassandra is not configured, not reachable, or answered garbage."""


class _NotFound(Exception):
    pass


def _age_seconds(then: str, now: str) -> float:
    def parse(value: str) -> datetime:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    return (parse(now) - parse(then)).total_seconds()


class CassandraClient:
    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.strip().rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return self.base_url != ""

    # --- HTTP -----------------------------------------------------------

    def _get_json(self, path: str):
        if not self.configured:
            raise CassandraUnavailable("Cassandra URL is not configured")
        request = urllib.request.Request(self.base_url + path, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise _NotFound(path) from e
            raise CassandraUnavailable(f"HTTP {e.code} from {path}") from e
        except (OSError, http.client.HTTPException, ValueError) as e:
            # URLError, timeouts and refused connections are OSError; bad JSON/UTF-8 is ValueError.
            raise CassandraUnavailable(f"{type(e).__name__}: {e}") from e

    # --- catalog listing (used by the sync) ------------------------------

    def fetch_item(self, item_id: str) -> dict | None:
        """GET /api/items/{id}. None when Cassandra does not know the item."""
        try:
            data = self._get_json("/api/items/" + urllib.parse.quote(item_id, safe=""))
        except _NotFound:
            return None
        if not isinstance(data, dict):
            raise CassandraUnavailable("unexpected item answer")
        return data

    def list_groups(self) -> list[str]:
        data = self._get_json("/api/groups")
        if not isinstance(data, list):
            raise CassandraUnavailable("unexpected groups answer")
        return [str(g) for g in data]

    def list_items(self, group: str) -> list[dict]:
        """GET /api/items?group=<group>&limit=1000. An empty group means no group filter."""
        query = {"limit": _ITEMS_LIMIT}
        if group:
            query["group"] = group
        try:
            data = self._get_json("/api/items?" + urllib.parse.urlencode(query))
        except _NotFound as e:
            raise CassandraUnavailable("items endpoint not found") from e
        if not isinstance(data, list):
            raise CassandraUnavailable("unexpected items answer")
        return [row for row in data if isinstance(row, dict)]

    def iter_all_items(self):
        """Yield every active item once.

        Walks the groups (one call each), then makes one ungrouped call to pick up
        items with an empty group name. Limitation: Cassandra has no offset paging,
        so a group with more than 1000 items (or more than 1000 ungrouped items)
        is truncated.
        """
        groups = self.list_groups()
        seen: set[str] = set()
        for group in groups:
            for row in self.list_items(group):
                item_id = row.get("item_id")
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    yield row
        known_groups = set(groups)
        for row in self.list_items(""):
            item_id = row.get("item_id")
            if item_id and item_id not in seen and (row.get("group_name") or "") not in known_groups:
                seen.add(item_id)
                yield row

    # --- reference photos with cache --------------------------------------

    def _absolute(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return self.base_url + "/" + url.lstrip("/")

    def _build(self, item_id: str, data: dict, stale: bool) -> dict:
        photos = [p for p in (data.get("photos") or []) if isinstance(p, dict) and p.get("url")]
        # Reference photos first (stable sort keeps Cassandra's order inside each group).
        photos.sort(key=lambda p: p.get("source") != "reference")
        return {
            "item_id": item_id,
            "name": str(data.get("name") or ""),
            "photos": [{"url": self._absolute(str(p["url"]))} for p in photos],
            "cassandra_url": self.base_url,
            "stale": stale,
        }

    @staticmethod
    def _slim(item: dict | None) -> str:
        """What the cache keeps of an item answer: name and photo urls/sources. {} = unknown item."""
        if item is None:
            return "{}"
        photos = [
            {"url": p.get("url"), "source": p.get("source")}
            for p in (item.get("photos") or [])
            if isinstance(p, dict) and p.get("url")
        ]
        return json.dumps({"name": item.get("name") or "", "photos": photos}, ensure_ascii=False)

    def _cache_read(self, conn, lock, item_id: str):
        try:
            with lock:
                return conn.execute("SELECT * FROM cassandra_cache WHERE item_id=?", (item_id,)).fetchone()
        except sqlite3.Error as e:
            log.warning("cassandra_cache read failed: %s", e)
            return None

    def _cache_write(self, conn, lock, item_id: str, payload: str, now: str, ok: bool) -> None:
        try:
            with lock:
                conn.execute(
                    "INSERT OR REPLACE INTO cassandra_cache (item_id, json, fetched_at, ok) VALUES (?, ?, ?, ?)",
                    (item_id, payload, now, 1 if ok else 0),
                )
                conn.commit()
        except sqlite3.Error as e:
            log.warning("cassandra_cache write failed: %s", e)

    def reference_photos(self, conn, lock, item_id: str, now: str) -> dict:
        """Reference photos of one item, from the cache or from Cassandra. Never raises.

        Positive entries live 10 minutes, negative ones (ok=0, fetch failed) 60 s. A
        failed fetch keeps the old json in the negative entry, so stale data is served
        (stale=True) until Cassandra answers again.
        """
        if not self.configured:
            return self._build(item_id, {}, False)

        row = self._cache_read(conn, lock, item_id)
        old_json = ""
        if row is not None:
            old_json = row["json"]
            try:
                age = _age_seconds(row["fetched_at"], now)
            except ValueError:
                age = float("inf")
            ttl = POSITIVE_TTL_SECONDS if row["ok"] else NEGATIVE_TTL_SECONDS
            if 0 <= age < ttl:
                return self._from_cache(item_id, old_json, stale=not row["ok"])

        # Network call outside the lock.
        try:
            item = self.fetch_item(item_id)
        except CassandraUnavailable as e:
            log.info("Cassandra unavailable for %s: %s", item_id, e)
            self._cache_write(conn, lock, item_id, old_json, now, ok=False)
            return self._from_cache(item_id, old_json, stale=True)
        payload = self._slim(item)
        self._cache_write(conn, lock, item_id, payload, now, ok=True)
        return self._from_cache(item_id, payload, stale=False)

    def _from_cache(self, item_id: str, payload: str, stale: bool) -> dict:
        try:
            data = json.loads(payload) if payload else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Nothing cached = nothing stale to report.
        return self._build(item_id, data, stale and bool(data))

    def prefetch(self, conn, lock, item_ids, now: str, budget_seconds: float = 2.0) -> None:
        """Fill the cache for many ids within a wall-clock budget. Never raises.

        Workers still running when the budget ends finish in the background (bounded by
        the HTTP timeout); their results land in the cache for the next request.
        """
        ids = list(dict.fromkeys(item_ids))
        if not ids or not self.configured:
            return
        executor = None
        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="cassandra-prefetch")
            futures = [executor.submit(self._prefetch_one, conn, lock, i, now) for i in ids]
            concurrent.futures.wait(futures, timeout=budget_seconds)
        except Exception:
            log.exception("prefetch failed")
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    def _prefetch_one(self, conn, lock: threading.Lock, item_id: str, now: str) -> None:
        try:
            self.reference_photos(conn, lock, item_id, now)
        except Exception:
            log.exception("prefetch of %s failed", item_id)
