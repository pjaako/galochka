import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from conftest import EXPERT
from galochka import qr, repo
from galochka.auth import SESSION_COOKIE, actor_of

BAD = {"X-Expert-Password": "wrong"}
ALPHABET = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")


# --- helpers ---------------------------------------------------------------------------------


def _create_worker(client, name="Anna"):
    resp = client.post("/api/expert/workers", headers=EXPERT, json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _other_client(client):
    """A TestClient with its own empty cookie jar on the same app (no lifespan of its own)."""
    return TestClient(client.app)


def _login_qr(c, png):
    return c.post("/api/login/qr", files={"image": ("badge.jpg", png, "image/jpeg")})


def _rows(client, sql, *args):
    with client.app.state.lock:
        return client.app.state.conn.execute(sql, args).fetchall()


def _audit_actions(client):
    return [(r["actor"], r["action"], r["entity_id"]) for r in _rows(client, "SELECT * FROM audit ORDER BY id")]


def _svg_to_png(svg: str, scale: int = 10) -> bytes:
    """Rasterise a qrcode SvgPathImage (only axis-aligned squares) so decode_qr can read it."""
    side = float(re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))
    px = int(round(side * scale))
    image = Image.new("RGB", (px, px), "white")
    draw = ImageDraw.Draw(image)
    for x1, y1, x2, y2 in re.findall(r"M([\d.]+),([\d.]+)H([\d.]+)V([\d.]+)H", svg):
        draw.rectangle(
            [round(float(x1) * scale), round(float(y1) * scale), round(float(x2) * scale) - 1, round(float(y2) * scale) - 1],
            fill="black",
        )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _time_travel(monkeypatch, minutes):
    when = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    monkeypatch.setattr("galochka.auth._now", lambda: when)


def _set_session_minutes(client, minutes):
    with client.app.state.lock:
        repo.set_setting(client.app.state.conn, "session_minutes", str(minutes))


# --- expert auth -----------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, BAD])
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/expert/workers"),
        ("post", "/api/expert/workers"),
        ("patch", "/api/expert/workers/1"),
        ("post", "/api/expert/workers/1/badge"),
        ("delete", "/api/expert/workers/1/badge"),
        ("get", "/api/expert/workers/1/badge.svg"),
    ],
)
def test_worker_expert_routes_need_password(client, method, path, headers):
    resp = getattr(client, method)(path, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Nicht berechtigt."}


# --- workers and badges ----------------------------------------------------------------------


def test_create_worker_returns_badge_and_token_once(client):
    worker = _create_worker(client, "  Anna Beispiel  ")
    assert worker["name"] == "Anna Beispiel"
    assert worker["active"] is True
    assert set(worker) == {"id", "name", "active", "created_at", "badge", "token"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", worker["token"])
    code = worker["badge"]["short_code"]
    assert len(code) == 6 and set(code) <= ALPHABET
    assert worker["badge"]["created_at"] == worker["created_at"]


def test_token_is_stored_hashed_for_lookup(client):
    worker = _create_worker(client)
    row = _rows(client, "SELECT * FROM badges")[0]
    assert row["token_hash"] == hashlib.sha256(worker["token"].encode()).hexdigest()
    assert row["token"] == worker["token"]  # plaintext only for reprinting (migration 002)


def test_listings_never_contain_the_token(client):
    worker = _create_worker(client)
    listing = client.get("/api/expert/workers", headers=EXPERT)
    assert listing.status_code == 200
    assert worker["token"] not in listing.text
    assert "token" not in json.dumps(listing.json())
    assert listing.json() == [
        {
            "id": worker["id"],
            "name": "Anna",
            "active": True,
            "created_at": worker["created_at"],
            "badge": worker["badge"],
        }
    ]
    patched = client.patch(f"/api/expert/workers/{worker['id']}", headers=EXPERT, json={"name": "Anna B"})
    assert "token" not in patched.json()
    assert worker["token"] not in patched.text


def test_list_orders_active_first_then_name_and_shows_missing_badge(client):
    b = _create_worker(client, "Berta")
    a = _create_worker(client, "anton")
    c = _create_worker(client, "Carl")
    client.patch(f"/api/expert/workers/{a['id']}", headers=EXPERT, json={"active": False})
    client.delete(f"/api/expert/workers/{c['id']}/badge", headers=EXPERT)
    listing = client.get("/api/expert/workers", headers=EXPERT).json()
    assert [w["name"] for w in listing] == ["Berta", "Carl", "anton"]
    assert listing[1]["badge"] is None
    assert listing[2]["active"] is False and listing[2]["badge"] is not None
    assert b["id"] == listing[0]["id"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 81])
def test_create_worker_validates_name(client, name):
    assert client.post("/api/expert/workers", headers=EXPERT, json={"name": name}).status_code == 422
    assert client.get("/api/expert/workers", headers=EXPERT).json() == []


def test_patch_worker_renames_and_unknown_is_404(client):
    worker = _create_worker(client)
    resp = client.patch(f"/api/expert/workers/{worker['id']}", headers=EXPERT, json={"name": " Anna B "})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Anna B"
    assert resp.json()["badge"] == worker["badge"]
    assert client.patch("/api/expert/workers/999", headers=EXPERT, json={"name": "x"}).status_code == 404
    assert client.post("/api/expert/workers/999/badge", headers=EXPERT).status_code == 404
    assert client.delete("/api/expert/workers/999/badge", headers=EXPERT).status_code == 404
    assert client.get("/api/expert/workers/999/badge.svg", headers=EXPERT).status_code == 404


def test_badge_svg_decodes_to_the_badge_payload(client):
    worker = _create_worker(client)
    resp = client.get(f"/api/expert/workers/{worker['id']}/badge.svg", headers=EXPERT)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.headers["cache-control"] == "no-store"
    assert "<path" in resp.text
    assert qr.decode_qr(_svg_to_png(resp.text)) == f"galochka:badge:{worker['token']}"


def test_issue_badge_short_codes_are_unique_among_unrevoked(conn, monkeypatch):
    now = "2026-11-01T10:00:00+00:00"
    w1 = repo.create_worker(conn, "A", now)
    w2 = repo.create_worker(conn, "B", now)
    # Force a collision: the second badge first draws the same code, then a fresh one.
    draws = iter("AAAAAA" + "AAAAAA" + "BBBBBB")
    monkeypatch.setattr("galochka.repo.secrets.choice", lambda alphabet: next(draws))
    _, _, code1 = repo.issue_badge(conn, w1, now)
    _, _, code2 = repo.issue_badge(conn, w2, now)
    assert (code1, code2) == ("AAAAAA", "BBBBBB")
    # A revoked code may be reused.
    repo.revoke_badges(conn, w1, now)
    draws2 = iter("AAAAAA")
    monkeypatch.setattr("galochka.repo.secrets.choice", lambda alphabet: next(draws2))
    assert repo.issue_badge(conn, w1, now)[2] == "AAAAAA"


# --- login by QR -------------------------------------------------------------------------------


def test_login_by_qr_sets_httponly_session_cookie_and_me_works(client, qr_png):
    worker = _create_worker(client)
    c = _other_client(client)
    resp = _login_qr(c, qr_png(f"galochka:badge:{worker['token']}"))
    assert resp.status_code == 200
    assert resp.json() == {"worker": {"id": worker["id"], "name": "Anna"}}
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low
    assert "secure" not in low and "max-age" not in low and "expires" not in low

    me = c.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert body["worker"] == {"id": worker["id"], "name": "Anna"}
    assert datetime.fromisoformat(body["expires_at"]) > datetime.now(timezone.utc) + timedelta(minutes=29)


def test_login_by_qr_bad_inputs(client, qr_png):
    worker = _create_worker(client)
    c = _other_client(client)
    unreadable = _login_qr(c, b"this is not an image")
    assert unreadable.status_code == 400
    assert unreadable.json() == {"detail": "Das Bild konnte nicht gelesen werden."}
    blank = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(blank, format="PNG")
    assert _login_qr(c, blank.getvalue()).status_code == 400

    unknown = "Ausweis oder Kurzcode unbekannt oder gesperrt."
    for text in (
        "hello world",
        "galochka:job:" + worker["token"],
        "galochka:badge:short",
        "galochka:badge:" + "A" * 32,  # well-formed, but no such token
    ):
        resp = _login_qr(c, qr_png(text))
        assert resp.status_code == 401, text
        assert resp.json() == {"detail": unknown}
    assert SESSION_COOKIE not in c.cookies
    assert c.get("/api/me").status_code == 401


def test_login_by_qr_accepts_a_phone_style_jpeg(client, qr_png):
    worker = _create_worker(client)
    png = Image.open(io.BytesIO(qr_png(f"galochka:badge:{worker['token']}"))).convert("RGB")
    canvas = Image.new("RGB", (1600, 1200), (120, 110, 100))
    canvas.paste(png.resize((600, 600)), (500, 300))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=85)
    assert _login_qr(_other_client(client), buf.getvalue()).status_code == 200


# --- login by code -----------------------------------------------------------------------------


def test_login_by_code_is_case_insensitive_and_strips_whitespace(client):
    worker = _create_worker(client)
    code = worker["badge"]["short_code"]
    c = _other_client(client)
    resp = c.post("/api/login/code", json={"code": f"  {code[:3].lower()} {code[3:].lower()}\n"})
    assert resp.status_code == 200
    assert resp.json() == {"worker": {"id": worker["id"], "name": "Anna"}}
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert c.get("/api/me").json()["worker"]["id"] == worker["id"]


@pytest.mark.parametrize("code", ["", "   ", "AAAAAA", "O0I1LL", "x" * 64])
def test_login_by_wrong_code_is_401(client, code):
    _create_worker(client)
    c = _other_client(client)
    resp = c.post("/api/login/code", json={"code": code})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Ausweis oder Kurzcode unbekannt oder gesperrt."}
    assert SESSION_COOKIE not in c.cookies


def test_login_code_rejects_malformed_body(client):
    c = _other_client(client)
    assert c.post("/api/login/code", json={}).status_code == 422
    assert c.post("/api/login/code", json={"code": "x" * 65}).status_code == 422


def test_new_login_replaces_the_old_session_cookie(client):
    a = _create_worker(client, "Anna")
    b = _create_worker(client, "Bernd")
    c = _other_client(client)
    c.post("/api/login/code", json={"code": a["badge"]["short_code"]})
    old = c.cookies.get(SESSION_COOKIE)
    c.post("/api/login/code", json={"code": b["badge"]["short_code"]})
    assert c.cookies.get(SESSION_COOKIE) != old
    assert c.get("/api/me").json()["worker"]["name"] == "Bernd"
    assert _rows(client, "SELECT 1 FROM sessions WHERE token=?", old) == []
    assert len(_rows(client, "SELECT 1 FROM sessions")) == 1


# --- session lifetime ----------------------------------------------------------------------------


def test_me_without_cookie_or_with_garbage_is_401(client):
    c = _other_client(client)
    for headers in ({}, {"Cookie": f"{SESSION_COOKIE}=nonsense"}):
        resp = c.get("/api/me", headers=headers)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Die Sitzung ist abgelaufen. Bitte neu anmelden."}


def test_idle_session_expires_and_row_is_deleted(worker_client, client, monkeypatch):
    c, worker = worker_client
    _set_session_minutes(client, 1)
    assert c.get("/api/me").status_code == 200
    token = c.cookies.get(SESSION_COOKIE)
    _time_travel(monkeypatch, 5)
    resp = c.get("/api/me")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Die Sitzung ist abgelaufen. Bitte neu anmelden."}
    assert _rows(client, "SELECT 1 FROM sessions WHERE token=?", token) == []
    assert c.get("/api/me").status_code == 401  # and stays expired


def test_activity_slides_the_expiry(worker_client, client, monkeypatch):
    c, _ = worker_client  # default 30 minutes
    _time_travel(monkeypatch, 20)
    assert c.get("/api/me").status_code == 200  # bumps last_seen_at to +20
    _time_travel(monkeypatch, 40)
    assert c.get("/api/me").status_code == 200  # 20 min idle < 30
    _time_travel(monkeypatch, 40 + 31)
    assert c.get("/api/me").status_code == 401


def test_worker_routes_accept_the_session_cookie(worker_client):
    c, _ = worker_client
    assert c.get("/api/items").status_code == 200
    assert TestClient(c.app).get("/api/items").status_code == 401


def test_expired_sessions_are_cleaned_on_login(client, monkeypatch):
    worker = _create_worker(client)
    old = _other_client(client)
    old.post("/api/login/code", json={"code": worker["badge"]["short_code"]})
    with client.app.state.lock:
        client.app.state.conn.execute("UPDATE sessions SET last_seen_at='2020-01-01T00:00:00+00:00'")
        client.app.state.conn.commit()
    _other_client(client).post("/api/login/code", json={"code": worker["badge"]["short_code"]})
    assert len(_rows(client, "SELECT 1 FROM sessions")) == 1


# --- logout --------------------------------------------------------------------------------------


def test_logout_is_204_clears_cookie_and_kills_the_session(worker_client, client):
    c, worker = worker_client
    token = c.cookies.get(SESSION_COOKIE)
    resp = c.post("/api/logout")
    assert resp.status_code == 204
    assert resp.content == b""
    cleared = resp.headers["set-cookie"].lower()
    assert cleared.startswith(f"{SESSION_COOKIE}=")
    assert "max-age=0" in cleared or "1970" in cleared
    assert SESSION_COOKIE not in c.cookies
    assert c.get("/api/me").status_code == 401
    # the old cookie is dead on the server side, not only removed from the browser
    replay = TestClient(client.app).get("/api/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"})
    assert replay.status_code == 401
    assert _rows(client, "SELECT 1 FROM sessions") == []


def test_logout_without_session_is_still_204(client):
    assert _other_client(client).post("/api/logout").status_code == 204


# --- revoke / re-issue / deactivate ----------------------------------------------------------------


def test_revoke_badge_ends_sessions_and_kills_both_credentials(client, qr_png):
    worker = _create_worker(client)
    c = _other_client(client)
    _login_qr(c, qr_png(f"galochka:badge:{worker['token']}"))
    assert c.get("/api/me").status_code == 200

    assert client.delete(f"/api/expert/workers/{worker['id']}/badge", headers=EXPERT).status_code == 204
    assert c.get("/api/me").status_code == 401
    fresh = _other_client(client)
    assert _login_qr(fresh, qr_png(f"galochka:badge:{worker['token']}")).status_code == 401
    assert fresh.post("/api/login/code", json={"code": worker["badge"]["short_code"]}).status_code == 401
    assert client.get(f"/api/expert/workers/{worker['id']}/badge.svg", headers=EXPERT).status_code == 404
    listing = client.get("/api/expert/workers", headers=EXPERT).json()
    assert listing[0]["badge"] is None and listing[0]["active"] is True
    # revoked rows are kept as history, plaintext token is gone
    row = _rows(client, "SELECT * FROM badges")[0]
    assert row["revoked_at"] and row["token"] == ""


def test_reissue_keeps_worker_id_and_replaces_the_badge(client, qr_png):
    worker = _create_worker(client)
    c = _other_client(client)
    c.post("/api/login/code", json={"code": worker["badge"]["short_code"]})

    resp = client.post(f"/api/expert/workers/{worker['id']}/badge", headers=EXPERT)
    assert resp.status_code == 201
    new = resp.json()
    assert set(new) == {"short_code", "token"}
    assert new["token"] != worker["token"]
    assert c.get("/api/me").status_code == 401  # old session gone

    fresh = _other_client(client)
    assert _login_qr(fresh, qr_png(f"galochka:badge:{worker['token']}")).status_code == 401
    assert fresh.post("/api/login/code", json={"code": worker["badge"]["short_code"]}).status_code == 401
    ok = _login_qr(fresh, qr_png(f"galochka:badge:{new['token']}"))
    assert ok.status_code == 200
    assert ok.json()["worker"]["id"] == worker["id"]
    assert fresh.post("/api/login/code", json={"code": new["short_code"].lower()}).status_code == 200

    svg = client.get(f"/api/expert/workers/{worker['id']}/badge.svg", headers=EXPERT).text
    assert qr.decode_qr(_svg_to_png(svg)) == f"galochka:badge:{new['token']}"
    assert len(_rows(client, "SELECT 1 FROM workers")) == 1
    assert len(_rows(client, "SELECT 1 FROM badges WHERE revoked_at IS NULL")) == 1
    assert len(_rows(client, "SELECT 1 FROM badges")) == 2


def test_deactivated_worker_is_logged_out_and_cannot_log_in(client, qr_png):
    worker = _create_worker(client)
    c = _other_client(client)
    c.post("/api/login/code", json={"code": worker["badge"]["short_code"]})
    resp = client.patch(f"/api/expert/workers/{worker['id']}", headers=EXPERT, json={"active": False})
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert c.get("/api/me").status_code == 401
    assert _login_qr(_other_client(client), qr_png(f"galochka:badge:{worker['token']}")).status_code == 401
    assert _other_client(client).post("/api/login/code", json={"code": worker["badge"]["short_code"]}).status_code == 401

    # reactivating makes the same badge work again
    client.patch(f"/api/expert/workers/{worker['id']}", headers=EXPERT, json={"active": True})
    assert _other_client(client).post("/api/login/code", json={"code": worker["badge"]["short_code"]}).status_code == 200


def test_inactive_worker_session_row_is_rejected_even_if_it_survives(worker_client, client):
    c, worker = worker_client
    with client.app.state.lock:
        client.app.state.conn.execute("UPDATE workers SET active=0 WHERE id=?", (worker["id"],))
        client.app.state.conn.commit()
    assert c.get("/api/me").status_code == 401
    assert _rows(client, "SELECT 1 FROM sessions") == []


# --- audit -------------------------------------------------------------------------------------------


def test_audit_trail_for_the_badge_lifecycle(client):
    worker = _create_worker(client)
    wid = str(worker["id"])
    c = _other_client(client)
    c.post("/api/login/code", json={"code": worker["badge"]["short_code"]})
    c.post("/api/logout")
    client.patch(f"/api/expert/workers/{wid}", headers=EXPERT, json={"name": "Anna B", "active": False})
    client.post(f"/api/expert/workers/{wid}/badge", headers=EXPERT)
    client.delete(f"/api/expert/workers/{wid}/badge", headers=EXPERT)
    actions = _audit_actions(client)
    assert actions == [
        ("expert", "worker.create", wid),
        ("expert", "badge.issue", wid),
        (f"worker:{wid}", "login", wid),
        (f"worker:{wid}", "logout", wid),
        ("expert", "worker.update", wid),
        ("expert", "badge.issue", wid),
        ("expert", "badge.revoke", wid),
    ]
    detail = json.loads(_rows(client, "SELECT detail FROM audit WHERE action='worker.update'")[0]["detail"])
    assert detail == {"name": "Anna B", "active": False}
    # no credential ever lands in the audit trail
    dump = json.dumps([dict(r) for r in _rows(client, "SELECT * FROM audit")])
    assert worker["token"] not in dump and worker["badge"]["short_code"] not in dump


# --- fixtures and helpers --------------------------------------------------------------------------------


def test_worker_client_fixture_and_second_worker(worker_client, new_worker_client):
    c, anna = worker_client
    assert c.get("/api/me").json()["worker"] == {"id": anna["id"], "name": "Anna"}
    c2, bernd = new_worker_client("Bernd")
    assert bernd["id"] != anna["id"]
    assert c2.get("/api/me").json()["worker"]["name"] == "Bernd"
    assert c.get("/api/me").json()["worker"]["name"] == "Anna"  # separate cookie jars


def test_actor_of():
    assert actor_of(None) == "expert"
    assert actor_of({"id": 7, "name": "x"}) == "worker:7"


def test_require_worker_or_expert_returns_none_for_expert_and_row_for_worker(client, worker_client):
    from fastapi import Depends, FastAPI

    from galochka.auth import require_worker_or_expert

    c, worker = worker_client
    probe = FastAPI()
    probe.state = client.app.state
    seen = {}

    @probe.get("/probe")
    def _probe(who=Depends(require_worker_or_expert)):
        seen["who"] = who
        return {"actor": actor_of(who)}

    p = TestClient(probe)
    assert p.get("/probe", headers=EXPERT).json() == {"actor": "expert"}
    assert seen["who"] is None
    assert p.get("/probe", headers=BAD).status_code == 401
    token = c.cookies.get(SESSION_COOKIE)
    got = p.get("/probe", headers={"Cookie": f"{SESSION_COOKIE}={token}"})
    assert got.json() == {"actor": f"worker:{worker['id']}"}
    assert seen["who"]["name"] == "Anna"
