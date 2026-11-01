from pathlib import Path

import pytest

from conftest import EXPERT
from galochka.importer import parse_bom

FIXTURE = Path(__file__).parent / "fixtures" / "bom_BO00099.csv"
URL = "/api/expert/import/bom"
NAME = "D00080 BSU A1207U PENGAUGE BOM"
ALL_IDS = [r.item_id for r in parse_bom(FIXTURE.read_bytes()).rows]
ZERO_IDS = ["A00533", "A01961"]  # the sample has two rows with Quantity 0 (rows 10 and 19)


def _post(client, data=None, content=None, **form):
    content = FIXTURE.read_bytes() if content is None else content
    return client.post(
        URL, headers=EXPERT, files={"file": ("bom.csv", content, "text/csv")}, data={**(data or {}), **form}
    )


def _bom_csv(*rows):
    head = '"BOM number","BOM name","Product number","Product name","Part No.","Part description","Parameters","Quantity","Notes"'
    return ("﻿" + "\r\n".join([head, *rows]) + "\r\n").encode("utf-8")


def _templates(client):
    return client.get("/api/expert/templates?all=1", headers=EXPERT).json()


@pytest.fixture
def catalog(add_items):
    """All BOM parts plus the product are known, except A00314 and E00124."""
    unknown = {"A00314", "E00124"}
    add_items(*[(i, f"Katalog {i}") for i in ALL_IDS if i not in unknown], ("D00080", "Produkt Pengauge"))
    return unknown


def test_requires_expert_password(client):
    resp = client.post(URL, files={"file": ("b.csv", FIXTURE.read_bytes(), "text/csv")})
    assert resp.status_code == 401


def test_dry_run_creates_nothing_and_reports_zero_rows_and_unknown_items(client, catalog):
    resp = _post(client, dry_run="1")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["bom_number"], data["bom_name"]) == ("BO00099", NAME)
    assert (data["product_item_id"], data["product_name"], data["product_known"]) == (
        "D00080", "BSU A1207U PENGAUGE", True,
    )
    assert data["template_id"] is None and data["existing_template_id"] is None
    assert len(data["lines"]) == 19
    assert [ln["position"] for ln in data["lines"]] == list(range(1, 20))
    assert [ln["item_id"] for ln in data["lines"] if ln["skipped_zero_qty"]] == ZERO_IDS
    assert data["unknown_items"] == ["A00314", "E00124"]
    by_id = {ln["item_id"]: ln for ln in data["lines"]}
    assert by_id["A00314"]["known"] is False and by_id["A00314"]["item_name"] == ""
    assert by_id["A00108"]["known"] is True and by_id["A00108"]["item_name"] == "Katalog A00108"
    assert by_id["A00081"]["version"] == "3.0" and by_id["A00031"]["qty"] == 4
    assert _templates(client) == []


def test_dry_run_is_the_default_and_the_query_parameter_works(client, catalog):
    assert _post(client).status_code == 200
    assert _post(client, dry_run="").json()["template_id"] is None
    resp = client.post(
        URL + "?dry_run=1", headers=EXPERT, files={"file": ("b.csv", FIXTURE.read_bytes(), "text/csv")}
    )
    assert resp.json()["template_id"] is None
    assert _templates(client) == []


def test_unknown_product_is_reported(client):
    data = _post(client).json()
    assert data["product_known"] is False
    assert len(data["unknown_items"]) == 17  # nothing is in the catalog; the two qty-0 rows are not counted


def test_import_creates_ordered_lines_without_zero_quantity_rows(client, catalog):
    resp = _post(client, dry_run="0")
    assert resp.status_code == 200, resp.text
    template_id = resp.json()["template_id"]
    tpl = client.get(f"/api/expert/templates/{template_id}", headers=EXPERT).json()
    assert tpl["name"] == NAME and tpl["product_item_id"] == "D00080" and tpl["active"] is True
    assert tpl["second_check"] is None
    expected = [i for i in ALL_IDS if i not in ZERO_IDS]
    assert len(tpl["lines"]) == 17
    assert [ln["position"] for ln in tpl["lines"]] == list(range(1, 18))
    # known items keep their id, unknown ones lose it but keep the label from the file
    assert [ln["item_id"] for ln in tpl["lines"]] == [None if i in catalog else i for i in expected]
    by_label = {ln["label"]: ln for ln in tpl["lines"]}
    unknown_line = tpl["lines"][2]  # A00314
    assert unknown_line["item_id"] is None and unknown_line["label"] == "Box for A1207U [Case  MAX 002]"
    assert tpl["lines"][0]["label"] == "Calibration_Certificate_A5_BLANKO"
    assert "Inlay A1207U for MAX 002 · Version 3.0" in by_label  # version is appended to the label
    assert "A1207 Calibration sample 5/25 mm · Version H" in by_label
    assert all(ln["photo_required"] and not ln["serial_required"] and ln["quality_points"] == [] for ln in tpl["lines"])
    assert [ln["qty"] for ln in tpl["lines"]][-2:] == [4, 4]
    assert [r["action"] for r in client.app.state.conn.execute("SELECT action FROM audit WHERE entity='template'")] == [
        "bom.import"
    ]
    assert [t["id"] for t in client.get("/api/templates", headers=EXPERT).json()] == [template_id]


def test_include_zero_qty_adds_the_two_rows_with_qty_one(client, catalog):
    resp = _post(client, dry_run="0", include_zero_qty="1")
    data = resp.json()
    assert not any(ln["skipped_zero_qty"] for ln in data["lines"])
    tpl = client.get(f"/api/expert/templates/{data['template_id']}", headers=EXPERT).json()
    assert len(tpl["lines"]) == 19
    assert [ln["position"] for ln in tpl["lines"]] == list(range(1, 20))
    assert tpl["lines"][9]["item_id"] == "A00533" and tpl["lines"][9]["qty"] == 1
    assert tpl["lines"][18]["item_id"] == "A01961"
    # the dry run with the box ticked shows no skipped rows either
    assert not any(ln["skipped_zero_qty"] for ln in _post(client, include_zero_qty="1").json()["lines"])


def test_form_fields_override_name_and_product(client, catalog, add_items):
    add_items(("P2", "Anderes Produkt"))
    data = _post(client, dry_run="0", name="  Mein Set ", product_item_id="P2").json()
    tpl = client.get(f"/api/expert/templates/{data['template_id']}", headers=EXPERT).json()
    assert tpl["name"] == "Mein Set" and tpl["product_item_id"] == "P2"


def test_unknown_form_product_is_422_and_nothing_is_created(client, catalog):
    resp = _post(client, dry_run="0", product_item_id="NOPE")
    assert resp.status_code == 422 and "NOPE" in resp.json()["detail"]
    assert _templates(client) == []


def test_unknown_file_product_gives_template_without_product(client):
    data = _post(client, dry_run="0").json()
    tpl = client.get(f"/api/expert/templates/{data['template_id']}", headers=EXPERT).json()
    assert tpl["product_item_id"] is None and len(tpl["lines"]) == 17
    assert all(ln["item_id"] is None for ln in tpl["lines"])


def test_existing_template_is_detected_by_name_and_by_product(client, catalog):
    first = _post(client, dry_run="0").json()["template_id"]
    again = _post(client).json()
    assert again["existing_template_id"] == first
    # default mode is "new": a second template with the same name appears
    second = _post(client, dry_run="0").json()["template_id"]
    assert second != first and len(_templates(client)) == 2
    assert _post(client).json()["existing_template_id"] == second  # newest wins
    # renamed template, found through the product
    client.put(
        f"/api/expert/templates/{first}", headers=EXPERT,
        json={"name": "Umbenannt", "product_item_id": "D00080", "lines": []},
    )
    client.delete(f"/api/expert/templates/{second}", headers=EXPERT)
    assert _post(client).json()["existing_template_id"] == first


def test_replace_mode_replaces_the_lines_of_an_existing_template(client, catalog):
    tpl = client.post(
        "/api/expert/templates", headers=EXPERT,
        json={"name": "Eigener Name", "second_check": False, "lines": [{"label": "alt 1"}, {"label": "alt 2"}]},
    ).json()
    resp = _post(client, dry_run="0", mode=f"replace:{tpl['id']}")
    assert resp.status_code == 200 and resp.json()["template_id"] == tpl["id"]
    got = client.get(f"/api/expert/templates/{tpl['id']}", headers=EXPERT).json()
    assert got["name"] == "Eigener Name" and got["second_check"] is False  # kept
    assert got["product_item_id"] is None  # replace keeps the template header unless the form overrides it
    assert len(got["lines"]) == 17 and got["lines"][0]["label"] == "Calibration_Certificate_A5_BLANKO"
    assert not {ln["id"] for ln in got["lines"]} & {ln["id"] for ln in tpl["lines"]}
    assert len(_templates(client)) == 1
    assert client.app.state.conn.execute("SELECT COUNT(*) FROM template_lines").fetchone()[0] == 17


def test_replace_mode_errors(client, catalog):
    assert _post(client, dry_run="0", mode="replace:999").status_code == 404
    assert _post(client, dry_run="0", mode="replace:abc").status_code == 422
    assert _post(client, dry_run="0", mode="merge").status_code == 422
    assert _templates(client) == []


def test_bad_files_are_400(client):
    assert _post(client, content=b"").status_code == 400
    assert _post(client, content=_bom_csv()).status_code == 400  # header only
    no_qty = b'"Part No.","Part description"\r\n"A","x"\r\n'
    resp = _post(client, content=no_qty)
    assert resp.status_code == 400 and "CSV" in resp.json()["detail"]
    assert _post(client, content="Part No.,Quantity\nÄ,1\n".encode("latin-1")).status_code == 400


def test_fractional_quantities_round_up_and_decimal_comma(client, add_items):
    add_items("X1", "X2", "X3")
    content = _bom_csv(
        '"B1","Set","","","X1","Eins","","1,2","Hinweis"',
        '"B1","Set","","","X2","Zwei","","0,5",',
        '"B1","Set","","","X3","Drei","","1500",',
    )
    tpl_id = _post(client, content=content, dry_run="0").json()["template_id"]
    tpl = client.get(f"/api/expert/templates/{tpl_id}", headers=EXPERT).json()
    assert [ln["qty"] for ln in tpl["lines"]] == [2, 1, 999]
    assert tpl["lines"][0]["instruction"] == "Hinweis"
    assert tpl["name"] == "Set" and tpl["product_item_id"] is None


def test_name_falls_back_to_bom_number_and_long_text_is_cut(client, add_items):
    long_label = "L" * 200
    content = _bom_csv(f'"B7","","","","X1","{long_label}","3.0","1","{"n" * 600}"')
    tpl_id = _post(client, content=content, dry_run="0").json()["template_id"]
    tpl = client.get(f"/api/expert/templates/{tpl_id}", headers=EXPERT).json()
    assert tpl["name"] == "B7"
    assert len(tpl["lines"][0]["label"]) == 120 and len(tpl["lines"][0]["instruction"]) == 500
