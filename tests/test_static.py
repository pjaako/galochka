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
    for path in ("/auftraege", "/packen/1", "/pruefen/1", "/rma/1"):
        html = client.get(path).text
        assert 'data-i18n="common.not_available"' in html
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
