import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PACKAGE_DIR = ROOT / "galochka"
STATIC_DIR = PACKAGE_DIR / "static"

KEY_PATTERN = re.compile(
    r'data-i18n(?:-placeholder)?="([\w.]+)"'
    r"|\bt\(\s*[\"']([\w.]+)[\"']\s*\)"
)

PAGES = [
    "/",
    "/auftraege",
    "/anmelden",
    "/packen/1",
    "/pruefen/1",
    "/rma/1",
    "/experte",
    "/druck/etikett/1",
    "/druck/badge/1",
]

# Keys the server (or a later task's client code) relies on.
REQUIRED_KEYS = {
    "nav.brand",
    "nav.jobs",
    "nav.logout",
    "nav.expert",
    "error.unauthorized",
    "error.not_found",
    "error.too_large",
    "error.bad_image",
    "error.session_expired",
    "error.generic",
    "error.badge_unknown",
    "login.scan",
    "login.code_submit",
    "print.button",
    "expert.title",
    "expert.login",
    "expert.wrong_password",
    "expert.tab_artikel",
    "expert.tab_vorlagen",
    "expert.tab_auftraege",
    "expert.tab_mitarbeiter",
    "expert.tab_einstellungen",
    "expert.tab_berichte",
    "expert.tab_export",
}


def _strings() -> dict:
    return json.loads((STATIC_DIR / "strings.de.json").read_text(encoding="utf-8"))


def test_required_keys_present():
    assert REQUIRED_KEYS <= set(_strings())


def test_all_i18n_keys_used_in_static_files_and_python_exist():
    strings = _strings()
    used = set()
    files = list(STATIC_DIR.glob("*.html")) + list(STATIC_DIR.glob("*.js")) + list(PACKAGE_DIR.glob("*.py"))
    assert files
    for path in files:
        for match in KEY_PATTERN.finditer(path.read_text(encoding="utf-8")):
            used.add(match.group(1) or match.group(2))
    missing = used - set(strings)
    assert not missing, f"keys used in code but missing from strings.de.json: {missing}"


def test_strings_are_flat_non_empty_strings():
    for key, value in _strings().items():
        assert isinstance(value, str) and value, key


@pytest.mark.parametrize("path", PAGES)
def test_pages_are_html_and_revalidated(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"
    assert 'lang="de"' in resp.text
    assert '<script src="/static/app.js"></script>' in resp.text


def test_root_and_auftraege_are_the_same_page(client):
    assert client.get("/").text == client.get("/auftraege").text
    assert 'href="/experte"' in client.get("/auftraege").text


def test_print_page_is_body_print_and_serves_both_routes(client):
    label = client.get("/druck/etikett/7").text
    badge = client.get("/druck/badge/7").text
    assert label == badge
    assert '<body class="print">' in label


def test_worker_pages_have_nav_skeleton(client):
    for path in ("/anmelden", "/auftraege", "/packen/1", "/pruefen/1", "/rma/1", "/druck/etikett/1"):
        html = client.get(path).text
        assert 'class="nav__brand"' in html
        assert 'class="nav__user"' in html
        assert 'id="nav-logout"' in html and 'data-i18n="nav.logout"' in html


def test_worker_pages_check_the_session_and_redirect_to_login(client):
    for path in ("/auftraege", "/packen/1", "/pruefen/1", "/rma/1"):
        html = client.get(path).text
        assert 'data-i18n="common.not_available"' not in html, path
        assert "C.loadMe()" in html and "C.goLogin()" in html, path
    # anmelden shows the current user but never redirects; the print pages do their own auth
    assert "C.loadMe()" in client.get("/anmelden").text
    assert "C.goLogin()" not in client.get("/anmelden").text
    assert "C.loadMe()" not in client.get("/experte").text
    assert "C.loadMe()" not in client.get("/druck/badge/1").text


def test_anmelden_page_has_scan_button_and_code_form(client):
    html = client.get("/anmelden").text
    assert 'class="photo-button"' in html
    assert '<input type="file" id="badge-input" accept="image/*" capture="environment" hidden>' in html
    assert 'data-i18n="login.scan"' in html
    assert "photo-button--working" in html
    assert 'C.downscaleImage(file, 1600, 0.9)' in html
    assert 'formData.append("image"' in html
    assert "/api/login/qr" in html and "/api/login/code" in html
    code_input = re.search(r'<input type="text" id="code-input"[^>]*>', html, re.S).group(0)
    assert 'maxlength="6"' in code_input
    assert 'autocapitalize="characters"' in code_input
    assert 'inputmode="text"' in code_input
    # ?next= is only followed for same-origin relative paths
    assert 'next.charAt(0) === "/"' in html and 'next.charAt(1) !== "/"' in html
    assert '"/auftraege"' in html


def test_app_js_has_session_helpers():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in ("loadMe", "logout", "goLogin", "fmt"):
        assert re.search(rf"\b{name}\b", js), name
    assert '"/api/me"' in js and '"/api/logout"' in js
    assert "noRedirect: true" in js


def test_experte_page_has_all_tab_sections(client):
    html = client.get("/experte").text
    for name in ("artikel", "vorlagen", "auftraege", "mitarbeiter", "einstellungen", "berichte", "export"):
        assert f'id="tab-{name}"' in html
        assert f'data-tab="{name}"' in html


def test_unknown_page_is_404(client):
    assert client.get("/gibt-es-nicht").status_code == 404


def test_static_assets_served_and_revalidated(client):
    for path, ctype in [
        ("/static/app.js", "javascript"),
        ("/static/style.css", "text/css"),
        ("/static/strings.de.json", "application/json"),
        ("/static/acs_logo.png", "image/png"),
        ("/static/fonts/IBMPlexSans-Regular.woff2", "font/woff2"),
        ("/static/fonts/IBMPlexSans-Medium.woff2", "font/woff2"),
        ("/static/fonts/IBMPlexSans-SemiBold.woff2", "font/woff2"),
        ("/static/fonts/IBMPlexMono-Medium.woff2", "font/woff2"),
        ("/static/fonts/LICENSE.txt", "text/plain"),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert ctype in resp.headers["content-type"], path
        assert resp.headers["cache-control"] == "no-cache", path


def test_photos_are_not_marked_no_cache(client, data_dir):
    (data_dir / "photos" / "jobs").mkdir(parents=True)
    (data_dir / "photos" / "jobs" / "a.jpg").write_bytes(b"jpeg")
    resp = client.get("/photos/jobs/a.jpg")
    assert resp.status_code == 200
    assert "cache-control" not in resp.headers


def test_app_js_exports_and_conventions():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in (
        "downscaleImage",
        "loadStrings",
        "api",
        "toast",
        "expertApi",
        "getExpertPassword",
        "setExpertPassword",
        "clearExpertPassword",
        "renderPhotoTile",
    ):
        assert re.search(rf"\b{name}\b", js), name
    assert '"galochka.expertPassword"' in js
    # 401 on a worker path sends the browser to the badge login
    assert '"/anmelden?next="' in js
    assert 'path.indexOf("/api/expert") !== 0' in js
    # plain-HTTP LAN: none of the APIs that need a secure origin
    for banned in ("getUserMedia", "serviceWorker", "navigator.clipboard", "innerHTML"):
        assert banned not in js, banned


def test_no_secure_origin_apis_or_innerhtml_in_pages():
    for path in STATIC_DIR.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for banned in ("getUserMedia", "serviceWorker", "navigator.clipboard", "innerHTML"):
            assert banned not in text, f"{path.name}: {banned}"


def test_style_css_has_label_print_rules():
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert re.search(r"@page\s*\{\s*size:\s*89mm 36mm;\s*margin:\s*2mm;", css)
    assert "body.print" in css
    assert "--brand" in css and "prefers-color-scheme: dark" in css


def test_experte_mitarbeiter_tab(client):
    html = client.get("/experte").text
    section = re.search(r'<section id="tab-mitarbeiter".*?</section>', html, re.S).group(0)
    assert "hidden" in section.split(">", 1)[0]
    assert 'id="worker-form"' in section and 'id="worker-name"' in section
    assert 'data-i18n="expert.worker_create"' in section
    assert 'id="worker-list"' in section
    for key in ("expert.worker_print", "expert.worker_reissue", "expert.worker_deactivate", "expert.worker_activate",
                "expert.worker_reissue_confirm"):
        assert key in html, key
    assert "/api/expert/workers" in html and "/badge" in html
    assert "/druck/badge/" in html and 'target = "_blank"' in html


def test_druck_badge_page_layout_and_blob_flow(client):
    html = client.get("/druck/badge/3").text
    assert "URL.createObjectURL" in html and "blob: true" in html
    assert "/badge.svg" in html and "C.expertApi" in html
    assert "window.print()" in html and 'data-i18n="print.button"' in html
    assert 'id="login-form"' in html and "C.getExpertPassword()" in html
    assert 'class="print-label print-label--badge"' in html
    assert 'id="badge-name"' in html and 'id="badge-code"' in html
    # the box label branch is a real view now
    assert 'id="label-stub"' not in html and 'id="label-view"' in html
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.badge-label__qr\s*\{[^}]*width:\s*30mm", css)
    assert "--mono" in css and "badge-label__code" in css


def test_experte_artikel_tab_has_sync_and_csv_import(client):
    html = client.get("/experte").text
    assert 'id="items-sync-btn"' in html
    assert 'data-i18n="expert.items_sync"' in html
    assert '<input type="file" id="items-csv-file" class="input-id" accept=".csv">' in html
    assert "/api/expert/sync/items" in html and "/api/expert/import/items" in html
    assert "/api/expert/items/stats" in html


def test_experte_vorlagen_tab_markup(client):
    html = client.get("/experte").text
    section = re.search(r'<section id="tab-vorlagen".*?</section>', html, re.S).group(0)
    assert "hidden" in section.split(">", 1)[0]
    for element_id in ("tpl-new-btn", "tpl-list", "tpl-editor", "tpl-name", "tpl-product", "tpl-second-check",
                       "tpl-lines", "tpl-add-line", "tpl-save-btn", "tpl-cancel-btn"):
        assert f'id="{element_id}"' in section, element_id
    # second check select: Standard / Ja / Nein
    for key in ("expert.tpl_second_default", "expert.tpl_second_yes", "expert.tpl_second_no"):
        assert f'data-i18n="{key}"' in section
    for key in ("expert.tpl_new", "expert.tpl_add_line", "expert.tpl_cancel", "expert.save"):
        assert f'data-i18n="{key}"' in section
    for key in ("expert.tpl_edit", "expert.tpl_duplicate", "expert.tpl_deactivate", "expert.tpl_activate",
                "expert.tpl_delete", "expert.tpl_delete_confirm", "expert.tpl_line_up", "expert.tpl_line_down",
                "expert.tpl_line_remove", "expert.tpl_line_points", "expert.tpl_line_photo", "expert.tpl_line_serial"):
        assert key in html, key
    assert "/api/expert/templates" in html and "/duplicate" in html and "/api/items?q=" in html
    assert 'lineIconButton("↑"' in html and 'lineIconButton("↓"' in html


def test_experte_bom_import_in_artikel_tab(client):
    html = client.get("/experte").text
    section = re.search(r'<section id="tab-artikel".*?<section id="tab-vorlagen"', html, re.S).group(0)
    assert section.index('id="items-csv-form"') < section.index('id="bom-section"')
    assert '<input type="file" id="bom-file" class="input-id" accept=".csv">' in section
    for element_id in ("bom-form", "bom-preview-btn", "bom-preview", "bom-name", "bom-product", "bom-mode-new",
                       "bom-mode-replace", "bom-zero", "bom-lines", "bom-import-btn"):
        assert f'id="{element_id}"' in section, element_id
    assert 'data-i18n="expert.bom_preview"' in section and 'data-i18n="expert.bom_import"' in section
    assert "/api/expert/import/bom" in html
    assert 'formData.append("dry_run"' in html and 'formData.append("include_zero_qty"' in html
    assert '"replace:"' in html and 'switchTab("vorlagen")' in html


def test_template_and_bom_strings_exist_in_german():
    strings = _strings()
    for key in ("error.template_in_use", "error.unknown_item", "error.bom_missing_columns", "error.bom_empty",
                "expert.tpl_second_default", "expert.bom_skipped", "expert.bom_zero_label"):
        assert key in strings, key
    assert "Aufträgen" in strings["error.template_in_use"]
    # every key the Vorlagen/BOM code passes around as a plain string literal exists
    html = (STATIC_DIR / "experte.html").read_text(encoding="utf-8")
    for key in set(re.findall(r'"(expert\.(?:tpl|bom)_[\w]+)"', html)):
        assert key in strings, key


def test_auftraege_page_markup(client):
    html = client.get("/auftraege").text
    for element_id in ("new-job-btn", "rma-btn", "new-job-form", "new-template", "new-order", "new-customer",
                       "filter-all", "filter-mine", "job-list", "rma-form", "rma-template", "rma-no", "rma-customer",
                       "rma-submit"):
        assert f'id="{element_id}"' in html, element_id
    # "RMA annehmen" is live: enabled button, inline form posting to /api/rma
    assert not re.search(r'<button[^>]*id="rma-btn"[^>]*\bdisabled\b', html)
    assert 'data-i18n="jobs.rma_hint"' not in html
    assert '"/api/rma"' in html and "rma_no:" in html and '"/rma/" + job.id' in html
    for key in ("jobs.new", "jobs.rma", "jobs.filter_all", "jobs.filter_mine", "jobs.create"):
        assert f'data-i18n="{key}"' in html, key
    assert "/api/jobs?status=" in html and "/api/templates" in html and "/api/jobs/" in html
    assert '"/packen/"' in html and '"/pruefen/"' in html and '"/rma/"' in html
    assert "/ship" in html and "/claim" in html
    assert '"status." + job.status' in html


def _packen_source(client) -> str:
    """packen.html plus the shared line-by-line component it loads."""
    return client.get("/packen/7").text + client.get("/static/lines.js").text


def test_packen_page_markup(client):
    page = client.get("/packen/7").text
    assert '<script src="/static/lines.js"></script>' in page and "Lines.mount(" in page
    assert page.index("/static/app.js") < page.index("/static/lines.js")
    html = _packen_source(client)
    for element_id in ("pack-head", "pack-progress", "pack-bar-fill", "pack-strip", "pack-body", "pack-actions"):
        assert f'"{element_id}"' in html, element_id
    assert '"progress-bar"' in html
    # camera only through a file input (plain HTTP), photo downscaled before upload
    assert 'accept = "image/*"' in html and 'setAttribute("capture", "environment")' in html
    assert "C.downscaleImage(draft.photo, 1600, 0.9)" in html
    assert "/api/lines/" in html and "/confirm" in html and "/finish" in html and "/claim" in html
    assert "aria-pressed" in html and "touchstart" in html and "touchend" in html
    assert "Math.abs(dx) < 60" in html
    for key in ("pack.action_ok", "pack.action_missing", "pack.action_notok", "pack.finish", "pack.no_reference",
                "pack.done_second_check", "pack.done_released", "pack.logout_done", "error.quality_incomplete",
                "pack.extra"):
        assert key in html, key
    assert "onerror" in html and "photo-tile" in html
    assert 'autocapitalize' in html


def test_packen_and_job_strings_exist_in_german():
    strings = _strings()
    for state in ("ok", "missing", "not_ok", "open", "withheld"):
        assert f"pack.state_{state}" in strings
    for status in ("open", "packing", "packed", "checked", "shipped", "cancelled", "intake", "repair", "outgoing"):
        assert f"status.{status}" in strings
    assert strings["pack.no_reference"] == "Foto in Cassandra erfassen"
    assert strings["pack.done_second_check"].startswith("Gepackt")
    # keys passed around as plain string literals in the job pages exist
    for name in ("packen.html", "lines.js", "auftraege.html", "experte.html", "rma.html"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        keys = set(re.findall(r'"((?:pack|jobs)\.[\w]+)"', text)) | set(re.findall(r'"(expert\.job[s]?_[\w]+)"', text))
        for key in keys:
            if not key.endswith("_"):  # "pack.state_" is a prefix completed at runtime (checked above)
                assert key in strings, (name, key)


def test_experte_auftraege_tab_markup(client):
    html = client.get("/experte").text
    section = re.search(r'<section id="tab-auftraege".*?</section>', html, re.S).group(0)
    assert "hidden" in section.split(">", 1)[0]
    for element_id in ("job-form", "job-kind", "job-template", "job-order", "job-customer", "job-count", "job-note",
                       "job-lines", "job-extras", "job-extra-add", "job-create-btn", "job-filter", "job-rows"):
        assert f'id="{element_id}"' in section, element_id
    # rma is creatable by the expert: enabled option, fixed count, "RMA-Nummer" label
    assert re.search(r'<option value="rma"[^>]*>', section) and not re.search(r'<option value="rma"[^>]*\bdisabled\b', section)
    assert 'id="job-order-label"' in section and 'id="job-count-field"' in section
    assert "applyJobKind" in html and "kind: jobKind.value" in html and '"expert.job_rma_order"' in html
    assert _strings()["expert.job_kind_rma"] == "RMA" and _strings()["expert.job_rma_order"] == "RMA-Nummer"
    for key in ("expert.job_cancel", "expert.job_reopen", "expert.job_label", "expert.job_omit"):
        assert key in html, key
    assert "/api/expert/jobs" in html and "/api/expert/templates/" in html and "/druck/etikett/" in html
    assert '"cancelled"' in html and "withheld: true" in html


def test_pack_css_has_the_packen_pieces():
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for selector in (".q-btn", ".q-pair", ".serial-input", ".pack-actions", ".line-strip", ".line-dot", ".state-badge",
                     ".sum-row", ".progress-bar", ".btn--working"):
        assert selector in css, selector
    assert "env(safe-area-inset-bottom)" in css and "position: sticky" in css


def test_app_js_api_errors_carry_the_body():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "data: data" in js


def test_pruefen_page_markup(client):
    html = client.get("/pruefen/7").text
    for element_id in ("chk-head", "chk-order", "chk-packers", "chk-attempt", "chk-body", "chk-actions"):
        assert f'id="{element_id}"' in html, element_id
    # camera only through a file input (plain HTTP), box photo downscaled before upload
    assert 'input.accept = "image/*"' in html and 'setAttribute("capture", "environment")' in html
    assert "C.downscaleImage(boxPhoto, 1600, 0.9)" in html
    assert "/api/jobs/" in html and "/check" in html and "/ship" in html
    assert 'form.append("result", "ok")' in html and 'form.append("result", "rejected")' in html
    assert 'err.status === 403' in html and "/druck/etikett/" in html and 'target = "_blank"' in html
    for key in ("check.release", "check.reject", "check.box_photo", "check.print_label", "check.packed_by",
                "check.attempt", "check.state_extra", "jobs.ship"):
        assert f'"{key}"' in html, key
    assert "C.loadMe()" in html and "C.goLogin()" in html


def test_druck_label_branch_markup(client):
    html = client.get("/druck/etikett/7").text
    for element_id in ("label-view", "label-box", "label-text", "label-qr", "label-print-btn"):
        assert f'id="{element_id}"' in html, element_id
    assert 'class="print-label print-label--box"' in html
    assert "/api/jobs/" in html and "/label" in html and "/qr.svg" in html
    assert "noRedirect: true" in html and "C.expertApi(path)" in html and "C.getExpertPassword()" in html
    assert 'C.t("label.rma")' in html and 'C.t("label.unchecked")' in html
    assert "window.print()" in html


def test_label_css_has_box_label_rules():
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "print-label--box" in css and "box-label__qr" in css
    assert re.search(r"\.box-label__qr\s*\{[^}]*width:\s*28mm", css)
    assert "@media print" in css and "body.print .nav," in css and "body.print .no-print," in css


def test_auftraege_has_label_link_for_checked_jobs(client):
    html = client.get("/auftraege").text
    assert '"/druck/etikett/" + job.id' in html and 'job.status === "checked" || job.status === "shipped"' in html


def test_check_and_label_strings_exist_in_german():
    strings = _strings()
    assert strings["check.release"] == "Freigeben"
    assert strings["check.reject"] == "Zurück an Packer"
    assert strings["check.box_photo"] == "Foto vom Karton"
    assert strings["check.print_label"] == "Etikett drucken"
    assert strings["label.unchecked"] == "ohne Prüfung"
    assert strings["error.same_worker"]
    for name in ("pruefen.html", "druck.html", "auftraege.html"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        for key in set(re.findall(r'"((?:check|label)\.[\w]+)"', text)):
            assert key in strings, (name, key)


def test_lines_component_is_served_and_shared_by_packen_and_rma(client):
    resp = client.get("/static/lines.js")
    assert resp.status_code == 200 and "javascript" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-cache"
    js = resp.text
    assert "window.Lines = { mount: mount" in js and "function mount(container, job, options)" in js
    for banned in ("getUserMedia", "serviceWorker", "navigator.clipboard", "innerHTML"):
        assert banned not in js, banned
    # the camera path stays a file input
    assert 'setAttribute("capture", "environment")' in js
    for path in ("/packen/1", "/rma/1"):
        html = client.get(path).text
        assert '<script src="/static/lines.js"></script>' in html, path
        assert "Lines.mount(" in html, path
        assert html.index("/static/app.js") < html.index("/static/lines.js"), path
    # the 600-line component lives once: packen.html no longer carries its own copy
    assert "function renderLineView" not in client.get("/packen/1").text
    assert "function renderLineView" not in client.get("/rma/1").text


def test_packen_page_handles_the_rma_return(client):
    html = client.get("/packen/1").text
    assert 'C.t("rma.head_outgoing")' in html
    assert "pack.rma_done_second_check" in html and "pack.rma_done_released" in html
    assert '"/rma/" + job.id' in html  # intake / repair jobs are sent to the RMA page
    js = client.get("/static/lines.js").text
    assert 'C.t("pack.rma_in_qty")' in js and "line.in_qty" in js


def test_rma_page_markup(client):
    html = client.get("/rma/7").text
    for element_id in ("rma-head", "rma-order", "rma-body", "rma-lines"):
        assert f'id="{element_id}"' in html, element_id
    # all four stages are handled; the outgoing hand-off goes to Packen
    for needle in ("/repair-notes", "/api/repair-notes/", "/outgoing", '"/packen/"', '"/pruefen/"',
                   '"/druck/etikett/"', "Lines.itemPicker", "Lines.mount"):
        assert needle in html, needle
    assert 'el("details"' in html and 'el("summary"' in html
    # intake actions are relabelled through string keys, never hard-coded German
    for key in ("rma.in_ok", "rma.in_missing", "rma.in_notok", "rma.in_extra", "rma.in_finish", "rma.prepare",
                "rma.act_replaced", "rma.act_kept", "rma.add_part", "rma.note_delete"):
        assert f'"{key}"' in html, key
    assert "C.loadMe()" in html and "C.goLogin()" in html and "/claim" in html
    for banned in ("getUserMedia", "serviceWorker", "navigator.clipboard", "innerHTML"):
        assert banned not in html, banned


def test_rma_strings_exist_in_german():
    strings = _strings()
    expected = {
        "rma.in_ok": "Angekommen",
        "rma.in_missing": "Fehlt / unvollständig",
        "rma.in_notok": "Beschädigt",
        "rma.in_extra": "Weiteres Teil gefunden",
        "rma.in_finish": "Eingang abschließen",
        "rma.act_replaced": "Ersetzt",
        "rma.act_kept": "Einbehalten",
        "rma.prepare": "Rücksendung vorbereiten",
        "jobs.rma_intake": "Eingang prüfen",
        "jobs.rma_pack": "Rücksendung packen",
        "pack.rma_done_released": "Rücksendung gepackt und freigegeben",
    }
    for key, text in expected.items():
        assert strings[key] == text, key
    assert strings["rma.head"] == "RMA {ref}" and strings["rma.head_outgoing"] == "RMA {ref} – Rücksendung"
    assert "Experten" in strings["error.outgoing_empty"]
    # keys passed as plain literals in the RMA code exist (rma.act_* / rma.info_* are completed at runtime)
    for name in ("rma.html", "lines.js", "auftraege.html", "packen.html"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        for key in set(re.findall(r'"((?:rma|pack|jobs)\.[\w]+)"', text)):
            if not key.endswith("_"):
                assert key in strings, (name, key)
    for status in ("outgoing", "packed", "checked", "shipped", "cancelled"):
        assert f"rma.info_{status}" in strings
    for action in ("replaced", "kept", "added"):
        assert f"rma.act_{action}" in strings


def test_auftraege_rma_cards_lead_to_the_right_page(client):
    html = client.get("/auftraege").text
    for key in ("jobs.rma_intake", "jobs.rma_repair", "jobs.rma_pack"):
        assert f'"{key}"' in html, key
    assert 'job.kind === "rma" ? C.fmt(C.t("rma.head")' in html
    assert '"jobs.rma_open"' not in html


def test_druck_rma_label_shows_only_a_present_check(client):
    html = client.get("/druck/etikett/7").text
    assert 'C.t("label.rma")' in html
    # an RMA label never prints the packers; "geprüft" only when there is a checker
    rma_branch = html.split("if (rma) {", 1)[1].split("} else {", 1)[0]
    assert "label.checked" in rma_branch and "data.checked_by" in rma_branch
    assert "label.packed" not in rma_branch and "label.unchecked" not in rma_branch


def test_experte_berichte_tab_markup(client):
    html = client.get("/experte").text
    section = re.search(r'<section id="tab-berichte".*?</section>', html, re.S).group(0)
    assert "hidden" in section.split(">", 1)[0]
    for element_id in ("rep-form", "rep-q", "rep-serial", "rep-worker", "rep-kind", "rep-status", "rep-from", "rep-to",
                       "rep-search-btn", "rep-reset-btn", "rep-table", "rep-rows", "rep-empty", "rep-more", "rep-detail",
                       "rma-open-refresh", "rma-open-list", "rma-open-empty"):
        assert f'id="{element_id}"' in section, element_id
    assert 'type="date"' in section and 'id="rep-from"' in section
    for endpoint in ("/api/expert/jobs?", "/history", "/api/expert/rma/open", "/api/expert/workers"):
        assert endpoint in html, endpoint
    for key in ("reports.col_deviations", "reports.superseded", "reports.rma_line", "reports.days_open", "rma.act_"):
        assert key in html, key
    assert "rep-rma__line--open" in html and "target = \"_blank\"" in html and "noopener" in html
    assert "innerHTML" not in html  # DOM building only
    strings = _strings()
    for key in ("reports.col_order", "reports.col_deviations", "reports.search", "reports.open_rma_title",
                "reports.days_open", "reports.days_open_one", "reports.days_open_today", "reports.rma_line",
                "audit.job.create", "audit.job.check", "audit.line.confirm", "audit.repair.note"):
        assert key in strings, key
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for selector in (".rep-row", ".rep-detail", ".rep-event", ".rep-thumb", ".rep-rma__line--open", "body.wide"):
        assert selector in css, selector


def test_experte_export_tab_markup(client):
    html = client.get("/experte").text
    section = re.search(r'<section id="tab-export".*?</section>', html, re.S).group(0)
    assert "hidden" in section.split(">", 1)[0]
    for element_id in ("export-form", "export-from", "export-to", "export-kind", "export-btn"):
        assert f'id="{element_id}"' in section, element_id
    assert 'data-i18n="export.download"' in section
    assert _strings()["export.download"] == "CSV herunterladen"
    # the expert header cannot ride on a link: blob fetch + object URL + download attribute
    assert "/api/expert/export.csv?" in html and "blob: true" in html
    assert "URL.createObjectURL" in html and "link.download" in html and "URL.revokeObjectURL" in html
    assert "90 * 24" in html  # default range: last 90 days
