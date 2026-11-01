import json

import pytest

from galochka import repo

NOW = "2026-11-01T10:00:00+00:00"
LATER = "2026-11-01T11:00:00+00:00"
Q = {"sauber": "ok", "unbeschädigt": "ok", "funktion": "ok"}  # all default quality points answered

LINES = [{"label": "Kabel", "qty": 3, "instruction": "Beutel"}, {"label": "Netzteil"}, {"label": "Gehäuse"}]


def _worker(conn, name="Anna"):
    return repo.create_worker(conn, name, NOW)


def _rma(conn, lines=None, order_ref="RMA-1", customisations=None, claimed_by=None, template_id=None):
    if template_id is None:
        template_id = repo.create_template(conn, "Set A", None, None, lines if lines is not None else LINES, NOW)
    (job_id,) = repo.create_jobs(
        conn, "rma", template_id, order_ref, "Kunde", 1, customisations or [], "expert", NOW, claimed_by=claimed_by
    )
    return job_id


def _intake_ids(conn, job_id):
    return [row["id"] for row, _ in repo.get_job_lines(conn, job_id, "intake")]


def _outgoing(conn, job_id):
    return [row for row, _ in repo.get_job_lines(conn, job_id, "outgoing")]


def _confirm(conn, line_id, wid, result="ok", qty=None, reason="", photo="jobs/x.jpg", attempt=1):
    line = repo.get_line(conn, line_id)
    qty = line["qty"] if qty is None else qty
    quality = Q if qty > 0 else {}
    return repo.add_confirmation(conn, line_id, wid, attempt, result, qty, [], quality, reason, photo, NOW)


def _repair_job(conn, wid, arrived=None, **kw):
    """An rma in status repair. arrived = {position index: (result, qty, reason)}; the rest arrives complete."""
    job_id = _rma(conn, **kw)
    arrived = arrived or {}
    for index, line_id in enumerate(_intake_ids(conn, job_id)):
        if repo.get_line(conn, line_id)["withheld"]:
            continue
        result, qty, reason = arrived.get(index, ("ok", None, ""))
        _confirm(conn, line_id, wid, result, qty, reason)
    assert repo.finish_job(conn, job_id, NOW) == "repair"
    return job_id


def _note(conn, job_id, wid, action, line_id=None, qty=1, reason="", label="", item_id=None, serial=""):
    return repo.add_repair_note(conn, job_id, wid, action, line_id, item_id, label, qty, serial, reason, NOW)


# --- create / intake --------------------------------------------------------------------------


def test_rma_with_claimed_by_is_claimed_and_in_intake(conn):
    wid = _worker(conn)
    job = repo.get_job(conn, _rma(conn, claimed_by=wid))
    assert (job["status"], job["claimed_by"], job["kind"], job["order_ref"]) == ("intake", wid, "rma", "RMA-1")
    assert job["claimed_at"] == NOW
    assert repo.get_job(conn, _rma(conn, order_ref="RMA-2"))["claimed_by"] is None


def test_intake_lines_apply_office_customisations(conn):
    tid = repo.create_template(conn, "Set A", None, None, LINES, NOW)
    tl = repo.get_template(conn, tid)["lines"]
    custom = [
        {"template_line_id": tl[2]["id"], "withheld": True, "reason": "nicht dabei"},
        {"label": "Beilage", "qty": 2, "reason": "Brief"},
    ]
    job_id = _rma(conn, template_id=tid, customisations=custom)
    rows = [r for r, _ in repo.get_job_lines(conn, job_id, "intake")]
    assert [(r["label"], r["source"], r["withheld"]) for r in rows] == [
        ("Kabel", "template", 0), ("Netzteil", "template", 0), ("Gehäuse", "template", 1), ("Beilage", "office", 0),
    ]


def test_intake_confirmation_semantics(conn):
    wid = _worker(conn)
    job_id = _rma(conn)
    cable, psu, case = _intake_ids(conn, job_id)
    # everything arrived
    assert _confirm(conn, psu, wid)
    # partially arrived: missing needs a reason, qty < line.qty
    with pytest.raises(ValueError, match="reason_required"):
        _confirm(conn, cable, wid, "missing", 1)
    with pytest.raises(ValueError, match="bad_qty"):
        _confirm(conn, cable, wid, "ok", 2)
    assert _confirm(conn, cable, wid, "missing", 1, "zwei fehlen")
    # nothing arrived: no photo and no quality points needed
    repo.add_confirmation(conn, case, wid, 1, "missing", 0, [], {}, "nicht im Karton", "", NOW)
    # arrived damaged: reason and photo
    with pytest.raises(ValueError, match="photo_required"):
        _confirm(conn, psu, wid, "not_ok", reason="Riss", photo="")
    with pytest.raises(ValueError, match="reason_required"):
        _confirm(conn, psu, wid, "not_ok")
    assert _confirm(conn, psu, wid, "not_ok", reason="Riss im Gehäuse")
    assert repo.finish_job(conn, job_id, NOW) == "repair"


def test_extra_item_found_at_intake_is_a_worker_line_with_photo(conn):
    wid = _worker(conn)
    job_id = _rma(conn)
    line_id = repo.add_worker_line(conn, job_id, "intake", None, "Fremdteil", 1, "lag im Karton", NOW)
    row = repo.get_line(conn, line_id)
    assert (row["source"], row["stage"], row["position"], row["photo_required"]) == ("worker", "intake", 4, 1)
    with pytest.raises(ValueError, match="photo_required"):
        _confirm(conn, line_id, wid, photo="")
    for lid in _intake_ids(conn, job_id):
        if lid != line_id:
            _confirm(conn, lid, wid)
    with pytest.raises(repo.LinesOpen):
        repo.finish_job(conn, job_id, NOW)
    _confirm(conn, line_id, wid)
    assert repo.finish_job(conn, job_id, NOW) == "repair"
    # no extra lines once the box is in repair
    with pytest.raises(ValueError, match="wrong_status"):
        repo.add_worker_line(conn, job_id, "intake", None, "Noch eins", 1, "grund", NOW)


# --- repair notes -----------------------------------------------------------------------------


def test_notes_need_status_repair(conn):
    wid = _worker(conn)
    job_id = _rma(conn)
    line = _intake_ids(conn, job_id)[0]
    _confirm(conn, line, wid)
    with pytest.raises(ValueError, match="wrong_status"):
        _note(conn, job_id, wid, "kept", line, reason="Kunde will es")
    with pytest.raises(KeyError):
        _note(conn, 9999, wid, "added", label="x", reason="abc")
    delivery = repo.create_jobs(conn, "delivery", repo.create_template(conn, "D", None, None, LINES, NOW),
                                "D-1", "", 1, [], "expert", NOW)[0]
    conn.execute("UPDATE jobs SET status='repair' WHERE id=?", (delivery,))
    with pytest.raises(ValueError, match="wrong_status"):
        _note(conn, delivery, wid, "added", label="x", reason="abc")


def test_kept_replaced_added_are_stored(conn):
    wid = _worker(conn)
    conn.execute("INSERT INTO items (id, name, updated_at) VALUES ('X1', 'Neuteil', ?)", (NOW,))
    job_id = _repair_job(conn, wid)
    cable, psu, _ = _intake_ids(conn, job_id)
    kept = _note(conn, job_id, wid, "kept", cable, qty=1, reason=" Kunde einverstanden ")
    replaced = _note(conn, job_id, wid, "replaced", psu, serial="SN-9")
    added = _note(conn, job_id, wid, "added", qty=2, reason="Ersatzteil", item_id="X1")
    notes = {n["id"]: n for n in repo.list_repair_notes(conn, job_id)}
    assert list(notes) == [kept, replaced, added]  # oldest first
    assert (notes[kept]["reason"], notes[kept]["line_label"], notes[kept]["label"], notes[kept]["qty"]) == (
        "Kunde einverstanden", "Kabel", "Kabel", 1,
    )
    assert (notes[replaced]["serial"], notes[replaced]["reason"], notes[replaced]["worker_name"]) == ("SN-9", "", "Anna")
    assert (notes[added]["job_line_id"], notes[added]["line_label"], notes[added]["label"], notes[added]["item_id"]) == (
        None, None, "Neuteil", "X1",
    )


@pytest.mark.parametrize(
    "kwargs,code",
    [
        (dict(action="kept", line=0, qty=1, reason="ab"), "reason_required"),
        (dict(action="kept", line=0, qty=1, reason=""), "reason_required"),
        (dict(action="kept", line=0, qty=0, reason="Kunde will"), "bad_qty"),
        (dict(action="kept", line=0, qty=4, reason="Kunde will"), "bad_qty"),  # confirmed 3
        (dict(action="replaced", line=0, qty=4), "bad_qty"),
        (dict(action="replaced", line=0, qty=True), "bad_qty"),
        (dict(action="replaced", line=None), "bad_line"),
        (dict(action="replaced", line="other_job"), "bad_line"),
        (dict(action="replaced", line=9999), "bad_line"),
        (dict(action="replaced", line="not_arrived"), "bad_line"),  # confirmed qty 0
        (dict(action="replaced", line="withheld"), "bad_line"),
        (dict(action="replaced", line="outgoing"), "bad_line"),
        (dict(action="replaced", line=0, serial="x" * 65), "invalid_request"),
        (dict(action="replaced", line=0, item_id="NOPE"), "unknown_item:NOPE"),
        (dict(action="bogus", line=0), "invalid_request"),
        (dict(action="added", label="Teil", qty=1, reason="ab"), "reason_required"),
        (dict(action="added", label="", qty=1, reason="Ersatz"), "label_required"),
        (dict(action="added", label="x" * 121, qty=1, reason="Ersatz"), "invalid_request"),
        (dict(action="added", label="Teil", qty=0, reason="Ersatz"), "bad_qty"),
        (dict(action="added", label="Teil", qty=1000, reason="Ersatz"), "bad_qty"),
        (dict(action="added", label="Teil", qty=1, reason="Ersatz", line=0), "bad_line"),
        (dict(action="added", label="Teil", qty=1, reason="Ersatz", item_id="NOPE"), "unknown_item:NOPE"),
    ],
)
def test_note_validation(conn, kwargs, code):
    wid = _worker(conn)
    tid = repo.create_template(conn, "Set A", None, None, LINES, NOW)
    tl = repo.get_template(conn, tid)["lines"]
    job_id = _repair_job(
        conn, wid, {2: ("missing", 0, "fehlte")}, template_id=tid,
        customisations=[{"template_line_id": tl[1]["id"], "withheld": True, "reason": "nicht dabei"}],
    )
    other = _repair_job(conn, wid, order_ref="RMA-2", template_id=tid)
    cable, withheld, not_arrived = _intake_ids(conn, job_id)
    outgoing = repo._insert_line(conn, job_id, "outgoing", 1, {"label": "K", "qty": 1, "quality_points": []})
    lines = {0: cable, "other_job": _intake_ids(conn, other)[0], "withheld": withheld, "not_arrived": not_arrived,
             "outgoing": outgoing, 9999: 9999, None: None}
    kwargs = dict(kwargs)
    line = lines[kwargs.pop("line", None)]
    with pytest.raises(ValueError) as e:
        _note(conn, job_id, wid, kwargs.pop("action"), line, **kwargs)
    assert str(e.value) == code
    assert conn.execute("SELECT COUNT(*) FROM repair_notes").fetchone()[0] == 0


def test_kept_sum_per_line_cannot_exceed_confirmed_qty(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    cable = _intake_ids(conn, job_id)[0]  # 3 arrived
    _note(conn, job_id, wid, "kept", cable, qty=2, reason="Kunde will")
    with pytest.raises(ValueError, match="bad_qty"):
        _note(conn, job_id, wid, "kept", cable, qty=2, reason="Kunde will")
    _note(conn, job_id, wid, "kept", cable, qty=1, reason="Kunde will auch den Rest")
    with pytest.raises(ValueError, match="bad_qty"):
        _note(conn, job_id, wid, "kept", cable, qty=1, reason="Kunde will")
    _note(conn, job_id, wid, "replaced", cable, qty=3)  # replaced is not part of the kept sum


def test_confirmed_qty_is_the_latest_confirmation(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid, {0: ("missing", 1, "zwei fehlen")})
    cable = _intake_ids(conn, job_id)[0]
    with pytest.raises(ValueError, match="bad_qty"):
        _note(conn, job_id, wid, "kept", cable, qty=2, reason="Kunde will")
    _note(conn, job_id, wid, "kept", cable, qty=1, reason="Kunde will")


def test_delete_note_only_by_author_and_in_repair(conn):
    anna, bert = _worker(conn, "Anna"), _worker(conn, "Bert")
    job_id = _repair_job(conn, anna)
    note = _note(conn, job_id, anna, "added", label="Teil", reason="Ersatz")
    with pytest.raises(ValueError, match="not_author"):
        repo.delete_repair_note(conn, note, bert)
    with pytest.raises(KeyError):
        repo.delete_repair_note(conn, 9999, anna)
    assert repo.delete_repair_note(conn, note, anna)["label"] == "Teil"
    assert repo.list_repair_notes(conn, job_id) == []
    keep = _note(conn, job_id, anna, "added", label="Teil", reason="Ersatz")
    repo.generate_outgoing(conn, job_id, NOW)
    with pytest.raises(ValueError, match="wrong_status"):
        repo.delete_repair_note(conn, keep, anna)
    assert len(repo.list_repair_notes(conn, job_id)) == 1


# --- generate_outgoing ------------------------------------------------------------------------


def test_kept_reduces_the_outgoing_qty(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    cable, psu, case = _intake_ids(conn, job_id)
    _note(conn, job_id, wid, "kept", cable, qty=1, reason="Kunde behält eins")
    ids = repo.generate_outgoing(conn, job_id, LATER)
    rows = _outgoing(conn, job_id)
    assert [r["id"] for r in rows] == ids
    assert [(r["position"], r["label"], r["qty"], r["source_line_id"], r["stage"]) for r in rows] == [
        (1, "Kabel", 2, cable, "outgoing"), (2, "Netzteil", 1, psu, "outgoing"), (3, "Gehäuse", 1, case, "outgoing"),
    ]
    assert rows[0]["instruction"] == "Beutel" and rows[0]["source"] == "template"
    assert json.loads(rows[0]["quality_points"]) == ["sauber", "unbeschädigt", "funktion"]
    assert rows[0]["photo_required"] == 1 and rows[0]["serial_required"] == 0 and rows[0]["reason"] == ""
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["stage"], job["stage_changed_at"]) == ("outgoing", "outgoing", LATER)
    assert (job["lines_total"], job["lines_done"]) == (3, 0)


def test_kept_everything_drops_the_line(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    cable, psu, case = _intake_ids(conn, job_id)
    _note(conn, job_id, wid, "kept", cable, qty=3, reason="Kunde behält alle")
    repo.generate_outgoing(conn, job_id, NOW)
    assert [r["label"] for r in _outgoing(conn, job_id)] == ["Netzteil", "Gehäuse"]


def test_missing_with_qty_zero_gives_no_outgoing_line_and_partial_gives_the_arrived_qty(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid, {0: ("missing", 1, "zwei fehlen"), 1: ("missing", 0, "nicht da")})
    cable, psu, case = _intake_ids(conn, job_id)
    repo.generate_outgoing(conn, job_id, NOW)
    assert [(r["label"], r["qty"], r["source_line_id"]) for r in _outgoing(conn, job_id)] == [
        ("Kabel", 1, cable), ("Gehäuse", 1, case),
    ]


def test_added_note_becomes_a_worker_line_without_source_line(conn):
    wid = _worker(conn)
    conn.execute("INSERT INTO items (id, name, updated_at) VALUES ('X1', 'Neuteil', ?)", (NOW,))
    job_id = _repair_job(conn, wid)
    repo.set_setting(conn, "default_quality_points", json.dumps(["rein"]))  # read when the outgoing list is made
    _note(conn, job_id, wid, "added", qty=2, reason="Zusatzteil eingebaut", item_id="X1")
    _note(conn, job_id, wid, "added", qty=1, reason="Dichtung neu", label="Dichtung")
    ids = repo.generate_outgoing(conn, job_id, NOW)
    rows = _outgoing(conn, job_id)
    assert [r["position"] for r in rows] == [1, 2, 3, 4, 5] and [r["id"] for r in rows] == ids
    first, second = rows[3], rows[4]  # after the three intake lines, in note order
    assert (first["source"], first["source_line_id"], first["item_id"], first["label"], first["qty"]) == (
        "worker", None, "X1", "Neuteil", 2,
    )
    assert (first["reason"], first["photo_required"], first["serial_required"]) == ("Zusatzteil eingebaut", 1, 0)
    assert json.loads(first["quality_points"]) == ["rein"] and first["photo_item_id"] == "X1"
    assert (second["label"], second["item_id"], second["reason"]) == ("Dichtung", None, "Dichtung neu")


def test_replaced_keeps_the_qty_and_carries_the_reason(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    cable, psu, _ = _intake_ids(conn, job_id)
    _note(conn, job_id, wid, "replaced", cable, qty=1, reason="Kontakt defekt")
    _note(conn, job_id, wid, "replaced", cable, qty=1, reason="Kabelbruch", serial="SN-1")
    _note(conn, job_id, wid, "replaced", psu)  # no reason given
    repo.generate_outgoing(conn, job_id, NOW)
    rows = _outgoing(conn, job_id)
    assert (rows[0]["qty"], rows[0]["reason"]) == (3, "ersetzt: Kontakt defekt; ersetzt: Kabelbruch")
    assert (rows[1]["qty"], rows[1]["reason"]) == (1, "ersetzt")


def test_withheld_intake_line_gives_nothing(conn):
    wid = _worker(conn)
    tid = repo.create_template(conn, "Set A", None, None, LINES, NOW)
    tl = repo.get_template(conn, tid)["lines"]
    job_id = _repair_job(
        conn, wid, template_id=tid,
        customisations=[{"template_line_id": tl[0]["id"], "withheld": True, "reason": "kommt nicht zurück"}],
    )
    repo.generate_outgoing(conn, job_id, NOW)
    assert [r["label"] for r in _outgoing(conn, job_id)] == ["Netzteil", "Gehäuse"]


def test_office_and_worker_intake_lines_keep_their_source_and_flags(conn):
    wid = _worker(conn)
    tid = repo.create_template(
        conn, "Set A", None, None,
        [{"label": "Gerät", "serial_required": True, "photo_required": False, "quality_points": ["x"]}], NOW,
    )
    job_id = _rma(conn, template_id=tid, customisations=[{"label": "Beilage", "qty": 1, "reason": "Brief"}])
    extra = repo.add_worker_line(conn, job_id, "intake", None, "Fremdteil", 2, "lag im Karton", NOW)
    device, office, _ = _intake_ids(conn, job_id)
    repo.add_confirmation(conn, device, wid, 1, "ok", 1, ["SN-1"], {"x": "ok"}, "", "", NOW)
    _confirm(conn, office, wid)
    _confirm(conn, extra, wid)
    repo.finish_job(conn, job_id, NOW)
    repo.generate_outgoing(conn, job_id, NOW)
    rows = _outgoing(conn, job_id)
    assert [(r["label"], r["source"], r["photo_required"], r["serial_required"], r["qty"]) for r in rows] == [
        ("Gerät", "template", 0, 1, 1), ("Beilage", "office", 1, 0, 1), ("Fremdteil", "worker", 1, 0, 2),
    ]
    assert json.loads(rows[0]["quality_points"]) == ["x"]
    assert [r["source_line_id"] for r in rows] == [device, office, extra]


def test_generate_is_one_shot_and_needs_repair(conn):
    wid = _worker(conn)
    intake = _rma(conn)
    with pytest.raises(ValueError, match="wrong_status"):
        repo.generate_outgoing(conn, intake, NOW)
    job_id = _repair_job(conn, wid, order_ref="RMA-2")
    repo.generate_outgoing(conn, job_id, NOW)
    with pytest.raises(ValueError, match="outgoing_exists"):
        repo.generate_outgoing(conn, job_id, NOW)
    for status in ("packed", "checked", "shipped", "cancelled"):
        other = _rma(conn, order_ref=f"S-{status}")
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, other))
        with pytest.raises(ValueError, match="wrong_status"):
            repo.generate_outgoing(conn, other, NOW)
    with pytest.raises(KeyError):
        repo.generate_outgoing(conn, 9999, NOW)
    delivery = repo.create_jobs(conn, "delivery", repo.create_template(conn, "D", None, None, LINES, NOW),
                                "D-1", "", 1, [], "expert", NOW)[0]
    conn.execute("UPDATE jobs SET status='repair' WHERE id=?", (delivery,))
    with pytest.raises(ValueError, match="wrong_status"):
        repo.generate_outgoing(conn, delivery, NOW)


def test_nothing_to_send_back_is_refused_and_leaves_the_job_alone(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid, {0: ("missing", 0, "nicht da"), 1: ("missing", 0, "fehlt"), 2: ("missing", 0, "weg")})
    with pytest.raises(ValueError, match="outgoing_empty"):
        repo.generate_outgoing(conn, job_id, NOW)
    assert repo.get_job(conn, job_id)["status"] == "repair" and _outgoing(conn, job_id) == []
    _note(conn, job_id, wid, "added", label="Ersatzgerät", reason="Austausch")
    assert len(repo.generate_outgoing(conn, job_id, NOW)) == 1


# --- outgoing stage ----------------------------------------------------------------------------


def test_outgoing_confirm_finish_check_ship(conn):
    anna, bert = _worker(conn, "Anna"), _worker(conn, "Bert")
    job_id = _repair_job(conn, anna)
    repo.generate_outgoing(conn, job_id, NOW)
    lines = _outgoing(conn, job_id)
    for line in lines[:-1]:
        _confirm(conn, line["id"], anna)
    with pytest.raises(repo.LinesOpen) as e:
        repo.finish_job(conn, job_id, NOW)
    assert e.value.line_ids == [lines[-1]["id"]]
    extra = repo.add_worker_line(conn, job_id, "outgoing", None, "Beilage", 1, "Wunsch", NOW)
    assert repo.get_line(conn, extra)["stage"] == "outgoing"
    _confirm(conn, lines[-1]["id"], anna)
    _confirm(conn, extra, anna)
    assert repo.finish_job(conn, job_id, NOW) == "packed"
    with pytest.raises(ValueError, match="same_worker"):
        repo.add_check(conn, job_id, anna, "ok", "", "jobs/box.jpg", NOW)
    repo.add_check(conn, job_id, bert, "ok", "", "jobs/box.jpg", NOW)
    repo.ship_job(conn, job_id, LATER)
    assert repo.get_job(conn, job_id)["status"] == "shipped"


def test_rejected_check_reopens_the_outgoing_lines(conn):
    anna, bert = _worker(conn, "Anna"), _worker(conn, "Bert")
    job_id = _repair_job(conn, anna)
    repo.generate_outgoing(conn, job_id, NOW)
    for line in _outgoing(conn, job_id):
        _confirm(conn, line["id"], anna)
    repo.finish_job(conn, job_id, NOW)
    repo.add_check(conn, job_id, bert, "rejected", "Kabel fehlt im Karton", "", LATER)
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["stage"], job["attempt"]) == ("outgoing", "outgoing", 2)
    assert (job["lines_total"], job["lines_done"]) == (3, 0)
    assert len(repo.open_line_ids(conn, job_id, "outgoing", 2)) == 3
    assert repo.open_line_ids(conn, job_id, "intake", 2) == []  # intake stays done
    for line in _outgoing(conn, job_id):
        _confirm(conn, line["id"], anna, attempt=2)
    assert repo.finish_job(conn, job_id, NOW) == "packed"


# --- expert reopen -----------------------------------------------------------------------------


def test_reopen_outgoing_to_repair_deletes_unconfirmed_outgoing_lines(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    _note(conn, job_id, wid, "added", label="Teil", reason="Ersatz")
    repo.generate_outgoing(conn, job_id, NOW)
    assert len(_outgoing(conn, job_id)) == 4
    assert repo.set_job_status(conn, job_id, "repair", "doch noch ein Teil", LATER) == "outgoing"
    assert _outgoing(conn, job_id) == [] and repo.get_job(conn, job_id)["status"] == "repair"
    assert len(repo.get_job_lines(conn, job_id, "intake")) == 3  # intake untouched
    assert len(repo.list_repair_notes(conn, job_id)) == 1  # notes untouched
    _note(conn, job_id, wid, "kept", _intake_ids(conn, job_id)[0], qty=1, reason="Kunde will")
    assert len(repo.generate_outgoing(conn, job_id, LATER)) == 4  # 3 intake lines + 1 added; cable now qty 2
    assert _outgoing(conn, job_id)[0]["qty"] == 2


def test_reopen_outgoing_to_repair_refused_after_a_confirmation_keeps_the_lines(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    repo.generate_outgoing(conn, job_id, NOW)
    _confirm(conn, _outgoing(conn, job_id)[0]["id"], wid)
    with pytest.raises(ValueError, match="outgoing_confirmed"):
        repo.set_job_status(conn, job_id, "repair", "", LATER)
    assert len(_outgoing(conn, job_id)) == 3 and repo.get_job(conn, job_id)["status"] == "outgoing"


@pytest.mark.parametrize("status", ["packed", "checked", "shipped"])
def test_reopen_finished_rma_to_outgoing_keeps_the_confirmations(conn, status):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    repo.generate_outgoing(conn, job_id, NOW)
    for line in _outgoing(conn, job_id):
        _confirm(conn, line["id"], wid)
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    repo.set_job_status(conn, job_id, "outgoing", "", LATER)
    assert repo.has_outgoing_confirmation(conn, job_id) and len(_outgoing(conn, job_id)) == 3


def test_reopen_cancelled_rma_drops_stale_unconfirmed_outgoing_lines(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    repo.generate_outgoing(conn, job_id, NOW)
    repo.set_job_status(conn, job_id, "cancelled", "", NOW)
    repo.set_job_status(conn, job_id, "intake", "", LATER)
    assert _outgoing(conn, job_id) == []
    assert repo.finish_job(conn, job_id, NOW) == "repair"
    assert len(repo.generate_outgoing(conn, job_id, NOW)) == 3


def test_reopen_cancelled_rma_with_confirmed_outgoing_is_refused(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid)
    repo.generate_outgoing(conn, job_id, NOW)
    _confirm(conn, _outgoing(conn, job_id)[0]["id"], wid)
    repo.set_job_status(conn, job_id, "cancelled", "", NOW)
    with pytest.raises(ValueError, match="bad_transition"):
        repo.set_job_status(conn, job_id, "intake", "", LATER)
    assert len(_outgoing(conn, job_id)) == 3


# --- balance -----------------------------------------------------------------------------------


def test_balance_in_kept_out_delta(conn):
    wid = _worker(conn)
    job_id = _repair_job(conn, wid, {1: ("missing", 0, "nicht da")})
    cable, psu, case = _intake_ids(conn, job_id)
    _note(conn, job_id, wid, "kept", cable, qty=1, reason="Kunde will")
    _note(conn, job_id, wid, "added", label="Teil", reason="Ersatz")
    before = repo.rma_balance(conn, job_id)
    assert [(b["label"], b["in_qty"], b["kept_qty"], b["out_qty"], b["delta"]) for b in before] == [
        ("Kabel", 3, 1, 0, 2), ("Gehäuse", 1, 0, 0, 1),  # Netzteil never arrived: not listed
    ]
    repo.generate_outgoing(conn, job_id, NOW)
    out = {r["source_line_id"]: r for r in _outgoing(conn, job_id)}
    _confirm(conn, out[cable]["id"], wid, "missing", 1, "nur eins im Karton")
    _confirm(conn, out[case]["id"], wid)
    after = {b["intake_line_id"]: b for b in repo.rma_balance(conn, job_id)}
    assert (after[cable]["out_qty"], after[cable]["delta"]) == (1, 1)  # 3 in - 1 kept - 1 out
    assert (after[case]["out_qty"], after[case]["delta"]) == (1, 0)
    assert after[cable]["item_id"] is None and after[cable]["intake_line_id"] == cable
    _confirm(conn, out[cable]["id"], wid, "ok", 2)  # corrected: the latest confirmation counts
    assert {b["intake_line_id"]: b["delta"] for b in repo.rma_balance(conn, job_id)} == {cable: 0, case: 0}


def test_open_rma_balances_lists_unshipped_unfinished_rmas_only(conn):
    wid = _worker(conn)
    active = {}
    for status in ("intake", "repair", "outgoing", "packed", "checked", "shipped", "cancelled"):
        job_id = _rma(conn, order_ref=f"R-{status}")
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        active[status] = job_id
    _rma(conn, order_ref="R-second-intake")  # another intake job
    tid = repo.create_template(conn, "D", None, None, LINES, NOW)
    repo.create_jobs(conn, "delivery", tid, "D-1", "", 1, [], "expert", NOW)
    result = repo.open_rma_balances(conn)
    assert [r["job"]["status"] for r in result] == ["intake", "repair", "outgoing", "packed", "checked", "intake"]
    assert all(r["lines"] == [] for r in result)  # nothing confirmed yet
