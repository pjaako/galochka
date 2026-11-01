import json
from pathlib import Path

import pytest

from conftest import EXPERT
from galochka import repo

T = "/api/expert/templates"
JOBS = "/api/expert/jobs"


def _template(client, lines=None, **kw):
    body = {"name": "Set A", "lines": lines if lines is not None else [{"label": "Kabel", "qty": 2}, {"label": "Netzteil"}]}
    body.update(kw)
    resp = client.post(T, headers=EXPERT, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _expert_jobs(client, template, **kw):
    body = {"template_id": template["id"], "order_ref": "A-100", "customer": "Kunde GmbH"}
    body.update(kw)
    resp = client.post(JOBS, headers=EXPERT, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _job(worker, job_id):
    resp = worker.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _packing_job(client, worker, template=None, **kw):
    template = template or _template(client)
    job = _expert_jobs(client, template, **kw)[0]
    assert worker.post(f"/api/jobs/{job['id']}/claim").status_code == 200
    return _job(worker, job["id"])


def _confirm(worker, line, result="ok", qty=None, image=None, **form):
    data = {"result": result, "qty": str(line["qty"] if qty is None else qty)}
    if "quality" not in form and int(data["qty"]) > 0:  # default: every quality point answered ok
        form["quality"] = {p: "ok" for p in line.get("quality_points", [])}
    data.update({k: v if isinstance(v, str) else json.dumps(v) for k, v in form.items()})
    files = {"image": ("p.jpg", image, "image/jpeg")} if image else None
    return worker.post(f"/api/lines/{line['id']}/confirm", data=data, files=files)


def _confirm_all(worker, job, image):
    for line in job["lines"]:
        if not line["withheld"]:
            assert _confirm(worker, line, image=image).status_code == 201


def _audit(client, action, entity_id=None):
    with client.app.state.lock:
        rows = client.app.state.conn.execute("SELECT * FROM audit WHERE action=? ORDER BY id", (action,)).fetchall()
    return [r for r in rows if entity_id is None or r["entity_id"] == str(entity_id)]


# --- auth -------------------------------------------------------------------------------------


def test_worker_routes_need_a_session(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/jobs", json={"template_id": 1, "order_ref": "x"}).status_code == 401
    assert client.get("/api/jobs/1").status_code == 401
    for path in ("claim", "finish", "ship"):
        assert client.post(f"/api/jobs/1/{path}").status_code == 401
    assert client.post("/api/jobs/1/lines", json={}).status_code == 401
    assert client.post("/api/lines/1/confirm", data={"result": "ok", "qty": "1"}).status_code == 401


def test_expert_job_routes_need_the_password(client):
    assert client.post(JOBS, json={}).status_code == 401
    assert client.patch(f"{JOBS}/1", json={"status": "cancelled"}).status_code == 401
    assert client.post(JOBS, headers={"X-Expert-Password": "falsch"}, json={}).status_code == 401


def test_expert_can_read_job_and_ship(client, worker_client):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    resp = client.get(f"/api/jobs/{job['id']}", headers=EXPERT)
    assert resp.status_code == 200 and resp.json()["id"] == job["id"]
    assert client.get("/api/jobs/9999", headers=EXPERT).status_code == 404
    client.app.state.conn.execute("UPDATE jobs SET status='checked' WHERE id=?", (job["id"],))
    assert client.post(f"/api/jobs/{job['id']}/ship", headers=EXPERT).status_code == 200
    assert _audit(client, "job.ship", job["id"])[0]["actor"] == "expert"


# --- expert create ----------------------------------------------------------------------------


def test_expert_create_count_three_makes_three_jobs(client, worker_client):
    tpl = _template(client)
    jobs = _expert_jobs(client, tpl, count=3, note="Eilig")
    assert [j["unit_no"] for j in jobs] == [1, 2, 3]
    assert {j["unit_count"] for j in jobs} == {3}
    assert {(j["status"], j["kind"], j["order_ref"], j["customer"], j["created_by"], j["note"]) for j in jobs} == {
        ("open", "delivery", "A-100", "Kunde GmbH", "expert", "Eilig")
    }
    assert {j["lines_total"] for j in jobs} == {2} and {j["ad_hoc"] for j in jobs} == {False}
    worker, _ = worker_client
    line_ids = [{ln["id"] for ln in _job(worker, j["id"])["lines"]} for j in jobs]
    assert len(set().union(*line_ids)) == 6  # each unit has its own snapshot
    assert len(_audit(client, "job.create")) == 3


def test_template_edit_after_job_creation_leaves_job_lines(client, worker_client):
    tpl = _template(client)
    job = _expert_jobs(client, tpl)[0]
    body = {"name": "Umbenannt", "active": True, "second_check": None, "product_item_id": None,
            "lines": [{"label": "Ganz neu", "qty": 9}]}
    assert client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=body).status_code == 200
    worker, _ = worker_client
    lines = _job(worker, job["id"])["lines"]
    assert [(ln["label"], ln["qty"]) for ln in lines] == [("Kabel", 2), ("Netzteil", 1)]
    assert _job(worker, job["id"])["template_name"] == "Set A"


def test_customisations_withhold_and_office_extra(client, worker_client, add_items, make_image):
    add_items(("X1", "Zusatzteil"))
    tpl = _template(client)
    job = _expert_jobs(
        client,
        tpl,
        customisations=[
            {"template_line_id": tpl["lines"][1]["id"], "withheld": True, "reason": "Kunde hat eins"},
            {"item_id": "X1", "qty": 2, "reason": "Sonderwunsch"},
        ],
    )[0]
    assert job["lines_total"] == 2
    worker, _ = worker_client
    worker.post(f"/api/jobs/{job['id']}/claim")
    detail = _job(worker, job["id"])
    lines = detail["lines"]
    assert [(ln["source"], ln["withheld"], ln["label"]) for ln in lines] == [
        ("template", False, "Kabel"), ("template", True, "Netzteil"), ("office", False, "Zusatzteil"),
    ]
    assert lines[1]["withheld_reason"] == "Kunde hat eins" and lines[1]["withheld_by"] == "expert"
    assert lines[2]["reason"] == "Sonderwunsch" and lines[2]["qty"] == 2
    assert detail["open_line_ids"] == [lines[0]["id"], lines[2]["id"]]
    resp = _confirm(worker, lines[1], image=make_image())
    assert resp.status_code == 400 and resp.json()["detail"] == "Diese Zeile wurde vom Büro zurückgehalten und braucht keine Bestätigung."
    _confirm_all(worker, detail, make_image())
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "packed"


@pytest.mark.parametrize(
    "extra,status",
    [
        ({"count": 0}, 422),
        ({"count": 21}, 422),
        ({"kind": "bogus"}, 422),
        ({"order_ref": ""}, 422),
        ({"order_ref": "x" * 41}, 422),
        ({"customer": "x" * 81}, 422),
        ({"kind": "rma", "count": 2}, 400),
        ({"template_id": 4242}, 404),
        ({"customisations": [{"template_line_id": 4242, "withheld": True, "reason": "abc"}]}, 400),
        ({"customisations": [{"label": "x", "qty": 1}]}, 400),
        ({"customisations": [{"item_id": "NOPE", "qty": 1, "reason": "abc"}]}, 400),
    ],
)
def test_expert_create_validation(client, extra, status):
    body = {"template_id": _template(client)["id"], "order_ref": "A-1", **extra}
    resp = client.post(JOBS, headers=EXPERT, json=body)
    assert resp.status_code == status, resp.text
    with client.app.state.lock:
        assert client.app.state.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_empty_and_inactive_template_are_400(client):
    empty = _template(client, lines=[], name="Leer")
    resp = client.post(JOBS, headers=EXPERT, json={"template_id": empty["id"], "order_ref": "A"})
    assert resp.status_code == 400 and resp.json()["detail"] == "Die Vorlage hat keine Zeilen."
    tpl = _template(client, name="Alt")
    body = {"name": "Alt", "active": False, "second_check": None, "product_item_id": None,
            "lines": [{"label": "x"}]}
    client.put(f"{T}/{tpl['id']}", headers=EXPERT, json=body)
    resp = client.post(JOBS, headers=EXPERT, json={"template_id": tpl["id"], "order_ref": "A"})
    assert resp.status_code == 400 and resp.json()["detail"] == "Die Vorlage ist deaktiviert."


def test_expert_creates_rma_and_duplicate_is_409(client):
    tpl = _template(client)
    job = _expert_jobs(client, tpl, kind="rma", order_ref="RMA-1")[0]
    assert (job["kind"], job["status"], job["stage"], job["lines_total"]) == ("rma", "intake", "intake", 2)
    resp = client.post(JOBS, headers=EXPERT, json={"kind": "rma", "template_id": tpl["id"], "order_ref": "RMA-1"})
    assert resp.status_code == 409 and resp.json()["detail"] == "Diese RMA-Nummer gibt es schon."
    assert len(_audit(client, "job.create")) == 1


# --- ad hoc, listing, claim -------------------------------------------------------------------


def test_ad_hoc_job_is_claimed_and_packing(client, worker_client):
    worker, me = worker_client
    tpl = _template(client)
    resp = worker.post("/api/jobs", json={"template_id": tpl["id"], "order_ref": " B-7 ", "customer": "Fa. Y"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert (job["status"], job["ad_hoc"], job["claimed_by"], job["claimed_by_name"], job["order_ref"]) == (
        "packing", True, me["id"], "Anna", "B-7",
    )
    assert job["created_by"] == f"worker:{me['id']}"
    row = _audit(client, "job.create", job["id"])[0]
    assert row["actor"] == f"worker:{me['id']}" and json.loads(row["detail"])["ad_hoc"] is True
    assert worker.post("/api/jobs", json={"template_id": tpl["id"], "order_ref": ""}).status_code == 422
    assert worker.post("/api/jobs", json={"template_id": 4242, "order_ref": "x"}).status_code == 404
    empty = _template(client, lines=[], name="Leer")
    assert worker.post("/api/jobs", json={"template_id": empty["id"], "order_ref": "x"}).status_code == 400


def test_claim_by_second_worker_overwrites_and_is_audited(client, new_worker_client):
    anna, anna_row = new_worker_client("Anna")
    bert, bert_row = new_worker_client("Bert")
    job = _expert_jobs(client, _template(client))[0]
    first = anna.post(f"/api/jobs/{job['id']}/claim")
    assert first.status_code == 200 and first.json()["status"] == "packing"
    second = bert.post(f"/api/jobs/{job['id']}/claim")
    assert second.status_code == 200
    assert (second.json()["claimed_by"], second.json()["claimed_by_name"]) == (bert_row["id"], "Bert")
    rows = _audit(client, "job.claim", job["id"])
    assert [(r["actor"], json.loads(r["detail"])["previous_claimed_by"]) for r in rows] == [
        (f"worker:{anna_row['id']}", None), (f"worker:{bert_row['id']}", anna_row["id"]),
    ]


def test_claim_unknown_or_packed_job(client, worker_client):
    worker, _ = worker_client
    assert worker.post("/api/jobs/9999/claim").status_code == 404
    job = _packing_job(client, worker)
    client.app.state.conn.execute("UPDATE jobs SET status='packed' WHERE id=?", (job["id"],))
    resp = worker.post(f"/api/jobs/{job['id']}/claim")
    assert resp.status_code == 409 and resp.json()["detail"] == "Das ist im aktuellen Status des Auftrags nicht möglich."


def test_list_jobs_defaults_filters_and_mine(client, new_worker_client, make_image):
    anna, anna_row = new_worker_client("Anna")
    bert, _ = new_worker_client("Bert")
    tpl = _template(client)
    a, b, c = (_expert_jobs(client, tpl, order_ref=ref)[0] for ref in ("A", "B", "C"))
    rma = _expert_jobs(client, tpl, kind="rma", order_ref="R")[0]
    anna.post(f"/api/jobs/{a['id']}/claim")
    bert.post(f"/api/jobs/{b['id']}/claim")
    client.app.state.conn.execute("UPDATE jobs SET status='shipped' WHERE id=?", (c["id"],))
    first_line = _job(anna, a["id"])["lines"][0]
    assert _confirm(anna, first_line, image=make_image()).status_code == 201

    rows = anna.get("/api/jobs").json()
    assert [r["id"] for r in rows] == [a["id"], b["id"], rma["id"]]  # shipped is not in the default list
    assert [(r["lines_done"], r["lines_total"]) for r in rows] == [(1, 2), (0, 2), (0, 2)]
    expected_keys = {"id", "kind", "status", "order_ref", "customer", "template_name", "unit_no", "unit_count",
                     "ad_hoc", "claimed_by_name", "lines_total", "lines_done", "created_at"}
    assert expected_keys <= set(rows[0])
    assert rows[0]["claimed_by_name"] == "Anna" and rows[2]["claimed_by_name"] is None
    assert [r["id"] for r in anna.get("/api/jobs?mine=1").json()] == [a["id"]]
    assert [r["id"] for r in bert.get("/api/jobs?mine=1").json()] == [b["id"]]
    assert [r["id"] for r in anna.get("/api/jobs?kind=rma").json()] == [rma["id"]]
    assert [r["id"] for r in anna.get("/api/jobs?status=shipped").json()] == [c["id"]]
    assert {r["id"] for r in anna.get("/api/jobs?status=open,packing").json()} == {a["id"], b["id"]}
    assert anna.get("/api/jobs?status=bogus").status_code == 400
    assert anna.get("/api/jobs?kind=bogus").status_code == 400
    assert anna_row["id"] == rows[0]["claimed_by"]


# --- job detail -------------------------------------------------------------------------------


def test_job_detail_shape(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    assert (job["second_check"], job["attempt"], job["repair_notes"], job["checks"]) == (True, 1, [], [])
    assert job["open_line_ids"] == [ln["id"] for ln in job["lines"]]
    line = job["lines"][0]
    assert line["latest"] is None and line["reference"] == {"photos": [], "stale": False}
    assert (line["label"], line["qty"], line["photo_required"], line["serial_required"], line["source"]) == (
        "Kabel", 2, True, False, "template",
    )
    assert line["quality_points"] == ["sauber", "unbeschädigt", "funktion"]
    assert worker.get("/api/jobs/9999").status_code == 404


def test_job_detail_uses_cassandra_outside_the_lock(client, worker_client, add_items):
    add_items(("A1", "Kabel"), ("P9", "Fotoartikel"))
    calls = []

    class FakeCassandra:
        def prefetch(self, conn, lock, ids, now, budget_seconds=2.0):
            assert lock.acquire(blocking=False), "prefetch must run outside the lock"
            lock.release()
            calls.append(("prefetch", list(ids), budget_seconds))

        def reference_photos(self, conn, lock, item_id, now):
            assert lock.acquire(blocking=False), "reference_photos takes the lock itself"
            lock.release()
            return {"item_id": item_id, "photos": [{"url": f"http://c/{item_id}.jpg"}], "stale": item_id == "P9"}

    client.app.state.cassandra = FakeCassandra()
    tpl = _template(client, lines=[{"item_id": "A1"}, {"label": "Mit Fremdfoto", "item_id": "A1", "photo_item_id": "P9"},
                                   {"label": "Ohne Artikel"}])
    worker, _ = worker_client
    job = _packing_job(client, worker, tpl)
    assert calls == [("prefetch", ["A1", "P9"], 2.0)]
    refs = [ln["reference"] for ln in job["lines"]]
    assert refs[0] == {"photos": [{"url": "http://c/A1.jpg"}], "stale": False}
    assert refs[1] == {"photos": [{"url": "http://c/P9.jpg"}], "stale": True}
    assert refs[2] == {"photos": [], "stale": False}


# --- worker extra lines -----------------------------------------------------------------------


def test_worker_extra_line_needs_reason_and_photo(client, worker_client, make_image, add_items):
    add_items(("X1", "Schraube"))
    worker, me = worker_client
    job = _packing_job(client, worker)
    resp = worker.post(f"/api/jobs/{job['id']}/lines", json={"item_id": "X1", "label": "Schraube M4", "qty": 4,
                                                             "reason": "lag lose im Karton"})
    assert resp.status_code == 201, resp.text
    line = resp.json()
    assert (line["source"], line["position"], line["qty"], line["reason"], line["latest"]) == (
        "worker", 3, 4, "lag lose im Karton", None,
    )
    assert _job(worker, job["id"])["open_line_ids"][-1] == line["id"]  # blocks finishing until confirmed
    resp = _confirm(worker, line)
    assert resp.status_code == 400 and resp.json()["detail"] == "Für diese Zeile ist ein Foto nötig."
    resp = _confirm(worker, line, image=make_image())
    assert resp.status_code == 201
    assert _audit(client, "line.add", job["id"])[0]["actor"] == f"worker:{me['id']}"


@pytest.mark.parametrize(
    "body,status",
    [
        ({"label": "x", "qty": 1, "reason": "ab"}, 422),
        ({"label": "x", "qty": 1, "reason": "y" * 201}, 422),
        ({"label": "", "qty": 1, "reason": "grund"}, 422),
        ({"label": "x" * 121, "qty": 1, "reason": "grund"}, 422),
        ({"label": "x", "qty": 0, "reason": "grund"}, 422),
        ({"label": "x", "qty": 1000, "reason": "grund"}, 422),
        ({"label": "x", "qty": 1}, 422),
        ({"item_id": "NOPE", "label": "x", "qty": 1, "reason": "grund"}, 400),
    ],
)
def test_worker_extra_line_validation(client, worker_client, body, status):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    assert worker.post(f"/api/jobs/{job['id']}/lines", json=body).status_code == status
    assert len(_job(worker, job["id"])["lines"]) == 2


def test_worker_extra_line_wrong_status_and_unknown_job(client, worker_client):
    worker, _ = worker_client
    body = {"label": "x", "qty": 1, "reason": "grund"}
    assert worker.post("/api/jobs/9999/lines", json=body).status_code == 404
    open_job = _expert_jobs(client, _template(client))[0]  # not claimed yet
    assert worker.post(f"/api/jobs/{open_job['id']}/lines", json=body).status_code == 409


# --- delete a worker-added line -----------------------------------------------------------------


def _extra_line(worker, job_id):
    resp = worker.post(f"/api/jobs/{job_id}/lines", json={"label": "Schraube", "qty": 1, "reason": "lag lose"})
    assert resp.status_code == 201
    return resp.json()


def test_delete_worker_line(client, worker_client):
    worker, me = worker_client
    job = _packing_job(client, worker)
    extra = _extra_line(worker, job["id"])
    assert worker.delete(f"/api/lines/{extra['id']}").status_code == 204
    detail = _job(worker, job["id"])
    assert extra["id"] not in [ln["id"] for ln in detail["lines"]] and extra["id"] not in detail["open_line_ids"]
    row = _audit(client, "line.delete", job["id"])[0]
    assert row["actor"] == f"worker:{me['id']}" and json.loads(row["detail"]) == {"line_id": extra["id"], "label": "Schraube"}
    assert worker.delete(f"/api/lines/{extra['id']}").status_code == 404


def test_delete_line_refused_cases(client, worker_client, make_image):
    worker, _ = worker_client
    assert client.delete("/api/lines/1").status_code == 401
    job = _packing_job(client, worker)
    template_line = job["lines"][0]
    resp = worker.delete(f"/api/lines/{template_line['id']}")
    assert resp.status_code == 409 and resp.json()["detail"] == "Das ist im aktuellen Status des Auftrags nicht möglich."
    confirmed = _extra_line(worker, job["id"])
    assert _confirm(worker, confirmed, image=make_image()).status_code == 201
    assert worker.delete(f"/api/lines/{confirmed['id']}").status_code == 409
    extra = _extra_line(worker, job["id"])
    _confirm_all(worker, _job(worker, job["id"]), make_image())
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "packed"
    assert worker.delete(f"/api/lines/{extra['id']}").status_code == 409  # job is packed
    assert worker.delete("/api/lines/9999").status_code == 404
    assert _audit(client, "line.delete") == []


# --- confirm ----------------------------------------------------------------------------------


def test_confirm_with_image_stores_normalised_jpeg(client, worker_client, make_image, data_dir):
    worker, me = worker_client
    job = _packing_job(client, worker)
    line = job["lines"][0]
    big = make_image(size=(3000, 2000), orientation=6)
    resp = _confirm(worker, line, image=big, quality={"sauber": "ok", "unbeschädigt": "ok", "funktion": "ok"}, reason="  ")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    conf = body["confirmation"]
    assert (conf["result"], conf["qty"], conf["attempt"], conf["worker_id"], conf["worker_name"]) == (
        "ok", 2, 1, me["id"], "Anna",
    )
    assert conf["quality"] == {"sauber": "ok", "unbeschädigt": "ok", "funktion": "ok"} and conf["serials"] == []
    assert conf["photo_url"].startswith(f"/photos/jobs/{job['id']}/") and conf["photo_url"].endswith(".jpg")
    assert body["line"]["id"] == line["id"] and body["line"]["latest"]["id"] == conf["id"]
    stored = Path(data_dir) / "photos" / conf["photo_url"].removeprefix("/photos/")
    assert stored.is_file()
    from PIL import Image
    with Image.open(stored) as img:
        assert img.format == "JPEG" and max(img.size) == 1024 and img.size[0] < img.size[1]  # EXIF rotated, downscaled
    served = worker.get(conf["photo_url"])
    assert served.status_code == 200 and served.content == stored.read_bytes()
    detail = _job(worker, job["id"])
    assert detail["lines"][0]["latest"]["photo_url"] == conf["photo_url"]
    assert detail["open_line_ids"] == [detail["lines"][1]["id"]]
    row = _audit(client, "line.confirm", job["id"])[0]
    assert row["actor"] == f"worker:{me['id']}" and json.loads(row["detail"])["line_id"] == line["id"]


def test_image_work_happens_outside_the_lock(client, worker_client, make_image, monkeypatch):
    from galochka import images

    worker, _ = worker_client
    line = _packing_job(client, worker)["lines"][0]
    seen = []
    real_normalize, real_save = images.normalize, images.save_photo

    def normalize(data, *a, **kw):
        seen.append(client.app.state.lock.acquire(blocking=False))
        if seen[-1]:
            client.app.state.lock.release()
        return real_normalize(data, *a, **kw)

    def save(*a, **kw):
        seen.append(client.app.state.lock.acquire(blocking=False))
        if seen[-1]:
            client.app.state.lock.release()
        return real_save(*a, **kw)

    monkeypatch.setattr(images, "normalize", normalize)
    monkeypatch.setattr(images, "save_photo", save)
    assert _confirm(worker, line, image=make_image()).status_code == 201
    assert seen == [True, True]


def test_second_confirmation_supersedes_but_both_rows_exist(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    line = job["lines"][0]
    first = _confirm(worker, line, result="missing", qty=0, reason="nicht da").json()["confirmation"]
    second = _confirm(worker, line, image=make_image()).json()["confirmation"]
    assert second["id"] > first["id"]
    latest = _job(worker, job["id"])["lines"][0]["latest"]
    assert latest["id"] == second["id"] and latest["result"] == "ok"
    with client.app.state.lock:
        n = client.app.state.conn.execute(
            "SELECT COUNT(*) FROM confirmations WHERE job_line_id=?", (line["id"],)
        ).fetchone()[0]
    assert n == 2


def test_missing_with_qty_zero_needs_no_photo_but_a_reason(client, worker_client):
    worker, _ = worker_client
    line = _packing_job(client, worker)["lines"][0]
    resp = _confirm(worker, line, result="missing", qty=0, reason="fehlt im Lager")
    assert resp.status_code == 201 and resp.json()["confirmation"]["photo_url"] is None
    resp = _confirm(worker, line, result="missing", qty=0)
    assert resp.status_code == 400 and resp.json()["detail"] == "Bitte einen Grund angeben (mindestens 3 Zeichen)."


def test_confirm_quantity_rules(client, worker_client, make_image):
    worker, _ = worker_client
    line = _packing_job(client, worker)["lines"][0]  # qty 2
    image = make_image()
    for qty, result, reason, status in [
        (1, "missing", "", 400),       # short without reason
        (1, "ok", "ein Stück", 400),   # short but marked ok
        (3, "ok", "", 400),            # too many
        (-1, "missing", "grund", 400),
        (1, "missing", "ein Stück fehlt", 201),
    ]:
        resp = _confirm(worker, line, result=result, qty=qty, reason=reason, image=image)
        assert resp.status_code == status, (qty, result, resp.text)
    resp = _confirm(worker, line, qty=3, image=image)
    assert resp.json()["detail"] == "Die Menge passt nicht zur Zeile."


def test_confirm_not_ok_needs_reason_and_photo(client, worker_client, make_image):
    worker, _ = worker_client
    line = _packing_job(client, worker)["lines"][0]
    assert _confirm(worker, line, result="not_ok", image=make_image()).status_code == 400
    assert _confirm(worker, line, result="not_ok", reason="Kratzer").status_code == 400
    resp = _confirm(worker, line, result="not_ok", reason="Kratzer", image=make_image(),
                    quality={"sauber": "not_ok", "unbeschädigt": "ok", "funktion": "ok"})
    assert resp.status_code == 201 and resp.json()["confirmation"]["result"] == "not_ok"


def test_confirm_serial_rules(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker, _template(client, lines=[{"label": "Gerät", "qty": 2, "serial_required": True}]))
    line = job["lines"][0]
    image = make_image()
    for serials in ([], ["A"], ["A", "B", "C"], ["A", ""], ["A", "B" * 65]):
        resp = _confirm(worker, line, image=image, serials=serials)
        assert resp.status_code == 400 and "Seriennummer" in resp.json()["detail"], serials
    resp = _confirm(worker, line, image=image, serials=[" SN-1 ", "SN-2"])
    assert resp.status_code == 201 and resp.json()["confirmation"]["serials"] == ["SN-1", "SN-2"]


def test_confirm_quality_rules(client, worker_client, make_image):
    worker, _ = worker_client
    line = _packing_job(client, worker)["lines"][0]
    image = make_image()
    resp = _confirm(worker, line, image=image, quality={"sauber": "ok", "funktion": "not_ok"})
    assert resp.status_code == 400 and "Qualitätspunkt" in resp.json()["detail"]
    assert _confirm(worker, line, image=image, quality={"unbekannt": "ok"}).status_code == 400
    assert _confirm(worker, line, image=image, quality={"sauber": "vielleicht"}).status_code == 400


def test_confirm_requires_all_quality_points_unless_nothing_arrived(client, worker_client, make_image):
    worker, _ = worker_client
    line = _packing_job(client, worker)["lines"][0]  # qty 2, three default points
    image = make_image()
    for quality in ({}, {"sauber": "ok"}, {"sauber": "ok", "unbeschädigt": "ok"}):
        resp = _confirm(worker, line, image=image, quality=quality)
        assert resp.status_code == 400 and resp.json()["detail"] == "Bitte alle Prüfpunkte beantworten.", quality
    resp = _confirm(worker, line, result="missing", qty=1, reason="Rest fehlt", image=image, quality={})
    assert resp.status_code == 400 and "Prüfpunkte" in resp.json()["detail"]
    # nothing arrived: nothing to inspect
    assert _confirm(worker, line, result="missing", qty=0, reason="fehlt", quality={}).status_code == 201


def test_confirm_malformed_form_fields(client, worker_client, make_image):
    worker, _ = worker_client
    line = _packing_job(client, worker)["lines"][0]
    image = make_image()
    assert _confirm(worker, line, image=image, serials="not json").status_code == 400
    assert _confirm(worker, line, image=image, serials='{"a": 1}').status_code == 400
    assert _confirm(worker, line, image=image, quality="[1]").status_code == 400
    assert _confirm(worker, line, result="bogus", image=image).status_code == 400
    assert worker.post(f"/api/lines/{line['id']}/confirm", data={"result": "ok", "qty": "x"}).status_code == 422
    assert worker.post("/api/lines/9999/confirm", data={"result": "ok", "qty": "1"}).status_code == 404


def test_confirm_bad_image_is_400_and_stores_nothing(client, worker_client, data_dir):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    resp = _confirm(worker, job["lines"][0], image=b"this is not an image")
    assert resp.status_code == 400 and resp.json()["detail"] == "Das Bild konnte nicht gelesen werden."
    assert not (Path(data_dir) / "photos" / "jobs").exists()
    assert _job(worker, job["id"])["lines"][0]["latest"] is None


def test_failed_insert_removes_the_saved_photo(client, worker_client, make_image, data_dir, monkeypatch):
    worker, _ = worker_client
    job = _packing_job(client, worker)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo, "add_confirmation", boom)
    from fastapi.testclient import TestClient
    quiet = TestClient(client.app, raise_server_exceptions=False)
    quiet.cookies.update(worker.cookies)
    resp = _confirm(quiet, job["lines"][0], image=make_image())
    assert resp.status_code == 500
    jobs_dir = Path(data_dir) / "photos" / "jobs" / str(job["id"])
    assert not jobs_dir.exists() or list(jobs_dir.iterdir()) == []
    with client.app.state.lock:
        assert client.app.state.conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_confirm_wrong_status_is_409(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    _confirm_all(worker, job, make_image())
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "packed"
    resp = _confirm(worker, job["lines"][0], image=make_image())
    assert resp.status_code == 409 and resp.json()["detail"] == "Das ist im aktuellen Status des Auftrags nicht möglich."
    # an unclaimed (open) job cannot be confirmed either
    open_job = _expert_jobs(client, _template(client, name="Zwei"))[0]
    open_line = _job(worker, open_job["id"])["lines"][0]
    assert _confirm(worker, open_line, image=make_image()).status_code == 409


# --- finish / ship ----------------------------------------------------------------------------


def test_finish_with_open_line_is_409_and_lists_ids(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    _confirm(worker, job["lines"][0], image=make_image())
    resp = worker.post(f"/api/jobs/{job['id']}/finish")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Es sind noch nicht alle Zeilen bestätigt.",
                           "open_line_ids": [job["lines"][1]["id"]]}
    assert _job(worker, job["id"])["status"] == "packing"
    assert _audit(client, "job.finish") == []


def test_finish_goes_to_packed_with_second_check(client, worker_client, make_image):
    worker, me = worker_client
    job = _packing_job(client, worker)
    _confirm_all(worker, job, make_image())
    resp = worker.post(f"/api/jobs/{job['id']}/finish")
    assert resp.status_code == 200 and resp.json()["status"] == "packed"
    row = _audit(client, "job.finish", job["id"])[0]
    assert row["actor"] == f"worker:{me['id']}" and json.loads(row["detail"]) == {"from": "packing", "to": "packed"}
    assert worker.post(f"/api/jobs/{job['id']}/finish").status_code == 409  # not packing any more


def test_finish_goes_to_checked_when_template_disables_second_check(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker, _template(client, second_check=False))
    assert job["second_check"] is False
    _confirm_all(worker, job, make_image())
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "checked"


def test_finish_goes_to_checked_when_global_default_is_off(client, worker_client, make_image):
    settings = {"second_check_default": False, "session_minutes": 30, "default_quality_points": ["sauber"]}
    assert client.put("/api/expert/settings", headers=EXPERT, json=settings).status_code == 204
    worker, _ = worker_client
    job = _packing_job(client, worker)
    assert job["second_check"] is False
    assert job["lines"][0]["quality_points"] == ["sauber"]  # default points resolved at creation
    _confirm_all(worker, job, make_image())
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "checked"
    # a per-template override wins over the global default
    job2 = _packing_job(client, worker, _template(client, second_check=True, name="Mit Kontrolle"))
    assert job2["second_check"] is True


def test_ship_only_from_checked(client, worker_client, make_image):
    worker, me = worker_client
    job = _packing_job(client, worker, _template(client, second_check=False))
    resp = worker.post(f"/api/jobs/{job['id']}/ship")
    assert resp.status_code == 409  # still packing
    _confirm_all(worker, job, make_image())
    worker.post(f"/api/jobs/{job['id']}/finish")
    resp = worker.post(f"/api/jobs/{job['id']}/ship")
    assert resp.status_code == 200 and resp.json()["status"] == "shipped"
    assert _audit(client, "job.ship", job["id"])[0]["actor"] == f"worker:{me['id']}"
    assert worker.post(f"/api/jobs/{job['id']}/ship").status_code == 409
    assert worker.post("/api/jobs/9999/ship").status_code == 404
    packed = _packing_job(client, worker)
    _confirm_all(worker, packed, make_image())
    worker.post(f"/api/jobs/{packed['id']}/finish")
    assert worker.post(f"/api/jobs/{packed['id']}/ship").status_code == 409  # packed, not yet checked


# --- expert status override -------------------------------------------------------------------


def _patch(client, job_id, status, **kw):
    return client.patch(f"{JOBS}/{job_id}", headers=EXPERT, json={"status": status, **kw})


def test_expert_patch_transitions(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker, _template(client, second_check=False))
    jid = job["id"]
    assert _patch(client, jid, "packing").status_code == 409  # already packing
    assert _patch(client, jid, "open").status_code == 409
    _confirm_all(worker, job, make_image())
    worker.post(f"/api/jobs/{jid}/finish")
    worker.post(f"/api/jobs/{jid}/ship")
    resp = _patch(client, jid, "packing", note="Teil vergessen")
    assert resp.status_code == 200 and resp.json()["status"] == "packing" and resp.json()["note"] == "Teil vergessen"
    assert _patch(client, jid, "cancelled").json()["status"] == "cancelled"
    assert _patch(client, jid, "cancelled").status_code == 409
    assert _patch(client, jid, "packing").status_code == 409
    assert _patch(client, jid, "open").json()["status"] == "open"
    assert _patch(client, jid, "shipped").status_code == 409
    resp = _patch(client, jid, "bogus")
    assert resp.status_code == 422
    assert _patch(client, 9999, "cancelled").status_code == 404
    rows = _audit(client, "job.status", jid)
    assert [(json.loads(r["detail"])["from"], json.loads(r["detail"])["to"]) for r in rows] == [
        ("shipped", "packing"), ("packing", "cancelled"), ("cancelled", "open"),
    ]
    assert {r["actor"] for r in rows} == {"expert"}


def test_expert_cannot_cancel_a_shipped_job(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker, _template(client, second_check=False))
    _confirm_all(worker, job, make_image())
    worker.post(f"/api/jobs/{job['id']}/finish")
    worker.post(f"/api/jobs/{job['id']}/ship")
    resp = _patch(client, job["id"], "cancelled")
    assert resp.status_code == 409 and resp.json()["detail"] == "Dieser Statuswechsel ist nicht erlaubt."


def test_expert_patch_outgoing_to_repair_rules(client, worker_client):
    tpl = _template(client)
    rma = _expert_jobs(client, tpl, kind="rma", order_ref="R-1")[0]
    conn = client.app.state.conn
    conn.execute("UPDATE jobs SET status='outgoing' WHERE id=?", (rma["id"],))
    assert _patch(client, rma["id"], "repair").json()["status"] == "repair"
    conn.execute("UPDATE jobs SET status='outgoing' WHERE id=?", (rma["id"],))
    worker, me = worker_client
    line_id = repo._insert_line(conn, rma["id"], "outgoing", 1, {"label": "K", "qty": 1, "quality_points": [],
                                                                "photo_required": False})
    repo.add_confirmation(conn, line_id, me["id"], 1, "ok", 1, [], {}, "", "", "2026-11-01T10:00:00+00:00")
    resp = _patch(client, rma["id"], "repair")
    assert resp.status_code == 409 and "Rücksendung" in resp.json()["detail"]
    assert _audit(client, "job.status", rma["id"])[0]["detail"]


# --- strings ----------------------------------------------------------------------------------


def test_status_and_error_strings_exist():
    strings = json.loads((Path(__file__).parent.parent / "galochka" / "static" / "strings.de.json").read_text("utf-8"))
    for status in repo.JOB_STATUSES:
        assert strings[f"status.{status}"]
    for code in ("photo_required", "reason_required", "bad_qty", "serial_count", "quality_not_ok", "line_withheld",
                 "wrong_status", "lines_open", "bad_transition", "outgoing_confirmed", "rma_duplicate",
                 "template_empty", "template_inactive", "unknown_template_line", "bad_count", "quality_incomplete"):
        assert strings[f"error.{code}"], code
