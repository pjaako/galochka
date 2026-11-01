"""Browser walkthrough of the expert reports: search, history panel, open RMAs and the CSV download (Playwright)."""

import json

import httpx
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from conftest import EXPERT  # noqa: E402
from test_browser_packen import PASSWORD, browser, live_server  # noqa: E402,F401  (module fixtures)


@pytest.fixture(scope="module")
def reported(live_server):
    """A packed delivery (serial SN-4711, one superseded confirmation) and an open RMA."""
    url = live_server["url"]
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (200, 150), (10, 120, 200)).save(buf, format="JPEG")
    image = buf.getvalue()
    quality = json.dumps({"sauber": "ok", "unbeschädigt": "ok", "funktion": "ok"})  # the default points
    with httpx.Client(base_url=url, headers=EXPERT) as api, httpx.Client(base_url=url) as worker:
        template = api.post("/api/expert/templates", json={"name": "Messgerät", "lines": [
            {"label": "Gerät", "qty": 1, "serial_required": True, "quality_points": []}]}).json()
        w = api.post("/api/expert/workers", json={"name": "Anna Beispiel"}).json()
        assert worker.post("/api/login/code", json={"code": w["badge"]["short_code"]}).status_code == 200
        job = api.post("/api/expert/jobs", json={"template_id": template["id"], "order_ref": "R-900",
                                                  "customer": "Müller GmbH"}).json()[0]
        assert worker.post(f"/api/jobs/{job['id']}/claim").status_code == 200
        line = worker.get(f"/api/jobs/{job['id']}").json()["lines"][0]
        for serial in ("SN-0000", "SN-4711"):  # the first confirmation is superseded
            resp = worker.post(f"/api/lines/{line['id']}/confirm",
                               data={"result": "ok", "qty": "1", "serials": json.dumps([serial]), "quality": quality},
                               files={"image": ("p.jpg", image, "image/jpeg")})
            assert resp.status_code == 201, resp.text
        assert worker.post(f"/api/jobs/{job['id']}/finish").status_code == 200
        rma = worker.post("/api/rma", json={"template_id": template["id"], "rma_no": "RMA-900", "customer": "Kunde"})
        assert rma.status_code == 201, rma.text
        rma_line = worker.get(f"/api/jobs/{rma.json()['id']}").json()["lines"][0]
        resp = worker.post(f"/api/lines/{rma_line['id']}/confirm",
                           data={"result": "ok", "qty": "1", "serials": json.dumps(["SN-R1"]), "quality": quality},
                           files={"image": ("p.jpg", image, "image/jpeg")})
        assert resp.status_code == 201, resp.text
        assert worker.post(f"/api/jobs/{rma.json()['id']}/finish").status_code == 200
    return {"job_id": job["id"], "rma_id": rma.json()["id"]}


def _login(browser, live_server):
    context = browser.new_context(viewport={"width": 1200, "height": 900}, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(8000)
    page.goto(f"{live_server['url']}/experte")
    page.fill("#password-input", PASSWORD)
    page.click("#login-form button[type=submit]")
    return context, page


def test_expert_searches_reads_the_history_and_downloads_the_csv(browser, live_server, reported):
    context, page = _login(browser, live_server)
    try:
        page.click("#tab-btn-berichte")
        # everything is listed first (no status ticked = all statuses), newest first
        expect(page.locator("#rep-rows tr")).to_have_count(2)
        page.fill("#rep-serial", "sn-4711")
        page.click("#rep-search-btn")
        rows = page.locator("#rep-rows tr")
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text("R-900")
        expect(rows.first).to_contain_text("Müller GmbH")
        expect(rows.first).to_contain_text("Gepackt")

        rows.first.click()
        detail = page.locator("#rep-detail")
        expect(detail).to_be_visible()
        expect(detail).to_contain_text("Auftrag %d: R-900" % reported["job_id"])
        expect(detail.locator(".rep-line .rep-event", has_text="Anna Beispiel")).to_have_count(2)
        expect(detail.locator(".rep-event--old")).to_have_count(1)  # the superseded one
        expect(detail.locator(".rep-event--old")).to_contain_text("SN-0000")
        photo = detail.locator("a.rep-photo").first
        assert photo.get_attribute("target") == "_blank" and photo.get_attribute("href").startswith("/photos/jobs/")
        detail.locator("details.rep-audit summary").click()
        expect(detail.locator(".rep-audit")).to_contain_text("Auftrag angelegt")
        detail.get_by_role("button", name="Schließen").click()
        expect(detail).to_be_hidden()

        page.click("#rep-reset-btn")
        page.select_option("#rep-kind", "rma")
        page.click("#rep-search-btn")
        expect(page.locator("#rep-rows tr")).to_have_count(1)
        expect(page.locator("#rep-rows tr")).to_contain_text("RMA-900")

        # open RMAs: delta 1 (arrived, nothing returned yet) is highlighted
        card = page.locator("#rma-open-list .rep-rma", has_text="RMA-900")
        expect(card).to_have_count(1)
        expect(card).to_contain_text("Heute angelegt")
        expect(card.locator(".rep-rma__line--open")).to_contain_text("Differenz 1")

        page.click("#tab-btn-export")
        assert page.input_value("#export-from") < page.input_value("#export-to")
        with page.expect_download() as info:
            page.click("#export-btn")
        download = info.value
        assert download.suggested_filename.startswith("galochka-export-") and download.suggested_filename.endswith(".csv")
        raw = open(download.path(), "rb").read()
        assert raw.startswith(b"\xef\xbb\xbfAuftrag-ID;Art;Status;")
        text = raw.decode("utf-8")
        assert "R-900" in text and "SN-4711" in text and "Müller GmbH" in text
    finally:
        context.close()
