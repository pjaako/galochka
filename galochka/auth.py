import hmac
import sqlite3

from fastapi import Header, HTTPException, Request

from galochka.api import t


def require_expert(request: Request, x_expert_password: str | None = Header(default=None)) -> None:
    """Dependency for expert routes: the shared password from the env file, in X-Expert-Password."""
    expected = request.app.state.settings.expert_password
    ok = x_expert_password is not None and hmac.compare_digest(
        x_expert_password.encode("utf-8"), expected.encode("utf-8")
    )
    if not ok:
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
