import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from galochka import db
from galochka.config import Settings

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


def create_app(settings: Settings) -> FastAPI:
    # Imported here to avoid a circular import: api_expert -> auth -> api.
    from galochka.api_expert import router as expert_router

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.photos_dir.mkdir(parents=True, exist_ok=True)

    conn = db.connect(settings.db_path)
    db.migrate(conn)

    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        conn.close()

    app = FastAPI(lifespan=lifespan)
    app.state.conn = conn
    app.state.lock = lock
    app.state.settings = settings

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

    @app.get("/health")
    def health():
        with lock:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ('shipped','cancelled')"
            ).fetchone()
        return {"status": "ok", "jobs_open": row["n"]}

    return app
