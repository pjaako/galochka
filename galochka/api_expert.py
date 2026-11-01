import json

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field, field_validator

from galochka import importer, repo
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
