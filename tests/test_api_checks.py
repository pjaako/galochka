"""Second check (POST /api/jobs/{id}/check), ship, box label data and the job QR code."""

import json

from conftest import EXPERT
from test_api_jobs import _audit, _confirm, _confirm_all, _job, _packing_job, _template


def _packed_job(client, packer, make_image, template=None, **kw):
    job = _packing_job(client, packer, template, **kw)
    _confirm_all(packer, job, make_image())
    resp = packer.post(f"/api/jobs/{job['id']}/finish")
    assert resp.status_code == 200 and resp.json()["status"] == "packed", resp.text
    return job["id"]


def _check(worker, job_id, result="ok", note="", image=None):
    files = {"image": ("box.jpg", image, "image/jpeg")} if image else None
    return worker.post(f"/api/jobs/{job_id}/check", data={"result": result, "note": note}, files=files)


def test_check_needs_a_session(client, worker_client, make_image):
    worker, _ = worker_client
    job_id = _packed_job(client, worker, make_image)
    assert client.post(f"/api/jobs/{job_id}/check", data={"result": "ok"}).status_code == 401


def test_packer_cannot_check_own_box(client, worker_client, make_image):
    packer, _ = worker_client
    job_id = _packed_job(client, packer, make_image)
    for result, note in (("ok", ""), ("rejected", "sieht schlecht aus")):
        resp = _check(packer, job_id, result, note, make_image())
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Die Prüfung muss eine andere Person durchführen als die, die gepackt hat."
    assert _job(packer, job_id)["status"] == "packed"
    assert _audit(client, "job.check") == []


def test_ok_needs_image_and_rejected_needs_note(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job_id = _packed_job(client, packer, make_image)
    resp = _check(checker, job_id, "ok")
    assert resp.status_code == 400 and resp.json()["detail"] == "Für diese Zeile ist ein Foto nötig."
    assert _check(checker, job_id, "rejected", "  ").status_code == 400
    assert _check(checker, job_id, "rejected", "ab").status_code == 400
    assert _check(checker, job_id, "maybe", "", make_image()).status_code == 400
    bad = checker.post(f"/api/jobs/{job_id}/check", data={"result": "ok"}, files={"image": ("x.jpg", b"nope")})
    assert bad.status_code == 400 and bad.json()["detail"] == "Das Bild konnte nicht gelesen werden."
    assert _job(checker, job_id)["status"] == "packed" and _job(checker, job_id)["checks"] == []


def test_ok_with_image_stores_photo_and_checks_the_job(client, new_worker_client, make_image, data_dir):
    packer, _ = new_worker_client("Anna")
    checker, bert = new_worker_client("Bert")
    job_id = _packed_job(client, packer, make_image)
    resp = _check(checker, job_id, "ok", "", make_image(size=(2000, 1500)))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["job"]["status"] == "checked" and body["check"]["result"] == "ok"
    assert body["check"]["worker_name"] == "Bert" and body["check"]["attempt"] == 1
    url = body["check"]["photo_url"]
    assert url.startswith(f"/photos/jobs/{job_id}/") and url.endswith(".jpg")
    assert (data_dir / "photos" / url.removeprefix("/photos/")).exists()
    assert client.get(url).status_code == 200
    detail = _job(checker, job_id)
    assert detail["status"] == "checked" and detail["packer_names"] == ["Anna"]
    assert [c["id"] for c in detail["checks"]] == [body["check"]["id"]]
    row = _audit(client, "job.check", job_id)[0]
    assert row["actor"] == f"worker:{bert['id']}"
    assert json.loads(row["detail"])["result"] == "ok"


def test_failed_check_insert_removes_the_photo(client, new_worker_client, make_image, data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from galochka import repo

    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job_id = _packed_job(client, packer, make_image)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    before = set((data_dir / "photos").glob(f"jobs/{job_id}/*.jpg"))
    monkeypatch.setattr(repo, "add_check", boom)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    quiet.cookies.update(checker.cookies)
    assert _check(quiet, job_id, "ok", "", make_image()).status_code == 500
    assert set((data_dir / "photos").glob(f"jobs/{job_id}/*.jpg")) == before  # the box photo was removed again
    monkeypatch.undo()
    assert _job(checker, job_id)["status"] == "packed" and _job(checker, job_id)["checks"] == []


def test_reject_goes_back_to_packing_with_a_new_attempt(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job_id = _packed_job(client, packer, make_image)
    before = _job(packer, job_id)
    assert before["attempt"] == 1 and before["packer_names"] == ["Anna"]
    non_withheld = [line["id"] for line in before["lines"] if not line["withheld"]]

    resp = _check(checker, job_id, "rejected", "Netzteil fehlt im Karton")
    assert resp.status_code == 201, resp.text
    assert resp.json()["job"]["status"] == "packing"
    assert resp.json()["check"]["note"] == "Netzteil fehlt im Karton" and resp.json()["check"]["photo_url"] is None

    after = _job(packer, job_id)
    assert after["status"] == "packing" and after["attempt"] == 2
    assert after["open_line_ids"] == non_withheld
    assert all(line["latest"] is not None and line["latest"]["attempt"] == 1 for line in after["lines"])
    assert after["checks"][0]["result"] == "rejected" and after["checks"][0]["attempt"] == 1
    assert after["packer_names"] == []
    with client.app.state.lock:
        n = client.app.state.conn.execute("SELECT COUNT(*) AS n FROM confirmations").fetchone()["n"]
    assert n == len(non_withheld)  # confirmations retained
    assert _audit(client, "job.check", job_id)[0]["detail"].count("Netzteil fehlt") == 1

    # finishing is blocked until every line is confirmed again
    assert packer.post(f"/api/jobs/{job_id}/finish").status_code == 409
    for line in after["lines"]:
        assert _confirm(packer, line, image=make_image()).status_code == 201
    assert packer.post(f"/api/jobs/{job_id}/finish").json()["status"] == "packed"
    again = _job(checker, job_id)
    assert again["attempt"] == 2 and again["packer_names"] == ["Anna"]

    # the earlier checker may check again (he did not pack attempt 2); the packer still may not
    assert _check(packer, job_id, "ok", "", make_image()).status_code == 403
    resp = _check(checker, job_id, "ok", "", make_image())
    assert resp.status_code == 201 and resp.json()["job"]["status"] == "checked"
    assert resp.json()["check"]["attempt"] == 2
    assert [c["result"] for c in _job(checker, job_id)["checks"]] == ["ok", "rejected"]


def test_second_attempt_checker_must_differ_from_the_new_packer(client, new_worker_client, make_image):
    anna, _ = new_worker_client("Anna")
    bert, _ = new_worker_client("Bert")
    job_id = _packed_job(client, anna, make_image)
    assert _check(bert, job_id, "rejected", "Karton unvollständig").status_code == 201
    job = _job(bert, job_id)
    for line in job["lines"]:
        assert _confirm(bert, line, image=make_image()).status_code == 201  # Bert re-packs
    assert bert.post(f"/api/jobs/{job_id}/finish").status_code == 200
    assert _check(bert, job_id, "ok", "", make_image()).status_code == 403
    assert _check(anna, job_id, "ok", "", make_image()).status_code == 201  # Anna did not pack attempt 2


def test_check_requires_packed_status(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job = _packing_job(client, packer)
    assert _check(checker, job["id"], "ok", "", make_image()).status_code == 409  # still packing
    _confirm_all(packer, job, make_image())
    packer.post(f"/api/jobs/{job['id']}/finish")
    assert _check(checker, job["id"], "ok", "", make_image()).status_code == 201
    assert _check(checker, job["id"], "ok", "", make_image()).status_code == 409  # already checked
    assert _check(checker, 9999, "ok", "", make_image()).status_code == 404


def test_no_check_when_template_disables_second_check(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job = _packing_job(client, packer, _template(client, second_check=False))
    _confirm_all(packer, job, make_image())
    assert packer.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "checked"
    assert _check(checker, job["id"], "ok", "", make_image()).status_code == 409


def test_no_check_when_global_default_is_off(client, new_worker_client, make_image):
    settings = {"second_check_default": False, "session_minutes": 30, "default_quality_points": ["sauber"]}
    assert client.put("/api/expert/settings", headers=EXPERT, json=settings).status_code == 204
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job = _packing_job(client, packer)
    _confirm_all(packer, job, make_image())
    assert packer.post(f"/api/jobs/{job['id']}/finish").json()["status"] == "checked"
    assert _check(checker, job["id"], "ok", "", make_image()).status_code == 409


def test_ship_from_checked_only(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job_id = _packed_job(client, packer, make_image)
    assert checker.post(f"/api/jobs/{job_id}/ship").status_code == 409  # packed, not yet checked
    _check(checker, job_id, "ok", "", make_image())
    resp = checker.post(f"/api/jobs/{job_id}/ship")
    assert resp.status_code == 200 and resp.json()["status"] == "shipped"
    assert checker.post(f"/api/jobs/{job_id}/ship").status_code == 409


def test_qr_svg_is_public_and_404_for_unknown_job(client, worker_client, make_image):
    worker, _ = worker_client
    job_id = _packed_job(client, worker, make_image)
    resp = client.get(f"/api/jobs/{job_id}/qr.svg")  # no cookie, no header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in resp.text
    assert client.get("/api/jobs/9999/qr.svg").status_code == 404


def test_qr_svg_decodes_to_the_job_payload(client, worker_client, make_image):
    from test_api_auth import _svg_to_png

    from galochka import qr

    worker, _ = worker_client
    job_id = _packed_job(client, worker, make_image)
    svg = client.get(f"/api/jobs/{job_id}/qr.svg").text
    assert qr.decode_qr(_svg_to_png(svg)) == f"galochka:job:{job_id}"


LABEL_KEYS = {"id", "kind", "status", "order_ref", "customer", "template_name", "unit_no", "unit_count",
              "packed_by", "checked_by", "checked_at", "shipped_at"}


def test_label_json_for_worker_and_expert(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job_id = _packed_job(client, packer, make_image, order_ref="L-77", customer="Label GmbH", count=2)
    label = packer.get(f"/api/jobs/{job_id}/label")
    assert label.status_code == 200
    data = label.json()
    assert set(data) == LABEL_KEYS
    assert (data["kind"], data["order_ref"], data["customer"], data["template_name"]) == (
        "delivery", "L-77", "Label GmbH", "Set A")
    assert (data["unit_no"], data["unit_count"], data["packed_by"]) == (1, 2, ["Anna"])
    assert data["checked_by"] is None and data["checked_at"] is None and data["shipped_at"] is None

    _check(checker, job_id, "ok", "", make_image())
    data = client.get(f"/api/jobs/{job_id}/label", headers=EXPERT).json()  # expert header, no cookie
    assert data["checked_by"] == "Bert" and data["checked_at"]
    assert data["status"] == "checked" and data["shipped_at"] is None

    checker.post(f"/api/jobs/{job_id}/ship")
    data = checker.get(f"/api/jobs/{job_id}/label").json()
    assert data["status"] == "shipped" and data["shipped_at"]


def test_label_without_second_check_has_no_checker(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker, _template(client, second_check=False))
    _confirm_all(worker, job, make_image())
    worker.post(f"/api/jobs/{job['id']}/finish")
    data = worker.get(f"/api/jobs/{job['id']}/label").json()
    assert data["status"] == "checked" and data["checked_by"] is None and data["packed_by"] == ["Anna"]


def test_label_needs_auth_and_known_job(client, worker_client, make_image):
    worker, _ = worker_client
    job_id = _packed_job(client, worker, make_image)
    assert client.get(f"/api/jobs/{job_id}/label").status_code == 401
    assert client.get(f"/api/jobs/{job_id}/label", headers={"X-Expert-Password": "falsch"}).status_code == 401
    assert worker.get("/api/jobs/9999/label").status_code == 404
    assert client.get("/api/jobs/9999/label", headers=EXPERT).status_code == 404


def test_check_strings_exist():
    from galochka.api import t

    assert t("error.same_worker")
