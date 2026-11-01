import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from galochka import db, repo
from galochka.cassandra_client import CassandraClient
from galochka.config import Settings

log = logging.getLogger("galochka")

_STATIC_DIR = Path(__file__).parent / "static"
_STRINGS_PATH = _STATIC_DIR / "strings.de.json"
_strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))


def t(key: str) -> str:
    return _strings[key]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_MAX_UPLOAD_BYTES = 40 * 1024 * 1024


def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=t("error.too_large"))
    return data


# Page routes that must be revalidated on every load (see the middleware below).
_REVALIDATE_PAGES = {"/", "/auftraege", "/anmelden", "/experte"}
_REVALIDATE_PREFIXES = ("/static/", "/packen/", "/pruefen/", "/rma/", "/druck/")


def _serve_page(filename: str) -> FileResponse:
    path = _STATIC_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=t("error.not_found"))
    return FileResponse(path)


_FIRST_SYNC_DELAY_SECONDS = 10
_SYNC_INTERVAL_SECONDS = 24 * 3600


def _auto_sync_loop(app: FastAPI, stop: threading.Event, first_delay: float, interval: float) -> None:
    """Daemon thread body: sync the catalog from Cassandra after first_delay, then every interval."""
    from galochka.api_expert import run_items_sync

    if stop.wait(first_delay):
        return
    while True:
        try:
            result = run_items_sync(app, "system")
            log.info("daily item sync: %s", result)
        except Exception as e:  # CassandraUnavailable, SQLite errors, a closed connection at shutdown
            log.warning("daily item sync failed: %s", e)
        if stop.wait(interval):
            return


def create_app(settings: Settings) -> FastAPI:
    # Imported here to avoid a circular import: api_expert -> auth -> api.
    from galochka.api_expert import router as expert_router
    from galochka.auth import require_worker_or_expert

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.photos_dir.mkdir(parents=True, exist_ok=True)

    conn = db.connect(settings.db_path)
    db.migrate(conn)

    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        if settings.cassandra_url and settings.auto_sync:
            threading.Thread(
                target=_auto_sync_loop,
                args=(app, stop, _FIRST_SYNC_DELAY_SECONDS, _SYNC_INTERVAL_SECONDS),
                name="galochka-item-sync",
                daemon=True,
            ).start()
        yield
        stop.set()
        conn.close()

    app = FastAPI(lifespan=lifespan)
    app.state.conn = conn
    app.state.lock = lock
    app.state.settings = settings
    # Tests replace this with a fake.
    app.state.cassandra = CassandraClient(settings.cassandra_url)

    @app.middleware("http")
    async def revalidate_assets(request, call_next):
        # Starlette's StaticFiles sends ETag/Last-Modified but no Cache-Control, so
        # browsers cache app.js heuristically and can serve a stale script next to a
        # freshly deployed page. "no-cache" = cache, but revalidate every time (cheap
        # 304). /photos/* is left alone: uuid file names are immutable.
        response = await call_next(request)
        path = request.url.path
        if path in _REVALIDATE_PAGES or path.startswith(_REVALIDATE_PREFIXES):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/photos", StaticFiles(directory=settings.photos_dir), name="photos")
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(expert_router)

    @app.get("/")
    @app.get("/auftraege")
    def auftraege_page():
        return _serve_page("auftraege.html")

    @app.get("/anmelden")
    def anmelden_page():
        return _serve_page("anmelden.html")

    # The job id in the path is read by the page's own script, not by the server.
    @app.get("/packen/{job_id}")
    def packen_page():
        return _serve_page("packen.html")

    @app.get("/pruefen/{job_id}")
    def pruefen_page():
        return _serve_page("pruefen.html")

    @app.get("/rma/{job_id}")
    def rma_page():
        return _serve_page("rma.html")

    @app.get("/experte")
    def experte_page():
        return _serve_page("experte.html")

    @app.get("/druck/etikett/{job_id}")
    @app.get("/druck/badge/{worker_id}")
    def druck_page():
        return _serve_page("druck.html")

    @app.get("/api/items", dependencies=[Depends(require_worker_or_expert)])
    def list_items(q: str = "", limit: int = Query(50, ge=1, le=500)):
        with lock:
            rows = repo.search_items(conn, q, limit)
        return [{"item_id": r["id"], "name": r["name"], "group_name": r["group_name"]} for r in rows]

    @app.get("/api/items/{item_id}/reference", dependencies=[Depends(require_worker_or_expert)])
    def item_reference(item_id: str):
        # Never fails: Cassandra down or unknown item = no photos. The Cassandra call
        # happens inside reference_photos, outside the lock.
        result = app.state.cassandra.reference_photos(conn, lock, item_id, _now())
        if not result["name"]:
            with lock:
                result["name"] = repo.get_item_name(conn, item_id) or ""
        return result

    @app.get("/health")
    def health():
        with lock:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ('shipped','cancelled')"
            ).fetchone()
        return {"status": "ok", "jobs_open": row["n"]}

    return app
