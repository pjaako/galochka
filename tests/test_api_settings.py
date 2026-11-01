import pytest

from conftest import EXPERT

BAD = {"X-Expert-Password": "wrong"}
VALID = {"second_check_default": False, "session_minutes": 45, "default_quality_points": ["sauber", "Funktion geprüft"]}


@pytest.mark.parametrize("method", ["get", "put"])
@pytest.mark.parametrize("headers", [{}, BAD])
def test_settings_require_expert_password(client, method, headers):
    kwargs = {"json": VALID} if method == "put" else {}
    resp = getattr(client, method)("/api/expert/settings", headers=headers, **kwargs)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Nicht berechtigt."}


def test_get_settings_shape_and_defaults(client):
    resp = client.get("/api/expert/settings", headers=EXPERT)
    assert resp.status_code == 200
    assert resp.json() == {
        "second_check_default": True,
        "session_minutes": 30,
        "default_quality_points": ["sauber", "unbeschädigt", "Funktion geprüft"],
    }


def test_put_settings_round_trip(client):
    resp = client.put("/api/expert/settings", headers=EXPERT, json=VALID)
    assert resp.status_code == 204
    assert resp.content == b""
    assert client.get("/api/expert/settings", headers=EXPERT).json() == VALID


def test_put_settings_persists_in_db(client):
    client.put("/api/expert/settings", headers=EXPERT, json=VALID)
    with client.app.state.lock:
        rows = dict(client.app.state.conn.execute("SELECT key, value FROM settings").fetchall())
    assert rows["second_check_default"] == "0"
    assert rows["session_minutes"] == "45"
    assert rows["default_quality_points"] == '["sauber", "Funktion geprüft"]'


def test_put_settings_strips_points_and_allows_empty_list(client):
    body = dict(VALID, default_quality_points=["  sauber  "])
    assert client.put("/api/expert/settings", headers=EXPERT, json=body).status_code == 204
    assert client.get("/api/expert/settings", headers=EXPERT).json()["default_quality_points"] == ["sauber"]
    body = dict(VALID, default_quality_points=[])
    assert client.put("/api/expert/settings", headers=EXPERT, json=body).status_code == 204


@pytest.mark.parametrize(
    "patch",
    [
        {"session_minutes": 0},
        {"session_minutes": 721},
        {"session_minutes": "abc"},
        {"default_quality_points": [""]},
        {"default_quality_points": ["   "]},
        {"default_quality_points": ["a", "a"]},
        {"default_quality_points": "sauber"},
        {"second_check_default": "maybe"},
    ],
)
def test_put_settings_validation_422_and_nothing_saved(client, patch):
    resp = client.put("/api/expert/settings", headers=EXPERT, json=dict(VALID, **patch))
    assert resp.status_code == 422
    assert client.get("/api/expert/settings", headers=EXPERT).json()["session_minutes"] == 30


def test_put_settings_missing_field_422(client):
    body = {k: v for k, v in VALID.items() if k != "session_minutes"}
    assert client.put("/api/expert/settings", headers=EXPERT, json=body).status_code == 422


def test_session_minutes_bounds_accepted(client):
    for minutes in (1, 720):
        assert client.put("/api/expert/settings", headers=EXPERT, json=dict(VALID, session_minutes=minutes)).status_code == 204


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "jobs_open": 0}


def test_health_counts_only_open_jobs(client):
    now = "2026-11-01T00:00:00+00:00"
    with client.app.state.lock:
        conn = client.app.state.conn
        tid = conn.execute("INSERT INTO templates (name, created_at, updated_at) VALUES ('t', ?, ?)", (now, now)).lastrowid
        for i, status in enumerate(["open", "packing", "shipped", "cancelled", "intake"]):
            conn.execute(
                "INSERT INTO jobs (kind, template_id, order_ref, status, created_by, created_at, stage_changed_at) "
                "VALUES ('delivery', ?, ?, ?, 'expert', ?, ?)",
                (tid, f"o{i}", status, now, now),
            )
        conn.commit()
    assert client.get("/health").json() == {"status": "ok", "jobs_open": 3}
