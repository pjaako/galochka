import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from galochka.api import create_app
from galochka.config import Settings
from galochka.db import connect, migrate

EXPERT = {"X-Expert-Password": "experte"}


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def conn(data_dir):
    connection = connect(data_dir / "galochka.db")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def make_image():
    def _make_image(color=(200, 30, 30), size=(640, 480), fmt="JPEG", orientation: int | None = None):
        image = Image.new("RGB", size, color)
        buf = io.BytesIO()
        if orientation is not None:
            exif = Image.Exif()
            exif[0x0112] = orientation
            image.save(buf, format=fmt, exif=exif)
        else:
            image.save(buf, format=fmt)
        return buf.getvalue()

    return _make_image


@pytest.fixture
def client(data_dir):
    app = create_app(Settings(data_dir=data_dir, expert_password="experte"))
    with TestClient(app) as c:
        yield c
