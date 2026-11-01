"""Reports (T8): job search, job history, open RMAs and the CSV export."""

import csv
import io
import json
from datetime import datetime, timedelta, timezone

from conftest import EXPERT
from test_api_checks import _check
from test_api_jobs import _confirm, _expert_jobs, _packing_job, _template
from test_api_rma import _new_rma, _note, _repair_rma
from test_api_rma import _template as _rma_template

JOBS = "/api/expert/jobs"
EXPORT = "/api/expert/export.csv"
HEADER = (
    "Auftrag-ID;Art;Status;Auftragsnummer;Kunde;Vorlage;Karton;Stufe;Position;Artikel;Bezeichnung;Soll-Menge;"
    "Ereignis;Ergebnis;Ist-Menge;Seriennummern;Prüfpunkte;Grund;Mitarbeiter;Versuch;Zeitpunkt;Foto"
)
SERIAL_LINES = [{"label": "Kabel", "qty": 2}, {"label": "Gerät", "qty": 1, "serial_required": True}]


def _search(client, **params):
    resp = client.get(JOBS, headers=EXPERT, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(client, **params):
    return [j["id"] for j in _search(client, **params)]


def _set(client, sql, args=()):
    with client.app.state.lock:
        client.app.state.conn.execute(sql, args)
        client.app.state.conn.commit()


def _pack(client, worker, image, serial="SN-1", **kw):
    """A delivery job packed by `worker` (status packed); the serial goes on the serial line."""
    tpl = _template(client, lines=SERIAL_LINES, name=kw.pop("template_name", "Set A"))
    job = _packing_job(client, worker, tpl, **kw)
    for line in job["lines"]:
        extra = {"serials": [serial]} if line["serial_required"] else {}
        assert _confirm(worker, line, image=image, **extra).status_code == 201
    assert worker.post(f"/api/jobs/{job['id']}/finish").status_code == 200
    return job["id"]


def _shipped_job(client, new_worker_client, make_image, **kw):
    packer, anna = new_worker_client("Anna")
    checker, bert = new_worker_client("Bert")
    job_id = _pack(client, packer, make_image(), **kw)
    resp = _check(checker, job_id, "ok", "", make_image())
    assert resp.status_code == 201, resp.text
    assert client.post(f"/api/jobs/{job_id}/ship", headers=EXPERT).status_code == 200
    return job_id, anna, bert, packer, checker


# --- auth -------------------------------------------------------------------------------------


def test_report_routes_need_the_expert_password(client):
    for path in (JOBS + "?q=x", JOBS + "/1/history", EXPORT, "/api/expert/rma/open"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"X-Expert-Password": "falsch"}).status_code == 401, path


# --- search -----------------------------------------------------------------------------------


def test_search_by_order_customer_and_template_is_a_case_insensitive_substring(client):
    tpl = _template(client, name="Prüfstand Mini")
    a = _expert_jobs(client, tpl, order_ref="AB-100", customer="Müller GmbH")[0]["id"]
    b = _expert_jobs(client, tpl, order_ref="CD-200", customer="Schmidt & Söhne")[0]["id"]
    other = _template(client, name="Netzteil")
    c = _expert_jobs(client, other, order_ref="EF_300", customer="50% Rabatt AG")[0]["id"]
    assert _ids(client, q="ab-1") == [a]
    assert _ids(client, q="  CD-200 ") == [b]
    assert _ids(client, q="müller") == [a] and _ids(client, q="MÜLLER") == [a]  # non-ASCII case folding
    assert _ids(client, q="söhne") == [b]
    assert _ids(client, q="netzteil") == [c]
    assert sorted(_ids(client, q="prüfstand")) == [a, b]
    assert _ids(client, q="nichts") == []
    # LIKE wildcards are literals
    assert _ids(client, q="_") == [c] and _ids(client, q="%") == [c]
    assert _ids(client, q="EF_3") == [c] and _ids(client, q="E%3") == []
    assert sorted(_ids(client)) == [a, b, c]  # no filter = every open job, newest first
    assert _ids(client) == [c, b, a]


def test_search_by_serial_finds_confirmations_and_repair_notes(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    packed = _pack(client, packer, make_image(), serial="SN-Ä-77", order_ref="A-1")
    other = _pack(client, packer, make_image(), serial="SN-OTHER", order_ref="A-2")
    rma = _repair_rma(packer, _rma_template(client), make_image(), rma_no="RMA-5")
    psu = rma["lines"][1]["id"]
    assert _note(packer, rma["id"], "replaced", job_line_id=psu, serial="Neu-4711").status_code == 201
    statuses = "packed,repair"
    assert _ids(client, serial="SN-Ä-77", status=statuses) == [packed]
    assert _ids(client, serial="sn-ä-77", status=statuses) == [packed]  # case-insensitive
    assert _ids(client, serial="SN-OTHER", status=statuses) == [other]
    assert _ids(client, serial="neu-4711", status=statuses) == [rma["id"]]  # repair note serial
    assert _ids(client, serial="SN-", status=statuses) == []  # exact element, not a substring
    assert _ids(client, serial="SN-Ä-77", status="open") == []  # combined with the status filter


def test_search_by_worker_covers_confirmer_checker_note_creator_and_claimer(client, new_worker_client, make_image):
    shipped, anna, bert, packer, checker = _shipped_job(client, new_worker_client, make_image, order_ref="A-1")
    claimer, carl = new_worker_client("Carl")
    claimed = _packing_job(client, claimer, order_ref="A-2")["id"]  # claimed, nothing confirmed
    untouched = _expert_jobs(client, _template(client), order_ref="A-3")[0]["id"]
    rma = _repair_rma(claimer, _rma_template(client), make_image(), rma_no="RMA-1")
    dora_client, dora = new_worker_client("Dora")
    noted = _note(dora_client, rma["id"], "added", label="Extra", reason="Ersatz dazu")
    assert noted.status_code == 201, noted.text
    created = dora_client.post("/api/jobs", json={"template_id": _template(client)["id"], "order_ref": "A-4"})
    assert created.status_code == 201, created.text
    everything = "open,packing,packed,checked,shipped,repair,intake,outgoing"
    assert _ids(client, worker_id=anna["id"], status=everything) == [shipped]  # confirmer
    assert _ids(client, worker_id=bert["id"], status=everything) == [shipped]  # checker
    assert sorted(_ids(client, worker_id=carl["id"], status=everything)) == sorted([claimed, rma["id"]])
    dora_jobs = _ids(client, worker_id=dora["id"], status=everything)
    assert rma["id"] in dora_jobs and created.json()["id"] in dora_jobs  # note writer, creator
    assert untouched not in dora_jobs and shipped not in dora_jobs
    assert _ids(client, worker_id=9999, status=everything) == []


def test_search_by_created_date_is_inclusive(client):
    tpl = _template(client)
    ids = [_expert_jobs(client, tpl, order_ref=f"D-{i}")[0]["id"] for i in range(3)]
    for job_id, stamp in zip(ids, ("2026-10-30T23:59:59+00:00", "2026-10-31T00:00:00+00:00", "2026-11-01T23:59:59+00:00")):
        _set(client, "UPDATE jobs SET created_at = ? WHERE id = ?", (stamp, job_id))
    assert _ids(client, **{"from": "2026-10-31"}) == [ids[2], ids[1]]
    assert _ids(client, to="2026-10-31") == [ids[1], ids[0]]
    assert _ids(client, **{"from": "2026-10-31", "to": "2026-11-01"}) == [ids[2], ids[1]]
    assert _ids(client, **{"from": "2026-11-01", "to": "2026-11-01"}) == [ids[2]]
    assert _ids(client, **{"from": "2026-11-02"}) == []
    for bad in ("2026-13-01", "morgen", "01.11.2026"):
        assert client.get(JOBS, headers=EXPERT, params={"from": bad}).status_code == 400
        assert client.get(JOBS, headers=EXPERT, params={"to": bad}).status_code == 400
    assert len(_ids(client, **{"from": "", "to": ""})) == 3  # empty = no bound


def test_search_by_status_and_kind(client, worker_client):
    worker, _ = worker_client
    tpl = _template(client)
    delivery = _expert_jobs(client, tpl, order_ref="L-1")[0]["id"]
    rma = _new_rma(worker, _rma_template(client))["id"]
    cancelled = _expert_jobs(client, tpl, order_ref="L-2")[0]["id"]
    assert client.patch(f"{JOBS}/{cancelled}", headers=EXPERT, json={"status": "cancelled"}).status_code == 200
    assert sorted(_ids(client)) == sorted([delivery, rma])  # default hides shipped and cancelled
    assert _ids(client, status="cancelled") == [cancelled]
    assert sorted(_ids(client, status="open,cancelled")) == sorted([delivery, cancelled])
    assert _ids(client, kind="rma") == [rma]
    assert _ids(client, kind="delivery") == [delivery]
    assert _ids(client, kind="rma", status="open") == []
    for bad in ({"status": "kaputt"}, {"status": ","}, {"kind": "x"}, {"worker_id": 0}, {"limit": 0}, {"limit": 501},
                {"offset": -1}):
        assert client.get(JOBS, headers=EXPERT, params=bad).status_code in (400, 422), bad


def test_search_pagination(client):
    tpl = _template(client)
    ids = [_expert_jobs(client, tpl, order_ref=f"P-{i}")[0]["id"] for i in range(5)]
    newest_first = ids[::-1]
    assert _ids(client, limit=2) == newest_first[:2]
    assert _ids(client, limit=2, offset=2) == newest_first[2:4]
    assert _ids(client, limit=2, offset=4) == newest_first[4:]
    assert _ids(client, limit=2, offset=6) == []
    assert _ids(client, q="P-") == newest_first  # default limit 100 is enough here
    assert len(_ids(client, limit=500)) == 5


def test_search_rows_carry_the_report_columns(client, new_worker_client, make_image):
    job_id, anna, bert, packer, checker = _shipped_job(client, new_worker_client, make_image, order_ref="R-1")
    row = _search(client, status="shipped")[0]
    assert row["id"] == job_id and row["status"] == "shipped"
    assert row["checked_by_name"] == "Bert" and row["deviations"] == 0
    assert row["shipped_at"] == row["stage_changed_at"] and row["shipped_at"]
    for key in ("order_ref", "customer", "template_name", "unit_no", "unit_count", "created_at", "lines_total"):
        assert key in row, key
    # not shipped / not checked yet: both are null
    open_job = _expert_jobs(client, _template(client), order_ref="R-2")[0]["id"]
    row = next(r for r in _search(client) if r["id"] == open_job)
    assert row["shipped_at"] is None and row["checked_by_name"] is None and row["deviations"] == 0


def test_deviations_count_non_ok_and_worker_lines_of_the_current_stage(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker, _template(client, lines=[{"label": "A"}, {"label": "B"}, {"label": "C"}]))
    a, b, c = job["lines"]
    image = make_image()
    assert _confirm(worker, a, image=image).status_code == 201
    assert _confirm(worker, b, "missing", qty=0, reason="Teil nicht da").status_code == 201
    assert _confirm(worker, c, "not_ok", image=image, reason="Kratzer", quality={"sauber": "not_ok", "unbeschädigt": "ok", "funktion": "ok"}).status_code == 201
    extra = worker.post(f"/api/jobs/{job['id']}/lines", json={"label": "Extra", "qty": 1, "reason": "Kunde wollte es"})
    assert extra.status_code == 201, extra.text
    assert _search(client, status="packing")[0]["deviations"] == 3  # missing, not_ok, worker line
    # fixing line b with an ok confirmation removes it from the count: the latest result counts
    assert _confirm(worker, b, image=image).status_code == 201
    assert _search(client, status="packing")[0]["deviations"] == 2


# --- history ----------------------------------------------------------------------------------


def test_history_unknown_job_is_404(client):
    assert client.get(f"{JOBS}/9999/history", headers=EXPERT).status_code == 404


def test_history_keeps_superseded_confirmations_oldest_first(client, worker_client, new_worker_client, make_image):
    anna_client, anna = worker_client
    job = _packing_job(client, anna_client, _template(client, lines=[{"label": "Kabel", "qty": 2}]))
    line = job["lines"][0]
    image = make_image()
    first = _confirm(anna_client, line, "missing", qty=1, image=image, reason="Nur eins im Regal")
    assert first.status_code == 201, first.text
    bert_client, bert = new_worker_client("Bert")
    second = _confirm(bert_client, line, image=image)
    assert second.status_code == 201, second.text
    history = client.get(f"{JOBS}/{job['id']}/history", headers=EXPERT).json()
    assert set(history) == {"job", "lines", "repair_notes", "checks", "audit"}
    assert history["job"]["id"] == job["id"] and history["job"]["order_ref"] == "A-100"
    [entry] = history["lines"]
    assert entry["line"]["label"] == "Kabel"
    confirmations = entry["confirmations"]
    assert [(c["worker_name"], c["result"], c["qty"]) for c in confirmations] == [
        ("Anna", "missing", 1), ("Bert", "ok", 2),
    ]
    assert confirmations[0]["id"] < confirmations[1]["id"]
    assert confirmations[0]["reason"] == "Nur eins im Regal" and confirmations[0]["photo_url"]
    assert confirmations[1]["photo_url"].startswith(f"/photos/jobs/{job['id']}/")
    for key in ("worker_name", "attempt", "result", "qty", "serials", "quality", "reason", "photo_url", "created_at"):
        assert key in confirmations[0], key
    assert entry["line"]["latest"]["id"] == confirmations[1]["id"]


def test_history_has_all_stages_checks_and_audit_with_actor_names(client, new_worker_client, make_image):
    packer, anna = new_worker_client("Anna")
    checker, bert = new_worker_client("Bert")
    job_id = _pack(client, packer, make_image(), order_ref="H-1")
    assert _check(checker, job_id, "rejected", "Netzteil fehlt im Karton").status_code == 201
    history = client.get(f"{JOBS}/{job_id}/history", headers=EXPERT).json()
    assert [(c["worker_name"], c["result"], c["attempt"]) for c in history["checks"]][0] == ("Bert", "rejected", 1)
    actions = [(a["action"], a["actor"], a["actor_name"]) for a in history["audit"]]
    assert actions[0] == ("job.create", "expert", None)
    assert ("job.claim", f"worker:{anna['id']}", "Anna") in actions
    assert ("job.finish", f"worker:{anna['id']}", "Anna") in actions
    assert ("job.check", f"worker:{bert['id']}", "Bert") in actions
    assert sum(1 for a in actions if a[0] == "line.confirm") == 2
    assert len(history["checks"]) == 1
    check_row = next(a for a in history["audit"] if a["action"] == "job.check")
    assert isinstance(check_row["detail"], dict) and check_row["detail"]["result"] == "rejected"
    assert all(set(a) == {"at", "actor", "actor_name", "action", "detail"} for a in history["audit"])
    assert [a["at"] for a in history["audit"]] == sorted(a["at"] for a in history["audit"])
    # audit rows of other jobs are not mixed in
    other = _pack(client, packer, make_image(), order_ref="H-2")
    other_history = client.get(f"{JOBS}/{other}/history", headers=EXPERT).json()
    assert len([a for a in other_history["audit"] if a["action"] == "job.create"]) == 1


def test_history_of_an_rma_lists_intake_then_outgoing_and_the_notes(client, new_worker_client, make_image):
    anna_client, anna = new_worker_client("Anna")
    bert_client, bert = new_worker_client("Bert")
    image = make_image()
    rma = _repair_rma(anna_client, _rma_template(client), image, rma_no="RMA-H")
    cable = rma["lines"][0]["id"]
    assert _note(anna_client, rma["id"], "kept", job_line_id=cable, qty=1, reason="Kunde behält eins").status_code == 201
    assert anna_client.post(f"/api/jobs/{rma['id']}/outgoing").status_code == 200
    history = client.get(f"{JOBS}/{rma['id']}/history", headers=EXPERT).json()
    stages = [e["line"]["stage"] for e in history["lines"]]
    assert stages == sorted(stages, key={"intake": 0, "outgoing": 1}.get) and set(stages) == {"intake", "outgoing"}
    assert all(e["confirmations"] for e in history["lines"] if e["line"]["stage"] == "intake" and not e["line"]["withheld"])
    assert [(n["action"], n["worker_name"], n["line_label"]) for n in history["repair_notes"]] == [
        ("kept", "Anna", "Kabel"),
    ]
    assert any(a["action"] == "repair.note" and a["actor_name"] == "Anna" for a in history["audit"])


# --- open RMAs --------------------------------------------------------------------------------


def test_open_rma_excludes_shipped_and_cancelled_and_has_days_open(client, worker_client, make_image):
    worker, _ = worker_client
    tpl = _rma_template(client)
    open_one = _repair_rma(worker, tpl, make_image(), rma_no="RMA-A")
    old = _repair_rma(worker, tpl, make_image(), rma_no="RMA-B")
    shipped = _new_rma(worker, tpl, "RMA-C")
    cancelled = _new_rma(worker, tpl, "RMA-D")
    assert client.patch(f"{JOBS}/{cancelled['id']}", headers=EXPERT, json={"status": "cancelled"}).status_code == 200
    _set(client, "UPDATE jobs SET status = 'shipped' WHERE id = ?", (shipped["id"],))
    stamp = (datetime.now(timezone.utc) - timedelta(days=12, hours=3)).isoformat(timespec="seconds")
    _set(client, "UPDATE jobs SET created_at = ? WHERE id = ?", (stamp, old["id"]))
    body = client.get("/api/expert/rma/open", headers=EXPERT).json()
    by_ref = {e["job"]["order_ref"]: e for e in body}
    assert sorted(by_ref) == ["RMA-A", "RMA-B"]
    assert by_ref["RMA-B"]["job"]["days_open"] == 12 and by_ref["RMA-A"]["job"]["days_open"] == 0
    assert by_ref["RMA-A"]["job"]["id"] == open_one["id"]
    deltas = [(ln["label"], ln["in_qty"], ln["kept_qty"], ln["out_qty"], ln["delta"]) for ln in by_ref["RMA-A"]["lines"]]
    assert deltas[0] == ("Kabel", 3, 0, 0, 3)


# --- CSV export -------------------------------------------------------------------------------


def _export(client, **params):
    resp = client.get(EXPORT, headers=EXPERT, params=params)
    assert resp.status_code == 200, resp.text
    return resp


def _rows(resp):
    text = resp.content.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:], newline=""), delimiter=";"))


def _scenario(client, new_worker_client, make_image):
    """One shipped delivery (2 confirmations, 1 check) and one RMA with a repair note."""
    job_id, anna, bert, packer, checker = _shipped_job(
        client, new_worker_client, make_image, order_ref="Ä-100", customer="Müller; Söhne \"GmbH\"",
        template_name="Prüfstand",
    )
    rma = _repair_rma(packer, _rma_template(client), make_image(), rma_no="RMA-X")
    assert _note(packer, rma["id"], "replaced", job_line_id=rma["lines"][1]["id"], serial="NEU-1").status_code == 201
    return job_id, rma["id"]


def test_export_format_header_bom_separator_crlf_and_filename(client, new_worker_client, make_image):
    _scenario(client, new_worker_client, make_image)
    today = datetime.now(timezone.utc).date()
    resp = _export(client)
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    start = (today - timedelta(days=90)).isoformat()
    assert resp.headers["content-disposition"] == f'attachment; filename="galochka-export-{start}-{today.isoformat()}.csv"'
    raw = resp.content
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert text[1:].split("\r\n", 1)[0] == HEADER
    assert "\n" not in text.replace("\r\n", "")  # CRLF only (embedded newlines would be quoted, none here)
    assert text.endswith("\r\n")
    rows = _rows(resp)
    assert rows[0] == HEADER.split(";") and len(rows[0]) == 22
    assert all(len(r) == 22 for r in rows)
    named = _export(client, **{"from": "2026-01-01", "to": "2026-12-31"})
    assert named.headers["content-disposition"] == 'attachment; filename="galochka-export-2026-01-01-2026-12-31.csv"'


def test_export_has_one_row_per_confirmation_check_and_note(client, new_worker_client, make_image):
    job_id, rma_id = _scenario(client, new_worker_client, make_image)
    with client.app.state.lock:
        conn = client.app.state.conn
        n_conf = conn.execute("SELECT COUNT(*) AS n FROM confirmations").fetchone()["n"]
        n_check = conn.execute("SELECT COUNT(*) AS n FROM checks").fetchone()["n"]
        n_note = conn.execute("SELECT COUNT(*) AS n FROM repair_notes").fetchone()["n"]
    rows = _rows(_export(client))[1:]
    events = [r[12] for r in rows]
    assert events.count("Bestätigung") == n_conf and events.count("Prüfung") == n_check
    assert events.count("Reparatur") == n_note == 1
    assert len(rows) == n_conf + n_check + n_note

    confirmations = [r for r in rows if r[0] == str(job_id) and r[12] == "Bestätigung"]
    assert len(confirmations) == 2
    cable, device = confirmations
    assert cable[1:12] == ["Lieferung", "Versendet", "Ä-100", 'Müller; Söhne "GmbH"', "Prüfstand", "1/1", "Packen", "1", "",
                           "Kabel", "2"]
    assert cable[13:15] == ["in Ordnung", "2"]
    assert cable[16] == "sauber=ok, unbeschädigt=ok, funktion=ok"  # quality points in order, umlauts intact
    assert cable[18] == "Anna" and cable[19] == "1"
    assert cable[21].startswith(f"/photos/jobs/{job_id}/") and cable[21].endswith(".jpg")
    assert device[15] == "SN-1" and device[11] == "1"
    assert cable[20] <= device[20]  # event time, oldest first

    [check] = [r for r in rows if r[0] == str(job_id) and r[12] == "Prüfung"]
    assert check[13] == "freigegeben" and check[18] == "Bert" and check[19] == "1"
    assert check[21].startswith(f"/photos/jobs/{job_id}/")

    [note] = [r for r in rows if r[12] == "Reparatur"]
    assert note[0] == str(rma_id) and note[1] == "RMA" and note[3] == "RMA-X"
    assert note[7] == "Reparatur" and note[13] == "Ersetzt" and note[15] == "NEU-1" and note[18] == "Anna"
    intake = [r for r in rows if r[0] == str(rma_id) and r[12] == "Bestätigung"]
    assert intake and {r[7] for r in intake} == {"Eingang"}
    # chronological order over all rows
    assert [r[20] for r in rows] == sorted(r[20] for r in rows)


def test_export_rejected_check_shows_note_and_multiple_serials_are_joined(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    tpl = _template(client, lines=[{"label": "Gerät", "qty": 2, "serial_required": True}])
    job = _packing_job(client, packer, tpl, order_ref="S-1")
    assert _confirm(packer, job["lines"][0], image=make_image(), serials=["A-1", "B-2"]).status_code == 201
    assert packer.post(f"/api/jobs/{job['id']}/finish").status_code == 200
    assert _check(checker, job["id"], "rejected", "Karton beschädigt").status_code == 201
    rows = _rows(_export(client))[1:]
    [conf] = [r for r in rows if r[12] == "Bestätigung"]
    assert conf[15] == "A-1, B-2"
    [check] = [r for r in rows if r[12] == "Prüfung"]
    assert check[13] == "zurückgewiesen" and check[17] == "Karton beschädigt" and check[21] == ""


def test_export_date_filter_is_inclusive_and_applies_to_the_event_time(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    checker, _ = new_worker_client("Bert")
    job_id = _pack(client, packer, make_image(), order_ref="E-1")
    assert _check(checker, job_id, "rejected", "Netzteil fehlt").status_code == 201
    with client.app.state.lock:
        ids = [r["id"] for r in client.app.state.conn.execute("SELECT id FROM confirmations ORDER BY id")]
    _set(client, "UPDATE confirmations SET created_at = '2026-09-01T10:00:00+00:00' WHERE id = ?", (ids[0],))
    _set(client, "UPDATE confirmations SET created_at = '2026-10-15T23:59:59+00:00' WHERE id = ?", (ids[1],))
    _set(client, "UPDATE checks SET created_at = '2026-10-16T00:00:00+00:00'")
    everything = _rows(_export(client, **{"from": "2026-01-01", "to": "2026-12-31"}))[1:]
    assert len(everything) == 3
    only_old = _rows(_export(client, **{"from": "2026-09-01", "to": "2026-09-01"}))[1:]
    assert [r[12] for r in only_old] == ["Bestätigung"] and only_old[0][20] == "2026-09-01T10:00:00+00:00"
    middle = _rows(_export(client, **{"from": "2026-10-01", "to": "2026-10-15"}))[1:]
    assert [r[20] for r in middle] == ["2026-10-15T23:59:59+00:00"]  # inclusive end date, check excluded
    assert [r[12] for r in _rows(_export(client, **{"from": "2026-10-16", "to": "2026-10-16"}))[1:]] == ["Prüfung"]
    assert len(_rows(_export(client, **{"from": "2026-09-02", "to": "2026-10-14"}))) == 1  # header only


def test_export_default_window_is_the_last_90_days(client, new_worker_client, make_image):
    packer, _ = new_worker_client("Anna")
    _pack(client, packer, make_image(), order_ref="W-1")
    with client.app.state.lock:
        first = client.app.state.conn.execute("SELECT MIN(id) AS i FROM confirmations").fetchone()["i"]
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(timespec="seconds")
    _set(client, "UPDATE confirmations SET created_at = ? WHERE id = ?", (old, first))
    with client.app.state.lock:
        total = client.app.state.conn.execute("SELECT COUNT(*) AS n FROM confirmations").fetchone()["n"]
    assert len(_rows(_export(client))) - 1 == total - 1
    assert len(_rows(_export(client, **{"from": old[:10]}))) - 1 == total


def test_export_kind_filter_and_bad_parameters(client, new_worker_client, make_image):
    _scenario(client, new_worker_client, make_image)
    assert {r[1] for r in _rows(_export(client, kind="rma"))[1:]} == {"RMA"}
    assert {r[1] for r in _rows(_export(client, kind="delivery"))[1:]} == {"Lieferung"}
    for bad in ({"kind": "x"}, {"from": "gestern"}, {"to": "2026-02-30"}, {"from": "2026-05-02", "to": "2026-05-01"}):
        assert client.get(EXPORT, headers=EXPERT, params=bad).status_code == 400, bad


def test_export_neutralises_spreadsheet_formulas(client, worker_client, make_image):
    worker, _ = worker_client
    job = _packing_job(client, worker, _template(client, lines=[{"label": "=HYPERLINK(\"x\")", "qty": 1}]),
                       customer="@evil")
    assert _confirm(worker, job["lines"][0], "missing", qty=0, reason="+1 fehlt").status_code == 201
    [row] = _rows(_export(client))[1:]
    assert row[4] == "'@evil" and row[10].startswith("'=") and row[17] == "'+1 fehlt"


def test_export_strings_exist_in_german():
    from galochka import api_expert
    from galochka.api import t

    for name in api_expert._EXPORT_COLUMNS:
        assert t("export.col_" + name)
    for key in ("export.event_confirmation", "export.event_check", "export.event_note", "export.stage_pack",
                "export.stage_intake", "export.stage_outgoing", "export.stage_repair", "export.result_ok",
                "export.result_missing", "export.result_not_ok", "export.check_ok", "export.check_rejected",
                "export.quality_ok", "export.quality_not_ok"):
        assert t(key)


# --- expert create form: rma -----------------------------------------------------------------


def test_expert_can_create_an_rma_through_the_jobs_endpoint(client):
    tpl = _template(client)
    resp = client.post(JOBS, headers=EXPERT, json={"kind": "rma", "template_id": tpl["id"], "order_ref": "RMA-77",
                                                   "customer": "K"})
    assert resp.status_code == 201, resp.text
    assert (resp.json()[0]["kind"], resp.json()[0]["status"], resp.json()[0]["unit_count"]) == ("rma", "intake", 1)
    assert client.post(JOBS, headers=EXPERT, json={"kind": "rma", "template_id": tpl["id"], "order_ref": "RMA-77"}
                       ).status_code == 409
    assert client.post(JOBS, headers=EXPERT, json={"kind": "rma", "template_id": tpl["id"], "order_ref": "RMA-78",
                                                   "count": 2}).status_code == 400
