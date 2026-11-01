"""Browser walkthrough of the worker UI: login, claim, pack two lines, finish (Playwright, skipped without Chromium)."""

import glob
import os
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

PASSWORD = EXPERT["X-Expert-Password"]


def _launch(p):
    """Chromium headless; falls back to a browser from PLAYWRIGHT_BROWSERS_PATH of another build number."""
    try:
        return p.chromium.launch()
    except Exception as first:  # noqa: BLE001 - any launch problem means: try the pre-installed build
        root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
        for pattern in ("chromium_headless_shell-*/*/chrome-headless-shell", "chromium-*/chrome-linux*/chrome"):
            for path in sorted(glob.glob(os.path.join(root, pattern)), reverse=True):
                try:
                    return p.chromium.launch(executable_path=path)
                except Exception:  # noqa: BLE001
                    continue
        pytest.skip(f"Chromium is not installed/launchable: {first}")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = _launch(p)
        yield b
        b.close()


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("galochka-live")
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
    url = f"http://127.0.0.1:{port}"
    yield {"url": url, "app": app}
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def seeded(live_server):
    """3 items, a 2-line template, a worker and one expert-created job. Returns ids and the login code."""
    app, url = live_server["app"], live_server["url"]
    with app.state.lock:
        for item_id, name in (("A1", "Kabel"), ("A2", "Netzteil"), ("A3", "Gehäuse")):
            app.state.conn.execute(
                "INSERT INTO items (id, name, updated_at) VALUES (?, ?, '2026-11-01T00:00:00+00:00')", (item_id, name)
            )
        app.state.conn.commit()
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        # no default quality points: only line 1 asks for them
        assert api.put(
            "/api/expert/settings",
            json={"second_check_default": True, "session_minutes": 30, "default_quality_points": []},
        ).status_code == 204
        template = api.post(
            "/api/expert/templates",
            json={
                "name": "Set A",
                "lines": [
                    {"item_id": "A1", "label": "Kabel", "qty": 1, "photo_required": True,
                     "quality_points": ["sauber", "funktion"], "instruction": "Kabel prüfen"},
                    {"item_id": "A2", "label": "Netzteil", "qty": 1, "photo_required": False, "serial_required": True},
                ],
            },
        )
        assert template.status_code == 201, template.text
        worker = api.post("/api/expert/workers", json={"name": "Anna Beispiel"})
        assert worker.status_code == 201, worker.text
        jobs = api.post(
            "/api/expert/jobs",
            json={"template_id": template.json()["id"], "order_ref": "A-100", "customer": "Kunde GmbH"},
        )
        assert jobs.status_code == 201, jobs.text
        return {
            "template_id": template.json()["id"],
            "job_id": jobs.json()[0]["id"],
            "code": worker.json()["badge"]["short_code"],
            "worker": worker.json(),
        }


@pytest.fixture
def photo_file(tmp_path, make_image):
    path = tmp_path / "teil.jpg"
    path.write_bytes(make_image(size=(1200, 900)))
    return str(path)


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 412, "height": 915}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.set_default_timeout(8000)
    yield page
    context.close()


def _login(page, url, code):
    page.goto(f"{url}/anmelden")
    page.fill("#code-input", code)
    page.click("#code-btn")
    page.wait_for_url(f"{url}/auftraege")


def test_worker_packs_a_job_end_to_end(page, live_server, seeded, photo_file, tmp_path):
    url = live_server["url"]
    _login(page, url, seeded["code"])
    expect(page.locator(".nav__user")).to_have_text("Anna Beispiel")

    # Aufträge: the expert-created job is listed, open and unclaimed
    card = page.locator(f'.card[data-job-id="{seeded["job_id"]}"]')
    expect(card).to_contain_text("A-100")
    expect(card).to_contain_text("Kunde GmbH")
    expect(card).to_contain_text("Offen")
    card.get_by_role("button", name="Packen").click()
    page.wait_for_url(f"{url}/packen/{seeded['job_id']}")

    # Packen: line 1 (photo required, two quality points); reference photo tile is empty without Cassandra
    expect(page.locator("#pack-progress")).to_have_text("0 von 2")
    expect(page.locator(".pack-line__label")).to_have_text("Kabel")
    expect(page.locator(".photo-tile--empty")).to_be_visible()
    expect(page.locator(".pack-ref__hint")).to_have_text("Foto in Cassandra erfassen")
    expect(page.locator(".nav__user")).to_have_text("Anna Beispiel")
    page.screenshot(path=str(tmp_path / "packen-line.png"))
    assert (tmp_path / "packen-line.png").stat().st_size > 1000

    # an incomplete answer never reaches the server: toast, still on line 1
    page.get_by_role("button", name="In Ordnung", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Bitte alle Prüfpunkte beantworten.")
    expect(page.locator("#pack-progress")).to_have_text("0 von 2")

    for q_ok in page.locator(".q-btn--ok").all():
        q_ok.click()
    expect(page.locator(".q-btn--ok[aria-pressed=true]")).to_have_count(2)
    page.get_by_role("button", name="In Ordnung", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Für diese Zeile ist ein Foto nötig.")

    page.set_input_files("input[type=file]", photo_file)
    expect(page.locator(".pack-photo")).to_be_visible()
    expect(page.locator(".photo-button")).to_contain_text("Neu aufnehmen")
    page.get_by_role("button", name="In Ordnung", exact=True).click()

    # line 2: serial number, no photo needed
    expect(page.locator(".pack-line__label")).to_have_text("Netzteil")
    expect(page.locator("#pack-progress")).to_have_text("1 von 2")
    page.get_by_role("button", name="In Ordnung", exact=True).click()
    expect(page.locator("#toast")).to_have_text(
        "Es wird genau eine Seriennummer pro Stück gebraucht, keine darf leer sein."
    )
    page.fill("input.serial-input", "SN-4711")
    page.get_by_role("button", name="In Ordnung", exact=True).click()

    # summary appears after the last line, progress complete
    expect(page.locator(".sum-row")).to_have_count(2)
    expect(page.locator("#pack-progress")).to_have_text("2 von 2")
    expect(page.locator(".sum-row--ok")).to_have_count(2)
    page.get_by_role("button", name="Fertig gepackt").click()
    expect(page.locator(".pack-result")).to_contain_text("Gepackt – Prüfung durch zweite Person erforderlich")
    expect(page.locator(".nav__user")).to_have_text("Anna Beispiel")

    # the server agrees
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        job = api.get(f"/api/jobs/{seeded['job_id']}").json()
    assert job["status"] == "packed"
    by_label = {line["label"]: line for line in job["lines"]}
    assert by_label["Kabel"]["latest"]["result"] == "ok"
    assert by_label["Kabel"]["latest"]["photo_url"].startswith("/photos/jobs/")
    assert by_label["Kabel"]["latest"]["quality"] == {"sauber": "ok", "funktion": "ok"}
    assert by_label["Kabel"]["latest"]["worker_name"] == "Anna Beispiel"
    assert by_label["Netzteil"]["latest"]["serials"] == ["SN-4711"]


def test_worker_deviation_flow_and_swipe(page, live_server, seeded, photo_file):
    """Fehlt with reason, a swipe that changes nothing, the 409 highlight on finish, a worker-added line."""
    url = live_server["url"]
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        created = api.post(
            "/api/expert/jobs",
            json={
                "template_id": seeded["template_id"],
                "order_ref": "B-200",
                "customer": "Zweite GmbH",
                "customisations": [{"template_line_id": None, "item_id": "A3", "label": "Gehäuse", "qty": 1,
                                    "reason": "Sonderwunsch"}],
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()[0]["id"]
    _login(page, url, seeded["code"])
    page.goto(f"{url}/packen/{job_id}")
    expect(page.locator("#pack-progress")).to_have_text("0 von 3")

    # swipe left moves to the next line without confirming anything
    box = page.locator("#pack-body").bounding_box()
    y = box["y"] + 40
    page.evaluate(
        """([x0, x1, y]) => {
          const el = document.getElementById('pack-body');
          const mk = (type, x) => new TouchEvent(type, {bubbles: true, changedTouches: [new Touch({identifier: 1, target: el, clientX: x, clientY: y})], touches: type === 'touchend' ? [] : [new Touch({identifier: 1, target: el, clientX: x, clientY: y})]});
          el.dispatchEvent(mk('touchstart', x0));
          el.dispatchEvent(mk('touchend', x1));
        }""",
        [300, 100, y],
    )
    expect(page.locator(".pack-line__label")).to_have_text("Netzteil")
    expect(page.locator("#pack-progress")).to_have_text("0 von 3")

    # "Fehlt" needs a reason (checked in the browser), then sends missing
    page.get_by_role("button", name="Fehlt", exact=True).click()
    page.get_by_role("button", name="Fehlt melden").click()
    expect(page.locator("#toast")).to_have_text("Bitte einen Grund angeben (mindestens 3 Zeichen).")
    page.fill("textarea[data-reason]", "Netzteil liegt nicht im Regal")
    page.get_by_role("button", name="Fehlt melden").click()
    expect(page.locator("#pack-progress")).to_have_text("1 von 3")

    # finishing with open lines: 409 -> the open lines are flagged and the first one is shown
    page.locator("#pack-strip .line-dot--summary").click()
    page.get_by_role("button", name="Fertig gepackt").click()
    expect(page.locator("#toast")).to_have_text("Es sind noch nicht alle Zeilen bestätigt.")
    expect(page.locator(".pack-line__label")).to_have_text("Kabel")
    expect(page.locator(".line-dot--flag")).to_have_count(2)

    # a worker-added extra part needs a photo and can be removed again while unconfirmed
    page.get_by_role("button", name="Zusätzliches Teil").click()
    page.fill("#extra-label", "Schraubenset")
    page.fill("#extra-reason", "Kunde wollte es dazu")
    page.get_by_role("button", name="Hinzufügen").click()
    expect(page.locator(".pack-line__label")).to_have_text("Schraubenset")
    expect(page.locator("#pack-progress")).to_have_text("1 von 4")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Entfernen").click()
    expect(page.locator("#pack-progress")).to_have_text("1 von 3")
    with httpx.Client(base_url=url, headers=EXPERT) as api:
        job = api.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "packing"
    assert [line["label"] for line in job["lines"]] == ["Kabel", "Netzteil", "Gehäuse"]
    assert {line["label"]: line["latest"]["result"] for line in job["lines"] if line["latest"]} == {"Netzteil": "missing"}


def test_expert_creates_a_customised_job_and_cancels_and_reopens_it(browser, live_server, seeded):
    url = live_server["url"]
    context = browser.new_context(viewport={"width": 1024, "height": 800})
    page = context.new_page()
    page.set_default_timeout(8000)
    try:
        page.goto(f"{url}/experte")
        page.fill("#password-input", PASSWORD)
        page.click("#login-form button[type=submit]")
        page.click("#tab-btn-auftraege")
        # RMA cannot be created yet
        expect(page.locator('#job-kind option[value="rma"]')).to_be_disabled()
        page.select_option("#job-template", label="Set A")
        expect(page.locator("#job-lines .tpl-line")).to_have_count(2)
        page.fill("#job-order", "C-300")
        page.fill("#job-customer", "Dritte AG")
        page.fill("#job-count", "2")

        # a withheld line without a reason is stopped in the browser
        page.locator('[data-template-line] input[type=checkbox]').first.check()
        page.click("#job-create-btn")
        expect(page.locator("#toast")).to_have_text(
            "Bitte für jede Anpassung einen Grund angeben (mindestens 3 Zeichen)."
        )
        page.locator('[data-template-line] input.input-id').first.fill("Kunde hat eigenes Kabel")
        page.click("#job-extra-add")
        extra = page.locator("#job-extras .tpl-line").first
        extra.locator("input.input-id").nth(1).fill("Beipackzettel")  # label (0 = item search)
        extra.locator("input.input-id").nth(3).fill("Kunde wünscht Anleitung")
        page.click("#job-create-btn")
        expect(page.locator("#toast")).to_contain_text("Angelegt: #")

        rows = page.locator("#job-rows tr", has_text="C-300")
        expect(rows).to_have_count(2)
        # newest first: carton 2/2 on top
        expect(rows.first).to_contain_text("Karton 2/2")
        expect(rows.nth(1)).to_contain_text("Karton 1/2")
        expect(rows.first).to_contain_text("Offen")
        with httpx.Client(base_url=url, headers=EXPERT) as api:
            listed = [j for j in api.get("/api/expert/jobs").json() if j["order_ref"] == "C-300"]
            job = api.get(f"/api/jobs/{listed[0]['id']}").json()
        assert len(listed) == 2
        assert sorted((line["label"], line["withheld"]) for line in job["lines"]) == [
            ("Beipackzettel", False), ("Kabel", True), ("Netzteil", False)]

        # cancel (confirm dialog), then it leaves the default list and shows up under "Storniert"
        page.once("dialog", lambda dialog: dialog.accept())
        rows.first.get_by_role("button", name="Stornieren").click()
        expect(page.locator("#job-rows tr", has_text="C-300")).to_have_count(1)
        page.select_option("#job-filter", "cancelled")
        cancelled = page.locator("#job-rows tr", has_text="C-300")
        expect(cancelled).to_have_count(1)
        page.once("dialog", lambda dialog: dialog.accept())
        cancelled.get_by_role("button", name="Wieder öffnen").click()
        expect(page.locator("#job-rows tr", has_text="C-300")).to_have_count(0)
        page.select_option("#job-filter", "")
        expect(page.locator("#job-rows tr", has_text="C-300")).to_have_count(2)
    finally:
        context.close()
