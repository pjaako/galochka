import json

import pytest

from galochka import repo

NOW = "2026-11-01T10:00:00+00:00"
LATER = "2026-11-01T11:00:00+00:00"
Q = {"sauber": "ok", "unbeschädigt": "ok", "Funktion geprüft": "ok"}  # all default quality points answered


def _worker(conn, name="Anna"):
    return repo.create_worker(conn, name, NOW)


def _template(conn, lines=None, second_check=None, name="Set A"):
    lines = lines if lines is not None else [{"label": "Kabel", "qty": 2}, {"label": "Netzteil"}]
    return repo.create_template(conn, name, None, second_check, lines, NOW)


def _job(conn, template_id=None, kind="delivery", **kw):
    template_id = template_id or _template(conn)
    args = dict(order_ref="A-1", customer="Kunde", count=1, customisations=[], created_by="expert", now=NOW)
    args.update(kw)
    (job_id,) = repo.create_jobs(conn, kind, template_id, **args)
    return job_id


def _packing_job(conn, worker_id, **kw):
    job_id = _job(conn, **kw)
    repo.claim_job(conn, job_id, worker_id, NOW)
    return job_id


def _line_ids(conn, job_id, stage="pack"):
    return [row["id"] for row, _ in repo.get_job_lines(conn, job_id, stage)]


def _confirm_all(conn, job_id, worker_id, attempt=1):
    for row, _ in repo.get_job_lines(conn, job_id, repo.current_stage(repo.get_job(conn, job_id))):
        if row["withheld"]:
            continue
        points = json.loads(row["quality_points"])
        repo.add_confirmation(
            conn, row["id"], worker_id, attempt, "ok", row["qty"], [], {p: "ok" for p in points}, "", "jobs/x.jpg", NOW
        )


# --- create_jobs ----------------------------------------------------------------------------


def test_create_jobs_snapshots_lines_per_unit(conn):
    tid = _template(conn, [{"label": "Kabel", "qty": 2, "instruction": "Beutel"}, {"label": "Netzteil"}])
    ids = repo.create_jobs(conn, "delivery", tid, "A-1", "Kunde", 3, [], "expert", NOW, note=" Eilig ")
    assert len(ids) == 3
    jobs = [repo.get_job(conn, i) for i in ids]
    assert [j["unit_no"] for j in jobs] == [1, 2, 3]
    assert {j["unit_count"] for j in jobs} == {3}
    assert {j["status"] for j in jobs} == {"open"}
    assert {j["note"] for j in jobs} == {"Eilig"}
    assert jobs[0]["stage_changed_at"] == NOW and jobs[0]["template_name"] == "Set A"
    all_ids = [_line_ids(conn, i) for i in ids]
    assert len({lid for ids_ in all_ids for lid in ids_}) == 6  # separate snapshots
    first = repo.get_job_lines(conn, ids[0], "pack")
    assert [(r["position"], r["label"], r["qty"], r["instruction"], r["source"]) for r, _ in first] == [
        (1, "Kabel", 2, "Beutel", "template"),
        (2, "Netzteil", 1, "", "template"),
    ]


def test_quality_points_resolve_default_and_keep_own(conn):
    repo.set_setting(conn, "default_quality_points", json.dumps(["rein", "heil"]))
    tid = _template(conn, [{"label": "A"}, {"label": "B", "quality_points": ["x"]}])
    job_id = _job(conn, tid)
    rows = [r for r, _ in repo.get_job_lines(conn, job_id, "pack")]
    assert json.loads(rows[0]["quality_points"]) == ["rein", "heil"]
    assert json.loads(rows[1]["quality_points"]) == ["x"]


def test_label_falls_back_to_item_name(conn):
    conn.execute("INSERT INTO items (id, name, updated_at) VALUES ('A1', 'Kabel USB', ?)", (NOW,))
    tid = _template(conn, [{"item_id": "A1"}])
    conn.execute("UPDATE template_lines SET label='' WHERE template_id=?", (tid,))
    job_id = _job(conn, tid)
    assert repo.get_job_lines(conn, job_id, "pack")[0][0]["label"] == "Kabel USB"


def test_template_edit_after_creation_leaves_job_lines(conn):
    tid = _template(conn, [{"label": "Alt"}])
    job_id = _job(conn, tid)
    template = repo.get_template(conn, tid)
    repo.replace_template(conn, tid, "Neu", None, True, None, [{"label": "Komplett anders", "qty": 5}], LATER)
    rows = repo.get_job_lines(conn, job_id, "pack")
    assert [(r["label"], r["qty"], r["template_line_id"]) for r, _ in rows] == [("Alt", 1, template["lines"][0]["id"])]
    assert repo.get_job(conn, job_id)["template_name"] == "Set A"


def test_withheld_and_office_extra_customisations(conn):
    tid = _template(conn, [{"label": "A"}, {"label": "B"}])
    tl = repo.get_template(conn, tid)["lines"]
    conn.execute("INSERT INTO items (id, name, updated_at) VALUES ('X1', 'Zusatzteil', ?)", (NOW,))
    custom = [
        {"template_line_id": tl[1]["id"], "withheld": True, "reason": "Kunde hat schon eins"},
        {"item_id": "X1", "qty": 3, "reason": "Sonderwunsch"},
        {"label": "Beilage", "qty": 1, "reason": "Brief"},
    ]
    job_id = _job(conn, tid, customisations=custom)
    rows = [r for r, _ in repo.get_job_lines(conn, job_id, "pack")]
    assert [(r["position"], r["label"], r["source"], r["withheld"]) for r in rows] == [
        (1, "A", "template", 0),
        (2, "B", "template", 1),
        (3, "Zusatzteil", "office", 0),
        (4, "Beilage", "office", 0),
    ]
    assert rows[1]["withheld_reason"] == "Kunde hat schon eins" and rows[1]["withheld_by"] == "expert"
    assert (rows[2]["qty"], rows[2]["reason"], rows[2]["item_id"]) == (3, "Sonderwunsch", "X1")
    job = repo.get_job(conn, job_id)
    assert (job["lines_total"], job["lines_done"]) == (3, 0)  # withheld line does not count
    assert repo.open_line_ids(conn, job_id, "pack", 1) == [rows[0]["id"], rows[2]["id"], rows[3]["id"]]


@pytest.mark.parametrize(
    "custom,code",
    [
        ([{"template_line_id": 99999, "withheld": True, "reason": "abc"}], "unknown_template_line"),
        ([{"template_line_id": 1, "withheld": True, "reason": ""}], "reason_required"),
        ([{"template_line_id": 1, "withheld": True, "reason": "ab"}], "reason_required"),
        ([{"template_line_id": 1, "withheld": False, "reason": "abc"}], "invalid_request"),
        ([{"label": "X", "qty": 1, "reason": ""}], "reason_required"),
        ([{"label": "", "qty": 1, "reason": "abc"}], "label_required"),
        ([{"label": "X", "qty": 0, "reason": "abc"}], "bad_qty"),
        ([{"item_id": "NOPE", "qty": 1, "reason": "abc"}], "unknown_item:NOPE"),
        ("not a list", "invalid_request"),
    ],
)
def test_bad_customisations_are_rejected_before_any_write(conn, custom, code):
    tid = _template(conn, [{"label": "A"}])
    if custom and isinstance(custom, list) and custom[0].get("template_line_id") == 1:
        custom[0]["template_line_id"] = repo.get_template(conn, tid)["lines"][0]["id"]
    with pytest.raises(ValueError, match=code):
        repo.create_jobs(conn, "delivery", tid, "A-1", "", 1, custom, "expert", NOW)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_template_must_be_active_and_non_empty(conn):
    empty = _template(conn, [], name="Leer")
    with pytest.raises(ValueError, match="template_empty"):
        repo.create_jobs(conn, "delivery", empty, "A-1", "", 1, [], "expert", NOW)
    inactive = _template(conn, name="Alt")
    repo.replace_template(conn, inactive, "Alt", None, False, None, [{"label": "x"}], NOW)
    with pytest.raises(ValueError, match="template_inactive"):
        repo.create_jobs(conn, "delivery", inactive, "A-1", "", 1, [], "expert", NOW)
    with pytest.raises(KeyError):
        repo.create_jobs(conn, "delivery", 4242, "A-1", "", 1, [], "expert", NOW)


@pytest.mark.parametrize("count", [0, 21, -1, True])
def test_count_bounds(conn, count):
    with pytest.raises(ValueError, match="bad_count"):
        repo.create_jobs(conn, "delivery", _template(conn), "A-1", "", count, [], "expert", NOW)


def test_rma_creates_intake_job_with_intake_lines(conn):
    job_id = _job(conn, kind="rma", order_ref="RMA-7")
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["stage"], job["lines_total"]) == ("intake", "intake", 2)
    assert len(_line_ids(conn, job_id, "intake")) == 2 and _line_ids(conn, job_id, "pack") == []


def test_rma_count_must_be_one_and_order_ref_unique(conn):
    tid = _template(conn)
    with pytest.raises(ValueError, match="bad_count"):
        repo.create_jobs(conn, "rma", tid, "RMA-1", "", 2, [], "expert", NOW)
    _job(conn, tid, kind="rma", order_ref="RMA-1")
    with pytest.raises(ValueError, match="rma_duplicate"):
        repo.create_jobs(conn, "rma", tid, "RMA-1", "", 1, [], "expert", NOW)
    # delivery jobs may share an order_ref
    repo.create_jobs(conn, "delivery", tid, "RMA-1", "", 1, [], "expert", NOW)
    repo.create_jobs(conn, "delivery", tid, "RMA-1", "", 1, [], "expert", NOW)


def test_ad_hoc_job_is_claimed_and_packing(conn):
    wid = _worker(conn)
    job_id = _job(conn, ad_hoc=True, claimed_by=wid, created_by=f"worker:{wid}")
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["claimed_by"], job["claimed_by_name"], job["ad_hoc"]) == ("packing", wid, "Anna", 1)
    assert job["claimed_at"] == NOW and job["created_by"] == f"worker:{wid}"
    # ad_hoc without a worker stays open
    assert repo.get_job(conn, _job(conn, ad_hoc=True))["status"] == "open"


# --- claim ----------------------------------------------------------------------------------


def test_claim_moves_open_to_packing_and_overwrites_claim(conn):
    anna, bert = _worker(conn, "Anna"), _worker(conn, "Bert")
    job_id = _job(conn)
    assert repo.claim_job(conn, job_id, anna, NOW) is None
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["claimed_by"], job["stage_changed_at"]) == ("packing", anna, NOW)
    assert repo.claim_job(conn, job_id, bert, LATER) == anna
    job = repo.get_job(conn, job_id)
    assert (job["claimed_by"], job["claimed_at"], job["stage_changed_at"]) == (bert, LATER, NOW)


@pytest.mark.parametrize("status", ["packed", "checked", "shipped", "cancelled"])
def test_claim_refused_in_other_statuses(conn, status):
    wid = _worker(conn)
    job_id = _job(conn)
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    with pytest.raises(ValueError, match="wrong_status"):
        repo.claim_job(conn, job_id, wid, NOW)
    with pytest.raises(KeyError):
        repo.claim_job(conn, 9999, wid, NOW)


@pytest.mark.parametrize("status", ["intake", "repair", "outgoing"])
def test_claim_rma_keeps_the_status(conn, status):
    anna, bert = _worker(conn, "Anna"), _worker(conn, "Bert")
    job_id = _job(conn, kind="rma", order_ref="R1")
    conn.execute("UPDATE jobs SET status=?, stage_changed_at=? WHERE id=?", (status, NOW, job_id))
    assert repo.claim_job(conn, job_id, anna, NOW) is None
    assert repo.claim_job(conn, job_id, bert, LATER) == anna
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["claimed_by"], job["claimed_at"], job["stage_changed_at"]) == (status, bert, LATER, NOW)


@pytest.mark.parametrize("status", ["packed", "checked", "shipped", "cancelled", "open", "packing"])
def test_claim_refused_for_rma_in_other_statuses(conn, status):
    job_id = _job(conn, kind="rma", order_ref="R1")
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    with pytest.raises(ValueError, match="wrong_status"):
        repo.claim_job(conn, job_id, _worker(conn), NOW)


def test_delete_worker_line_rules(conn):
    wid = _worker(conn)
    job_id = _packing_job(conn, wid)
    template_line = _line_ids(conn, job_id)[0]
    extra = repo.add_worker_line(conn, job_id, "pack", None, "Schraube", 1, "grund", NOW)
    for bad in (template_line,):  # not worker-added
        with pytest.raises(ValueError, match="wrong_status"):
            repo.delete_worker_line(conn, bad)
    with pytest.raises(KeyError):
        repo.delete_worker_line(conn, 9999)
    conn.execute("UPDATE jobs SET status='packed' WHERE id=?", (job_id,))
    with pytest.raises(ValueError, match="wrong_status"):
        repo.delete_worker_line(conn, extra)
    conn.execute("UPDATE jobs SET status='packing' WHERE id=?", (job_id,))
    assert repo.delete_worker_line(conn, extra)["label"] == "Schraube"
    assert repo.get_line(conn, extra) is None
    confirmed = repo.add_worker_line(conn, job_id, "pack", None, "Mutter", 1, "grund", NOW)
    repo.add_confirmation(conn, confirmed, wid, 1, "ok", 1, [], Q, "", "p.jpg", NOW)
    with pytest.raises(ValueError, match="wrong_status"):
        repo.delete_worker_line(conn, confirmed)


# --- worker extra lines -----------------------------------------------------------------------


def test_add_worker_line(conn):
    wid = _worker(conn)
    job_id = _packing_job(conn, wid)
    line_id = repo.add_worker_line(conn, job_id, "pack", None, " Schraube ", 4, " lag daneben ", NOW)
    row, latest = repo.get_line_with_latest(conn, line_id)
    assert latest is None
    assert (row["source"], row["position"], row["label"], row["qty"], row["reason"], row["photo_required"]) == (
        "worker", 3, "Schraube", 4, "lag daneben", 1,
    )
    assert json.loads(row["quality_points"]) == ["sauber", "unbeschädigt", "Funktion geprüft"]
    assert repo.get_job(conn, job_id)["lines_total"] == 3


def test_add_worker_line_rules(conn):
    wid = _worker(conn)
    open_job = _job(conn)
    with pytest.raises(ValueError, match="wrong_status"):  # not claimed yet
        repo.add_worker_line(conn, open_job, "pack", None, "x", 1, "grund", NOW)
    job_id = _packing_job(conn, wid)
    with pytest.raises(ValueError, match="reason_required"):
        repo.add_worker_line(conn, job_id, "pack", None, "x", 1, "ab", NOW)
    with pytest.raises(ValueError, match="bad_qty"):
        repo.add_worker_line(conn, job_id, "pack", None, "x", 0, "grund", NOW)
    with pytest.raises(ValueError, match="label_required"):
        repo.add_worker_line(conn, job_id, "pack", None, " ", 1, "grund", NOW)
    with pytest.raises(ValueError, match="unknown_item:Z9"):
        repo.add_worker_line(conn, job_id, "pack", "Z9", "x", 1, "grund", NOW)
    with pytest.raises(ValueError, match="wrong_status"):  # not the job's current stage
        repo.add_worker_line(conn, job_id, "outgoing", None, "x", 1, "grund", NOW)
    with pytest.raises(KeyError):
        repo.add_worker_line(conn, 9999, "pack", None, "x", 1, "grund", NOW)
    conn.execute("UPDATE jobs SET status='packed' WHERE id=?", (job_id,))
    with pytest.raises(ValueError, match="wrong_status"):
        repo.add_worker_line(conn, job_id, "pack", None, "x", 1, "grund", NOW)


# --- validate_confirmation --------------------------------------------------------------------


def _line(**kw):
    line = {
        "withheld": 0, "qty": 2, "photo_required": 1, "serial_required": 0, "source": "template",
        "quality_points": '["sauber","heil"]',
    }
    line.update(kw)
    return line


ALL_POINTS = {"sauber": "ok", "heil": "ok"}


def _check(line=None, result="ok", qty=2, serials=(), quality=None, reason="", has_photo=True):
    quality = ALL_POINTS if quality is None else quality  # default: every point answered
    return repo.validate_confirmation(line or _line(), result, qty, list(serials), quality, reason, has_photo)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, None),
        ({"quality": {"sauber": "ok", "heil": "ok"}}, None),
        ({"quality": {"sauber": "ok"}}, "error.quality_incomplete"),
        ({"quality": {}}, "error.quality_incomplete"),
        ({"quality": {"sauber": "ok", "heil": "not_ok"}, "result": "not_ok", "reason": "Kratzer"}, None),
        ({"quality": {"sauber": "ok"}, "result": "not_ok", "reason": "Kratzer"}, "error.quality_incomplete"),
        ({"qty": 1, "result": "missing", "reason": "Rest fehlt", "quality": {}}, "error.quality_incomplete"),
        ({"qty": 1, "result": "missing", "reason": "Rest fehlt", "quality": {"sauber": "ok", "heil": "ok"}}, None),
        ({"qty": 0, "result": "missing", "reason": "fehlt", "quality": {}}, None),  # nothing to inspect
        ({"line": _line(quality_points="[]"), "quality": {}}, None),  # line without quality points
        ({"line": _line(photo_required=0), "has_photo": False}, None),
        ({"has_photo": False}, "error.photo_required"),
        ({"line": _line(photo_required=0, source="worker"), "has_photo": False}, "error.photo_required"),
        ({"line": _line(withheld=1)}, "error.line_withheld"),
        ({"result": "bogus"}, "error.invalid_request"),
        ({"qty": 3}, "error.bad_qty"),
        ({"qty": -1, "result": "missing", "reason": "fehlt"}, "error.bad_qty"),
        ({"qty": True}, "error.bad_qty"),
        ({"qty": 1}, "error.bad_qty"),  # short but not marked missing
        ({"qty": 1, "result": "not_ok", "reason": "kaputt"}, "error.bad_qty"),
        ({"qty": 2, "result": "missing", "reason": "fehlt"}, "error.bad_qty"),  # missing needs a shortfall
        ({"qty": 1, "result": "missing", "reason": ""}, "error.reason_required"),
        ({"qty": 1, "result": "missing", "reason": " ab "}, "error.reason_required"),
        ({"qty": 1, "result": "missing", "reason": "Rest fehlt"}, None),
        ({"qty": 1, "result": "missing", "reason": "Rest fehlt", "has_photo": False}, "error.photo_required"),
        ({"qty": 0, "result": "missing", "reason": "fehlt", "has_photo": False}, None),  # nothing to photograph
        ({"qty": 0, "result": "missing", "reason": "fehlt"}, None),
        ({"result": "not_ok", "reason": ""}, "error.reason_required"),
        ({"result": "not_ok", "reason": "Kratzer", "has_photo": False}, "error.photo_required"),
        ({"line": _line(photo_required=0), "result": "not_ok", "reason": "Kratzer", "has_photo": False},
         "error.photo_required"),
        ({"result": "not_ok", "reason": "Kratzer"}, None),
        ({"reason": "x" * 201}, "error.invalid_request"),
        ({"line": _line(serial_required=1), "serials": []}, "error.serial_count"),
        ({"line": _line(serial_required=1), "serials": ["A"]}, "error.serial_count"),
        ({"line": _line(serial_required=1), "serials": ["A", "B", "C"]}, "error.serial_count"),
        ({"line": _line(serial_required=1), "serials": ["A", " "]}, "error.serial_count"),
        ({"line": _line(serial_required=1), "serials": ["A", "B" * 65]}, "error.serial_count"),
        ({"line": _line(serial_required=1), "serials": ["A", "B" * 64]}, None),
        ({"line": _line(serial_required=1), "serials": ["A", 5]}, "error.invalid_request"),
        ({"line": _line(serial_required=1), "qty": 1, "result": "missing", "reason": "fehlt", "serials": ["A"]}, None),
        ({"line": _line(serial_required=1), "qty": 0, "result": "missing", "reason": "fehlt"}, None),
        ({"serials": ["A"]}, None),  # optional serials may be given
        ({"serials": ["A", "B", "C"]}, "error.serial_count"),
        ({"quality": {"sauber": "ok", "heil": "not_ok"}}, "error.quality_not_ok"),
        ({"quality": {"sauber": "ok", "heil": "not_ok"}, "result": "not_ok", "reason": "Kratzer"}, None),
        ({"quality": {"fremd": "ok"}}, "error.invalid_request"),
        ({"quality": {"sauber": "maybe"}}, "error.invalid_request"),
        ({"line": _line(quality_points="[]"), "quality": {"sauber": "ok"}}, "error.invalid_request"),
    ],
)
def test_validate_confirmation(kwargs, expected):
    assert _check(**kwargs) == expected


# --- confirmations ----------------------------------------------------------------------------


def test_add_confirmation_stores_and_supersedes(conn):
    wid = _worker(conn)
    job_id = _packing_job(conn, wid)
    first_line = _line_ids(conn, job_id)[0]
    first = repo.add_confirmation(
        conn, first_line, wid, 1, "missing", 0, [], {}, "nicht da", "", NOW
    )
    second = repo.add_confirmation(
        conn, first_line, wid, 1, "ok", 2, [" S1 ", "S2"], Q, "", "jobs/1/a.jpg", LATER
    )
    assert second > first
    assert conn.execute("SELECT COUNT(*) FROM confirmations WHERE job_line_id=?", (first_line,)).fetchone()[0] == 2
    _, latest = repo.get_line_with_latest(conn, first_line)
    assert latest["id"] == second and latest["result"] == "ok" and latest["worker_name"] == "Anna"
    assert latest["serials"] == ["S1", "S2"] and latest["quality"] == Q
    assert latest["photo_url"] == "/photos/jobs/1/a.jpg" and latest["attempt"] == 1
    assert repo.get_confirmation(conn, first)["photo_url"] is None
    assert repo.get_job(conn, job_id)["lines_done"] == 1


def test_add_confirmation_enforces_rules_and_status(conn):
    wid = _worker(conn)
    job_id = _job(conn)
    line_id = _line_ids(conn, job_id)[0]
    with pytest.raises(ValueError, match="wrong_status"):  # open, not claimed
        repo.add_confirmation(conn, line_id, wid, 1, "ok", 2, [], Q, "", "p.jpg", NOW)
    repo.claim_job(conn, job_id, wid, NOW)
    with pytest.raises(ValueError, match="photo_required"):
        repo.add_confirmation(conn, line_id, wid, 1, "ok", 2, [], Q, "", "", NOW)
    with pytest.raises(ValueError, match="bad_qty"):
        repo.add_confirmation(conn, line_id, wid, 1, "ok", 3, [], Q, "", "p.jpg", NOW)
    with pytest.raises(KeyError):
        repo.add_confirmation(conn, 9999, wid, 1, "ok", 1, [], Q, "", "p.jpg", NOW)
    assert conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_withheld_line_cannot_be_confirmed(conn):
    wid = _worker(conn)
    tid = _template(conn, [{"label": "A"}, {"label": "B"}])
    tl = repo.get_template(conn, tid)["lines"]
    job_id = _job(conn, tid, customisations=[{"template_line_id": tl[0]["id"], "withheld": True, "reason": "abc"}])
    repo.claim_job(conn, job_id, wid, NOW)
    with pytest.raises(ValueError, match="line_withheld"):
        repo.add_confirmation(conn, _line_ids(conn, job_id)[0], wid, 1, "ok", 1, [], Q, "", "p.jpg", NOW)


def test_current_attempt_counts_rejected_checks_only(conn):
    wid = _worker(conn)
    job_id = _job(conn)
    assert repo.current_attempt(conn, job_id) == 1
    for result, note, photo in (("ok", "", "b.jpg"), ("rejected", "Teil fehlt", "")):
        conn.execute(
            "INSERT INTO checks (job_id, worker_id, result, note, photo_path, created_at) VALUES (?,?,?,?,?,?)",
            (job_id, wid, result, note, photo, NOW),
        )
    assert repo.current_attempt(conn, job_id) == 2


def test_new_attempt_reopens_lines_but_keeps_confirmations(conn):
    wid = _worker(conn)
    job_id = _packing_job(conn, wid)
    _confirm_all(conn, job_id, wid)
    assert repo.open_line_ids(conn, job_id, "pack", 1) == []
    conn.execute(
        "INSERT INTO checks (job_id, worker_id, result, note, created_at) VALUES (?, ?, 'rejected', 'x', ?)",
        (job_id, wid, NOW),
    )
    attempt = repo.current_attempt(conn, job_id)
    assert repo.open_line_ids(conn, job_id, "pack", attempt) == _line_ids(conn, job_id)
    job = repo.get_job(conn, job_id)
    assert (job["attempt"], job["lines_done"], job["lines_total"]) == (2, 0, 2)
    assert conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 2


# --- finish / ship ----------------------------------------------------------------------------


def test_finish_with_open_lines_lists_them(conn):
    wid = _worker(conn)
    job_id = _packing_job(conn, wid)
    ids = _line_ids(conn, job_id)
    repo.add_confirmation(conn, ids[0], wid, 1, "ok", 2, [], Q, "", "p.jpg", NOW)
    with pytest.raises(repo.LinesOpen) as exc:
        repo.finish_job(conn, job_id, LATER)
    assert exc.value.line_ids == [ids[1]]
    assert repo.get_job(conn, job_id)["status"] == "packing"


@pytest.mark.parametrize(
    "template_check,default,expected",
    [(None, "1", "packed"), (None, "0", "checked"), (0, "1", "checked"), (1, "0", "packed")],
)
def test_finish_status_follows_second_check_setting(conn, template_check, default, expected):
    repo.set_setting(conn, "second_check_default", default)
    wid = _worker(conn)
    job_id = _packing_job(conn, wid, template_id=_template(conn, second_check=bool(template_check)
                                                           if template_check is not None else None))
    _confirm_all(conn, job_id, wid)
    assert repo.second_check_applies(conn, repo.get_job(conn, job_id)) == (expected == "packed")
    assert repo.finish_job(conn, job_id, LATER) == expected
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["stage_changed_at"]) == (expected, LATER)


def test_finish_only_from_packing(conn):
    wid = _worker(conn)
    job_id = _job(conn)
    with pytest.raises(ValueError, match="wrong_status"):
        repo.finish_job(conn, job_id, NOW)
    with pytest.raises(KeyError):
        repo.finish_job(conn, 9999, NOW)
    repo.claim_job(conn, job_id, wid, NOW)
    _confirm_all(conn, job_id, wid)
    repo.finish_job(conn, job_id, NOW)
    with pytest.raises(ValueError, match="wrong_status"):
        repo.finish_job(conn, job_id, NOW)


def test_finish_with_everything_withheld_is_allowed(conn):
    wid = _worker(conn)
    tid = _template(conn, [{"label": "A"}])
    tl = repo.get_template(conn, tid)["lines"][0]["id"]
    job_id = _packing_job(
        conn, wid, template_id=tid, customisations=[{"template_line_id": tl, "withheld": True, "reason": "abc"}]
    )
    assert repo.finish_job(conn, job_id, NOW) == "packed"


def test_rma_finish_intake_goes_to_repair_and_ignores_attempts(conn):
    wid = _worker(conn)
    job_id = _job(conn, kind="rma", order_ref="R1")
    with pytest.raises(repo.LinesOpen):
        repo.finish_job(conn, job_id, NOW)
    _confirm_all(conn, job_id, wid)
    assert repo.finish_job(conn, job_id, NOW) == "repair"
    # a later rejected check does not reopen intake lines
    conn.execute("INSERT INTO checks (job_id, worker_id, result, note, created_at) VALUES (?, ?, 'rejected', 'x', ?)",
                 (job_id, wid, NOW))
    assert repo.open_line_ids(conn, job_id, "intake", repo.current_attempt(conn, job_id)) == []
    assert repo.get_job(conn, job_id)["lines_done"] == 2


def test_rma_outgoing_finish_uses_second_check(conn):
    wid = _worker(conn)
    job_id = _job(conn, kind="rma", order_ref="R1")
    conn.execute("UPDATE jobs SET status='outgoing' WHERE id=?", (job_id,))
    repo.set_setting(conn, "second_check_default", "0")
    # outgoing lines are generated by a later task; with none, nothing is open
    assert repo.current_stage(repo.get_job(conn, job_id)) == "outgoing"
    assert repo.finish_job(conn, job_id, NOW) == "checked"


def test_ship_only_from_checked(conn):
    job_id = _job(conn)
    for status in ("open", "packing", "packed", "shipped", "cancelled"):
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        with pytest.raises(ValueError, match="wrong_status"):
            repo.ship_job(conn, job_id, NOW)
    conn.execute("UPDATE jobs SET status='checked' WHERE id=?", (job_id,))
    repo.ship_job(conn, job_id, LATER)
    job = repo.get_job(conn, job_id)
    assert (job["status"], job["stage_changed_at"]) == ("shipped", LATER)
    with pytest.raises(KeyError):
        repo.ship_job(conn, 9999, NOW)


# --- expert status override -------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,old,new,ok",
    [
        ("delivery", "packed", "packing", True),
        ("delivery", "checked", "packing", True),
        ("delivery", "shipped", "packing", True),
        ("delivery", "packing", "packing", False),
        ("delivery", "open", "packing", False),
        ("delivery", "cancelled", "packing", False),
        ("delivery", "cancelled", "open", True),
        ("delivery", "open", "open", False),
        ("delivery", "packing", "open", False),
        ("delivery", "open", "cancelled", True),
        ("delivery", "packing", "cancelled", True),
        ("delivery", "packed", "cancelled", True),
        ("delivery", "checked", "cancelled", True),
        ("delivery", "shipped", "cancelled", False),
        ("delivery", "cancelled", "cancelled", False),
        ("delivery", "packing", "packed", False),
        ("delivery", "packed", "checked", False),
        ("delivery", "checked", "shipped", False),
        ("delivery", "open", "intake", False),
        ("delivery", "packing", "repair", False),
        ("rma", "cancelled", "intake", True),
        ("rma", "intake", "intake", False),
        ("rma", "cancelled", "open", False),
        ("rma", "repair", "cancelled", True),
        ("rma", "outgoing", "cancelled", True),
        ("rma", "shipped", "cancelled", False),
        ("rma", "outgoing", "repair", True),
        ("rma", "repair", "repair", False),
        ("rma", "intake", "repair", False),
        ("rma", "packed", "repair", False),
        ("rma", "packed", "outgoing", True),
        ("rma", "checked", "outgoing", True),
        ("rma", "shipped", "outgoing", True),
        ("rma", "packed", "packing", False),
    ],
)
def test_set_job_status_table(conn, kind, old, new, ok):
    job_id = _job(conn, kind=kind, order_ref="R1")
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (old, job_id))
    if ok:
        assert repo.set_job_status(conn, job_id, new, "", LATER) == old
        job = repo.get_job(conn, job_id)
        assert (job["status"], job["stage_changed_at"]) == (new, LATER)
    else:
        with pytest.raises(ValueError, match="bad_transition"):
            repo.set_job_status(conn, job_id, new, "", LATER)
        assert repo.get_job(conn, job_id)["status"] == old


def test_set_job_status_appends_note_and_rejects_unknown(conn):
    job_id = _job(conn, note="erste")
    repo.set_job_status(conn, job_id, "cancelled", " Kunde storniert ", NOW)
    assert repo.get_job(conn, job_id)["note"] == "erste\nKunde storniert"
    with pytest.raises(ValueError, match="bad_transition"):
        repo.set_job_status(conn, job_id, "bogus", "", NOW)
    with pytest.raises(KeyError):
        repo.set_job_status(conn, 9999, "cancelled", "", NOW)


def test_outgoing_to_repair_refused_once_an_outgoing_line_is_confirmed(conn):
    wid = _worker(conn)
    job_id = _job(conn, kind="rma", order_ref="R1")
    conn.execute("UPDATE jobs SET status='outgoing' WHERE id=?", (job_id,))
    line_id = repo._insert_line(
        conn, job_id, "outgoing", 1, {"label": "Kabel", "qty": 1, "quality_points": [], "photo_required": False}
    )
    repo.add_confirmation(conn, line_id, wid, 1, "ok", 1, [], {}, "", "", NOW)
    with pytest.raises(ValueError, match="outgoing_confirmed"):
        repo.set_job_status(conn, job_id, "repair", "", NOW)
    assert repo.get_job(conn, job_id)["status"] == "outgoing"


# --- listing ----------------------------------------------------------------------------------


def test_list_jobs_filters_and_counts(conn):
    anna, bert = _worker(conn, "Anna"), _worker(conn, "Bert")
    tid = _template(conn)
    a = _packing_job(conn, anna, template_id=tid, order_ref="A")
    b = _packing_job(conn, bert, template_id=tid, order_ref="B")
    c = _job(conn, tid, order_ref="C")
    d = _job(conn, tid, kind="rma", order_ref="D")
    e = _job(conn, tid, order_ref="E")
    conn.execute("UPDATE jobs SET status='shipped' WHERE id=?", (e,))
    repo.add_confirmation(conn, _line_ids(conn, a)[0], anna, 1, "ok", 2, [], Q, "", "p.jpg", NOW)

    default = repo.list_jobs(conn, repo.ACTIVE_JOB_STATUSES)
    assert [r["id"] for r in default] == [a, b, c, d]
    assert [(r["lines_done"], r["lines_total"]) for r in default] == [(1, 2), (0, 2), (0, 2), (0, 2)]
    assert default[0]["claimed_by_name"] == "Anna" and default[2]["claimed_by_name"] is None
    assert [r["id"] for r in repo.list_jobs(conn, ["shipped"])] == [e]
    assert [r["id"] for r in repo.list_jobs(conn, repo.ACTIVE_JOB_STATUSES, kind="rma")] == [d]
    assert [r["id"] for r in repo.list_jobs(conn, repo.ACTIVE_JOB_STATUSES, claimed_by=bert)] == [b]
    assert repo.job_to_dict(default[0])["template_name"] == "Set A"


@pytest.mark.parametrize(
    "kind,status,stage",
    [
        ("delivery", "open", "pack"), ("delivery", "shipped", "pack"), ("rma", "intake", "intake"),
        ("rma", "repair", "intake"), ("rma", "cancelled", "intake"), ("rma", "outgoing", "outgoing"),
        ("rma", "packed", "outgoing"), ("rma", "checked", "outgoing"), ("rma", "shipped", "outgoing"),
    ],
)
def test_current_stage(kind, status, stage):
    assert repo.current_stage({"kind": kind, "status": status}) == stage
