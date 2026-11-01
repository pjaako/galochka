# Conventions: how galochka code is written (inherited from Cassandra)

This is a code-level survey of the sister app Cassandra (`pjaako/cassandra`), which galochka copies in stack, style, tests, deploy and docs. Replace `cassandra` with `galochka` (package, env prefix `GALOCHKA_`, paths `/opt/galochka`, `/var/lib/galochka`, `/etc/galochka.env`, port 8081, service user `galochka`) unless the spec says otherwise. Where the spec (`docs/specs/2026-11-01-galochka-design.md`) and this file disagree, the spec wins. Cassandra's ML parts (embedder, index, quality, judge, model download in install.sh) do not apply.

Source: `/home/user/cassandra` (read-only survey). All paths below are relative to that repo.

---

## 1. Package layout and entry points

```
pyproject.toml
cassandra/__init__.py   (empty)
cassandra/cli.py  config.py  db.py  repo.py  service.py  api.py  api_expert.py
cassandra/images.py  qr.py  quality.py  importer.py  index.py  embedder.py  judge.py  evaluate.py
cassandra/migrations/001_init.sql 002_promoted_photo.sql 003_families.sql 004_versions.sql
cassandra/static/{erkennen,erfassen,katalog,item,experte}.html  app.js  style.css  strings.de.json  acs_logo.png
cassandra/static/fonts/{IBMPlexSans-Regular,IBMPlexSans-Medium,IBMPlexSans-SemiBold,IBMPlexMono-Medium}.woff2 + LICENSE.txt
deploy/install.sh  cassandra.service  cassandra-cli  backup.sh
tests/conftest.py  tests/fixtures/items_small.csv  tests/test_*.py
docs/STATUS.md  HANDOFF.md  KNOWN-ISSUES.md  docs/eval/  docs/superpowers/{specs,plans}/
CLAUDE.md  README.md  .gitignore
```

**pyproject.toml**
- `[project] name="cassandra"`, `requires-python=">=3.13"`.
- Dependencies: `fastapi`, `uvicorn[standard]`, `python-multipart`, `numpy`, `pillow`, `zxing-cpp`.
- Optional extras: `model=[torch,torchvision,transformers]`, `dev=[pytest,httpx2,qrcode]`, `browser=[playwright]`.
- `[project.scripts] cassandra = "cassandra.cli:main"`; setuptools build backend.
- `[tool.setuptools.packages.find] include=["cassandra*"]`.
- `[tool.setuptools.package-data] cassandra=["migrations/*.sql"]`.
- `[tool.pytest.ini_options] testpaths=["tests"]`, `markers=["model: tests that require the real ML model"]`.

**`.gitignore`**: `.venv/ data/ __pycache__/ *.pyc .pytest_cache/ .remember/ docs/eval/*.jpg *.egg-info/`

**cassandra/config.py**
```python
@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8080
    expert_password_default: str = "experte"
    model_name: str = "facebook/dinov2-small"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("CASSANDRA_DATA", "./data")),
            port=int(os.environ.get("CASSANDRA_PORT", 8080)),
            expert_password_default=os.environ.get("CASSANDRA_EXPERT_PASSWORD", "experte"),
            model_name=os.environ.get("CASSANDRA_MODEL", "facebook/dinov2-small"),
        )
    @property
    def db_path(self) -> Path: return self.data_dir / "cassandra.db"
    @property
    def photos_dir(self) -> Path: return self.data_dir / "photos"
```

Data dir layout:
- `<data>/cassandra.db`, plus the `-wal`/`-shm` files.
- `<data>/photos/<ITEM_ID>/<uuid4hex>.jpg` for reference photos.
- `<data>/photos/_queries/<uuid>.jpg` for query photos.
- In production, `<data>/hf-cache`.

**cassandra/cli.py**
- `main(argv: list[str] | None = None)` builds `Settings.from_env()`, then an `argparse.ArgumentParser(prog="cassandra")` with `add_subparsers(dest="command", required=True)`.
- Each subcommand calls `set_defaults(func=_cmd_x)`, and `args.func(args, settings)` runs it.
- Subcommands:
  - `serve --host (default "0.0.0.0") --port (default settings.port)`
  - `import <file>`
  - `set-password <password>`
  - `suggest-families --threshold 0.95`
  - `eval <folder> --k 10 --report`
- Server launch:
  ```python
  def _build_app(settings): return create_app(settings, load_embedder(settings))
  def _cmd_serve(args, settings):
      app = _build_app(settings)
      uvicorn.run(app, host=args.host, port=args.port)
  ```
- Every non-serve command builds the full app, works under the app's lock, then closes the connection in `finally`:
  ```python
  app = _build_app(settings)
  try:
      with app.state.lock:
          app.state.service.set_expert_password(args.password)
  finally:
      app.state.service.conn.close()
  ```
- Errors are printed in German via `t("error.bad_password")`, followed by `raise SystemExit(1)`.
- Timestamps come from `_now()`: `datetime.now(timezone.utc).isoformat(timespec="seconds")`, stored as TEXT.
- If `CASSANDRA_MODEL=fake`, `load_embedder` returns `FakeEmbedder` (dim 12). This lets the app run without the model.

## 2. Database layer

**cassandra/db.py** (complete file)
```python
def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    migrations_dir = Path(__file__).parent / "migrations"
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text()
        script = f"BEGIN;\n{sql}\nINSERT INTO schema_migrations(name) VALUES ('{path.name}');\nCOMMIT;"
        try:
            conn.executescript(script)
        except Exception:
            conn.rollback()
            raise
```

- Schema version is tracked as applied file names in `schema_migrations(name)`. There is no integer version number.
- Migration files are plain SQL named `NNN_snake.sql`. They contain no BEGIN/COMMIT (the runner wraps them).
- Seed data goes inside migrations. Example from 001:
  ```sql
  INSERT INTO settings(key,value) VALUES ('t_green','0.80'),...
  ```
- Later migrations use `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT ''` and `RENAME COLUMN`.

Table style (001_init.sql):
- `id TEXT PRIMARY KEY` for items; `id INTEGER PRIMARY KEY AUTOINCREMENT` for others.
- Text columns: `TEXT NOT NULL DEFAULT ''`.
- Booleans: `INTEGER NOT NULL DEFAULT 1`.
- Enums: `CHECK (source IN ('reference','query'))`.
- Timestamps: `created_at TEXT NOT NULL` / `updated_at TEXT NOT NULL`.
- References: `REFERENCES items(id)`, with `ON DELETE CASCADE` on child tables (003).
- Indexes: `CREATE INDEX photos_item ON photos(item_id);`.
- Key/value table: `settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)`.

**Request-lock pattern**
- One shared connection is created in `create_app`, plus `lock = threading.Lock()` stored in `app.state.lock`.
- Every DB access in a route is wrapped in `with lock:`.
- Image decoding, QR decoding, quality checks and outbound HTTP run outside the lock.
- HANDOFF §7: "Lock discipline: one SQLite connection and the vector index are shared; every access happens under `app.state.lock`. Image decoding, quality checks and HTTP calls to the judge run outside the lock. Keep it that way."

**cassandra/repo.py**
- Plain module-level functions taking `conn: sqlite3.Connection` first.
- They return `sqlite3.Row | None`, `list[sqlite3.Row]`, `int` (lastrowid), `bool` (rowcount>0), or small `dict`s. No dataclasses in repo.
- `now: str` is passed in by the caller.
- Every write function calls `conn.commit()` itself.
- Validation errors raise `ValueError`; missing rows raise `KeyError` (the service and API map these to 400 and 404). Multi-statement writes use `try/except ValueError: conn.rollback(); raise`.
- Item lookups use `COLLATE NOCASE`.

Representative functions:
```python
def get_item(conn: sqlite3.Connection, id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ? COLLATE NOCASE", (id,)).fetchone()

def create_family(conn: sqlite3.Connection, member_ids: list[str], hint: str, now: str) -> int:
    try:
        canonical_ids = _resolve_family_members(conn, member_ids)
        cur = conn.execute(
            "INSERT INTO families (hint, created_at) VALUES (?, ?)", (hint, now)
        )
        family_id = cur.lastrowid
        for member_id in canonical_ids:
            conn.execute(
                "INSERT INTO family_members (family_id, item_id) VALUES (?, ?)",
                (family_id, member_id),
            )
        conn.commit()
        return family_id
    except ValueError:
        conn.rollback()
        raise

def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]

def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
```

**cassandra/service.py**
- A `Service` class holds `conn, settings, embedder, index, photos_dir`; business logic lives here.
- It returns `@dataclass` results (`IdentifyResult`, `Candidate`, `QueueEntry`), which the API serialises with `dataclasses.asdict`.
- Password helpers:
  - `validate_expert_password(pw)`: `4 <= len(pw) <= 64 and all(32 < ord(c) < 127 for c in pw)`.
  - `Service.check_expert_password` compares with `hmac.compare_digest` against setting `expert_password`.
  - The password is stored as plain text in `settings`.

## 3. API layer (cassandra/api.py)

**Module-level helpers**
```python
_STATIC_DIR = Path(__file__).parent / "static"
_STRINGS_PATH = _STATIC_DIR / "strings.de.json"
_strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))
def t(key: str) -> str:
    return _strings[key]

_MAX_UPLOAD_BYTES = 40 * 1024 * 1024
def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=t("error.too_large"))
    return data
```

**`create_app(settings: Settings, embedder: Embedder) -> FastAPI`**
1. `mkdir` the data dir and photos dir (`parents=True, exist_ok=True`).
2. `db.connect` + `db.migrate`.
3. Seed default settings with `if repo.get_setting(conn, k) is None: repo.set_setting(...)`. For example, `expert_password` comes from `settings.expert_password_default`.
4. Build the `Service` and `lock = threading.Lock()`.
5. Lifespan: `@asynccontextmanager async def lifespan(app): yield; conn.close()`.
6. `app = FastAPI(lifespan=lifespan)`.
7. Set `app.state.service`, `app.state.lock`, plus in-memory caches (`app.state.check_cache = {}`, `app.state.prepared_cache = {}`).
8. Add an HTTP middleware that sets `Cache-Control: no-cache` on `/static/*`, on page routes, and on `/katalog/*`. `/photos/*` is left cacheable because uuid file names are immutable.
9. Mounts:
   ```python
   app.mount("/photos", StaticFiles(directory=settings.photos_dir), name="photos")
   app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
   ```
10. `app.include_router(expert_router)`. The import happens inside `create_app` (`from cassandra.api_expert import require_expert, router`) to avoid a circular import, because `api_expert` imports `t`, `_now`, `_read_upload` and `_serialize_candidate` from `api`.
11. Pages are served with `FileResponse`:
    ```python
    def _serve_page(filename: str) -> FileResponse:
        path = _STATIC_DIR / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail=t("error.not_found"))
        return FileResponse(path)
    @app.get("/")
    @app.get("/erkennen")
    def erkennen_page(): return _serve_page("erkennen.html")
    # /erfassen, /katalog, /katalog/{item_id} -> item.html, /experte
    ```
12. `GET /health` returns `{"status":"ok","photos":N,"items":N}`.
13. All routes are sync `def` closures inside `create_app` that use `lock`, `service` and `conn` from the closure.

**Error conventions**
- `HTTPException(status_code=..., detail=t("error.<key>"))`. The detail is always a German string from `strings.de.json`.
- Status codes in use: 400 (`bad_image`, `bad_angle`, `unknown_item`, `bad_version`), 401 (`unauthorized`), 403 (`undo_expired`), 404 (`not_found`), 409 (`judge_disabled`), 410 (`check_expired`), 413 (`too_large`), 422 (`bad_password`, `thresholds_order`, and pydantic validation).
- Service exceptions map as `KeyError` → 404, `ValueError` → 400, `BadImage` → 400.
- 204 routes declare `status_code=204` and `return Response(status_code=204)`.
- Creating routes use `status_code=201`.
- Errors look like `{"detail": "Nicht gefunden."}`.

**Pydantic models** are declared at module level, are minimal, and use `Field` constraints:
```python
class ChooseRequest(BaseModel):
    item_id: str
    rank: int
class SettingsRequest(BaseModel):
    t_green: float = Field(ge=0, le=1)
    autocrop: bool | None = None
```
Multipart routes use `image: UploadFile = File(...)` and `angle: str = Form(...)`. Query params use `limit: int = Query(200, ge=1, le=1000)`.

**cassandra/api_expert.py `require_expert`** (verbatim)
```python
def require_expert(request: Request, x_expert_password: str | None = Header(default=None)) -> None:
    service: Service = request.app.state.service
    with request.app.state.lock:
        ok = x_expert_password is not None and service.check_expert_password(x_expert_password)
    if not ok:
        raise HTTPException(status_code=401, detail=t("error.unauthorized"))

router = APIRouter(prefix="/api/expert", dependencies=[Depends(require_expert)])
```
- The header is `X-Expert-Password`.
- Router routes take `request: Request` and use `request.app.state.service` / `request.app.state.lock`.
- A single non-router route also uses it: `@app.delete("/api/photos/{photo_id}", status_code=204, dependencies=[Depends(require_expert)])`.

**`GET /api/items/{item_id}`**
- No auth. Lookup is case-insensitive. Unknown id → 404 `{"detail":"Nicht gefunden."}`.
- Response shape:
  ```json
  {
    "item_id": "A01143", "name": "...", "group_name": "...", "location": "...",
    "versions": "...", "drawing_revision": "...",
    "photo_count": 2,
    "photo_url": "/photos/A01143/<uuid>.jpg",
    "photos": [
      {"id": 1, "angle": "front", "source": "reference",
       "url": "/photos/A01143/<uuid>.jpg", "created_at": "2026-...+00:00", "version": ""}
    ],
    "family": null
  }
  ```
- `photo_count` counts only `source=="reference"` photos.
- `photo_url` is the first reference photo by id, or `null`.
- `photos` contains all photos, including `source:"query"` (promoted query photos). Possible `angle` values come from `VALID_ANGLES`: `top`, `bottom`, `front`, `side`, `oblique`, `other`.
- `family` is either `null` or `{"id":int,"hint":str,"members":[ids]}`.

**`GET /api/items?q=&group=&captured=all|yes|no&limit=200`**
- No auth. `limit` must be 1..1000, otherwise 422.
- Returns active items only, ordered by id, as a list of:
  ```json
  {"item_id","name","group_name","location","versions","drawing_revision","photo_count","photo_url"}
  ```
- `q` matches id or name, case-insensitive.

**Fetching photos**
- Photo URLs are root-relative (`/photos/...`). A remote caller has to prefix Cassandra's base URL, for example `http://<host>:8080/photos/A01143/abc.jpg`.
- `/photos` is an unauthenticated `StaticFiles` mount.
- There is no CORS middleware in Cassandra, so browser-side cross-origin fetches of the JSON from another origin would be blocked. Server-side HTTP calls are unaffected.

Other public JSON routes:
- `GET /api/groups` returns a list of strings.
- `GET /api/stats/capture` returns `{"captured","total"}`.
- `POST /api/qr` (see §4).

## 4. QR decoding

**cassandra/qr.py** (verbatim, minus imports)
```python
_ITEM_ID_RE = re.compile(r"^[A-Za-z]\d{4,6}$")
_MAX_SIDE = 2048

def _read(image: Image.Image) -> str | None:
    results = zxingcpp.read_barcodes(image)
    if not results:
        return None
    return results[0].text.strip()

def decode_qr(data: bytes, full_res_retry: bool = True) -> str | None:
    try:
        image = Image.open(io.BytesIO(data))
        if image.format == "JPEG":
            image.draft("RGB", (_MAX_SIDE, _MAX_SIDE))
        image.load()
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        return None
    image.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.BILINEAR)
    text = _read(image)
    if text is not None:
        return text
    if not full_res_retry:
        return None
    try:
        original = Image.open(io.BytesIO(data))
        original.load()
        original = ImageOps.exif_transpose(original).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        return None
    if original.size == image.size:
        return None
    return _read(original)

def extract_item_id(text: str) -> str:
    for token in re.split(r"[^A-Za-z0-9]+", text):
        if _ITEM_ID_RE.match(token):
            return token.upper()
    return text.strip()
```

**`POST /api/qr`**
```python
@app.post("/api/qr")
def scan_qr(image: UploadFile = File(...)):
    data = _read_upload(image)
    text = qr.decode_qr(data)  # image decoding stays outside the lock
    with lock:
        item_id, row = service.lookup_qr_text(text)
        ...
    return {"item_id": item_id, "item": item}   # item = list-shape item JSON or None
```
- `Service.lookup_qr_text` tries an exact (NOCASE) match on the raw text first, then falls back to `extract_item_id`.
- Responses:
  - Unreadable image: `{"item_id": null, "item": null}`.
  - Code found but item unknown: `{"item_id": "Z99999", "item": null}`.

**Front-end photo capture** (`erfassen.html`, QR button)
```html
<label class="btn btn--secondary qr-btn" for="qr-input">
  <span data-i18n="capture.scan_qr"></span>
  <input type="file" id="qr-input" accept="image/*" capture="environment" hidden>
</label>
```
```js
var blob = typeof C.downscaleImage === "function" ? await C.downscaleImage(file, 1600, 0.9) : file;
var formData = new FormData();
formData.append("image", blob, "qr.jpg");
var body = await C.api("/api/qr", { formData: formData });
```
- The Erkennen page uses the same pattern with `<label class="photo-button" for="photo-input" ... role="button" tabindex="0">`.
- On that page the photo is downscaled to 2048 px at quality 0.9 and sent as `formData.append("image", upload, "photo.jpg")`.
- After upload the input is reset with `fileInput.value = ""` in `finally`.
- The multipart field name is always `image`. Extra fields (`angle`, `compare_with`) are added with `formData.append`.

## 5. Front-end conventions (cassandra/static/app.js)

**Page structure**
- One HTML file per page. Each has:
  - `<html lang="de">`, `<meta charset="utf-8">`, `<meta name="viewport" content="width=device-width, initial-scale=1">`
  - `<title data-i18n="...">cassandra</title>`, `<link rel="stylesheet" href="/static/style.css">`
- The shared nav is copy-pasted into each page; there are no includes:
  ```html
  <nav class="nav">
    <img src="/static/acs_logo.png" alt="ACS" width="32" height="32" class="nav__logo">
    <span class="nav__brand" data-i18n="nav.brand"></span>
    <div class="nav__links">
      <a href="/" data-i18n="nav.identify"></a>
      <a href="/erfassen" data-i18n="nav.capture"></a>
      <a href="/katalog" data-i18n="nav.catalog" aria-current="page"></a>
    </div>
  </nav>
  <main class="main"> <h1 data-i18n="..."></h1> ... <footer class="footer"><a href="/experte" data-i18n="nav.expert"></a></footer></main>
  <script src="/static/app.js"></script>
  <script> (function () { ... async function init(){ try { await C.loadStrings(); } catch (err) { C.toast(C.t("error.generic")); } ... } init(); })(); </script>
  ```
- Page logic is inline in an IIFE at the bottom of each HTML file.
- JS is ES5-ish (`var`, `function`) with `async`/`await`.
- DOM is built with `document.createElement` and `textContent`, never `innerHTML`.

**`app.js`**
- An IIFE that exports `window.C = { downscaleImage, t, loadStrings, api, renderCard, toast, copyText, expertApi, getExpertPassword, setExpertPassword, clearExpertPassword, renderCandidateRow, renderLabel, renderPhotoTile }`.

**i18n** (verbatim)
```js
var stringsCache = null;
function t(key) {
  if (!stringsCache) return key;
  return stringsCache[key] || key;
}
async function loadStrings() {
  if (stringsCache) return stringsCache;
  var resp = await fetch("/static/strings.de.json");
  stringsCache = await resp.json();
  document.querySelectorAll("[data-i18n]").forEach(function (el) {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
    el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
  });
  return stringsCache;
}
```
- `strings.de.json` is a flat object with dotted keys: `nav.*`, `identify.*`, `capture.*`, `catalog.*`, `item.*`, `expert.*`, `error.*`, `check.*`, `card.*`, `color.*`, `angle.*`, `source.*`.
- Placeholders are written `{count}`, `{v}`, `{reason}` and filled with `.replace("{count}", n)`.
- The server reads the same file through `t()` in `api.py`.

**Fetch helper** (verbatim)
```js
async function api(path, opts) {
  opts = opts || {};
  var method = opts.method || (opts.formData || opts.body !== undefined ? "POST" : "GET");
  var init = { method: method };
  if (opts.formData) {
    init.body = opts.formData;
  } else if (opts.body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.body);
  }
  if (opts.headers) {
    init.headers = Object.assign({}, init.headers, opts.headers);
  }
  var resp = await fetch(path, init);
  if (resp.status === 204) {
    return null;
  }
  var data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!resp.ok) {
    var detail = data && typeof data.detail === "string" ? data.detail : t("error.generic");
    throw { status: resp.status, detail: detail };
  }
  return data;
}
```
Page error handler: `function reportError(err) { C.toast((err && err.detail) || C.t("error.generic")); }`

**Expert password**
- Stored in `sessionStorage` under key `"cassandra.expertPassword"`, with try/catch around every storage call.
- `expertApi(path, opts)` adds the header `{"X-Expert-Password": getExpertPassword() || ""}` and calls `api`.
- On a 401 it runs `clearExpertPassword()` and `toast(t("expert.wrong_password"))`, then rethrows.

**Toast**
- `toast(msg)` lazily creates `<div id="toast" class="toast" role="status" aria-live="polite">`.
- It adds `toast--visible` and removes it after 2500 ms.

**`downscaleImage(file, maxSide, quality)`**
- Uses `createImageBitmap(file, {imageOrientation:"from-image"})`, draws to a canvas, then `canvas.toBlob(..., "image/jpeg", quality)`.
- It never upscales. On any failure it resolves to the original file.

**Renderers**
- `renderCard(candidate, opts)` builds a `.card` with `__body`/`__thumb`/`__actions`. Options: `color`, `hideCopy`, `hideAllPhotos`, `extraButtons:[{label,primary,onClick}]`.
- `renderCandidateRow` builds a `<button class="candidate-row">`.
- `renderLabel` builds `.result-label` (`--green`/`--yellow`/`--red`).
- `renderPhotoTile(el, url, alt)` adds an `<img draggable=false>`, or `.photo-tile--empty` with a `"?"` mark when there is no photo.

**Dark theme and fonts**
- Dark theme is CSS-only through `@media (prefers-color-scheme: dark)`. There is no JS toggle.
- Fonts are self-hosted IBM Plex woff2 files under `/static/fonts/`, with `font-display: swap`.

## 6. style.css

**Tokens** (verbatim)
```css
:root {
  --bg: #efefec; --surface: #ffffff; --fg: #1b1c1a; --muted: #5f625c; --line: #c9cbc4;
  --brand: #007cc5;  /* ACS blue */  --brand-ink: #ffffff;
  --green: #2e7d32; --yellow: #c77700; --red: #b3261e;
  --radius: 4px;
  --font: "IBM Plex Sans", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) { :root {
  --bg: #15161a; --surface: #1f2126; --fg: #ecedea; --muted: #a0a39e; --line: #33363c;
  --brand: #4fb0f0; --green: #5cb860; --yellow: #e9a23b; --red: #e5534b; } }
```

**@font-face**: IBM Plex Sans at 400, 500 and 600; IBM Plex Mono at 500.

**Sections** are marked with comment banners:
```css
/* ============================================================
   TOKENS / FONTS / BASE / LAYOUT / NAV / CARDS / PHOTO TILES / CANDIDATE ROWS / BUTTONS AND INPUTS / TOAST / PAGES — <Name>
   ============================================================ */
```

**Base**
- `* {box-sizing: border-box}`.
- `body`: max-width 640px, centred, padding `0 16px 32px` (24px sides at ≥480px), 16px/1.5.
- `h1`: `600 28px/1.4`. `h2`: `600 20px/1.4`.
- Links use `var(--brand)` with no underline.
- `:focus-visible` gets a 2px brand outline with offset 2px.

**Responsive**: the only breakpoint is `@media (min-width: 480px)` (body padding, nav margins, `.photo-grid` going from 2 to 3 columns). Motion is gated by `@media (prefers-reduced-motion: no-preference)`. There is no `@media (pointer: fine)` rule. A comment notes the slot backup buttons are deliberately shown on every device, not only pointer-fine input.

**Main classes**
- `.nav`: flex, height 56px, surface background, bottom border; bleeds to the page edges with negative margins.
  - Children: `.nav__logo`, `.nav__brand` (600 16px, `margin-right:auto`), `.nav__links` (gap 16px).
  - `.nav a`: 500 16px, 3px transparent bottom border. `.nav a[aria-current="page"]` has a brand-coloured bottom border.
- `.main` (padding-top 16px).
- `.note`: 4px brand left border, padding 12px.
- `.footer`.
- `.results` (flex column).
- `.card` is the label-style card:
  - Grid areas `"body thumb" / "actions actions"`, surface background, 1px line border, radius 4px, padding `12px 12px 12px 16px`.
  - `::before` draws an 8px left colour strip in `--line`. `.card--green/--yellow/--red` recolour the strip and use a 2px border in that colour.
  - Children: `.card__id` (mono 500 20px), `.card__name` (500 16px), `.card__meta` and `.card__score` (13px muted), `.card__thumb` (72×72, contain), `.card__actions` (flex, gap 8px).
- `.result-label`: same strip-label look without a grid. Children: `__row`, `__id` (mono 20px), `__score`, `__name`, `__meta`. Colour modifiers `--green/--yellow/--red`.
- `.photo-tile`: bg, line border, radius; `img` uses `object-fit: contain` and `pointer-events:none`. Also `.photo-tile--empty` and `.photo-tile__mark`.
- `.candidate-row`: flex, min-height 72px, bottom border, 3px left border that turns brand when `[aria-current="true"]`. Children: `__body`, `__id` (mono 16px), `__name` (ellipsis), `__meta` (13px muted, flex gap 12), `__thumb` (56px), `__score`.
- Buttons:
  - `.btn`: flex-centred, full width, min-height 48px, 1px line border, surface background, 500 16px.
  - `.btn--primary`: brand background, `--brand-ink` text.
  - `.btn--secondary`: 2px brand border, brand text on surface.
  - `.btn:disabled`: opacity 0.4.
- `.input-id`, `.select`: block, full width, min-height 48px, 1px line border, surface background, 400 16px.
- `.photo-button`: the big capture label; min-height 64px, brand background, white text. `--working` sets opacity 0.6.
- `.toast`: fixed bottom 24px, centred, `--fg` background with `--bg` text, 14px, opacity 0. `.toast--visible` sets opacity 1.
- Page-specific classes:
  - Capture: `.compare`, `.compare-tile`, `.capture-row`, `.qr-btn`, `.slot-tile`, `.slot-action-btn--discard/--keep`, `.capture-progress`, `.progress-bar`, `.todo-row`.
  - Catalog: `.catalog-filters`.
  - Item page: `.item-header`, `.photo-grid`, `.photo-card__*`, `.badge`.
  - Expert page: `.login-form`, `.tabs`, `.tab-btn`, `.tab-section`, `.queue-entry`, `.field`, `.field-label`, `.settings-section`, `.stats-table`.
  - Also `.text-link` (min-height 44px).

## 7. Tests

**tests/conftest.py main fixtures** (verbatim core)
```python
@pytest.fixture
def data_dir(tmp_path):
    return tmp_path

@pytest.fixture
def conn(data_dir):
    connection = connect(data_dir / "cassandra.db")
    migrate(connection)
    yield connection
    connection.close()

@pytest.fixture
def make_image():
    def _make_image(color=(200, 30, 30), size=(640, 480), fmt="JPEG", orientation: int | None = None):
        image = Image.new("RGB", size, color)
        buf = io.BytesIO()
        if orientation is not None:
            exif = Image.Exif(); exif[0x0112] = orientation
            image.save(buf, format=fmt, exif=exif)
        else:
            image.save(buf, format=fmt)
        return buf.getvalue()
    return _make_image

@pytest.fixture
def client(data_dir):
    app = create_app(Settings(data_dir=data_dir, model_name="fake"), FakeEmbedder())
    with TestClient(app) as c:
        yield c
```

**Model stubbing**: `FakeEmbedder` (in `cassandra/embedder.py`) has `name="fake"` and `dim=12`, and embeds by resizing to 2×2 and normalising. Real-model tests are gated with `@pytest.mark.skipif(os.environ.get("CASSANDRA_TEST_MODEL") != "1", ...)`.

**`seeded_client`**
- Imports `tests/fixtures/items_small.csv` via `importer.import_items(service.conn, bytes, now)`.
- Adds reference photos with `images.normalize`, `images.save_photo(service.photos_dir, item_id, ...)`, `repo.add_photo`, `service.index.add`.
- Uses the fixed `now = "2026-09-24T00:00:00+00:00"`.

**Representative API tests** (`tests/test_api_catalog.py`)
```python
def test_item_detail_404_for_unknown_id(seeded_client):
    resp = seeded_client.get("/api/items/NOPE")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Nicht gefunden."}

def test_qr_unknown_item_returns_id_with_null_item(seeded_client):
    resp = seeded_client.post("/api/qr", files={"image": ("qr.png", _qr_png("Z99999"), "image/png")})
    assert resp.status_code == 200
    assert resp.json() == {"item_id": "Z99999", "item": None}
```
- QR images are generated with the `qrcode` package (`qrcode.make(data)` saved to PNG bytes).
- Expert auth tests use `GOOD = {"X-Expert-Password": "experte"}` and `BAD = {...: "wrong"}`, plus a `@pytest.mark.parametrize("method,path,kwargs", [...])` over every expert route. Each must return 401 with no header and with a wrong header.

**CLI tests** (`tests/test_cli.py`) use an autouse fixture that monkeypatches `CASSANDRA_DATA=tmp_path` and `CASSANDRA_MODEL=fake`, call `cli.main([...])`, and assert on `capsys` output and `SystemExit.code`.

**DB tests** (`tests/test_db.py`) cover: migrate creates tables, a second migrate is a no-op, seed defaults exist, the foreign_keys pragma is on, and per-migration column checks.

**Static/HTML tests** (`tests/test_static.py`)
- They read files from `STATIC_DIR = Path(__file__).parent.parent / "cassandra" / "static"` and do string or regex assertions on the HTML source, e.g. `'data-i18n="identify.take_photo"' in html`.
- `REQUIRED_KEYS`: a set that must be a subset of `strings.de.json`.
- Key-coverage test: every key used in static files must exist in `strings.de.json`, using
  ```python
  KEY_PATTERN = re.compile(r'data-i18n(?:-placeholder)?="([\w.]+)"' r"|\bt\(\s*[\"']([\w.]+)[\"']\s*\)")
  ```
- Route tests: `client.get("/")` returns 200 with `text/html` and contains a given key; app.js, fonts and logo are served.
- Cache-header test: `/static` and page routes are `no-cache`, `/photos` is not.

**Browser tests** (`tests/test_browser_erkennen.py`, `tests/test_browser_gestures.py`)
- Gated by `pytest.importorskip("playwright.sync_api")` at module top.
- A module-scoped `browser` fixture calls `p.chromium.launch()` and on exception runs `pytest.skip(f"Chromium is not installed/launchable: {e}")`.
- `live_server` fixture (module scope):
  - Builds `create_app(Settings(data_dir=tmp, model_name="fake"), FakeEmbedder())` and seeds it.
  - Picks a free port by binding to port 0.
  - Runs `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))` in a daemon thread, waits for `server.started` (10 s deadline), and sets `server.should_exit = True` on teardown.
- `page` fixture: `browser.new_context(viewport={"width":412,"height":915}, has_touch=True, is_mobile=True)`.
- Touch gestures go through CDP `Input.dispatchTouchEvent`.
- Install: `pip install -e '.[browser]'` and `playwright install chromium-headless-shell`.

## 8. Deploy

**deploy/install.sh** (`set -euo pipefail`, run as root, idempotent)
1. `apt-get install -y python3 python3-venv python3-pip git rsync sqlite3 sudo`.
2. Create the system user `cassandra` (nologin) if missing.
3. `rsync -a --delete --exclude .venv --exclude data --exclude .superpowers` the repo into `/opt/cassandra` unless it is already running from there. `.git` is kept so updates are `git pull` in `/opt/cassandra`.
4. `mkdir /var/lib/cassandra{,/hf-cache}` and `chown -R cassandra:cassandra`. The venv and code stay root-owned.
5. Create the venv if absent, then `pip install -e '.[model]' --extra-index-url https://download.pytorch.org/whl/cpu`.
6. If `/etc/cassandra.env` is absent (`FIRST_INSTALL=1`):
   - Generate `PASSWORD=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)`.
   - Write `CASSANDRA_DATA`, `CASSANDRA_PORT=8080`, `CASSANDRA_EXPERT_PASSWORD`, `CASSANDRA_MODEL`, `HF_HOME=$DATA_DIR/hf-cache`, `HF_HUB_OFFLINE=0`.
7. Source the env file (`set -a; . /etc/cassandra.env; set +a`). As user `cassandra`, run a python one-liner that calls `AutoImageProcessor.from_pretrained` and `AutoModel.from_pretrained` for `$CASSANDRA_MODEL` to pre-download the model. Then `sed` `HF_HUB_OFFLINE=1` into the env file.
8. `cp deploy/cassandra.service /etc/systemd/system/`, then `install -m 0755 deploy/cassandra-cli /usr/local/bin/cassandra-cli`, then `systemctl daemon-reload`, `enable --now`, `restart`.
9. Print `Cassandra is running at: http://$IP:8080` (from `hostname -I`, falling back to `hostname`). On first install it also prints `Generated expert password: $PW`.

**deploy/cassandra.service**
```ini
[Unit]
Description=Cassandra component identification
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5
[Service]
User=cassandra
Group=cassandra
EnvironmentFile=/etc/cassandra.env
WorkingDirectory=/opt/cassandra
ExecStart=/opt/cassandra/.venv/bin/cassandra serve --host 0.0.0.0
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
```

**deploy/cassandra-cli**
```bash
set -euo pipefail
set -a; . /etc/cassandra.env; set +a
exec sudo -u cassandra --preserve-env=CASSANDRA_DATA,CASSANDRA_PORT,CASSANDRA_EXPERT_PASSWORD,CASSANDRA_MODEL,HF_HOME,HF_HUB_OFFLINE \
  /opt/cassandra/.venv/bin/cassandra "$@"
```

**deploy/backup.sh `<target_dir>`**
- The data dir comes from `$CASSANDRA_DATA`, then the `CASSANDRA_DATA=` line in `/etc/cassandra.env`, then `/var/lib/cassandra`.
- Runs `sqlite3 "$DATA/cassandra.db" ".backup '$TMP/cassandra.db'"` into a `mktemp -d` (with `trap rm`).
- Archives with `tar -czf "$TARGET/cassandra-$(date -u +%F).tar.gz" -C "$TMP" cassandra.db -C "$DATA" photos`.
- Prunes to the newest 8: `ls -1 ... | sort | head -n -8 | xargs -r rm --`.
- Echoes the archive path on stdout.

**tests/test_deploy.py** (module skipped if `bash` is missing)
- `bash -n` syntax checks on install.sh, backup.sh and cassandra-cli.
- The unit file contains `ExecStart=/opt/cassandra/.venv/bin/cassandra serve` and `EnvironmentFile=/etc/cassandra.env`.
- install.sh contains `install -m 0755` and `/usr/local/bin/cassandra-cli`.
- It extracts the single `PASSWORD=` line from install.sh, runs it under `set -euo pipefail`, and asserts the result is 12 characters (a pipefail/SIGPIPE regression check).
- Backup tests (skipped if `sqlite3` is missing) build a fake data dir with `sqlite3 ... "CREATE TABLE t..."` and `photos/A01143_front.jpg`. They check that the archive lists `cassandra.db` and `photos/A01143_front.jpg`, and that 9 pre-existing dummy archives are pruned to 8, with the oldest removed.

## 9. Docs conventions

- **docs/STATUS.md**: starts `# Status` / `Updated: YYYY-MM-DD`, then `## Done` with dated bullets per task (what changed, test-suite counts like "228 passed, 3 skipped"). It records what is done and what comes next, and is updated at the end of every task and every session.
- **docs/HANDOFF.md**: guide for whoever deploys and maintains the app. Numbered sections: 1 What you are taking over, 2 First deployment, 3 Routine operations, 4 What the people do, 5 Milestones, 6 Settled decisions, 7 How to work on the code, 8 Troubleshooting, 9 Backlog.
- **docs/KNOWN-ISSUES.md**: `# Known issues and deferred minors (as of DATE)`. Deferred minors collected from reviews, grouped under `## Behaviour` and so on; none of them block use.
- Also present: `docs/eval/` (dated eval reports and a README) and `docs/superpowers/specs/<date>-<topic>-design.md` plus `docs/superpowers/plans/<date>-<topic>.md`.

**CLAUDE.md** ("Cassandra — cold-start context for agents")
- Sections: What this is / Roles and working agreement / Key decisions / Environment / Human tasks.
- Opening: "Read this first in every new session. If you deploy or maintain the server, read `docs/HANDOFF.md` next. Then `docs/STATUS.md` (what is done, what is next), `docs/KNOWN-ISSUES.md`, and the spec and the plan linked below."
- "Progress: `docs/STATUS.md` — update it at the end of every task and every session."
- "The lead agent ... is **project owner**: specs, task breakdown, review, decisions, human task lists. It does **not** write product code itself (owner's explicit economic decision). Implementation is delegated to cheaper subagents (Sonnet) via `superpowers:subagent-driven-development`, one plan task per subagent, with a review before the next task."
- "Communicate with the owner in Russian. UI strings are German and live only in `cassandra/static/strings.de.json`. Code, comments, README in English."
- "Ask, don't assume: the owner prefers one focused question at a time."
- "Key decisions (do not re-litigate without the owner)"; out of scope: "HTTPS, user accounts", "i18n beyond one JSON".
- Environment rules:
  - Debian 13 trixie, Python 3.13.
  - "On the owner's dev PC port 8080 is taken by his llama-server; run test servers on 8081."
  - Dev setup: `python3 -m venv .venv && .venv/bin/pip install -e .[dev]`, tests with `pytest -q`, run with `CASSANDRA_MODEL=fake .venv/bin/cassandra serve`.
- Git rules:
  - "`main` holds reviewed merges; day-to-day work is on `feature/mvp` (subagent-driven, one commit per task). Push after every completed task".
  - Push over HTTPS via `gh`: `git -c credential.helper= -c credential.helper='!gh auth git-credential' push https://github.com/...`.
  - The SDD ledger lives at `.superpowers/sdd/<plan>/progress.md` (git-ignored).

## 10. cassandra/images.py, reusable for photo storage

```python
class BadImage(ValueError): pass

def normalize(data: bytes, max_side: int = 1024, crop: bool = False, margin: float = 0.08) -> bytes:
    try:
        image = Image.open(io.BytesIO(data)); image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise BadImage(str(e)) from e
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")
    # (optional crop via find_object_bbox when crop=True)
    width, height = image.size
    long_side = max(width, height)
    if long_side > max_side:
        scale = max_side / long_side
        image = image.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()

def save_photo(photos_dir: Path, subdir: str, data: bytes) -> str:
    rel_path = f"{subdir}/{uuid.uuid4().hex}.jpg"
    full_path = photos_dir / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(data)
    return rel_path

def load(photos_dir: Path, rel_path: str) -> Image.Image:
    image = Image.open(photos_dir / rel_path); image.load()
    return image.convert("RGB")
```
- `find_object_bbox(image, threshold=60, min_fill=0.02, max_fill=0.90)` takes the median colour of a 4% border band as the background, applies a `MedianFilter(5)` to the mask, and returns `(l, t, r, b)` or `None`.
- The DB stores `rel_path` (e.g. `A01143/3f2a....jpg`). The API turns it into the URL `f"/photos/{path}"`, and `/photos` is served by the `StaticFiles` mount.
- An API route maps `BadImage` to `HTTPException(400, t("error.bad_image"))`.
- Tests check EXIF orientation via `make_image(orientation=6)`.