-- galochka schema v1. See docs/specs/2026-11-01-galochka-design.md (Data model).
-- Timestamps are ISO-8601 UTC TEXT. Booleans are INTEGER 0/1. JSON columns are TEXT.
-- Actors ('created_by', 'withheld_by', audit.actor) are 'expert' or 'worker:<id>'.

-- Item catalog. Same columns as Cassandra so import_items can be copied verbatim;
-- `source` records where the row last came from. Rows are never deleted, only
-- deactivated (templates and job lines reference them).
CREATE TABLE items (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  group_name TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  versions TEXT NOT NULL DEFAULT '',
  drawing_revision TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'cassandra' CHECK (source IN ('cassandra','csv'))
);

-- Persistent cache of Cassandra item detail (reference photos). No FK: ids may
-- be cached before the item is synced. ok=0 is a negative entry (fetch failed).
CREATE TABLE cassandra_cache (
  item_id TEXT PRIMARY KEY,
  json TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL,
  ok INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE workers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- A worker has at most one active (non-revoked) badge; revoked rows are kept as history.
CREATE TABLE badges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  short_code TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE UNIQUE INDEX badges_short_code_active ON badges(short_code) WHERE revoked_at IS NULL;
CREATE INDEX badges_worker ON badges(worker_id);

CREATE TABLE sessions (
  token TEXT PRIMARY KEY,
  worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE INDEX sessions_worker ON sessions(worker_id);

-- second_check: NULL = follow settings.second_check_default, 0/1 = override.
CREATE TABLE templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_item_id TEXT REFERENCES items(id),
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  second_check INTEGER CHECK (second_check IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- quality_points: JSON array; '[]' = use settings.default_quality_points at job creation.
-- photo_item_id: show this item's Cassandra photos instead of item_id's.
CREATE TABLE template_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  item_id TEXT REFERENCES items(id),
  label TEXT NOT NULL DEFAULT '',
  instruction TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 1,
  photo_required INTEGER NOT NULL DEFAULT 1,
  serial_required INTEGER NOT NULL DEFAULT 0,
  quality_points TEXT NOT NULL DEFAULT '[]',
  photo_item_id TEXT REFERENCES items(id),
  UNIQUE (template_id, position)
);

-- One job = one box. Delivery: open, packing, packed, checked, shipped, cancelled.
-- RMA: intake, repair, outgoing (then packed, checked, shipped), cancelled.
-- template_name is a snapshot; template_id is RESTRICT so used templates are deactivated, not deleted.
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('delivery','rma')),
  template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE RESTRICT,
  template_name TEXT NOT NULL DEFAULT '',
  order_ref TEXT NOT NULL DEFAULT '',
  customer TEXT NOT NULL DEFAULT '',
  unit_no INTEGER NOT NULL DEFAULT 1,
  unit_count INTEGER NOT NULL DEFAULT 1,
  ad_hoc INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN
    ('open','packing','packed','checked','shipped','cancelled','intake','repair','outgoing')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  claimed_by INTEGER REFERENCES workers(id),
  claimed_at TEXT NOT NULL DEFAULT '',
  stage_changed_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX jobs_rma_order_ref ON jobs(order_ref) WHERE kind = 'rma';
CREATE INDEX jobs_status ON jobs(status);
CREATE INDEX jobs_order_ref ON jobs(order_ref);
CREATE INDEX jobs_template ON jobs(template_id);

-- Snapshot of the template lines at job creation (template edits never touch existing jobs).
-- template_line_id has no FK on purpose: template lines may be replaced afterwards.
-- quality_points is the resolved JSON array. source_line_id links an outgoing line to its intake line.
CREATE TABLE job_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  template_line_id INTEGER,
  stage TEXT NOT NULL DEFAULT 'pack' CHECK (stage IN ('pack','intake','outgoing')),
  position INTEGER NOT NULL,
  item_id TEXT REFERENCES items(id),
  label TEXT NOT NULL DEFAULT '',
  instruction TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 1,
  photo_required INTEGER NOT NULL DEFAULT 1,
  serial_required INTEGER NOT NULL DEFAULT 0,
  quality_points TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT 'template' CHECK (source IN ('template','office','worker')),
  withheld INTEGER NOT NULL DEFAULT 0 CHECK (withheld IN (0,1)),
  withheld_reason TEXT NOT NULL DEFAULT '',
  withheld_by TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  source_line_id INTEGER REFERENCES job_lines(id) ON DELETE SET NULL,
  UNIQUE (job_id, stage, position)
);

-- Append-only evidence. "Current" confirmation of a line = the row with the highest id.
-- serials: JSON array; quality: JSON object {point: 'ok'|'not_ok'};
-- photo_path is relative to the photos dir (jobs/<job_id>/<uuid>.jpg), '' = no photo.
CREATE TABLE confirmations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_line_id INTEGER NOT NULL REFERENCES job_lines(id) ON DELETE CASCADE,
  worker_id INTEGER NOT NULL REFERENCES workers(id),
  attempt INTEGER NOT NULL DEFAULT 1,
  result TEXT NOT NULL CHECK (result IN ('ok','missing','not_ok')),
  qty INTEGER NOT NULL DEFAULT 0,
  serials TEXT NOT NULL DEFAULT '[]',
  quality TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  photo_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX confirmations_line ON confirmations(job_line_id, id);
CREATE INDEX confirmations_worker ON confirmations(worker_id);

-- replaced/kept refer to an intake line; added has no line.
CREATE TABLE repair_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  job_line_id INTEGER REFERENCES job_lines(id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (action IN ('replaced','kept','added')),
  item_id TEXT REFERENCES items(id),
  label TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 1,
  serial TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  worker_id INTEGER NOT NULL REFERENCES workers(id),
  created_at TEXT NOT NULL,
  CHECK ((action IN ('replaced','kept') AND job_line_id IS NOT NULL)
      OR (action = 'added' AND job_line_id IS NULL))
);
CREATE INDEX repair_notes_job ON repair_notes(job_id);

-- Second check, append-only. A rejection needs a note, an approval needs the box photo.
CREATE TABLE checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  worker_id INTEGER NOT NULL REFERENCES workers(id),
  result TEXT NOT NULL CHECK (result IN ('ok','rejected')),
  note TEXT NOT NULL DEFAULT '',
  photo_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  CHECK (result <> 'rejected' OR note <> ''),
  CHECK (result <> 'ok' OR photo_path <> '')
);
CREATE INDEX checks_job ON checks(job_id, id);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO settings(key, value) VALUES
  ('second_check_default', '1'),
  ('session_minutes', '30'),
  ('default_quality_points', '["sauber","unbeschädigt","Funktion geprüft"]'),
  ('items_synced_at', '');

CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_entity ON audit(entity, entity_id);
CREATE INDEX audit_at ON audit(at);
