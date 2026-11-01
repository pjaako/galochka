from pathlib import Path

import pytest

from galochka.importer import BOM_COLUMNS, ImportError_, parse_bom

FIXTURE = Path(__file__).parent / "fixtures" / "bom_BO00099.csv"


def _csv(header: str, *rows: str, bom: bool = True, eol: str = "\r\n") -> bytes:
    text = eol.join([header, *rows]) + eol
    return ("﻿" + text if bom else text).encode("utf-8")


def test_columns_dict_covers_every_field():
    assert set(BOM_COLUMNS) == {
        "bom_number", "bom_name", "product_item_id", "product_name",
        "item_id", "label", "version", "uom", "qty", "notes",
    }
    assert BOM_COLUMNS["item_id"] == ["Part No."] and BOM_COLUMNS["qty"] == ["Quantity"]


def test_real_sample_header_and_rows():
    bom = parse_bom(FIXTURE.read_bytes())
    assert bom.bom_number == "BO00099"
    assert bom.bom_name == "D00080 BSU A1207U PENGAUGE BOM"
    assert bom.product_item_id == "D00080"
    assert bom.product_name == "BSU A1207U PENGAUGE"
    assert len(bom.rows) == 19
    assert [r.position for r in bom.rows] == list(range(1, 20))
    first = bom.rows[0]
    assert (first.item_id, first.label, first.version, first.uom, first.qty) == (
        "A00108", "Calibration_Certificate_A5_BLANKO", "", "pcs", 1.0,
    )
    assert bom.rows[9].item_id == "A00533" and bom.rows[9].qty == 0.0
    assert bom.rows[18].item_id == "A01961" and bom.rows[18].qty == 0.0  # the sample has two qty-0 rows
    assert bom.rows[5].version == "3.0"  # row 6
    assert any(r.version == "H" for r in bom.rows)
    assert bom.rows[16].qty == 4.0 and isinstance(bom.rows[16].qty, float)
    assert "Umhängeband" in bom.rows[7].label


def test_decimal_comma_and_whitespace_tolerant_headers():
    data = _csv(
        '" part no. ","QUANTITY","bom   number","Notes"',
        '"X1","1,5","BO1","hint"',
        '"X2","2","BO1",',
        eol="\n",
        bom=False,
    )
    bom = parse_bom(data)
    assert [r.qty for r in bom.rows] == [1.5, 2.0]
    assert bom.rows[0].notes == "hint"
    assert bom.bom_number == "BO1" and bom.bom_name == ""


def test_rows_without_part_no_are_skipped_and_positions_stay_dense():
    data = _csv('"Part No.","Quantity"', '"A",1', '"",5', '"B",2')
    bom = parse_bom(data)
    assert [(r.item_id, r.position) for r in bom.rows] == [("A", 1), ("B", 2)]


def test_missing_required_column_raises():
    with pytest.raises(ImportError_) as exc:
        parse_bom(_csv('"Part No.","Part description"', '"A","x"'))
    assert exc.value.args[0] == "error.bom_missing_columns"
    with pytest.raises(ImportError_):
        parse_bom(_csv('"Part description","Quantity"', '"x",1'))


@pytest.mark.parametrize("data", [b"", b"\xef\xbb\xbf", b"  \r\n", _csv('"Part No.","Quantity"')])
def test_empty_file_raises(data):
    with pytest.raises(ImportError_) as exc:
        parse_bom(data)
    assert exc.value.args[0] == "error.bom_empty"


def test_bad_quantity_and_encoding_raise():
    with pytest.raises(ImportError_) as exc:
        parse_bom(_csv('"Part No.","Quantity"', '"A","viele"'))
    assert exc.value.args[0] == "error.bom_bad_qty"
    with pytest.raises(ImportError_) as exc:
        parse_bom("Part No.,Quantity\nÄ,1\n".encode("latin-1"))
    assert exc.value.args[0] == "error.bom_encoding"
