import json

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator

from galochka import repo
from galochka.auth import require_expert

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
