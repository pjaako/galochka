import hmac
import sqlite3

from fastapi import Header, HTTPException, Request

from galochka.api import t


def _expert_password_ok(request: Request, given: str | None) -> bool:
    expected = request.app.state.settings.expert_password
    return given is not None and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def require_expert(request: Request, x_expert_password: str | None = Header(default=None)) -> None:
    """Dependency for expert routes: the shared password from the env file, in X-Expert-Password."""
    if not _expert_password_ok(request, x_expert_password):
        raise HTTPException(status_code=401, detail=t("error.unauthorized"))


def require_worker(request: Request) -> sqlite3.Row:
    """Dependency for worker routes: returns the logged-in worker row.

    STUB until T3 (workers, badges, sessions): T3 will read the `galochka_session`
    cookie, check last_seen_at + session_minutes, bump last_seen_at and return the
    workers row. Until then no session can exist, so every call is rejected with
    401 error.session_expired (the client redirects to /anmelden). No route in T1
    uses it.
    """
    raise HTTPException(status_code=401, detail=t("error.session_expired"))


def require_worker_or_expert(
    request: Request, x_expert_password: str | None = Header(default=None)
) -> sqlite3.Row | None:
    """Dependency for routes both roles use (item search, reference photos).

    A correct expert password wins and returns None; otherwise the request must carry
    a worker session (require_worker, a stub until T3, so 401 for now). A wrong expert
    password falls through to the worker check.
    """
    if _expert_password_ok(request, x_expert_password):
        return None
    return require_worker(request)
