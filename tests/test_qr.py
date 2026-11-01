import io
import re

import qrcode
from PIL import Image

from galochka import qr

TOKEN = "AbC-_123" * 4  # 32 url-safe characters


def _png(text: str, **kwargs) -> bytes:
    buf = io.BytesIO()
    qrcode.make(text, box_size=8, border=4, **kwargs).save(buf, format="PNG")
    return buf.getvalue()


def test_decode_qr_round_trip():
    assert qr.decode_qr(_png(f"galochka:badge:{TOKEN}")) == f"galochka:badge:{TOKEN}"


def test_decode_qr_applies_exif_orientation():
    image = Image.open(io.BytesIO(_png("hello"))).convert("RGB").rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 8  # stored rotated; viewers rotate it back
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    assert qr.decode_qr(buf.getvalue()) == "hello"


def test_decode_qr_unreadable_inputs_return_none():
    assert qr.decode_qr(b"not an image") is None
    blank = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(blank, format="PNG")
    assert qr.decode_qr(blank.getvalue()) is None


def test_decode_qr_finds_small_code_in_large_photo_with_full_res_retry():
    # A small code on a big canvas stays readable (thumbnail first, full resolution as retry).
    code = Image.open(io.BytesIO(_png("galochka:badge:" + TOKEN))).convert("RGB")
    canvas = Image.new("RGB", (3600, 2700), "white")
    canvas.paste(code.resize((300, 300)), (1500, 1200))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=95)
    assert qr.decode_qr(buf.getvalue()) == "galochka:badge:" + TOKEN


def test_badge_regex():
    assert qr.BADGE_RE.fullmatch(f"galochka:badge:{TOKEN}").group(1) == TOKEN
    for bad in (
        "galochka:badge:short",
        f"galochka:badge:{TOKEN}x",
        f"galochka:job:{TOKEN}",
        f"x galochka:badge:{TOKEN}",
        f"galochka:badge:{TOKEN[:-1]}!",
        f"galochka:badge:{TOKEN}\n",
    ):
        assert qr.BADGE_RE.fullmatch(bad) is None, bad


def test_make_svg_is_a_scalable_svg_document():
    svg = qr.make_svg("galochka:badge:" + TOKEN)
    assert svg.startswith("<?xml")
    assert "<svg" in svg and "viewBox=" in svg
    assert re.search(r"<path [^>]*d=\"M", svg)
    # box_size and border change the size
    assert qr.make_svg("x", box_size=10, border=4) != qr.make_svg("x", box_size=6, border=2)


def test_make_svg_and_png_encode_the_same_matrix():
    # SVG cannot be rasterised here, so compare the module count with a PNG of the same
    # text: both come from one QR matrix, and the PNG decodes back to the text.
    text = "galochka:badge:" + TOKEN
    svg = qr.make_svg(text, box_size=10, border=0)
    side = float(re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))
    png = Image.open(io.BytesIO(_png(text)))
    modules = (png.size[0] // 8) - 8  # box_size 8, border 4 on both sides
    assert abs(side - modules) < 1e-6
    assert qr.decode_qr(_png(text)) == text
