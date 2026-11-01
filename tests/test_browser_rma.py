"""Browser walkthrough of the RMA flow: intake -> repair notes -> outgoing -> second check -> label.

Playwright, skipped without Chromium. The line-by-line UI is the shared Lines component; here it runs in
its RMA clothes (intake) and in Packen (outgoing return).
"""

import json
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
    data_dir = tmp_path_factory.mktemp("galochka-live-rma")
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
    """Items, a 2-line RMA template (Platine x2 with quality points, Gehäuse x1) and workers Anna and Bert."""
    app, url = live_server["app"], live_server["url"]
    with app.state.lock:
        for item_id, name in (("P1", "Platine"), ("G1", "Gehäuse"), ("S1", "Schraubenset")):
            app.state.conn.execute(
                "INSERT INTO items (id, name, updated_at) VALUES (?, ?, '2026-11-01T00:00:00+00:00')", (item_id, name)
            )
        app.state.conn.commit()
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        # no default quality points: only the Platine line asks for them
        assert api.put(
            "/api/expert/settings",
            json={"second_check_default": True, "session_minutes": 30, "default_quality_points": []},
        ).status_code == 204
        template = api.post(
            "/api/expert/templates",
            json={
                "name": "RMA Set",
                "lines": [
                    {"item_id": "P1", "label": "Platine", "qty": 2, "photo_required": True,
                     "quality_points": ["sauber", "funktion"]},
                    {"item_id": "G1", "label": "Gehäuse", "qty": 1, "photo_required": True, "quality_points": []},
                ],
            },
        )
        assert template.status_code == 201, template.text
        codes = {}
        for name in ("Anna Annahme", "Bert Prüfer"):
            resp = api.post("/api/expert/workers", json={"name": name})
            assert resp.status_code == 201, resp.text
            codes[name] = resp.json()["badge"]["short_code"]
    return {"template_id": template.json()["id"], "anna": codes["Anna Annahme"], "bert": codes["Bert Prüfer"]}


@pytest.fixture
def photo_file(tmp_path, make_image):
    path = tmp_path / "teil.jpg"
    path.write_bytes(make_image(size=(1200, 900)))
    return str(path)


@pytest.fixture
def box_photo(tmp_path, make_image):
    path = tmp_path / "karton.jpg"
    path.write_bytes(make_image(size=(1600, 1200), color=(20, 120, 200)))
    return str(path)


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


def _job(url, job_id):
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        return api.get(f"/api/jobs/{job_id}").json()


def _worker_api(url, code):
    client = httpx.Client(base_url=url)
    assert client.post("/api/login/code", json={"code": code}).status_code == 200
    return client


def _confirm(worker, line, result, qty, make_image, reason=""):
    quality = {point: "ok" for point in line["quality_points"]} if qty > 0 else {}
    resp = worker.post(
        f"/api/lines/{line['id']}/confirm",
        data={"result": result, "qty": str(qty), "reason": reason, "quality": json.dumps(quality)},
        files={"image": ("t.jpg", make_image(), "image/jpeg")},
    )
    assert resp.status_code == 201, resp.text


def _api_intake(url, seeded, rma_no, make_image):
    """Anna accepts an RMA by API: Platine 1 of 2 arrived, Gehäuse arrived, intake finished (status repair)."""
    anna = _worker_api(url, seeded["anna"])
    created = anna.post("/api/rma", json={"template_id": seeded["template_id"], "rma_no": rma_no, "customer": "API GmbH"})
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    lines = anna.get(f"/api/jobs/{job_id}").json()["lines"]
    _confirm(anna, lines[0], "missing", 1, make_image, reason="Eine Platine fehlt")
    _confirm(anna, lines[1], "ok", 1, make_image)
    assert anna.post(f"/api/jobs/{job_id}/finish").json()["status"] == "repair"
    return anna, job_id, lines


def test_rma_flow_intake_repair_outgoing_check_and_label(browser, live_server, seeded, photo_file, box_photo, tmp_path):
    url = live_server["url"]
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["anna"])
        # Aufträge: "RMA annehmen" is live and opens an inline form
        page.click("#rma-btn")
        expect(page.locator("#rma-form")).to_be_visible()
        page.select_option("#rma-template", label="RMA Set")
        page.click("#rma-submit")
        expect(page.locator("#toast")).to_have_text("Bitte eine RMA-Nummer eingeben.")
        page.fill("#rma-no", "R-1")
        page.fill("#rma-customer", "Kunde GmbH")
        page.click("#rma-submit")
        page.wait_for_url(re.compile(rf"{re.escape(url)}/rma/\d+$"))
        job_id = int(page.url.rsplit("/", 1)[1])

        # intake: the Lines component with the RMA wording
        expect(page.locator("#pack-order")).to_have_text("RMA R-1")
        expect(page.locator("#pack-meta")).to_have_text("Kunde GmbH · RMA Set · Eingang")
        expect(page.locator("#pack-progress")).to_have_text("0 von 2")
        expect(page.locator(".pack-line__label")).to_have_text("Platine")
        expect(page.get_by_role("button", name="Angekommen", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Beschädigt", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Weiteres Teil gefunden", exact=True)).to_be_visible()
        assert _job(url, job_id)["claimed_by_name"] == "Anna Annahme"
        page.set_viewport_size({"width": 412, "height": 520})  # the action bar stays on the screen
        bar = page.locator(".pack-actions").bounding_box()
        assert bar["y"] + bar["height"] <= 520.5, bar
        page.set_viewport_size({"width": 412, "height": 915})
        page.screenshot(path=str(tmp_path / "rma-intake.png"))
        assert (tmp_path / "rma-intake.png").stat().st_size > 1000

        # line 1 (qty 2): only one arrived -> "Fehlt / unvollständig" with a reason, quality and photo for the one
        page.get_by_role("button", name="Fehlt / unvollständig", exact=True).click()
        page.locator('button[aria-label="Mehr"]').click()
        expect(page.locator(".stepper__value")).to_have_text("Vorhanden: 1 von 2")
        page.fill("textarea[data-reason]", "Zweite Platine lag nicht im Karton")
        for q_ok in page.locator(".q-btn--ok").all():
            q_ok.click()
        page.set_input_files("input[type=file]", photo_file)
        page.get_by_role("button", name="Fehlt melden").click()
        expect(page.locator("#pack-progress")).to_have_text("1 von 2")

        # line 2: arrived, photo needed
        expect(page.locator(".pack-line__label")).to_have_text("Gehäuse")
        page.get_by_role("button", name="Angekommen", exact=True).click()
        expect(page.locator("#toast")).to_have_text("Für diese Zeile ist ein Foto nötig.")
        page.set_input_files("input[type=file]", photo_file)
        page.get_by_role("button", name="Angekommen", exact=True).click()
        expect(page.locator(".sum-row")).to_have_count(2)
        expect(page.locator("#pack-progress")).to_have_text("2 von 2")

        # "Eingang abschließen" -> status repair, the page switches to the repair view
        page.get_by_role("button", name="Eingang abschließen", exact=True).click()
        expect(page.locator("#rma-order")).to_have_text("RMA R-1")
        expect(page.locator("#rma-meta")).to_have_text("Kunde GmbH · RMA Set · Reparatur")
        assert _job(url, job_id)["status"] == "repair"
        platine = page.locator(".rma-line", has_text="Platine")
        gehaeuse = page.locator(".rma-line", has_text="Gehäuse")
        expect(page.locator("#rma-intake .rma-line")).to_have_count(2)
        expect(platine).to_contain_text("Eingang: 1 von 2")
        expect(platine).to_contain_text("von Anna Annahme")
        expect(platine.locator(".state-badge--missing")).to_have_text("Fehlt")
        expect(gehaeuse.locator(".state-badge--ok")).to_have_text("OK")
        expect(page.locator("#rma-preview")).to_have_text("Rücksendung: Platine × 1, Gehäuse × 1")

        # Gehäuse stays with us: a reason is mandatory
        gehaeuse.locator("[data-action=kept]").click()
        page.get_by_role("button", name="Speichern").click()
        expect(page.locator("#toast")).to_have_text("Bitte einen Grund angeben (mindestens 3 Zeichen).")
        page.fill(".rma-form textarea", "Kunde lässt das Gehäuse bei uns")
        page.get_by_role("button", name="Speichern").click()
        expect(page.locator("#toast")).to_have_text("Notiz gespeichert.")
        expect(page.locator("#rma-preview")).to_have_text("Rücksendung: Platine × 1")
        expect(gehaeuse).to_contain_text("Einbehalten: 1")
        expect(gehaeuse.locator("[data-action=kept]")).to_be_disabled()

        # the Platine that did arrive is repaired (replaced part, reason optional)
        platine.locator("[data-action=replaced]").click()
        page.fill(".rma-form input[type=text]", "SN-77")
        page.fill(".rma-form textarea", "Lötstelle repariert")
        page.get_by_role("button", name="Speichern").click()
        expect(page.locator(".rma-note")).to_have_count(2)
        expect(page.locator("#rma-preview")).to_have_text("Rücksendung: Platine × 1")
        note = page.locator(".rma-note", has_text="Lötstelle repariert")
        expect(note).to_contain_text("Seriennummer: SN-77")
        expect(note.get_by_role("button", name="Löschen")).to_be_visible()
        page.screenshot(path=str(tmp_path / "rma-repair.png"), full_page=True)
        assert (tmp_path / "rma-repair.png").stat().st_size > 1000

        # Rücksendung vorbereiten -> the outgoing packing on the Packen page
        page.get_by_role("button", name="Rücksendung vorbereiten").click()
        page.wait_for_url(f"{url}/packen/{job_id}")
        expect(page.locator("#pack-order")).to_have_text("RMA R-1 – Rücksendung")
        expect(page.locator("#pack-meta")).to_have_text("Kunde GmbH · RMA Set")
        expect(page.locator("#pack-progress")).to_have_text("0 von 1")
        expect(page.locator(".pack-line__label")).to_have_text("Platine")
        expect(page.locator(".pack-line__in")).to_have_text("Eingang: 1")
        expect(page.locator(".pack-line__reason")).to_have_text("ersetzt: Lötstelle repariert")
        expect(page.locator(".pack-line__qty")).to_have_text("Menge: 1")
        expect(page.get_by_role("button", name="In Ordnung", exact=True)).to_be_visible()
        assert _job(url, job_id)["status"] == "outgoing"

        for q_ok in page.locator(".q-btn--ok").all():
            q_ok.click()
        page.set_input_files("input[type=file]", photo_file)
        page.get_by_role("button", name="In Ordnung", exact=True).click()
        expect(page.locator("#pack-progress")).to_have_text("1 von 1")
        expect(page.locator(".sum-row")).to_contain_text("Eingang: 1")
        page.get_by_role("button", name="Fertig gepackt").click()
        expect(page.locator(".pack-result")).to_contain_text(
            "Rücksendung gepackt – Prüfung durch zweite Person erforderlich"
        )
        job = _job(url, job_id)
        assert job["status"] == "packed"
        assert [line["label"] for line in job["lines"]] == ["Platine"]
        # the RMA page of a packed return: status, link to the check, read-only folds
        page.goto(f"{url}/rma/{job_id}")
        expect(page.get_by_role("link", name="Prüfen")).to_have_attribute("href", f"/pruefen/{job_id}")
        expect(page.locator("#rma-fold-intake")).to_be_visible()
        page.locator("#rma-fold-notes summary").click()
        expect(page.locator("#rma-fold-notes .rma-note")).to_have_count(2)
        expect(page.locator("#rma-fold-notes").get_by_role("button", name="Löschen")).to_have_count(0)
    finally:
        context.close()

    # Bert releases the return box (a different worker than the packer)
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["bert"])
        page.goto(f"{url}/pruefen/{job_id}")
        expect(page.locator("#chk-order")).to_have_text("RMA R-1 – Rücksendung")  # same heading as on Packen
        expect(page.locator(".chk-row")).to_have_count(1)
        expect(page.locator(".chk-row")).to_contain_text("Platine")
        page.set_input_files("input[type=file]", box_photo)
        page.get_by_role("button", name="Freigeben").click()
        expect(page.locator("#chk-released")).to_be_visible()
        assert _job(url, job_id)["status"] == "checked"
    finally:
        context.close()

    # The label from the office PC: RMA number, customer, set name, QR, the check; one 89 x 36 mm page
    context = browser.new_context(viewport={"width": 1024, "height": 800})
    page = context.new_page()
    page.set_default_timeout(8000)
    try:
        page.goto(f"{url}/druck/etikett/{job_id}")
        page.fill("#password-input", PASSWORD)
        page.click("#login-form button[type=submit]")
        expect(page.locator("#label-order")).to_have_text("RMA R-1")
        text = page.locator("#label-text")
        expect(text).to_contain_text("Kunde GmbH")
        expect(text).to_contain_text("RMA Set")
        assert re.search(r"geprüft: Bert Prüfer \d{2}\.\d{2}\.\d{4}", text.inner_text())
        expect(text).not_to_contain_text("gepackt")
        expect(text).not_to_contain_text("Karton")
        page.wait_for_function("document.getElementById('label-qr').complete")
        assert page.evaluate("document.getElementById('label-qr').naturalWidth") > 0
        assert page.evaluate("document.getElementById('label-qr').getAttribute('src')") == f"/api/jobs/{job_id}/qr.svg"
        page.emulate_media(media="print")
        pdf = page.pdf(prefer_css_page_size=True)
        assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 1
        mediabox = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        assert mediabox is not None
        width_mm, height_mm = (float(v) / 72 * 25.4 for v in mediabox.groups())
        assert abs(width_mm - 89) < 1 and abs(height_mm - 36) < 1
    finally:
        context.close()

    # Aufträge lists the finished RMA with "Versendet" and "Etikett"
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["bert"])
        card = page.locator(f'.card[data-job-id="{job_id}"]')
        expect(card).to_contain_text("RMA R-1")
        expect(card).to_contain_text("Geprüft")
        expect(card.get_by_role("button", name="Als versendet markieren")).to_be_visible()
        expect(card.get_by_role("link", name="Etikett")).to_have_attribute("href", f"/druck/etikett/{job_id}")
    finally:
        context.close()


def test_open_rma_report_shows_the_delta_until_the_return_is_confirmed(live_server, seeded, make_image):
    url = live_server["url"]
    anna, job_id, lines = _api_intake(url, seeded, "R-20", make_image)
    # Gehäuse stays with us, the Platine (1 of 2 arrived) goes back
    kept = anna.post(
        f"/api/jobs/{job_id}/repair-notes",
        json={"action": "kept", "job_line_id": lines[1]["id"], "qty": 1, "reason": "Behalten"},
    )
    assert kept.status_code == 201, kept.text

    def deltas():
        with httpx.Client(base_url=url, headers=EXPERT) as expert:
            rows = expert.get("/api/expert/rma/open").json()
        (row,) = [r for r in rows if r["job"]["id"] == job_id]
        return {line["label"]: line["delta"] for line in row["lines"]}

    assert deltas() == {"Platine": 1, "Gehäuse": 0}  # the Platine is in the house, not returned yet
    outgoing = anna.post(f"/api/jobs/{job_id}/outgoing")
    assert outgoing.status_code == 200, outgoing.text
    (out_line,) = outgoing.json()["lines"]
    assert out_line["in_qty"] == 1 and out_line["qty"] == 1
    assert deltas() == {"Platine": 1, "Gehäuse": 0}  # listed, but not confirmed yet
    _confirm(anna, out_line, "ok", 1, make_image)
    assert deltas() == {"Platine": 0, "Gehäuse": 0}


def test_repair_view_added_part_delete_and_empty_return(browser, live_server, seeded, make_image):
    url = live_server["url"]
    anna, job_id, lines = _api_intake(url, seeded, "R-30", make_image)
    # everything that arrived is kept -> nothing to send back
    for line, qty in ((lines[0], 1), (lines[1], 1)):
        resp = anna.post(
            f"/api/jobs/{job_id}/repair-notes",
            json={"action": "kept", "job_line_id": line["id"], "qty": qty, "reason": "Alles bleibt hier"},
        )
        assert resp.status_code == 201, resp.text
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["bert"])  # a colleague opens the RMA: takes it over, sees but cannot delete notes
        # opening the link of a colleague's RMA asks first; cancelling leaves it with Anna and goes back to the list
        messages = []
        page.once("dialog", lambda dialog: (messages.append(dialog.message), dialog.dismiss()))
        page.goto(f"{url}/rma/{job_id}")
        page.wait_for_url(f"{url}/auftraege")
        assert "Anna" in messages[0] and "übernehmen" in messages[0]
        assert _job(url, job_id)["claimed_by_name"] == "Anna Annahme"
        page.once("dialog", lambda dialog: dialog.accept())
        page.goto(f"{url}/rma/{job_id}")
        expect(page.locator("#rma-preview")).to_have_text("Rücksendung: nichts")
        expect(page.locator("#rma-outgoing .note")).to_contain_text("im Expertenbereich stornieren")
        assert _job(url, job_id)["claimed_by_name"] == "Bert Prüfer"
        expect(page.locator(".rma-note")).to_have_count(2)
        expect(page.get_by_role("button", name="Löschen")).to_have_count(0)

        page.get_by_role("button", name="Rücksendung vorbereiten").click()
        expect(page.locator("#toast")).to_have_text(
            "Es gibt nichts zurückzusenden. Bitte den Auftrag im Expertenbereich stornieren."
        )
        assert _job(url, job_id)["status"] == "repair"

        # a part that was not in the box: item search, reason mandatory
        page.get_by_role("button", name="Teil hinzufügen").click()
        page.fill(".rma-form .item-picker input", "Schrauben")
        page.get_by_role("button", name="S1 – Schraubenset").click()
        page.get_by_role("button", name="Speichern").click()
        expect(page.locator("#toast")).to_have_text("Bitte einen Grund angeben (mindestens 3 Zeichen).")
        page.fill(".rma-form textarea", "Liegt dem Gerät bei")
        page.get_by_role("button", name="Speichern").click()
        expect(page.locator("#rma-preview")).to_have_text("Rücksendung: Schraubenset × 1 (hinzugefügt)")
        added = page.locator(".rma-note", has_text="Schraubenset")
        expect(added.locator(".state-badge")).to_have_text("Hinzugefügt")

        # the author can delete the note again (confirm dialog)
        page.once("dialog", lambda dialog: dialog.accept())
        added.get_by_role("button", name="Löschen").click()
        expect(page.locator("#rma-preview")).to_have_text("Rücksendung: nichts")
        expect(page.locator(".rma-note")).to_have_count(2)
    finally:
        context.close()


def test_duplicate_rma_number_is_a_toast(browser, live_server, seeded, make_image):
    url = live_server["url"]
    _api_intake(url, seeded, "R-40", make_image)
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["anna"])
        page.click("#rma-btn")
        page.select_option("#rma-template", label="RMA Set")
        page.fill("#rma-no", "R-40")
        page.click("#rma-submit")
        expect(page.locator("#toast")).to_have_text("Diese RMA-Nummer gibt es schon.")
        assert page.url == f"{url}/auftraege"
        # the cards of RMAs in repair lead to the RMA page
        card = page.locator(".card", has_text="RMA R-40")
        expect(card).to_contain_text("In Reparatur")
        expect(card.get_by_role("link", name="Reparatur")).to_have_attribute("href", re.compile(r"^/rma/\d+$"))
    finally:
        context.close()


def test_intake_card_and_page_for_a_cancelled_rma(browser, live_server, seeded):
    url = live_server["url"]
    anna = _worker_api(url, seeded["anna"])
    created = anna.post("/api/rma", json={"template_id": seeded["template_id"], "rma_no": "R-50", "customer": ""})
    job_id = created.json()["id"]
    context, page = _mobile_page(browser)
    try:
        _login(page, url, seeded["anna"])
        card = page.locator(f'.card[data-job-id="{job_id}"]')
        expect(card.get_by_role("link", name="Eingang prüfen")).to_have_attribute("href", f"/rma/{job_id}")
        with httpx.Client(base_url=url, headers=EXPERT) as expert:
            assert expert.patch(f"/api/expert/jobs/{job_id}", json={"status": "cancelled"}).status_code == 200
        page.goto(f"{url}/rma/{job_id}")
        expect(page.locator("#rma-order")).to_have_text("RMA R-50")
        expect(page.locator(".rma-info")).to_have_text("Diese RMA wurde storniert.")
        expect(page.locator("#rma-fold-intake")).to_be_visible()
    finally:
        context.close()
