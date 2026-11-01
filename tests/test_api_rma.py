import json

import pytest

from conftest import EXPERT

T = "/api/expert/templates"
JOBS = "/api/expert/jobs"
LINES = [{"label": "Kabel", "qty": 3}, {"label": "Netzteil"}, {"label": "Gehäuse"}]


def _template(client, lines=None, **kw):
    body = {"name": "Set A", "lines": lines if lines is not None else LINES}
    body.update(kw)
    resp = client.post(T, headers=EXPERT, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _new_rma(worker, template, rma_no="RMA-1", customer="Kunde GmbH"):
    resp = worker.post("/api/rma", json={"template_id": template["id"], "rma_no": rma_no, "customer": customer})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _job(worker, job_id):
    resp = worker.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _confirm(worker, line, result="ok", qty=None, image=None, **form):
    data = {"result": result, "qty": str(line["qty"] if qty is None else qty)}
    if "quality" not in form and int(data["qty"]) > 0:
        form["quality"] = {p: "ok" for p in line.get("quality_points", [])}
    data.update({k: v if isinstance(v, str) else json.dumps(v) for k, v in form.items()})
    files = {"image": ("p.jpg", image, "image/jpeg")} if image else None
    return worker.post(f"/api/lines/{line['id']}/confirm", data=data, files=files)


def _confirm_lines(worker, lines, image):
    for line in lines:
        if not line["withheld"]:
            assert _confirm(worker, line, image=image).status_code == 201


def _repair_rma(worker, template, image, rma_no="RMA-1"):
    """An RMA in status repair; everything arrived. Returns the job detail."""
    job = _new_rma(worker, template, rma_no)
    _confirm_lines(worker, _job(worker, job["id"])["lines"], image)
    assert worker.post(f"/api/jobs/{job['id']}/finish").status_code == 200
    return _job(worker, job["id"])


def _note(worker, job_id, action, **body):
    return worker.post(f"/api/jobs/{job_id}/repair-notes", json={"action": action, **body})


def _audit(client, action, entity_id=None):
    with client.app.state.lock:
        rows = client.app.state.conn.execute("SELECT * FROM audit WHERE action=? ORDER BY id", (action,)).fetchall()
    return [r for r in rows if entity_id is None or r["entity_id"] == str(entity_id)]


def _patch(client, job_id, status):
    return client.patch(f"{JOBS}/{job_id}", headers=EXPERT, json={"status": status})


# --- auth -------------------------------------------------------------------------------------


def test_rma_routes_need_a_session(client):
    assert client.post("/api/rma", json={"template_id": 1, "rma_no": "x"}).status_code == 401
    assert client.post("/api/jobs/1/repair-notes", json={"action": "added"}).status_code == 401
    assert client.delete("/api/repair-notes/1").status_code == 401
    assert client.post("/api/jobs/1/outgoing").status_code == 401


def test_open_rma_needs_the_expert_password(client):
    assert client.get("/api/expert/rma/open").status_code == 401
    assert client.get("/api/expert/rma/open", headers={"X-Expert-Password": "falsch"}).status_code == 401


# --- create -----------------------------------------------------------------------------------


def test_worker_creates_an_rma(client, worker_client):
    worker, me = worker_client
    tpl = _template(client)
    job = _new_rma(worker, tpl, rma_no=" RMA-77 ")
    assert (job["kind"], job["status"], job["stage"], job["order_ref"], job["customer"]) == (
        "rma", "intake", "intake", "RMA-77", "Kunde GmbH",
    )
    assert (job["claimed_by"], job["claimed_by_name"], job["created_by"], job["unit_count"]) == (
        me["id"], "Anna", f"worker:{me['id']}", 1,
    )
    assert (job["lines_total"], job["lines_done"], job["template_name"]) == (3, 0, "Set A")
    row = _audit(client, "job.create", job["id"])[0]
    assert row["actor"] == f"worker:{me['id']}"
    assert json.loads(row["detail"]) == {"kind": "rma", "template_id": tpl["id"], "order_ref": "RMA-77", "ad_hoc": True}
    detail = _job(worker, job["id"])
    assert [(ln["stage"], ln["label"], ln["qty"], ln["source"]) for ln in detail["lines"]] == [
        ("intake", "Kabel", 3, "template"), ("intake", "Netzteil", 1, "template"), ("intake", "Gehäuse", 1, "template"),
    ]
    assert [ln["id"] for ln in detail["intake_lines"]] == [ln["id"] for ln in detail["lines"]]
    assert detail["repair_notes"] == []
    assert [j["id"] for j in worker.get("/api/jobs?kind=rma").json()] == [job["id"]]


def test_duplicate_rma_number_is_409_even_after_the_job_moved_on(client, worker_client):
    worker, _ = worker_client
    tpl = _template(client)
    first = _new_rma(worker, tpl, "RMA-1")
    resp = worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": "RMA-1"})
    assert resp.status_code == 409 and resp.json()["detail"] == "Diese RMA-Nummer gibt es schon."
    assert worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": " RMA-1 "}).status_code == 409
    assert _patch(client, first["id"], "cancelled").status_code == 200
    assert worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": "RMA-1"}).status_code == 409
    # a delivery with the same order number is unrelated
    assert worker.post("/api/jobs", json={"template_id": tpl["id"], "order_ref": "RMA-1"}).status_code == 201
    assert len(_audit(client, "job.create")) == 2


def test_rma_create_validation(client, worker_client):
    worker, _ = worker_client
    tpl = _template(client)
    assert worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": ""}).status_code == 422
    assert worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": "   "}).status_code == 422
    assert worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": "x" * 41}).status_code == 422
    assert worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": "x" * 40}).status_code == 201
    assert worker.post("/api/rma", json={"template_id": tpl["id"], "rma_no": "A", "customer": "k" * 81}).status_code == 422
    assert worker.post("/api/rma", json={"template_id": 4242, "rma_no": "B"}).status_code == 404
    empty = _template(client, lines=[], name="Leer")
    resp = worker.post("/api/rma", json={"template_id": empty["id"], "rma_no": "C"})
    assert resp.status_code == 400 and resp.json()["detail"] == "Die Vorlage hat keine Zeilen."
    assert worker.post("/api/rma", json={"template_id": tpl["id"]}).status_code == 422
    assert worker.get("/api/jobs?kind=rma").json()[0]["order_ref"] == "x" * 40


def test_expert_created_rma_has_the_office_customisations_in_intake(client, worker_client, add_items):
    add_items(("X1", "Zusatzteil"))
    worker, _ = worker_client
    tpl = _template(client)
    resp = client.post(
        JOBS, headers=EXPERT,
        json={"kind": "rma", "template_id": tpl["id"], "order_ref": "RMA-9", "customer": "K",
              "customisations": [
                  {"template_line_id": tpl["lines"][2]["id"], "withheld": True, "reason": "bleibt beim Kunden"},
                  {"item_id": "X1", "qty": 2, "reason": "Zubehör mitgeschickt"}]},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()[0]
    assert (job["status"], job["kind"], job["claimed_by"], job["created_by"], job["lines_total"]) == (
        "intake", "rma", None, "expert", 3,
    )
    lines = _job(worker, job["id"])["lines"]
    assert [(ln["stage"], ln["label"], ln["source"], ln["withheld"], ln["qty"]) for ln in lines] == [
        ("intake", "Kabel", "template", False, 3), ("intake", "Netzteil", "template", False, 1),
        ("intake", "Gehäuse", "template", True, 1), ("intake", "Zusatzteil", "office", False, 2),
    ]
    assert lines[2]["withheld_reason"] == "bleibt beim Kunden" and lines[3]["reason"] == "Zubehör mitgeschickt"
    # a worker can take over the unclaimed RMA without changing its status
    claimed = worker.post(f"/api/jobs/{job['id']}/claim")
    assert claimed.status_code == 200 and (claimed.json()["status"], claimed.json()["claimed_by_name"]) == ("intake", "Anna")


# --- intake -----------------------------------------------------------------------------------


def test_intake_confirmations_extra_item_and_finish(client, worker_client, make_image):
    worker, me = worker_client
    job = _new_rma(worker, _template(client))
    cable, psu, case = _job(worker, job["id"])["lines"]
    img = make_image()
    assert _confirm(worker, psu, image=img).status_code == 201  # ok = all arrived
    # partly arrived: reason required, no photo needed when nothing arrived
    resp = _confirm(worker, cable, "missing", 1, image=img)
    assert resp.status_code == 400 and "Grund" in resp.json()["detail"]
    resp = _confirm(worker, cable, "missing", 1, image=img, reason="zwei fehlen")
    assert resp.status_code == 201 and resp.json()["confirmation"]["qty"] == 1
    assert _confirm(worker, case, "missing", 0, reason="nicht im Karton").status_code == 201
    # arrived damaged: reason and photo
    assert _confirm(worker, psu, "not_ok", reason="Riss").status_code == 400
    assert _confirm(worker, psu, "not_ok", image=img).status_code == 400
    assert _confirm(worker, psu, "not_ok", image=img, reason="Riss im Gehäuse").status_code == 201
    # an extra item found in the box: worker line, photo required
    resp = worker.post(f"/api/jobs/{job['id']}/lines", json={"label": "Fremdteil", "qty": 1, "reason": "lag im Karton"})
    assert resp.status_code == 201
    extra = resp.json()
    assert (extra["source"], extra["stage"], extra["photo_required"]) == ("worker", "intake", True)
    resp = worker.post(f"/api/jobs/{job['id']}/finish")
    assert resp.status_code == 409 and resp.json()["open_line_ids"] == [extra["id"]]
    assert _confirm(worker, extra).status_code == 400  # photo missing
    assert _confirm(worker, extra, image=img).status_code == 201
    done = worker.post(f"/api/jobs/{job['id']}/finish")
    assert done.status_code == 200 and (done.json()["status"], done.json()["stage"]) == ("repair", "intake")
    assert _audit(client, "job.finish", job["id"])[0]["detail"] == json.dumps({"from": "intake", "to": "repair"})
    # no second check at intake, and no more extras or confirmations in repair
    assert worker.post(f"/api/jobs/{job['id']}/check", data={"result": "ok"}).status_code in (403, 409)
    assert _confirm(worker, psu, image=img).status_code == 409
    resp = worker.post(f"/api/jobs/{job['id']}/lines", json={"label": "Noch eins", "qty": 1, "reason": "grund"})
    assert resp.status_code == 409
    detail = _job(worker, job["id"])
    assert len(detail["intake_lines"]) == 4 and detail["intake_lines"][0]["latest"]["qty"] == 1
    assert detail["intake_lines"][1]["latest"]["result"] == "not_ok"


def test_claim_rma_in_each_worker_stage_keeps_the_status(client, new_worker_client, make_image):
    anna, _ = new_worker_client("Anna")
    bert, bert_row = new_worker_client("Bert")
    job = _repair_rma(anna, _template(client), make_image())
    for expected in ("repair", "outgoing"):
        resp = bert.post(f"/api/jobs/{job['id']}/claim")
        assert resp.status_code == 200
        assert (resp.json()["status"], resp.json()["claimed_by"], resp.json()["claimed_by_name"]) == (
            expected, bert_row["id"], "Bert",
        )
        anna.post(f"/api/jobs/{job['id']}/claim")  # take it back
        if expected == "repair":
            assert anna.post(f"/api/jobs/{job['id']}/outgoing").status_code == 200
    assert len(_audit(client, "job.claim", job["id"])) == 4
    client.app.state.conn.execute("UPDATE jobs SET status='packed' WHERE id=?", (job["id"],))
    assert bert.post(f"/api/jobs/{job['id']}/claim").status_code == 409


# --- repair notes -----------------------------------------------------------------------------


def test_repair_note_routes(client, worker_client, make_image, add_items):
    add_items(("X1", "Neuteil"))
    worker, me = worker_client
    job = _repair_rma(worker, _template(client), make_image())
    cable, psu, _ = [ln["id"] for ln in job["lines"]]
    resp = _note(worker, job["id"], "kept", job_line_id=cable, qty=1, reason="Kunde behält eins")
    assert resp.status_code == 201, resp.text
    kept = resp.json()
    assert set(kept) >= {"id", "action", "job_line_id", "line_label", "item_id", "label", "qty", "serial", "reason",
                         "worker_name", "created_at"}
    assert (kept["action"], kept["line_label"], kept["label"], kept["qty"], kept["worker_name"]) == (
        "kept", "Kabel", "Kabel", 1, "Anna",
    )
    replaced = _note(worker, job["id"], "replaced", job_line_id=psu, serial="SN-1").json()
    assert (replaced["reason"], replaced["serial"], replaced["qty"]) == ("", "SN-1", 1)
    added = _note(worker, job["id"], "added", item_id="X1", qty=2, reason="Ersatzteil dazu").json()
    assert (added["job_line_id"], added["line_label"], added["label"], added["item_id"]) == (None, None, "Neuteil", "X1")
    notes = _job(worker, job["id"])["repair_notes"]
    assert [n["id"] for n in notes] == [kept["id"], replaced["id"], added["id"]]
    assert notes[0]["line_label"] == "Kabel" and notes[2]["worker_name"] == "Anna"
    rows = _audit(client, "repair.note", job["id"])
    assert [json.loads(r["detail"])["action"] for r in rows] == ["kept", "replaced", "added"]
    assert rows[0]["actor"] == f"worker:{me['id']}"


@pytest.mark.parametrize(
    "body,status,fragment",
    [
        ({"action": "kept", "qty": 1, "reason": "Kunde will"}, 400, "Zeile"),  # no line
        ({"action": "kept", "job_line_id": 0, "qty": 1, "reason": "ab"}, 400, "Grund"),
        ({"action": "kept", "job_line_id": 0, "qty": 4, "reason": "Kunde will"}, 400, "Menge"),
        ({"action": "kept", "job_line_id": 0, "qty": 0, "reason": "Kunde will"}, 400, "Menge"),
        ({"action": "replaced", "job_line_id": 99999}, 400, "Zeile"),
        ({"action": "replaced", "job_line_id": 0, "serial": "s" * 65}, 400, "Ungültige"),
        ({"action": "added", "label": "Teil", "reason": "ab"}, 400, "Grund"),
        ({"action": "added", "reason": "Ersatzteil"}, 400, "Bezeichnung"),
        ({"action": "added", "label": "Teil", "qty": 1000, "reason": "Ersatzteil"}, 400, "Menge"),
        ({"action": "added", "label": "Teil", "reason": "Ersatzteil", "item_id": "NOPE"}, 400, "Unbekannter Artikel: NOPE"),
        ({"action": "added", "label": "Teil", "reason": "Ersatzteil", "job_line_id": 0}, 400, "Zeile"),
        ({"action": "bogus"}, 422, None),
    ],
)
def test_repair_note_validation_errors(client, worker_client, make_image, body, status, fragment):
    worker, _ = worker_client
    job = _repair_rma(worker, _template(client), make_image())
    if body.get("job_line_id") == 0:
        body = {**body, "job_line_id": job["lines"][0]["id"]}
    resp = worker.post(f"/api/jobs/{job['id']}/repair-notes", json=body)
    assert resp.status_code == status, resp.text
    if fragment:
        assert fragment in resp.json()["detail"]
    assert _job(worker, job["id"])["repair_notes"] == []


def test_repair_note_needs_status_repair_and_a_known_job(client, worker_client, make_image):
    worker, _ = worker_client
    tpl = _template(client)
    intake = _new_rma(worker, tpl, "R-I")
    resp = _note(worker, intake["id"], "added", label="Teil", reason="Ersatz")
    assert resp.status_code == 409 and resp.json()["detail"] == "Das ist im aktuellen Status des Auftrags nicht möglich."
    assert _note(worker, 9999, "added", label="Teil", reason="Ersatz").status_code == 404
    repair = _repair_rma(worker, tpl, make_image(), "R-R")
    assert worker.post(f"/api/jobs/{repair['id']}/outgoing").status_code == 200
    assert _note(worker, repair["id"], "added", label="Teil", reason="Ersatz").status_code == 409
    delivery = worker.post("/api/jobs", json={"template_id": tpl["id"], "order_ref": "D-1"}).json()
    assert _note(worker, delivery["id"], "added", label="Teil", reason="Ersatz").status_code == 409


def test_delete_note_author_only_and_not_after_outgoing(client, new_worker_client, make_image):
    anna, anna_row = new_worker_client("Anna")
    bert, _ = new_worker_client("Bert")
    job = _repair_rma(anna, _template(client), make_image())
    note = _note(anna, job["id"], "added", label="Teil", reason="Ersatz").json()
    resp = bert.delete(f"/api/repair-notes/{note['id']}")
    assert resp.status_code == 403 and "Person" in resp.json()["detail"]
    assert len(_job(anna, job["id"])["repair_notes"]) == 1
    assert anna.delete("/api/repair-notes/9999").status_code == 404
    assert anna.delete(f"/api/repair-notes/{note['id']}").status_code == 204
    assert _job(anna, job["id"])["repair_notes"] == []
    row = _audit(client, "repair.note_delete", job["id"])[0]
    assert row["actor"] == f"worker:{anna_row['id']}" and json.loads(row["detail"])["id"] == note["id"]
    keep = _note(anna, job["id"], "added", label="Teil", reason="Ersatz").json()
    assert anna.post(f"/api/jobs/{job['id']}/outgoing").status_code == 200
    assert anna.delete(f"/api/repair-notes/{keep['id']}").status_code == 409
    assert len(_job(anna, job["id"])["repair_notes"]) == 1


# --- outgoing ---------------------------------------------------------------------------------


def test_generate_outgoing_route(client, worker_client, make_image):
    worker, me = worker_client
    job = _repair_rma(worker, _template(client), make_image())
    cable, psu, case = job["lines"]
    _note(worker, job["id"], "kept", job_line_id=cable["id"], qty=1, reason="Kunde behält eins")
    _note(worker, job["id"], "replaced", job_line_id=psu["id"], reason="Brummt")
    _note(worker, job["id"], "added", label="Dichtung", reason="Neu eingebaut")
    resp = worker.post(f"/api/jobs/{job['id']}/outgoing")
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert (detail["status"], detail["stage"], detail["lines_total"], detail["lines_done"]) == ("outgoing", "outgoing", 4, 0)
    lines = detail["lines"]
    assert [(ln["stage"], ln["label"], ln["qty"], ln["source"], ln["source_line_id"], ln["in_qty"]) for ln in lines] == [
        ("outgoing", "Kabel", 2, "template", cable["id"], 3),
        ("outgoing", "Netzteil", 1, "template", psu["id"], 1),
        ("outgoing", "Gehäuse", 1, "template", case["id"], 1),
        ("outgoing", "Dichtung", 1, "worker", None, None),
    ]
    assert lines[1]["reason"] == "ersetzt: Brummt" and lines[3]["reason"] == "Neu eingebaut"
    assert lines[3]["photo_required"] is True and all(ln["latest"] is None for ln in lines)
    assert lines[0]["quality_points"] == ["sauber", "unbeschädigt", "funktion"] and "reference" in lines[0]
    # the intake lines and the notes stay visible next to the outgoing lines
    assert [ln["stage"] for ln in detail["intake_lines"]] == ["intake"] * 3
    assert detail["intake_lines"][0]["latest"]["qty"] == 3 and len(detail["repair_notes"]) == 3
    row = _audit(client, "job.outgoing", job["id"])[0]
    assert json.loads(row["detail"]) == {"line_ids": [ln["id"] for ln in lines], "lines": 4}
    assert row["actor"] == f"worker:{me['id']}"
    assert _job(worker, job["id"])["lines"] == lines


def test_generate_outgoing_errors(client, worker_client, make_image):
    worker, _ = worker_client
    tpl = _template(client)
    intake = _new_rma(worker, tpl, "R-I")
    resp = worker.post(f"/api/jobs/{intake['id']}/outgoing")
    assert resp.status_code == 409 and resp.json()["detail"] == "Das ist im aktuellen Status des Auftrags nicht möglich."
    assert worker.post("/api/jobs/9999/outgoing").status_code == 404
    repair = _repair_rma(worker, tpl, make_image(), "R-R")
    assert worker.post(f"/api/jobs/{repair['id']}/outgoing").status_code == 200
    again = worker.post(f"/api/jobs/{repair['id']}/outgoing")
    assert again.status_code == 409 and again.json()["detail"] == "Die Rücksendeliste wurde schon erstellt."
    assert len(_job(worker, repair["id"])["lines"]) == 3
    assert len(_audit(client, "job.outgoing")) == 1
    delivery = worker.post("/api/jobs", json={"template_id": tpl["id"], "order_ref": "D-1"}).json()
    assert worker.post(f"/api/jobs/{delivery['id']}/outgoing").status_code == 409


def test_nothing_to_return_is_a_409(client, worker_client, make_image):
    worker, _ = worker_client
    job = _new_rma(worker, _template(client, lines=[{"label": "Kabel"}]))
    line = _job(worker, job["id"])["lines"][0]
    assert _confirm(worker, line, "missing", 0, reason="nicht im Karton").status_code == 201
    assert worker.post(f"/api/jobs/{job['id']}/finish").status_code == 200
    resp = worker.post(f"/api/jobs/{job['id']}/outgoing")
    assert resp.status_code == 409 and "nichts zurückzusenden" in resp.json()["detail"]
    assert _job(worker, job["id"])["status"] == "repair"


def test_outgoing_stage_confirm_finish_check_ship(client, new_worker_client, make_image):
    anna, _ = new_worker_client("Anna")
    bert, _ = new_worker_client("Bert")
    img = make_image()
    job = _repair_rma(anna, _template(client), img)
    detail = anna.post(f"/api/jobs/{job['id']}/outgoing").json()
    first, *rest = detail["lines"]
    # outgoing lines are confirmed like packing lines, with the current stage only
    assert _confirm(anna, job["lines"][0], image=img).status_code == 409  # intake line: wrong stage now
    assert _confirm(anna, first, image=img).status_code == 201
    resp = anna.post(f"/api/jobs/{job['id']}/finish")
    assert resp.status_code == 409 and resp.json()["open_line_ids"] == [ln["id"] for ln in rest]
    extra = anna.post(f"/api/jobs/{job['id']}/lines", json={"label": "Beilage", "qty": 1, "reason": "Wunsch"}).json()
    assert (extra["stage"], extra["source"]) == ("outgoing", "worker")
    _confirm_lines(anna, rest + [extra], img)
    done = anna.post(f"/api/jobs/{job['id']}/finish")
    assert done.status_code == 200 and done.json()["status"] == "packed"
    # second check: the packer is refused, another worker approves with the box photo
    assert anna.post(f"/api/jobs/{job['id']}/check", data={"result": "ok"}, files={"image": ("b.jpg", img, "image/jpeg")}
                     ).status_code == 403
    assert bert.post(f"/api/jobs/{job['id']}/check", data={"result": "ok"}).status_code == 400
    resp = bert.post(f"/api/jobs/{job['id']}/check", data={"result": "ok"}, files={"image": ("b.jpg", img, "image/jpeg")})
    assert resp.status_code == 201 and resp.json()["job"]["status"] == "checked"
    assert anna.post(f"/api/jobs/{job['id']}/ship").json()["status"] == "shipped"


def test_second_check_off_lets_the_outgoing_box_skip_to_checked(client, worker_client, make_image):
    worker, _ = worker_client
    img = make_image()
    job = _repair_rma(worker, _template(client, second_check=False), img)
    detail = worker.post(f"/api/jobs/{job['id']}/outgoing").json()
    _confirm_lines(worker, detail["lines"], img)
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "checked"
    assert worker.post(f"/api/jobs/{job['id']}/ship").json()["status"] == "shipped"


def test_rejected_check_sends_the_rma_back_to_outgoing_with_open_lines(client, new_worker_client, make_image):
    anna, _ = new_worker_client("Anna")
    bert, _ = new_worker_client("Bert")
    img = make_image()
    job = _repair_rma(anna, _template(client), img)
    detail = anna.post(f"/api/jobs/{job['id']}/outgoing").json()
    _confirm_lines(anna, detail["lines"], img)
    anna.post(f"/api/jobs/{job['id']}/finish")
    resp = bert.post(f"/api/jobs/{job['id']}/check", data={"result": "rejected", "note": "Netzteil fehlt im Karton"})
    assert resp.status_code == 201 and resp.json()["job"]["status"] == "outgoing"
    again = _job(anna, job["id"])
    assert (again["status"], again["attempt"], again["lines_done"]) == ("outgoing", 2, 0)
    assert again["open_line_ids"] == [ln["id"] for ln in again["lines"]]
    assert all(ln["stage"] == "outgoing" and ln["latest"]["attempt"] == 1 for ln in again["lines"])  # evidence kept
    _confirm_lines(anna, again["lines"], img)
    assert anna.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "packed"


# --- expert reopen ----------------------------------------------------------------------------


def test_expert_reopen_outgoing_to_repair_regenerates(client, worker_client, make_image):
    worker, _ = worker_client
    job = _repair_rma(worker, _template(client), make_image())
    assert worker.post(f"/api/jobs/{job['id']}/outgoing").status_code == 200
    resp = _patch(client, job["id"], "repair")
    assert resp.status_code == 200 and resp.json()["status"] == "repair"
    detail = _job(worker, job["id"])
    assert [ln["stage"] for ln in detail["lines"]] == ["intake"] * 3  # outgoing lines are gone
    kept = _note(worker, job["id"], "kept", job_line_id=detail["lines"][0]["id"], qty=3, reason="Kunde behält alle")
    assert kept.status_code == 201
    regenerated = worker.post(f"/api/jobs/{job['id']}/outgoing")
    assert regenerated.status_code == 200
    assert [ln["label"] for ln in regenerated.json()["lines"]] == ["Netzteil", "Gehäuse"]


def test_expert_reopen_outgoing_to_repair_refused_after_a_confirmation(client, worker_client, make_image):
    worker, _ = worker_client
    img = make_image()
    job = _repair_rma(worker, _template(client), img)
    detail = worker.post(f"/api/jobs/{job['id']}/outgoing").json()
    assert _confirm(worker, detail["lines"][0], image=img).status_code == 201
    resp = _patch(client, job["id"], "repair")
    assert resp.status_code == 409 and "Rücksendung" in resp.json()["detail"]
    again = _job(worker, job["id"])
    assert again["status"] == "outgoing" and len(again["lines"]) == 3 and again["lines"][0]["latest"]["qty"] == 3
    # any other target from outgoing is not a valid reopen
    for status in ("intake", "open", "packing", "outgoing"):
        assert _patch(client, job["id"], status).status_code == 409


@pytest.mark.parametrize("status", ["packed", "checked", "shipped"])
def test_expert_reopen_finished_rma_to_outgoing(client, worker_client, make_image, status):
    worker, _ = worker_client
    img = make_image()
    job = _repair_rma(worker, _template(client, second_check=False), img)
    detail = worker.post(f"/api/jobs/{job['id']}/outgoing").json()
    _confirm_lines(worker, detail["lines"], img)
    worker.post(f"/api/jobs/{job['id']}/finish")
    client.app.state.conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job["id"]))
    assert _patch(client, job["id"], "outgoing").json()["status"] == "outgoing"
    assert _patch(client, job["id"], "repair").status_code == 409  # confirmed lines exist
    assert _patch(client, job["id"], "cancelled").json()["status"] == "cancelled"
    assert _patch(client, job["id"], "intake").status_code == 409  # confirmed outgoing: cannot restart intake


def test_expert_cancelled_rma_back_to_intake(client, worker_client, make_image):
    worker, _ = worker_client
    job = _repair_rma(worker, _template(client), make_image())
    worker.post(f"/api/jobs/{job['id']}/outgoing")
    assert _patch(client, job["id"], "cancelled").status_code == 200
    resp = _patch(client, job["id"], "intake")
    assert resp.status_code == 200 and resp.json()["status"] == "intake"
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "repair"  # intake confirmations are kept
    assert len(worker.post(f"/api/jobs/{job['id']}/outgoing").json()["lines"]) == 3


# --- open rma report --------------------------------------------------------------------------


def test_open_rma_report_delta_math_and_shipping(client, worker_client, make_image):
    worker, _ = worker_client
    img = make_image()
    tpl = _template(client, lines=[{"label": "Kabel", "qty": 3}, {"label": "Netzteil"}], second_check=False)
    job = _repair_rma(worker, tpl, img)
    cable, psu = job["lines"]
    _note(worker, job["id"], "kept", job_line_id=cable["id"], qty=1, reason="Kunde behält eins")
    report = client.get("/api/expert/rma/open", headers=EXPERT)
    assert report.status_code == 200
    (entry,) = report.json()
    assert entry["job"]["id"] == job["id"] and entry["job"]["status"] == "repair" and entry["job"]["kind"] == "rma"
    assert [(ln["label"], ln["in_qty"], ln["kept_qty"], ln["out_qty"], ln["delta"]) for ln in entry["lines"]] == [
        ("Kabel", 3, 1, 0, 2), ("Netzteil", 1, 0, 0, 1),
    ]
    assert entry["lines"][0]["intake_line_id"] == cable["id"] and entry["lines"][0]["item_id"] is None
    outgoing = worker.post(f"/api/jobs/{job['id']}/outgoing").json()["lines"]
    assert outgoing[0]["qty"] == 2
    assert _confirm(worker, outgoing[0], "missing", 1, image=img, reason="eins fehlt im Karton").status_code == 201
    assert _confirm(worker, outgoing[1], image=img).status_code == 201
    (entry,) = client.get("/api/expert/rma/open", headers=EXPERT).json()
    assert [(ln["out_qty"], ln["delta"]) for ln in entry["lines"]] == [(1, 1), (1, 0)]  # 3 in, 1 kept, 1 out -> 1 lost
    assert _confirm(worker, outgoing[0], image=img).status_code == 201  # corrected to 2
    (entry,) = client.get("/api/expert/rma/open", headers=EXPERT).json()
    assert [ln["delta"] for ln in entry["lines"]] == [0, 0]
    assert worker.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "checked"
    assert len(client.get("/api/expert/rma/open", headers=EXPERT).json()) == 1  # checked is still in the house
    assert worker.post(f"/api/jobs/{job['id']}/ship").status_code == 200
    assert client.get("/api/expert/rma/open", headers=EXPERT).json() == []


def test_open_rma_report_excludes_cancelled_and_deliveries_and_lists_oldest_first(client, worker_client, make_image):
    worker, _ = worker_client
    tpl = _template(client)
    a = _new_rma(worker, tpl, "A")
    b = _new_rma(worker, tpl, "B")
    c = _new_rma(worker, tpl, "C")
    worker.post("/api/jobs", json={"template_id": tpl["id"], "order_ref": "D"})
    _patch(client, b["id"], "cancelled")
    report = client.get("/api/expert/rma/open", headers=EXPERT).json()
    assert [r["job"]["id"] for r in report] == [a["id"], c["id"]]
    assert all(r["lines"] == [] for r in report)  # nothing confirmed yet: nothing has arrived


def test_job_detail_of_a_delivery_has_no_intake_lines(client, worker_client):
    worker, _ = worker_client
    job = worker.post("/api/jobs", json={"template_id": _template(client)["id"], "order_ref": "D-1"}).json()
    detail = _job(worker, job["id"])
    assert "intake_lines" not in detail and detail["repair_notes"] == []
    assert all("in_qty" not in ln for ln in detail["lines"])
