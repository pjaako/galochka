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


def test_stub_pages_check_the_session_and_redirect_to_login(client):
    for path in ("/pruefen/1", "/rma/1"):
        html = client.get(path).text
        assert 'data-i18n="common.not_available"' in html
        assert "C.loadMe()" in html and "C.goLogin()" in html, path
    for path in ("/auftraege", "/packen/1"):
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
    # the box label branch stays a stub until T6
    assert 'id="label-stub"' in html and 'data-i18n="common.not_available"' in html
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
                       "filter-all", "filter-mine", "job-list"):
        assert f'id="{element_id}"' in html, element_id
    # RMA creation arrives with version 2: the button is disabled and carries the hint
    assert re.search(r'<button[^>]*id="rma-btn"[^>]*\bdisabled\b', html)
    assert 'data-i18n="jobs.rma_hint"' in html
    for key in ("jobs.new", "jobs.rma", "jobs.filter_all", "jobs.filter_mine", "jobs.create"):
        assert f'data-i18n="{key}"' in html, key
    assert "/api/jobs?status=" in html and "/api/templates" in html and "/api/jobs/" in html
    assert '"/packen/"' in html and '"/pruefen/"' in html and '"/rma/"' in html
    assert "/ship" in html and "/claim" in html
    assert '"status." + job.status' in html


def test_packen_page_markup(client):
    html = client.get("/packen/7").text
    for element_id in ("pack-head", "pack-progress", "pack-bar-fill", "pack-strip", "pack-body", "pack-actions"):
        assert f'id="{element_id}"' in html, element_id
    assert 'class="progress-bar"' in html
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
    for name in ("packen.html", "auftraege.html", "experte.html"):
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
    # rma is not creatable yet
    assert re.search(r'<option value="rma"[^>]*\bdisabled\b', section)
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
