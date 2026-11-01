import json
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field, StringConstraints, field_validator

from galochka import importer, qr, repo
from galochka.api import _now, _read_upload, t
from galochka.audit import audit
from galochka.auth import require_expert
from galochka.cassandra_client import CassandraUnavailable

router = APIRouter(prefix="/api/expert", dependencies=[Depends(require_expert)])

_DEFAULT_QUALITY_POINTS = ["sauber", "unbeschädigt", "funktion"]


class SettingsRequest(BaseModel):
    second_check_default: bool
    session_minutes: int = Field(ge=1, le=720)
    default_quality_points: list[str] = Field(max_length=20)

    @field_validator("default_quality_points")
    @classmethod
    def _points_valid(cls, points: list[str]) -> list[str]:
        cleaned = [p.strip() for p in points]
        if any(not p or len(p) > 60 for p in cleaned):
            raise ValueError("quality points must be non-empty strings of at most 60 characters")
        # Points are used as keys of confirmations.quality, so they must be unique.
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("quality points must be unique")
        return cleaned


def _read_settings(conn) -> dict:
    try:
        points = json.loads(repo.get_setting(conn, "default_quality_points", "[]"))
    except ValueError:
        points = list(_DEFAULT_QUALITY_POINTS)
    try:
        minutes = int(repo.get_setting(conn, "session_minutes", "30"))
    except ValueError:
        minutes = 30
    return {
        "second_check_default": repo.get_setting(conn, "second_check_default", "1") == "1",
        "session_minutes": minutes,
        "default_quality_points": points,
    }


@router.get("/settings")
def get_settings(request: Request):
    with request.app.state.lock:
        return _read_settings(request.app.state.conn)


@router.put("/settings", status_code=204)
def put_settings(body: SettingsRequest, request: Request):
    conn = request.app.state.conn
    with request.app.state.lock:
        try:
            repo.set_setting(conn, "second_check_default", "1" if body.second_check_default else "0", commit=False)
            repo.set_setting(conn, "session_minutes", str(body.session_minutes), commit=False)
            repo.set_setting(
                conn,
                "default_quality_points",
                json.dumps(body.default_quality_points, ensure_ascii=False),
                commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return Response(status_code=204)


def run_items_sync(app: FastAPI, actor: str) -> dict:
    """Sync the item catalog from Cassandra and write the audit row.

    The HTTP part runs outside the lock, the DB part inside. Raises CassandraUnavailable
    (also when no URL is configured). Used by the expert button and the daily thread.
    """
    catalog = importer.fetch_catalog(app.state.cassandra)
    now = _now()
    with app.state.lock:
        result = importer.apply_catalog(app.state.conn, catalog, now)
        audit(app.state.conn, now, actor, "items.sync", "items", "", result)
    return result


@router.get("/items/stats")
def items_stats(request: Request):
    conn = request.app.state.conn
    with request.app.state.lock:
        total = conn.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
        synced_at = repo.get_setting(conn, "items_synced_at", "") or None
    return {
        "total_active": total,
        "synced_at": synced_at,
        "cassandra_url": request.app.state.settings.cassandra_url,
    }


@router.post("/sync/items")
def sync_items(request: Request):
    if not request.app.state.settings.cassandra_url:
        raise HTTPException(status_code=409, detail=t("error.cassandra_not_configured"))
    try:
        return run_items_sync(request.app, "expert")
    except CassandraUnavailable:
        raise HTTPException(status_code=502, detail=t("error.cassandra_unreachable"))


@router.post("/import/items")
def import_items(request: Request, file: UploadFile = File(...)):
    data = _read_upload(file)
    now = _now()
    conn = request.app.state.conn
    with request.app.state.lock:
        try:
            result = importer.import_items(conn, data, now)
        except importer.ImportError_:
            raise HTTPException(status_code=400, detail=t("error.bad_csv"))
        audit(conn, now, "expert", "items.import", "items", "", dict(result, filename=file.filename or ""))
    return result


# --- workers and badges ---------------------------------------------------------------------

WorkerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class WorkerCreate(BaseModel):
    name: WorkerName


class WorkerUpdate(BaseModel):
    name: WorkerName | None = None
    active: bool | None = None


def _worker_json(row) -> dict:
    """Listing shape. Never contains the badge token."""
    badge = None
    if row["badge_short_code"] is not None:
        badge = {"short_code": row["badge_short_code"], "created_at": row["badge_created_at"]}
    return {
        "id": row["id"],
        "name": row["name"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "badge": badge,
    }


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=t("error.not_found"))


@router.get("/workers")
def list_workers(request: Request):
    with request.app.state.lock:
        rows = repo.list_workers(request.app.state.conn)
    return [_worker_json(r) for r in rows]


@router.post("/workers", status_code=201)
def create_worker(body: WorkerCreate, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        try:
            worker_id = repo.create_worker(conn, body.name, now, commit=False)
            badge_id, token, _ = repo.issue_badge(conn, worker_id, now, commit=False)
            audit(conn, now, "expert", "worker.create", "worker", worker_id, {"name": body.name})
            audit(conn, now, "expert", "badge.issue", "worker", worker_id, {"badge_id": badge_id})
        except Exception:
            conn.rollback()
            raise
        result = _worker_json(repo.get_worker(conn, worker_id))
    # The token is shown here once; later only the printable badge SVG carries it.
    result["token"] = token
    return result


@router.patch("/workers/{worker_id}")
def update_worker(worker_id: int, body: WorkerUpdate, request: Request):
    conn = request.app.state.conn
    changes = body.model_dump(exclude_none=True)
    with request.app.state.lock:
        try:
            repo.update_worker(conn, worker_id, name=body.name, active=body.active, commit=False)
        except KeyError:
            conn.rollback()
            raise _not_found()
        except Exception:
            conn.rollback()
            raise
        if changes:
            audit(conn, _now(), "expert", "worker.update", "worker", worker_id, changes)
        else:
            conn.commit()
        return _worker_json(repo.get_worker(conn, worker_id))


@router.post("/workers/{worker_id}/badge", status_code=201)
def reissue_badge(worker_id: int, request: Request):
    """Issue a new badge: the old one is revoked (and the worker logged out) in the same step."""
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        try:
            revoked = repo.revoke_badges(conn, worker_id, now, commit=False)
            badge_id, token, short_code = repo.issue_badge(conn, worker_id, now, commit=False)
        except KeyError:
            conn.rollback()
            raise _not_found()
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "badge.issue", "worker", worker_id, {"badge_id": badge_id, "revoked": revoked})
    return {"short_code": short_code, "token": token}


@router.delete("/workers/{worker_id}/badge", status_code=204)
def revoke_badge(worker_id: int, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        if repo.get_worker(conn, worker_id) is None:
            raise _not_found()
        try:
            revoked = repo.revoke_badges(conn, worker_id, now, commit=False)
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "badge.revoke", "worker", worker_id, {"revoked": revoked})
    return Response(status_code=204)


@router.get("/workers/{worker_id}/badge.svg")
def badge_svg(worker_id: int, request: Request):
    with request.app.state.lock:
        badge = repo.get_active_badge(request.app.state.conn, worker_id)
    if badge is None or not badge["token"]:
        raise _not_found()
    svg = qr.make_svg(f"galochka:badge:{badge['token']}")  # rendering outside the lock
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
