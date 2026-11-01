import pytest

from conftest import EXPERT
from galochka import repo

T = "/api/expert/templates"


def _line(label="Kabel", **kw):
    return {"label": label, **kw}


def _create(client, name="Set A", lines=None, **kw):
    body = {"name": name, "lines": lines if lines is not None else [_line("Eins"), _line("Zwei")], **kw}
    resp = client.post(T, headers=EXPERT, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_body(tpl, **kw):
    body = {
        "name": tpl["name"],
        "product_item_id": tpl["product_item_id"],
        "second_check": tpl["second_check"],
        "active": tpl["active"],
        "lines": [
            {k: ln[k] for k in ("id", "item_id", "label", "instruction", "qty", "photo_required",
                                "serial_required", "quality_points", "photo_item_id")}
            for ln in tpl["lines"]
        ],
    }
    body.update(kw)
    return body


def test_expert_routes_need_the_password(client):
    assert client.get(T).status_code == 401
    assert client.post(T, json={"name": "x", "lines": []}).status_code == 401
    assert client.get(f"{T}/1").status_code == 401
    assert client.put(f"{T}/1", json={"name": "x", "lines": []}).status_code == 401
    assert client.delete(f"{T}/1").status_code == 401
    assert client.post(f"{T}/1/duplicate").status_code == 401
    assert client.get(T, headers={"X-Expert-Password": "falsch"}).status_code == 401


def test_worker_list_auth_and_shape(client, new_worker_client):
    assert client.get("/api/templates").status_code == 401
    tpl = _create(client, "Aktiv", product_item_id=None, second_check=False)
    worker, _ = new_worker_client("Anna")
    for resp in (worker.get("/api/templates"), client.get("/api/templates", headers=EXPERT)):
        assert resp.status_code == 200
        assert resp.json() == [
            {"id": tpl["id"], "name": "Aktiv", "product_item_id": None, "line_count": 2, "second_check": False}
        ]


def test_inactive_template_hidden_from_worker_list(client, new_worker_client):
    tpl = _create(client, "Alt")
    keep = _create(client, "Neu")
    resp = client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=_put_body(tpl, active=False))
    assert resp.status_code == 200 and resp.json()["active"] is False
    worker, _ = new_worker_client("Anna")
    assert [t["id"] for t in worker.get("/api/templates").json()] == [keep["id"]]
    assert [t["id"] for t in client.get(T, headers=EXPERT).json()] == [keep["id"]]
    listing = client.get(f"{T}?all=1", headers=EXPERT).json()
    assert {t["id"]: t["active"] for t in listing} == {tpl["id"]: False, keep["id"]: True}


def test_create_and_get_full_template(client, add_items):
    add_items(("P1", "Produkt"), ("A1", "Kabel USB"), ("A2", "Netzteil"))
    tpl = _create(
        client,
        "Set USB",
        product_item_id="P1",
        second_check=True,
        lines=[
            {"item_id": "A1", "qty": 2, "instruction": "Beutel", "quality_points": ["sauber", "heil"],
             "serial_required": True, "photo_item_id": "A2"},
            {"item_id": "A2", "label": "Eigenes Label", "photo_required": False},
            {"label": "Freitext"},
        ],
    )
    got = client.get(f"{T}/{tpl['id']}", headers=EXPERT).json()
    assert got == tpl
    assert got["product_item_id"] == "P1" and got["second_check"] is True and got["active"] is True
    assert [ln["position"] for ln in got["lines"]] == [1, 2, 3]
    first, second, third = got["lines"]
    assert first["label"] == "Kabel USB"  # empty label defaults to the item name
    assert first["item_name"] == "Kabel USB" and first["qty"] == 2 and first["serial_required"] is True
    assert first["quality_points"] == ["sauber", "heil"] and first["photo_item_id"] == "A2"
    assert first["photo_required"] is True and first["instruction"] == "Beutel"
    assert second["label"] == "Eigenes Label" and second["photo_required"] is False and second["quality_points"] == []
    assert third["item_id"] is None and third["item_name"] == "" and third["label"] == "Freitext"
    assert [k for k in first] == ["id", "position", "item_id", "item_name", "label", "instruction", "qty",
                                  "photo_required", "serial_required", "quality_points", "photo_item_id"]


def test_get_unknown_template_is_404(client):
    for resp in (
        client.get(f"{T}/99", headers=EXPERT),
        client.put(f"{T}/99", headers=EXPERT, json={"name": "x", "lines": []}),
        client.delete(f"{T}/99", headers=EXPERT),
        client.post(f"{T}/99/duplicate", headers=EXPERT),
    ):
        assert resp.status_code == 404


def test_put_keeps_ids_of_unchanged_lines_and_reorders(client):
    tpl = _create(client, lines=[_line("A"), _line("B"), _line("C")])
    a, b, c = tpl["lines"]
    body = _put_body(tpl)
    # reorder C, A, B; edit A; drop nothing
    body["lines"] = [body["lines"][2], dict(body["lines"][0], qty=5), body["lines"][1]]
    got = client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=body).json()
    assert [(ln["id"], ln["label"], ln["position"]) for ln in got["lines"]] == [
        (c["id"], "C", 1), (a["id"], "A", 2), (b["id"], "B", 3),
    ]
    assert got["lines"][1]["qty"] == 5


def test_put_inserts_new_and_deletes_missing_lines(client):
    tpl = _create(client, lines=[_line("A"), _line("B"), _line("C")])
    a, b, c = tpl["lines"]
    body = _put_body(tpl, name="Neu benannt", second_check=False)
    body["lines"] = [body["lines"][1], {"label": "Neu"}, body["lines"][2]]
    got = client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=body).json()
    assert got["name"] == "Neu benannt" and got["second_check"] is False
    assert [ln["label"] for ln in got["lines"]] == ["B", "Neu", "C"]
    assert got["lines"][0]["id"] == b["id"] and got["lines"][2]["id"] == c["id"]
    assert got["lines"][1]["id"] not in (a["id"], b["id"], c["id"])
    # a foreign line id (from another template) is not adopted: a fresh line is inserted
    other = _create(client, "Anderes", lines=[_line("X")])
    foreign = other["lines"][0]["id"]
    body["lines"] = [{"id": foreign, "label": "Fremd"}]
    got = client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=body).json()
    assert got["lines"][0]["id"] != foreign
    assert [ln["label"] for ln in client.get(f"{T}/{other['id']}", headers=EXPERT).json()["lines"]] == ["X"]


def test_put_without_active_keeps_flag_and_can_empty_lines(client):
    tpl = _create(client)
    client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=_put_body(tpl, active=False))
    body = _put_body(tpl)
    del body["active"]
    body["lines"] = []
    got = client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=body).json()
    assert got["active"] is False and got["lines"] == []


def test_duplicate(client, add_items):
    add_items("A1")
    tpl = _create(client, "Set", lines=[{"item_id": "A1", "qty": 3}, _line("Frei")])
    client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=_put_body(tpl, active=False))
    resp = client.post(f"{T}/{tpl['id']}/duplicate", headers=EXPERT)
    assert resp.status_code == 201
    copy = resp.json()
    assert copy["id"] != tpl["id"] and copy["name"] == "Set (Kopie)" and copy["active"] is True
    assert [(ln["item_id"], ln["label"], ln["qty"], ln["position"]) for ln in copy["lines"]] == [
        ("A1", "Item A1", 3, 1), (None, "Frei", 1, 2),
    ]
    assert not {ln["id"] for ln in copy["lines"]} & {ln["id"] for ln in tpl["lines"]}
    long_name = _create(client, "N" * 120, lines=[])
    assert len(client.post(f"{T}/{long_name['id']}/duplicate", headers=EXPERT).json()["name"]) == 120


def test_delete_template_and_lines(client):
    tpl = _create(client)
    assert client.delete(f"{T}/{tpl['id']}", headers=EXPERT).status_code == 204
    assert client.get(f"{T}/{tpl['id']}", headers=EXPERT).status_code == 404
    conn = client.app.state.conn
    assert conn.execute("SELECT COUNT(*) FROM template_lines").fetchone()[0] == 0


def test_delete_with_jobs_is_409_and_keeps_template(client, add_job):
    tpl = _create(client)
    add_job(tpl["id"])
    resp = client.delete(f"{T}/{tpl['id']}", headers=EXPERT)
    assert resp.status_code == 409
    assert "Aufträgen" in resp.json()["detail"]
    assert client.get(f"{T}/{tpl['id']}", headers=EXPERT).status_code == 200
    assert repo.template_has_jobs(client.app.state.conn, tpl["id"]) is True


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "lines": []},
        {"name": "   ", "lines": []},
        {"name": "x" * 121, "lines": []},
        {"lines": []},
        {"name": "x", "lines": [{"label": ""}]},  # label_required is a 422 from the repo layer
        {"name": "x", "lines": [{"label": "l" * 121}]},
        {"name": "x", "lines": [{"label": "l", "instruction": "i" * 501}]},
        {"name": "x", "lines": [{"label": "l", "qty": 0}]},
        {"name": "x", "lines": [{"label": "l", "qty": 1000}]},
        {"name": "x", "lines": [{"label": "l", "qty": 1.5}]},
        {"name": "x", "lines": [{"label": "l", "photo_required": "vielleicht"}]},
        {"name": "x", "lines": [{"label": "l", "quality_points": ["a", "a"]}]},
        {"name": "x", "lines": [{"label": "l", "quality_points": [" "]}]},
        {"name": "x", "lines": [{"label": "l", "quality_points": ["p" * 61]}]},
        {"name": "x", "lines": [{"label": "l", "quality_points": [str(i) for i in range(11)]}]},
        {"name": "x", "lines": [{"label": "l", "item_id": "GIBTSNICHT"}]},
        {"name": "x", "lines": [{"label": "l", "photo_item_id": "GIBTSNICHT"}]},
        {"name": "x", "product_item_id": "GIBTSNICHT", "lines": []},
        {"name": "x", "second_check": "ja", "lines": []},
    ],
)
def test_create_validation_is_422_and_creates_nothing(client, body):
    resp = client.post(T, headers=EXPERT, json=body)
    assert resp.status_code == 422, resp.text
    assert client.get(f"{T}?all=1", headers=EXPERT).json() == []


def test_unknown_item_detail_names_the_item(client):
    resp = client.post(T, headers=EXPERT, json={"name": "x", "lines": [{"item_id": "NOPE"}]})
    assert resp.status_code == 422 and "NOPE" in resp.json()["detail"]


def test_quality_points_boundary_and_trim(client):
    points = [f"p{i}" for i in range(10)]
    ok = _create(client, lines=[_line("l", quality_points=points)])
    assert ok["lines"][0]["quality_points"] == points
    trimmed = _create(client, "T", lines=[_line(" l ", quality_points=[" sauber "], qty=999)])
    assert trimmed["lines"][0]["quality_points"] == ["sauber"] and trimmed["lines"][0]["label"] == "l"


def test_failed_put_leaves_template_unchanged(client):
    tpl = _create(client, lines=[_line("A"), _line("B")])
    body = _put_body(tpl, name="Anders")
    body["lines"] = [dict(body["lines"][1]), {"label": "x", "item_id": "NOPE"}]
    assert client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=body).status_code == 422
    assert client.get(f"{T}/{tpl['id']}", headers=EXPERT).json() == tpl
    # a repo-level failure after the first writes (unknown item found while resolving) also rolls back
    conn = client.app.state.conn
    with pytest.raises(ValueError):
        repo.replace_template(conn, tpl["id"], "Z", None, True, None, [{"label": "q"}, {"item_id": "NOPE"}], "now")
    conn.rollback()
    assert client.get(f"{T}/{tpl['id']}", headers=EXPERT).json() == tpl


def test_audit_rows(client):
    tpl = _create(client)
    client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=_put_body(tpl))
    copy = client.post(f"{T}/{tpl['id']}/duplicate", headers=EXPERT).json()
    client.delete(f"{T}/{copy['id']}", headers=EXPERT)
    rows = client.app.state.conn.execute(
        "SELECT actor, action, entity_id FROM audit WHERE entity='template' ORDER BY id"
    ).fetchall()
    assert [(r["actor"], r["action"], r["entity_id"]) for r in rows] == [
        ("expert", "template.create", str(tpl["id"])),
        ("expert", "template.replace", str(tpl["id"])),
        ("expert", "template.duplicate", str(copy["id"])),
        ("expert", "template.delete", str(copy["id"])),
    ]
