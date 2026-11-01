import io
import re

import pytest
from PIL import Image

from galochka.images import BadImage, load, normalize, save_photo


def test_normalize_applies_exif_orientation_6(make_image):
    out = normalize(make_image(size=(640, 480), orientation=6))
    assert Image.open(io.BytesIO(out)).size == (480, 640)


def test_normalize_strips_exif(make_image):
    image = Image.open(io.BytesIO(normalize(make_image(size=(640, 480), orientation=6))))
    assert not image.getexif()


def test_normalize_downsizes_large_image_keeping_aspect(make_image):
    out = normalize(make_image(size=(3000, 2000)))
    assert Image.open(io.BytesIO(out)).size == (1024, 683)


def test_normalize_small_image_not_upsized(make_image):
    out = normalize(make_image(size=(300, 200)))
    assert Image.open(io.BytesIO(out)).size == (300, 200)


def test_normalize_custom_max_side(make_image):
    out = normalize(make_image(size=(800, 400)), max_side=200)
    assert Image.open(io.BytesIO(out)).size == (200, 100)


def test_normalize_png_with_alpha_becomes_rgb_jpeg():
    buf = io.BytesIO()
    Image.new("RGBA", (300, 200), (10, 20, 30, 128)).save(buf, format="PNG")
    image = Image.open(io.BytesIO(normalize(buf.getvalue())))
    assert image.format == "JPEG" and image.mode == "RGB"


@pytest.mark.parametrize("data", [b"not an image at all, just garbage bytes", b""])
def test_normalize_bad_bytes_raises_bad_image(data):
    with pytest.raises(BadImage):
        normalize(data)


def test_save_photo_path_shape_and_content(tmp_path, make_image):
    data = normalize(make_image())
    rel = save_photo(tmp_path, "jobs/7", data)
    assert re.fullmatch(r"jobs/7/[0-9a-f]{32}\.jpg", rel)
    assert (tmp_path / rel).read_bytes() == data
    assert save_photo(tmp_path, "jobs/7", data) != rel


def test_load_returns_rgb_image(tmp_path, make_image):
    rel = save_photo(tmp_path, "jobs/1", make_image(size=(64, 32)))
    image = load(tmp_path, rel)
    assert image.mode == "RGB" and image.size == (64, 32)
