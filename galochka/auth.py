import hmac
import sqlite3
from datetime import datetime, timedelta

from fastapi import Header, HTTPException, Request

from galochka import repo
from galochka.api import _now, t

SESSION_COOKIE = "galochka_session"


def _expert_password_ok(request: Request, given: str | None) -> bool:
    expected = request.app.state.settings.expert_password
    return given is not None and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def require_expert(request: Request, x_expert_password: str | None = Header(default=None)) -> None:
    """Dependency for expert routes: the shared password from the env file, in X-Expert-Password."""
    if not _expert_password_ok(request, x_expert_password):
        raise HTTPException(status_code=401, detail=t("error.unauthorized"))


def require_worker(request: Request) -> sqlite3.Row:
    """Dependency for worker routes: returns the logged-in worker row (id, name, ...).

    Reads the `galochka_session` cookie. A missing session, an inactive worker or an
    idle time over `session_minutes` raises 401 error.session_expired (the client then
    redirects to /anmelden) and deletes the session row. Otherwise last_seen_at is bumped.
    Sets request.state.session_expires_at for /api/me.
    """
    token = request.cookies.get(SESSION_COOKIE)
    now = _now()
    conn = request.app.state.conn
    with request.app.state.lock:
        row = repo.get_session_worker(conn, token) if token else None
        if row is not None:
            minutes = repo.session_minutes(conn)
            idle_until = datetime.fromisoformat(row["last_seen_at"]) + timedelta(minutes=minutes)
            if not row["active"] or idle_until < datetime.fromisoformat(now):
                repo.delete_session(conn, token)
                row = None
            else:
                repo.touch_session(conn, token, now)
                request.state.session_expires_at = (
                    datetime.fromisoformat(now) + timedelta(minutes=minutes)
                ).isoformat(timespec="seconds")
    if row is None:
        raise HTTPException(status_code=401, detail=t("error.session_expired"))
    return row


def require_worker_or_expert(
    request: Request, x_expert_password: str | None = Header(default=None)
) -> sqlite3.Row | None:
    """Dependency for routes both roles use (item search, reference photos, job reads).

    A correct expert password wins and returns None; otherwise the request must carry
    a worker session and the worker row is returned (401 error.session_expired if not).
    A wrong expert password falls through to the worker check.
    """
    if _expert_password_ok(request, x_expert_password):
        return None
    return require_worker(request)


def actor_of(worker: sqlite3.Row | None) -> str:
    """Audit actor string: 'worker:<id>' for a worker row, 'expert' for None."""
    return "expert" if worker is None else f"worker:{worker['id']}"
