"""Browser walkthrough of the second check and the box label (Playwright, skipped without Chromium)."""

import re
import socket
import threading
import time

import httpx
import pytest

pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from conftest import EXPERT  # noqa: E402
from galochka.api import create_app  # noqa: E402
from galochka.config import Settings  # noqa: E402
from test_browser_packen import _launch  # noqa: E402

PASSWORD = EXPERT["X-Expert-Password"]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = _launch(p)
        yield b
        b.close()


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("galochka-live-check")
    app = create_app(Settings(data_dir=data_dir, cassandra_url="", auto_sync=False, expert_password=PASSWORD))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield {"url": f"http://127.0.0.1:{port}", "app": app}
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def seeded(live_server):
    """A 1-line template, workers Anna (packs) and Bert (checks) with their login codes."""
    url = live_server["url"]
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        # no default quality points: the packing helper only sends photo and qty
        assert api.put(
            "/api/expert/settings",
            json={"second_check_default": True, "session_minutes": 30, "default_quality_points": []},
        ).status_code == 204
        template = api.post(
            "/api/expert/templates",
            json={"name": "Set B", "lines": [{"label": "Kabel", "qty": 1, "photo_required": True,
                                              "quality_points": []}]},
        )
        assert template.status_code == 201, template.text
        workers = {}
        for name in ("Anna Packer", "Bert Prüfer"):
            resp = api.post("/api/expert/workers", json={"name": name})
            assert resp.status_code == 201, resp.text
            workers[name] = resp.json()["badge"]["short_code"]
    return {"template_id": template.json()["id"], "anna": workers["Anna Packer"], "bert": workers["Bert Prüfer"]}


@pytest.fixture
def box_photo(tmp_path, make_image):
    path = tmp_path / "karton.jpg"
    path.write_bytes(make_image(size=(1600, 1200), color=(20, 120, 200)))
    return str(path)


@pytest.fixture
def pack_job(live_server, seeded, make_image):
    """Factory: an expert-created 1-line job packed and finished by Anna (status packed). Returns its id."""
    url = live_server["url"]

    def _pack(order_ref):
        with httpx.Client(base_url=url, headers=EXPERT) as expert:
            created = expert.post(
                "/api/expert/jobs",
                json={"template_id": seeded["template_id"], "order_ref": order_ref, "customer": "Prüf GmbH"},
            )
            assert created.status_code == 201, created.text
            job_id = created.json()[0]["id"]
        with httpx.Client(base_url=url) as anna:
            assert anna.post("/api/login/code", json={"code": seeded["anna"]}).status_code == 200
            assert anna.post(f"/api/jobs/{job_id}/claim").status_code == 200
            line = anna.get(f"/api/jobs/{job_id}").json()["lines"][0]
            confirm = anna.post(
                f"/api/lines/{line['id']}/confirm",
                data={"result": "ok", "qty": "1", "quality": "{}"},
                files={"image": ("k.jpg", make_image(), "image/jpeg")},
            )
            assert confirm.status_code == 201, confirm.text
            assert anna.post(f"/api/jobs/{job_id}/finish").json()["status"] == "packed"
        return job_id

    return _pack


def _status(url, job_id):
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        return api.get(f"/api/jobs/{job_id}").json()


def _mobile_page(browser):
    context = browser.new_context(viewport={"width": 412, "height": 915}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.set_default_timeout(8000)
    return context, page


def _login(page, url, code):
    page.goto(f"{url}/anmelden")
    page.fill("#code-input", code)
    page.click("#code-btn")
    page.wait_for_url(f"{url}/auftraege")


def test_second_worker_releases_the_box(browser, live_server, seeded, pack_job, box_photo, tmp_path):
    url = live_server["url"]
    job_id = pack_job("P-100")
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["bert"])
        # Aufträge offers "Prüfen" for the packed job
        page.locator(f'.card[data-job-id="{job_id}"]').get_by_role("link", name="Prüfen").click()
        page.wait_for_url(f"{url}/pruefen/{job_id}")

        expect(page.locator("#chk-order")).to_have_text("P-100 – Prüf GmbH")
        expect(page.locator("#chk-packers")).to_have_text("gepackt von Anna Packer")
        row = page.locator(".chk-row")
        expect(row).to_have_count(1)
        expect(row).to_contain_text("Kabel")
        expect(row).to_contain_text("von Anna Packer")
        expect(row.locator(".state-badge--ok")).to_have_text("OK")
        expect(row.locator(".chk-thumb img")).to_have_count(1)
        assert row.locator(".chk-thumb").get_attribute("target") == "_blank"
        page.screenshot(path=str(tmp_path / "pruefen.png"))
        assert (tmp_path / "pruefen.png").stat().st_size > 1000

        # no box photo: stopped in the browser, nothing is sent
        page.get_by_role("button", name="Freigeben").click()
        expect(page.locator("#toast")).to_have_text("Für die Freigabe ist ein Foto vom Karton nötig.")
        assert _status(url, job_id)["status"] == "packed"

        page.set_input_files("input[type=file]", box_photo)
        expect(page.locator(".pack-photo")).to_be_visible()
        page.get_by_role("button", name="Freigeben").click()
        released = page.locator("#chk-released")
        expect(released).to_be_visible()
        expect(released).to_contain_text("Freigegeben")
        label_link = released.get_by_role("link", name="Etikett drucken")
        assert label_link.get_attribute("href") == f"/druck/etikett/{job_id}"
        assert label_link.get_attribute("target") == "_blank"
        expect(released.get_by_role("button", name="Versendet")).to_be_visible()
        expect(released.get_by_role("link", name="Zur Übersicht")).to_be_visible()

        job = _status(url, job_id)
        assert job["status"] == "checked"
        assert job["checks"][0]["worker_name"] == "Bert Prüfer" and job["checks"][0]["photo_url"].startswith("/photos/")

        released.get_by_role("button", name="Versendet").click()
        expect(page.locator("#toast")).to_have_text("Als versendet markiert.")
        assert _status(url, job_id)["status"] == "shipped"
    finally:
        context.close()


def test_second_worker_rejects_with_a_reason(browser, live_server, seeded, pack_job):
    url = live_server["url"]
    job_id = pack_job("P-200")
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["bert"])
        page.goto(f"{url}/pruefen/{job_id}")
        page.get_by_role("button", name="Zurück an Packer").click()
        page.get_by_role("button", name="Zurück schicken").click()
        expect(page.locator("#toast")).to_have_text("Bitte einen Grund angeben (mindestens 3 Zeichen).")
        assert _status(url, job_id)["status"] == "packed"
        page.fill("#chk-reject-note", "Kabel ist verkratzt")
        page.get_by_role("button", name="Zurück schicken").click()
        page.wait_for_url(f"{url}/auftraege")
        job = _status(url, job_id)
        assert job["status"] == "packing" and job["attempt"] == 2
        assert job["checks"][0]["result"] == "rejected" and job["checks"][0]["note"] == "Kabel ist verkratzt"
    finally:
        context.close()


def test_packer_is_locked_out_of_the_check(browser, live_server, seeded, pack_job):
    url = live_server["url"]
    job_id = pack_job("P-300")
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["anna"])
        page.goto(f"{url}/pruefen/{job_id}")
        expect(page.locator("#chk-locked")).to_contain_text("selbst gepackt")
        expect(page.locator(".chk-row")).to_have_count(1)
        expect(page.get_by_role("button", name="Freigeben")).to_have_count(0)
    finally:
        context.close()


def test_pruefen_shows_status_for_a_job_that_is_not_packed(browser, live_server, seeded):
    url = live_server["url"]
    with httpx.Client(base_url=url, headers=EXPERT) as expert:
        job_id = expert.post(
            "/api/expert/jobs", json={"template_id": seeded["template_id"], "order_ref": "P-400"}
        ).json()[0]["id"]
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["bert"])
        page.goto(f"{url}/pruefen/{job_id}")
        expect(page.locator("#chk-body")).to_contain_text("Status „Offen“")
        expect(page.get_by_role("link", name="Zur Übersicht")).to_be_visible()
        expect(page.get_by_role("button", name="Freigeben")).to_have_count(0)
    finally:
        context.close()


def test_box_label_prints_only_the_label(browser, live_server, seeded, pack_job, make_image, tmp_path):
    url = live_server["url"]
    job_id = pack_job("P-500")
    with httpx.Client(base_url=url) as bert:
        assert bert.post("/api/login/code", json={"code": seeded["bert"]}).status_code == 200
        checked = bert.post(
            f"/api/jobs/{job_id}/check", data={"result": "ok"}, files={"image": ("b.jpg", make_image(), "image/jpeg")}
        )
        assert checked.status_code == 201, checked.text

    context = browser.new_context(viewport={"width": 1024, "height": 800})  # the office PC: no worker session
    page = context.new_page()
    page.set_default_timeout(8000)
    try:
        page.goto(f"{url}/druck/etikett/{job_id}")
        # no session and no stored password: the expert password is asked for
        expect(page.locator("#login-form")).to_be_visible()
        expect(page.locator("#label-view")).to_be_hidden()
        page.fill("#password-input", PASSWORD)
        page.click("#login-form button[type=submit]")

        expect(page.locator("#label-order")).to_have_text("P-500")
        text = page.locator("#label-text")
        expect(text).to_contain_text("Prüf GmbH")
        expect(text).to_contain_text("Set B")
        expect(text).to_contain_text("gepackt: Anna Packer")
        expect(text).to_contain_text("geprüft: Bert Prüfer ")
        assert re.search(r"geprüft: Bert Prüfer \d{2}\.\d{2}\.\d{4}", text.inner_text())
        expect(text).not_to_contain_text("Karton")  # single carton: no n/N
        page.wait_for_function("document.getElementById('label-qr').complete")
        assert page.evaluate("document.getElementById('label-qr').naturalWidth") > 0
        assert page.evaluate("document.getElementById('label-qr').getAttribute('src')") == f"/api/jobs/{job_id}/qr.svg"
        page.screenshot(path=str(tmp_path / "etikett-screen.png"))

        page.emulate_media(media="print")
        expect(page.locator("#label-box")).to_be_visible()
        expect(page.locator("#label-qr")).to_be_visible()
        expect(page.locator(".nav")).to_be_hidden()
        expect(page.locator("#label-print-btn")).to_be_hidden()
        box = page.locator("#label-box").bounding_box()
        assert 20 < box["height"] < 140  # ~32 mm = 121 px; a label, not a page
        page.screenshot(path=str(tmp_path / "etikett-print.png"))
        assert (tmp_path / "etikett-print.png").stat().st_size > 1000

        # @page 89 x 36 mm applies: the printed PDF is one page of that size
        pdf = page.pdf(prefer_css_page_size=True)
        pages = re.findall(rb"/Type\s*/Page[^s]", pdf)
        assert len(pages) == 1
        mediabox = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        assert mediabox is not None
        width_mm, height_mm = (float(v) / 72 * 25.4 for v in mediabox.groups())
        assert abs(width_mm - 89) < 1 and abs(height_mm - 36) < 1
    finally:
        context.close()


def test_label_of_an_unreleased_box_warns_and_unknown_job_says_so(browser, live_server, seeded, pack_job):
    url = live_server["url"]
    job_id = pack_job("P-600")
    context = browser.new_context(viewport={"width": 1024, "height": 800})
    page = context.new_page()
    page.set_default_timeout(8000)
    try:
        page.goto(f"{url}/druck/etikett/{job_id}")
        page.fill("#password-input", PASSWORD)
        page.click("#login-form button[type=submit]")  # sessionStorage keeps the password for the next load
        expect(page.locator("#label-warning")).to_contain_text("noch nicht freigegeben")
        expect(page.locator("#label-text")).not_to_contain_text("geprüft")
        page.goto(f"{url}/druck/etikett/99999")
        expect(page.locator("#error-note")).to_have_text("Auftrag nicht gefunden.")
    finally:
        context.close()
