import json
import logging
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from galochka import db, images, qr, repo
from galochka.audit import audit
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


class CodeLoginRequest(BaseModel):
    code: str = Field(max_length=64)


_MAX_UPLOAD_BYTES = 40 * 1024 * 1024


def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=t("error.too_large"))
    return data


# --- jobs: error mapping and request models (shared with api_expert) ------------------------

# ValueError codes from repo that are state conflicts (409); every other code is a 400.
_CONFLICT_CODES = {"wrong_status", "lines_open", "bad_transition", "outgoing_confirmed", "rma_duplicate"}


def job_error(e: ValueError) -> HTTPException:
    """Map a repo ValueError code to HTTP: 'error.<code>' from strings.de.json, 409 for conflicts, else 400."""
    code = str(e)
    if code.startswith("unknown_item:"):
        return HTTPException(status_code=400, detail=f"{t('error.unknown_item')} {code.split(':', 1)[1]}")
    key = f"error.{code}"
    if key not in _strings:
        return HTTPException(status_code=400, detail=t("error.invalid_request"))
    return HTTPException(status_code=409 if code in _CONFLICT_CODES else 400, detail=_strings[key])


@contextmanager
def db_write(conn):
    """Wrap a write under the lock: roll back on any error; KeyError -> 404, ValueError -> job_error.

    repo.LinesOpen passes through (after the rollback) so the route can answer with the line ids.
    """
    try:
        yield
    except repo.LinesOpen:
        conn.rollback()
        raise
    except KeyError:
        conn.rollback()
        raise HTTPException(status_code=404, detail=t("error.not_found"))
    except ValueError as e:
        conn.rollback()
        raise job_error(e)
    except Exception:
        conn.rollback()
        raise


Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=repo.MAX_REASON)]


class JobCreateRequest(BaseModel):
    """Ad hoc job started by a worker."""

    template_id: int
    order_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=repo.MAX_ORDER_REF)]
    customer: Annotated[str, StringConstraints(strip_whitespace=True, max_length=repo.MAX_CUSTOMER)] = ""


class LineCreateRequest(BaseModel):
    item_id: str | None = Field(default=None, max_length=80)
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=repo.MAX_NAME)]
    qty: int = Field(ge=1, le=repo.MAX_QTY)
    reason: Reason


def _parse_json(raw: str, kind: type):
    try:
        data = json.loads(raw or ("[]" if kind is list else "{}"))
    except ValueError:
        raise HTTPException(status_code=400, detail=t("error.invalid_request"))
    if not isinstance(data, kind):
        raise HTTPException(status_code=400, detail=t("error.invalid_request"))
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
    from galochka.auth import SESSION_COOKIE, actor_of, require_worker, require_worker_or_expert

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

    def _start_session(request: Request, response: Response, badge, via: str) -> dict:
        """Create a session for the worker of `badge`, audit the login and set the cookie.

        A session cookie the browser still sends (someone else's, or an old one) is
        deleted first. The cookie is HttpOnly, SameSite=Lax, Path=/ and has no Max-Age
        (it dies with the browser; the server expires idle sessions itself). No Secure
        flag: the LAN is plain HTTP.
        """
        now = _now()
        old_token = request.cookies.get(SESSION_COOKIE)
        with lock:
            if old_token:
                repo.delete_session(conn, old_token)
            repo.delete_expired_sessions(conn, now, repo.session_minutes(conn))
            token = repo.create_session(conn, badge["worker_id"], now)
            audit(conn, now, f"worker:{badge['worker_id']}", "login", "worker", badge["worker_id"], {"via": via})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/", secure=False)
        return {"worker": {"id": badge["worker_id"], "name": badge["name"]}}

    @app.post("/api/login/qr")
    def login_qr(request: Request, response: Response, image: UploadFile = File(...)):
        data = _read_upload(image)
        text = qr.decode_qr(data)  # image decoding stays outside the lock
        if text is None:
            raise HTTPException(status_code=400, detail=t("error.bad_image"))
        match = qr.BADGE_RE.fullmatch(text)
        badge = None
        if match:
            with lock:
                badge = repo.find_badge_by_token(conn, match.group(1))
        if badge is None:
            raise HTTPException(status_code=401, detail=t("error.badge_unknown"))
        return _start_session(request, response, badge, "qr")

    @app.post("/api/login/code")
    def login_code(body: CodeLoginRequest, request: Request, response: Response):
        code = "".join(body.code.split()).upper()
        with lock:
            badge = repo.find_badge_by_code(conn, code) if code else None
        if badge is None:
            raise HTTPException(status_code=401, detail=t("error.badge_unknown"))
        return _start_session(request, response, badge, "code")

    @app.post("/api/logout", status_code=204)
    def logout(request: Request):
        # Lenient on purpose: an expired or missing session still clears the cookie.
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            with lock:
                row = repo.get_session_worker(conn, token)
                repo.delete_session(conn, token)
                if row is not None:
                    audit(conn, _now(), f"worker:{row['id']}", "logout", "worker", row["id"])
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
        return response

    @app.get("/api/me")
    def me(request: Request, worker=Depends(require_worker)):
        return {
            "worker": {"id": worker["id"], "name": worker["name"]},
            "expires_at": request.state.session_expires_at,
        }

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

    @app.get("/api/templates", dependencies=[Depends(require_worker_or_expert)])
    def list_templates():
        with lock:
            rows = repo.list_templates(conn, active_only=True)
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "product_item_id": r["product_item_id"],
                "line_count": r["line_count"],
                "second_check": r["second_check"],
            }
            for r in rows
        ]

    # --- jobs -------------------------------------------------------------------------------

    def _job_dict(job_id: int) -> dict:
        """The job as JSON (call under the lock)."""
        return repo.job_to_dict(repo.get_job(conn, job_id))

    @app.get("/api/jobs")
    def list_jobs(
        worker=Depends(require_worker),
        status: str | None = Query(None),
        kind: str | None = Query(None),
        mine: int = Query(0),
    ):
        statuses = [x.strip() for x in status.split(",") if x.strip()] if status else list(repo.ACTIVE_JOB_STATUSES)
        if not statuses or any(x not in repo.JOB_STATUSES for x in statuses) or kind not in (None, "delivery", "rma"):
            raise HTTPException(status_code=400, detail=t("error.invalid_request"))
        with lock:
            rows = repo.list_jobs(conn, statuses, kind, worker["id"] if mine else None)
        return [repo.job_to_dict(r) for r in rows]

    @app.post("/api/jobs", status_code=201)
    def create_adhoc_job(body: JobCreateRequest, worker=Depends(require_worker)):
        now = _now()
        actor = f"worker:{worker['id']}"
        with lock:
            with db_write(conn):
                (job_id,) = repo.create_jobs(
                    conn, "delivery", body.template_id, body.order_ref, body.customer, 1, [], actor, now,
                    ad_hoc=True, claimed_by=worker["id"], commit=False,
                )
                audit(conn, now, actor, "job.create", "job", job_id,
                      {"kind": "delivery", "template_id": body.template_id, "order_ref": body.order_ref,
                       "ad_hoc": True})
            return _job_dict(job_id)

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(require_worker_or_expert)])
    def job_detail(job_id: int):
        now = _now()
        with lock:
            job = repo.get_job(conn, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=t("error.not_found"))
            lines = repo.get_job_lines(conn, job_id, repo.current_stage(job))
            data = repo.job_to_dict(job)
            data["second_check"] = repo.second_check_applies(conn, job)
            data["attempt"] = job["attempt"]
            data["open_line_ids"] = repo.open_line_ids(conn, job_id, job["stage"], job["attempt"])
        # Cassandra calls (prefetch with a 2 s budget, then cache reads) happen outside the lock.
        photo_ids = {row["photo_item_id"] or row["item_id"] for row, _ in lines} - {None}
        client = app.state.cassandra
        client.prefetch(conn, lock, sorted(photo_ids), now, budget_seconds=2.0)
        references = {i: client.reference_photos(conn, lock, i, now) for i in photo_ids}
        data["lines"] = []
        for row, latest in lines:
            line = repo.line_to_dict(row, latest)
            ref = references.get(row["photo_item_id"] or row["item_id"])
            line["reference"] = (
                {"photos": ref["photos"], "stale": ref["stale"]} if ref else {"photos": [], "stale": False}
            )
            data["lines"].append(line)
        data["repair_notes"] = []  # filled by the RMA task
        data["checks"] = []  # filled by the second-check task
        return data

    @app.post("/api/jobs/{job_id}/claim")
    def claim_job(job_id: int, worker=Depends(require_worker)):
        now = _now()
        with lock:
            with db_write(conn):
                previous = repo.claim_job(conn, job_id, worker["id"], now, commit=False)
                audit(conn, now, f"worker:{worker['id']}", "job.claim", "job", job_id,
                      {"previous_claimed_by": previous})
            return _job_dict(job_id)

    @app.post("/api/jobs/{job_id}/lines", status_code=201)
    def add_job_line(job_id: int, body: LineCreateRequest, worker=Depends(require_worker)):
        now = _now()
        with lock:
            with db_write(conn):
                job = repo.get_job(conn, job_id)
                if job is None:
                    raise KeyError(job_id)
                line_id = repo.add_worker_line(
                    conn, job_id, repo.current_stage(job), body.item_id, body.label, body.qty, body.reason, now,
                    commit=False,
                )
                audit(conn, now, f"worker:{worker['id']}", "line.add", "job", job_id,
                      {"line_id": line_id, "item_id": body.item_id, "label": body.label, "qty": body.qty,
                       "reason": body.reason})
            row, latest = repo.get_line_with_latest(conn, line_id)
            line = repo.line_to_dict(row, latest)
        line["reference"] = {"photos": [], "stale": False}
        return line

    @app.delete("/api/lines/{line_id}", status_code=204)
    def delete_job_line(line_id: int, worker=Depends(require_worker)):
        now = _now()
        with lock:
            with db_write(conn):
                line = repo.delete_worker_line(conn, line_id, commit=False)
                audit(conn, now, f"worker:{worker['id']}", "line.delete", "job", line["job_id"],
                      {"line_id": line_id, "label": line["label"]})
        return Response(status_code=204)

    @app.post("/api/lines/{line_id}/confirm", status_code=201)
    def confirm_line(
        line_id: int,
        result: str = Form(...),
        qty: int = Form(...),
        reason: str = Form(""),
        serials: str = Form("[]"),
        quality: str = Form("{}"),
        image: UploadFile | None = File(None),
        worker=Depends(require_worker),
    ):
        serial_list = [x.strip() if isinstance(x, str) else x for x in _parse_json(serials, list)]
        quality_map = _parse_json(quality, dict)
        reason = reason.strip()
        # 1. Cheap checks first, under the lock, so a bad request never costs an image decode.
        with lock:
            line = repo.get_line(conn, line_id)
            if line is None:
                raise HTTPException(status_code=404, detail=t("error.not_found"))
            job_id = line["job_id"]
            if repo.lines_blocker(repo.get_job(conn, job_id), line["stage"]):
                raise HTTPException(status_code=409, detail=t("error.wrong_status"))
            error = repo.validate_confirmation(line, result, qty, serial_list, quality_map, reason, image is not None)
        if error:
            raise job_error(ValueError(error.removeprefix("error.")))
        # 2. Decode, normalise and write the photo outside the lock.
        photo_path = ""
        if image is not None:
            try:
                data = images.normalize(_read_upload(image))
            except images.BadImage:
                raise HTTPException(status_code=400, detail=t("error.bad_image"))
            photo_path = images.save_photo(settings.photos_dir, f"jobs/{job_id}", data)
        # 3. Re-check and INSERT under the lock; a failed insert removes the file again.
        now = _now()
        try:
            with lock:
                with db_write(conn):
                    attempt = repo.current_attempt(conn, job_id)
                    confirmation_id = repo.add_confirmation(
                        conn, line_id, worker["id"], attempt, result, qty, serial_list, quality_map, reason,
                        photo_path, now, commit=False,
                    )
                    audit(conn, now, f"worker:{worker['id']}", "line.confirm", "job", job_id,
                          {"line_id": line_id, "confirmation_id": confirmation_id, "result": result, "qty": qty,
                           "attempt": attempt, "photo": bool(photo_path)})
                confirmation = repo.get_confirmation(conn, confirmation_id)
                row, latest = repo.get_line_with_latest(conn, line_id)
                line_json = repo.line_to_dict(row, latest)
        except Exception:
            if photo_path:
                (settings.photos_dir / photo_path).unlink(missing_ok=True)
            raise
        return {"confirmation": confirmation, "line": line_json}

    @app.post("/api/jobs/{job_id}/finish")
    def finish_job(job_id: int, worker=Depends(require_worker)):
        now = _now()
        try:
            with lock:
                with db_write(conn):
                    before = repo.get_job(conn, job_id)
                    new_status = repo.finish_job(conn, job_id, now, commit=False)
                    audit(conn, now, f"worker:{worker['id']}", "job.finish", "job", job_id,
                          {"from": before["status"], "to": new_status})
                return _job_dict(job_id)
        except repo.LinesOpen as e:
            return JSONResponse(
                status_code=409, content={"detail": t("error.lines_open"), "open_line_ids": e.line_ids}
            )

    @app.post("/api/jobs/{job_id}/ship")
    def ship_job(job_id: int, worker=Depends(require_worker_or_expert)):
        now = _now()
        with lock:
            with db_write(conn):
                repo.ship_job(conn, job_id, now, commit=False)
                audit(conn, now, actor_of(worker), "job.ship", "job", job_id, {})
            return _job_dict(job_id)

    @app.get("/health")
    def health():
        with lock:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ('shipped','cancelled')"
            ).fetchone()
        return {"status": "ok", "jobs_open": row["n"]}

    return app
