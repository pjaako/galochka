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
    app = create_app(Settings(data_dir=data_dir, expert_password="experte", auto_sync=False))
    with TestClient(app) as c:
        yield c


def _png_of(text: str) -> bytes:
    """A QR code of `text` as PNG bytes (stands in for the phone photo of a badge)."""
    import qrcode

    buf = io.BytesIO()
    qrcode.make(text, box_size=8, border=4).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def qr_png():
    return _png_of


@pytest.fixture
def new_worker_client(client):
    """Factory: create a worker via the expert API and log in by short code.

    Returns (TestClient, worker) where the TestClient has its own cookie jar (the
    worker's session) on the same app, and worker is the creation answer with `token`
    ({id, name, active, created_at, badge:{short_code, created_at}, token}). Call it
    twice for two different logged-in workers (second check).
    """

    def _new(name: str = "Anna"):
        resp = client.post("/api/expert/workers", headers=EXPERT, json={"name": name})
        assert resp.status_code == 201, resp.text
        worker = resp.json()
        worker_client = TestClient(client.app)  # no `with`: lifespan stays with `client`
        login = worker_client.post("/api/login/code", json={"code": worker["badge"]["short_code"]})
        assert login.status_code == 200, login.text
        return worker_client, worker

    return _new


@pytest.fixture
def worker_client(new_worker_client):
    """(TestClient logged in as the worker "Anna", worker dict). See new_worker_client."""
    return new_worker_client("Anna")
