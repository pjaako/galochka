"""Regression tests for the T9 backend fix wave (review findings 1-15)."""

import io
import json
import logging
import threading
import time
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import EXPERT
from galochka import cli, images, qr, repo
from galochka.api import create_app
from galochka.cassandra_client import _ITEMS_LIMIT, CassandraClient, CassandraUnavailable
from galochka.config import Settings
from galochka.importer import apply_catalog, fetch_catalog, sync_items
from test_api_jobs import _confirm, _expert_jobs, _job, _packing_job, _template
from test_api_reports import SERIAL_LINES, _export, _pack, _rows, _set
from test_api_rma import _confirm as _rma_confirm
from test_api_rma import _new_rma, _note, _repair_rma
from test_api_rma import _template as _rma_template
from test_cassandra_client import FakeCassandra

HUGE = "99999999999999999999"


# --- 1. generated outgoing lines cannot be deleted ----------------------------------------------


def _delete_line(worker, line_id):
    return worker.delete(f"/api/lines/{line_id}")


def test_generated_added_line_cannot_be_deleted(client, worker_client, make_image):
    worker, _ = worker_client
    job = _repair_rma(worker, _rma_template(client), make_image())
    assert _note(worker, job["id"], "added", label="Dichtung", reason="Neu eingebaut").status_code == 201
    detail = worker.post(f"/api/jobs/{job['id']}/outgoing").json()
    added = [ln for ln in detail["lines"] if ln["label"] == "Dichtung"][0]
    assert (added["source"], added["generated"]) == ("worker", True)
    resp = _delete_line(worker, added["id"])
    assert resp.status_code == 409
    assert _job(worker, job["id"])["lines_total"] == 4  # still on the return list


def test_generated_line_inherited_from_worker_intake_line_cannot_be_deleted(client, worker_client, make_image):
    worker, _ = worker_client
    img = make_image()
    job = _new_rma(worker, _rma_template(client))
    for line in _job(worker, job["id"])["lines"]:
        assert _rma_confirm(worker, line, image=img).status_code == 201
    resp = worker.post(f"/api/jobs/{job['id']}/lines", json={"label": "Fremdteil", "qty": 1, "reason": "lag im Karton"})
    extra = resp.json()
    assert extra["generated"] is False
    assert _delete_line(worker, extra["id"]).status_code == 204  # deletable while unconfirmed and not generated
    resp = worker.post(f"/api/jobs/{job['id']}/lines", json={"label": "Fremdteil", "qty": 1, "reason": "lag im Karton"})
    extra = resp.json()
    assert _rma_confirm(worker, extra, image=img).status_code == 201
    assert worker.post(f"/api/jobs/{job['id']}/finish").status_code == 200
    detail = worker.post(f"/api/jobs/{job['id']}/outgoing").json()
    inherited = [ln for ln in detail["lines"] if ln["label"] == "Fremdteil"][0]
    assert (inherited["source"], inherited["generated"]) == ("worker", True)
    assert _delete_line(worker, inherited["id"]).status_code == 409


def test_worker_added_outgoing_line_is_still_deletable(client, worker_client, make_image):
    worker, _ = worker_client
    job = _repair_rma(worker, _rma_template(client), make_image())
    assert worker.post(f"/api/jobs/{job['id']}/outgoing").status_code == 200
    resp = worker.post(f"/api/jobs/{job['id']}/lines", json={"label": "Beilage", "qty": 1, "reason": "lag dabei"})
    assert resp.status_code == 201
    line = resp.json()
    assert (line["source"], line["stage"], line["generated"]) == ("worker", "outgoing", False)
    assert _delete_line(worker, line["id"]).status_code == 204


# --- 2. job detail never blocks per line on Cassandra --------------------------------------------


class SlowFailingCassandra(CassandraClient):
    def fetch_item(self, item_id):
        time.sleep(2.0)
        raise CassandraUnavailable("down")


def test_job_detail_with_slow_cassandra_answers_fast_and_then_from_cache(client, worker_client, add_items):
    ids = [f"S{i:02d}" for i in range(12)]
    add_items(*ids)
    tpl = _template(client, lines=[{"item_id": i} for i in ids])
    worker, _ = worker_client
    job = _packing_job(client, worker, tpl)
    client.app.state.cassandra = SlowFailingCassandra("http://cassandra.invalid:8080")
    started = time.monotonic()
    resp = worker.get(f"/api/jobs/{job['id']}")
    first = time.monotonic() - started
    assert resp.status_code == 200 and len(resp.json()["lines"]) == 12
    assert first < 3.5, first
    started = time.monotonic()
    assert worker.get(f"/api/jobs/{job['id']}").status_code == 200
    second = time.monotonic() - started
    assert second < 0.5, second


def test_reference_photos_fetch_false_never_calls_the_network(conn):
    calls = []

    class Spy(CassandraClient):
        def fetch_item(self, item_id):
            calls.append(item_id)
            return {"name": "X", "photos": [{"url": "/p.jpg", "source": "reference"}]}

    lock = threading.Lock()
    spy = Spy("http://cassandra.invalid:8080")
    now = "2026-11-01T10:00:00+00:00"
    assert spy.reference_photos(conn, lock, "A1", now, fetch=False) == {
        "item_id": "A1", "name": "", "photos": [], "cassandra_url": spy.base_url, "stale": False,
    }
    assert calls == []
    assert spy.reference_photos(conn, lock, "A1", now)["photos"]  # a real fetch fills the cache
    later = "2026-11-01T11:00:00+00:00"  # expired: served as stale, still no network
    result = spy.reference_photos(conn, lock, "A1", later, fetch=False)
    assert result["photos"] and result["stale"] is True
    assert calls == ["A1"]


# --- 3. truncated item listing -------------------------------------------------------------------


def _item_row(i):
    return {"item_id": f"T{i:05d}", "name": f"Teil {i}", "group_name": ""}


def test_full_ungrouped_listing_deactivates_nothing_and_reports_truncated(conn):
    fake = FakeCassandra()
    try:
        sync_items(conn, _Static([{"item_id": "OLD", "name": "Alt", "group_name": "G"}]), "2026-11-01T10:00:00+00:00")
        fake.list_rows = {"": [_item_row(i) for i in range(_ITEMS_LIMIT)]}
        items, complete = fetch_catalog(CassandraClient(fake.url))
        assert len(items) == _ITEMS_LIMIT and complete is False
        result = apply_catalog(conn, items, "2026-11-02T10:00:00+00:00", complete)
        assert result["deactivated"] == 0 and result["truncated"] is True
        assert conn.execute("SELECT active FROM items WHERE id='OLD'").fetchone()["active"] == 1
    finally:
        fake.stop()


def test_a_full_group_marks_the_catalog_incomplete(conn):
    fake = FakeCassandra()
    try:
        fake.groups = ["Big"]
        fake.list_rows = {"Big": [dict(_item_row(i), group_name="Big") for i in range(_ITEMS_LIMIT)], "": []}
        assert fetch_catalog(CassandraClient(fake.url))[1] is False
    finally:
        fake.stop()


def test_normal_listing_is_complete_and_unchanged(conn):
    fake = FakeCassandra()
    try:
        fake.groups = ["G"]
        fake.list_rows = {"G": [dict(_item_row(i), group_name="G") for i in range(3)], "": []}
        client = CassandraClient(fake.url)
        result = sync_items(conn, client, "2026-11-01T10:00:00+00:00")
        assert result["added"] == 3 and "truncated" not in result
        fake.list_rows = {"G": [dict(_item_row(0), group_name="G")], "": []}
        result = sync_items(conn, client, "2026-11-02T10:00:00+00:00")
        assert (result["deactivated"], "truncated" in result) == (2, False)
    finally:
        fake.stop()


class _Static:
    def __init__(self, items):
        self.items = items

    def iter_all_items(self):
        yield from self.items


def test_expert_sync_endpoint_shows_truncation(data_dir):
    fake = FakeCassandra()
    try:
        fake.list_rows = {"": [_item_row(i) for i in range(_ITEMS_LIMIT)]}
        app = create_app(Settings(data_dir=data_dir, expert_password="experte", auto_sync=False, cassandra_url=fake.url))
        with TestClient(app) as c:
            body = c.post("/api/expert/sync/items", headers=EXPERT).json()
        assert body["truncated"] is True and body["deactivated"] == 0
        assert "unvollständige Liste" in body["warning"]
    finally:
        fake.stop()


# --- 4. image decoding robustness ----------------------------------------------------------------


def _png_with_huge_ztxt() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    png = buf.getvalue()
    data = b"Comment\x00\x00" + zlib.compress(b"\x00" * (8 * 1024 * 1024))
    chunk = len(data).to_bytes(4, "big") + b"zTXt" + data
    chunk += zlib.crc32(b"zTXt" + data).to_bytes(4, "big")
    ihdr_end = 8 + 25  # signature + IHDR chunk
    return png[:ihdr_end] + chunk + png[ihdr_end:]


def _big_bilevel_png() -> bytes:
    buf = io.BytesIO()
    Image.new("1", (10000, 10000)).save(buf, format="PNG")
    assert len(buf.getvalue()) < 2_000_000
    return buf.getvalue()


@pytest.mark.parametrize("make_bad", [_png_with_huge_ztxt, _big_bilevel_png], ids=["ztxt", "pixels"])
def test_normalize_refuses_hostile_images(make_bad):
    with pytest.raises(images.BadImage):
        images.normalize(make_bad())


@pytest.mark.parametrize("make_bad", [_png_with_huge_ztxt, _big_bilevel_png], ids=["ztxt", "pixels"])
def test_confirm_with_hostile_image_is_400(client, worker_client, make_bad):
    worker, _ = worker_client
    job = _packing_job(client, worker)
    resp = _confirm(worker, job["lines"][0], image=make_bad())
    assert resp.status_code == 400 and resp.json()["detail"] == "Das Bild konnte nicht gelesen werden."
    assert _job(worker, job["id"])["lines_done"] == 0


@pytest.mark.parametrize("make_bad", [_png_with_huge_ztxt, _big_bilevel_png], ids=["ztxt", "pixels"])
def test_login_qr_with_hostile_image_fails_gracefully(client, make_bad):
    assert qr.decode_qr(make_bad()) is None
    resp = client.post("/api/login/qr", files={"image": ("b.png", make_bad(), "image/png")})
    assert resp.status_code in (400, 401)


# --- 5. expert password hardening ----------------------------------------------------------------


def test_empty_expert_password_env_stops_startup(monkeypatch):
    monkeypatch.setenv("GALOCHKA_EXPERT_PASSWORD", "")
    with pytest.raises(SystemExit):
        cli.main(["migrate"])


def test_missing_expert_password_env_keeps_default_and_warns(monkeypatch, caplog):
    monkeypatch.delenv("GALOCHKA_EXPERT_PASSWORD", raising=False)
    with caplog.at_level(logging.WARNING, logger="galochka"):
        settings = Settings.from_env()
    assert settings.expert_password == "experte"
    assert "GALOCHKA_EXPERT_PASSWORD" in caplog.text


def test_empty_expert_header_is_401_even_with_an_empty_configured_password(data_dir, client):
    assert client.get("/api/expert/settings", headers={"X-Expert-Password": ""}).status_code == 401
    app = create_app(Settings(data_dir=data_dir / "other", expert_password="", auto_sync=False))
    with TestClient(app) as c:
        assert c.get("/api/expert/settings", headers={"X-Expert-Password": ""}).status_code == 401


# --- 6. huge integer ids -------------------------------------------------------------------------


def test_huge_ids_never_give_500(client, worker_client):
    worker, _ = worker_client
    for resp in (
        worker.get(f"/api/jobs/{HUGE}"),
        worker.post(f"/api/jobs/{HUGE}/claim"),
        worker.post(f"/api/jobs/{HUGE}/outgoing"),
        worker.delete(f"/api/lines/{HUGE}"),
        worker.delete(f"/api/repair-notes/{HUGE}"),
        worker.post(f"/api/lines/{HUGE}/confirm", data={"result": "ok", "qty": "1"}),
        client.get(f"/api/jobs/{HUGE}/qr.svg"),
        client.get(f"/api/jobs/{HUGE}/label", headers=EXPERT),
        client.patch(f"/api/expert/jobs/{HUGE}", headers=EXPERT, json={"status": "cancelled"}),
        client.get(f"/api/expert/jobs/{HUGE}/history", headers=EXPERT),
        client.get(f"/api/expert/templates/{HUGE}", headers=EXPERT),
        client.patch(f"/api/expert/workers/{HUGE}", headers=EXPERT, json={"name": "x"}),
        client.get(f"/api/expert/workers/{HUGE}/badge.svg", headers=EXPERT),
        worker.post("/api/rma", json={"template_id": int(HUGE), "rma_no": "R"}),
        worker.post("/api/jobs", json={"template_id": int(HUGE), "order_ref": "A"}),
        client.post("/api/expert/jobs", headers=EXPERT, json={"template_id": int(HUGE), "order_ref": "A"}),
        client.get("/api/expert/jobs", headers=EXPERT, params={"worker_id": HUGE}),
    ):
        assert resp.status_code in (404, 422), (resp.request.url, resp.status_code, resp.text)


def test_huge_job_line_id_in_a_repair_note_is_422(client, worker_client, make_image):
    worker, _ = worker_client
    job = _repair_rma(worker, _rma_template(client), make_image())
    assert _note(worker, job["id"], "kept", job_line_id=int(HUGE), qty=1, reason="Kunde will").status_code == 422


# --- 7. re-confirming an intake line that has repair notes ---------------------------------------


def test_reconfirming_an_intake_line_with_notes_is_409(client, worker_client, make_image):
    worker, _ = worker_client
    img = make_image()
    job = _repair_rma(worker, _rma_template(client), img)
    cable = job["lines"][0]  # 3 arrived
    assert _note(worker, job["id"], "kept", job_line_id=cable["id"], qty=2, reason="Kunde will").status_code == 201
    assert client.patch(f"/api/expert/jobs/{job['id']}", headers=EXPERT, json={"status": "cancelled"}).status_code == 200
    resp = client.patch(f"/api/expert/jobs/{job['id']}", headers=EXPERT, json={"status": "intake"})
    assert resp.status_code == 200 and resp.json()["status"] == "intake"
    resp = _rma_confirm(worker, cable, image=img)
    assert resp.status_code == 409 and resp.json()["detail"] == "Für diese Position gibt es bereits Reparaturnotizen."
    # lines without notes can still be corrected
    assert _rma_confirm(worker, job["lines"][1], image=img).status_code == 201


# --- 8. kept + replaced on one line --------------------------------------------------------------


def test_kept_then_replaced_cannot_exceed_the_confirmed_qty(client, worker_client, make_image):
    worker, _ = worker_client
    job = _repair_rma(worker, _rma_template(client, lines=[{"label": "Kabel", "qty": 1}]), make_image())
    line = job["lines"][0]
    assert _note(worker, job["id"], "kept", job_line_id=line["id"], qty=1, reason="Kunde will").status_code == 201
    resp = _note(worker, job["id"], "replaced", job_line_id=line["id"], qty=1)
    assert resp.status_code == 400 and resp.json()["detail"] == "Die Menge passt nicht zur Zeile."


# --- 9. local time in the reports and the export -------------------------------------------------


def test_export_uses_local_days_and_prints_local_time(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    _pack(client, packer, make_image(), order_ref="TZ-1")
    with client.app.state.lock:
        ids = [r["id"] for r in client.app.state.conn.execute("SELECT id FROM confirmations ORDER BY id")]
    _set(client, "UPDATE confirmations SET created_at = '2026-03-01T23:30:00+00:00' WHERE id = ?", (ids[0],))
    _set(client, "UPDATE confirmations SET created_at = '2026-01-01T12:00:00+00:00' WHERE id != ?", (ids[0],))
    on_the_2nd = _rows(_export(client, **{"from": "2026-03-02", "to": "2026-03-02"}))[1:]
    assert [r[20] for r in on_the_2nd] == ["02.03.2026 00:30"]
    assert len(_rows(_export(client, **{"from": "2026-03-01", "to": "2026-03-01"}))) == 1  # header only


def test_job_search_window_uses_local_days(client):
    tpl = _template(client)
    job_id = _expert_jobs(client, tpl)[0]["id"]
    _set(client, "UPDATE jobs SET created_at = '2026-03-01T23:30:00+00:00' WHERE id = ?", (job_id,))

    def ids(**params):
        resp = client.get("/api/expert/jobs", headers=EXPERT, params=params)
        return [j["id"] for j in resp.json()]

    assert ids(**{"from": "2026-03-02", "to": "2026-03-02"}) == [job_id]
    assert ids(**{"from": "2026-03-01", "to": "2026-03-01"}) == []


def test_timezone_setting(monkeypatch):
    monkeypatch.setenv("GALOCHKA_EXPERT_PASSWORD", "x")
    monkeypatch.delenv("GALOCHKA_TZ", raising=False)
    assert Settings.from_env().tz == "Europe/Berlin"
    monkeypatch.setenv("GALOCHKA_TZ", "UTC")
    assert Settings.from_env().tz == "UTC"


# --- 10. deviations across stages ----------------------------------------------------------------


def test_shipped_rma_counts_deviations_of_the_intake_stage(client, worker_client, make_image):
    worker, _ = worker_client
    img = make_image()
    job = _new_rma(worker, _rma_template(client))
    cable, psu, case = _job(worker, job["id"])["lines"]
    assert _rma_confirm(worker, cable, "missing", 0, reason="nicht im Karton").status_code == 201
    for line in (psu, case):
        assert _rma_confirm(worker, line, image=img).status_code == 201
    assert worker.post(f"/api/jobs/{job['id']}/finish").status_code == 200
    _set(client, "UPDATE jobs SET status = 'shipped' WHERE id = ?", (job["id"],))
    rows = client.get("/api/expert/jobs", headers=EXPERT, params={"status": "shipped"}).json()
    assert [r["deviations"] for r in rows if r["id"] == job["id"]][0] >= 1


# --- 11. specific BOM errors ---------------------------------------------------------------------


def test_bom_with_a_bad_quantity_gets_its_own_message(client):
    head = '"BOM number","BOM name","Product number","Product name","Part No.","Part description","Parameters","Quantity","Notes"'
    content = ("﻿" + head + '\r\n"B1","N","P","P","A1","x","","viele",""\r\n').encode("utf-8")
    resp = client.post("/api/expert/import/bom", headers=EXPERT, files={"file": ("b.csv", content, "text/csv")})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Die Spalte „Quantity“ enthält einen ungültigen Wert."


# --- 12. duplicate serials -----------------------------------------------------------------------


def test_duplicate_serials_are_refused(client, worker_client, make_image):
    worker, _ = worker_client
    tpl = _template(client, lines=[{"label": "Gerät", "qty": 2, "serial_required": True}])
    job = _packing_job(client, worker, tpl)
    line = job["lines"][0]
    resp = _confirm(worker, line, image=make_image(), serials=["SN-1", " SN-1 "])
    assert resp.status_code == 400 and resp.json()["detail"] == "Seriennummern dürfen sich nicht wiederholen."
    assert _confirm(worker, line, image=make_image(), serials=["SN-1", "SN-2"]).status_code == 201


def test_validate_confirmation_flags_duplicates():
    line = {"withheld": 0, "qty": 2, "serial_required": 1, "quality_points": "[]", "photo_required": 0,
            "source": "template"}
    assert repo.validate_confirmation(line, "ok", 2, ["a", "a"], {}, "", False) == "error.serial_duplicate"
    assert repo.validate_confirmation(line, "ok", 2, ["a", "b"], {}, "", False) is None


# --- 13. shutdown under the lock -----------------------------------------------------------------


def test_shutdown_closes_the_connection_only_after_taking_the_lock(data_dir):
    app = create_app(Settings(data_dir=data_dir, expert_password="experte", auto_sync=False))
    tc = TestClient(app)
    tc.__enter__()
    app.state.lock.acquire()
    closer = threading.Thread(target=lambda: tc.__exit__(None, None, None))
    closer.start()
    closer.join(0.5)
    try:
        assert closer.is_alive()  # waits for the lock
        app.state.conn.execute("SELECT 1")  # still open
    finally:
        app.state.lock.release()
    closer.join(5)
    assert not closer.is_alive()
    with pytest.raises(Exception):
        app.state.conn.execute("SELECT 1")


# --- 15. every worker route needs a session ------------------------------------------------------

WORKER_ROUTES = [
    ("GET", "/api/me"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("POST", "/api/rma"),
    ("POST", "/api/jobs/1/claim"),
    ("POST", "/api/jobs/1/lines"),
    ("DELETE", "/api/lines/1"),
    ("POST", "/api/lines/1/confirm"),
    ("POST", "/api/jobs/1/finish"),
    ("POST", "/api/jobs/1/check"),
    ("POST", "/api/jobs/1/repair-notes"),
    ("DELETE", "/api/repair-notes/1"),
    ("POST", "/api/jobs/1/outgoing"),
    # worker or expert
    ("GET", "/api/items"),
    ("GET", "/api/items/A1/reference"),
    ("GET", "/api/templates"),
    ("GET", "/api/jobs/1"),
    ("GET", "/api/jobs/1/label"),
    ("POST", "/api/jobs/1/ship"),
]
PUBLIC_ROUTES = {
    ("POST", "/api/login/qr"), ("POST", "/api/login/code"), ("POST", "/api/logout"),
    ("GET", "/api/jobs/1/qr.svg"), ("GET", "/health"),
}


@pytest.mark.parametrize("method,path", WORKER_ROUTES)
def test_worker_routes_answer_401_without_a_cookie(client, method, path):
    resp = client.request(method, path)
    assert resp.status_code == 401, resp.text


def test_every_api_route_is_either_listed_here_or_expert_only_or_public(client):
    import re

    known = set(WORKER_ROUTES) | PUBLIC_ROUTES
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path.startswith("/api/expert/"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            concrete = re.sub(r"\{[^}]+\}", "1", path).replace("/items/1/reference", "/items/A1/reference")
            assert (method, concrete) in known, (method, path)
