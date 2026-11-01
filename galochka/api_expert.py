import json
import math
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from galochka import importer, qr, repo
from galochka.api import _now, _read_upload, db_write, t
from galochka.audit import audit
from galochka.auth import require_expert
from galochka.cassandra_client import CassandraUnavailable

router = APIRouter(prefix="/api/expert", dependencies=[Depends(require_expert)])

_DEFAULT_QUALITY_POINTS = ["sauber", "unbeschädigt", "funktion"]


class SettingsRequest(BaseModel):
    second_check_default: bool
    session_minutes: int = Field(ge=1, le=720)
    default_quality_points: list[str] = Field(max_length=20)

    @field_validator("default_quality_points")
    @classmethod
    def _points_valid(cls, points: list[str]) -> list[str]:
        cleaned = [p.strip() for p in points]
        if any(not p or len(p) > 60 for p in cleaned):
            raise ValueError("quality points must be non-empty strings of at most 60 characters")
        # Points are used as keys of confirmations.quality, so they must be unique.
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("quality points must be unique")
        return cleaned


def _read_settings(conn) -> dict:
    try:
        points = json.loads(repo.get_setting(conn, "default_quality_points", "[]"))
    except ValueError:
        points = list(_DEFAULT_QUALITY_POINTS)
    try:
        minutes = int(repo.get_setting(conn, "session_minutes", "30"))
    except ValueError:
        minutes = 30
    return {
        "second_check_default": repo.get_setting(conn, "second_check_default", "1") == "1",
        "session_minutes": minutes,
        "default_quality_points": points,
    }


@router.get("/settings")
def get_settings(request: Request):
    with request.app.state.lock:
        return _read_settings(request.app.state.conn)


@router.put("/settings", status_code=204)
def put_settings(body: SettingsRequest, request: Request):
    conn = request.app.state.conn
    with request.app.state.lock:
        try:
            repo.set_setting(conn, "second_check_default", "1" if body.second_check_default else "0", commit=False)
            repo.set_setting(conn, "session_minutes", str(body.session_minutes), commit=False)
            repo.set_setting(
                conn,
                "default_quality_points",
                json.dumps(body.default_quality_points, ensure_ascii=False),
                commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return Response(status_code=204)


def run_items_sync(app: FastAPI, actor: str) -> dict:
    """Sync the item catalog from Cassandra and write the audit row.

    The HTTP part runs outside the lock, the DB part inside. Raises CassandraUnavailable
    (also when no URL is configured). Used by the expert button and the daily thread.
    """
    catalog = importer.fetch_catalog(app.state.cassandra)
    now = _now()
    with app.state.lock:
        result = importer.apply_catalog(app.state.conn, catalog, now)
        audit(app.state.conn, now, actor, "items.sync", "items", "", result)
    return result


@router.get("/items/stats")
def items_stats(request: Request):
    conn = request.app.state.conn
    with request.app.state.lock:
        total = conn.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
        synced_at = repo.get_setting(conn, "items_synced_at", "") or None
    return {
        "total_active": total,
        "synced_at": synced_at,
        "cassandra_url": request.app.state.settings.cassandra_url,
    }


@router.post("/sync/items")
def sync_items(request: Request):
    if not request.app.state.settings.cassandra_url:
        raise HTTPException(status_code=409, detail=t("error.cassandra_not_configured"))
    try:
        return run_items_sync(request.app, "expert")
    except CassandraUnavailable:
        raise HTTPException(status_code=502, detail=t("error.cassandra_unreachable"))


@router.post("/import/items")
def import_items(request: Request, file: UploadFile = File(...)):
    data = _read_upload(file)
    now = _now()
    conn = request.app.state.conn
    with request.app.state.lock:
        try:
            result = importer.import_items(conn, data, now)
        except importer.ImportError_:
            raise HTTPException(status_code=400, detail=t("error.bad_csv"))
        audit(conn, now, "expert", "items.import", "items", "", dict(result, filename=file.filename or ""))
    return result


# --- workers and badges ---------------------------------------------------------------------

WorkerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class WorkerCreate(BaseModel):
    name: WorkerName


class WorkerUpdate(BaseModel):
    name: WorkerName | None = None
    active: bool | None = None


def _worker_json(row) -> dict:
    """Listing shape. Never contains the badge token."""
    badge = None
    if row["badge_short_code"] is not None:
        badge = {"short_code": row["badge_short_code"], "created_at": row["badge_created_at"]}
    return {
        "id": row["id"],
        "name": row["name"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "badge": badge,
    }


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=t("error.not_found"))


@router.get("/workers")
def list_workers(request: Request):
    with request.app.state.lock:
        rows = repo.list_workers(request.app.state.conn)
    return [_worker_json(r) for r in rows]


@router.post("/workers", status_code=201)
def create_worker(body: WorkerCreate, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        try:
            worker_id = repo.create_worker(conn, body.name, now, commit=False)
            badge_id, token, _ = repo.issue_badge(conn, worker_id, now, commit=False)
            audit(conn, now, "expert", "worker.create", "worker", worker_id, {"name": body.name})
            audit(conn, now, "expert", "badge.issue", "worker", worker_id, {"badge_id": badge_id})
        except Exception:
            conn.rollback()
            raise
        result = _worker_json(repo.get_worker(conn, worker_id))
    # The token is shown here once; later only the printable badge SVG carries it.
    result["token"] = token
    return result


@router.patch("/workers/{worker_id}")
def update_worker(worker_id: int, body: WorkerUpdate, request: Request):
    conn = request.app.state.conn
    changes = body.model_dump(exclude_none=True)
    with request.app.state.lock:
        try:
            repo.update_worker(conn, worker_id, name=body.name, active=body.active, commit=False)
        except KeyError:
            conn.rollback()
            raise _not_found()
        except Exception:
            conn.rollback()
            raise
        if changes:
            audit(conn, _now(), "expert", "worker.update", "worker", worker_id, changes)
        else:
            conn.commit()
        return _worker_json(repo.get_worker(conn, worker_id))


@router.post("/workers/{worker_id}/badge", status_code=201)
def reissue_badge(worker_id: int, request: Request):
    """Issue a new badge: the old one is revoked (and the worker logged out) in the same step."""
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        try:
            revoked = repo.revoke_badges(conn, worker_id, now, commit=False)
            badge_id, token, short_code = repo.issue_badge(conn, worker_id, now, commit=False)
        except KeyError:
            conn.rollback()
            raise _not_found()
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "badge.issue", "worker", worker_id, {"badge_id": badge_id, "revoked": revoked})
    return {"short_code": short_code, "token": token}


@router.delete("/workers/{worker_id}/badge", status_code=204)
def revoke_badge(worker_id: int, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        if repo.get_worker(conn, worker_id) is None:
            raise _not_found()
        try:
            revoked = repo.revoke_badges(conn, worker_id, now, commit=False)
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "badge.revoke", "worker", worker_id, {"revoked": revoked})
    return Response(status_code=204)


@router.get("/workers/{worker_id}/badge.svg")
def badge_svg(worker_id: int, request: Request):
    with request.app.state.lock:
        badge = repo.get_active_badge(request.app.state.conn, worker_id)
    if badge is None or not badge["token"]:
        raise _not_found()
    svg = qr.make_svg(f"galochka:badge:{badge['token']}")  # rendering outside the lock
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


# --- templates ------------------------------------------------------------------------------


class TemplateLineIn(BaseModel):
    """One template line. The limits live in repo.clean_line (shared with the repo layer)."""

    id: int | None = None
    item_id: str | None = None
    label: str = ""
    instruction: str = ""
    qty: int = 1
    photo_required: bool = True
    serial_required: bool = False
    quality_points: list[str] = []
    photo_item_id: str | None = None

    @model_validator(mode="after")
    def _limits(self):
        repo.clean_line(self.model_dump())  # raises ValueError -> 422
        return self


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=repo.MAX_NAME)
    product_item_id: str | None = None
    second_check: bool | None = None
    lines: list[TemplateLineIn] = Field(default_factory=list, max_length=500)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, name: str) -> str:
        return repo.clean_template_name(name)


class TemplateUpdate(TemplateCreate):
    active: bool | None = None  # None = keep the current flag


def _value_error(e: ValueError) -> HTTPException:
    """Map a repo ValueError (bad reference or value in a template payload) to 422."""
    message = str(e)
    if message.startswith("unknown_item:"):
        return HTTPException(status_code=422, detail=f"{t('error.unknown_item')} {message.split(':', 1)[1]}")
    if message == "label_required":
        return HTTPException(status_code=422, detail=t("error.label_required"))
    return HTTPException(status_code=422, detail=t("error.invalid_request"))


def _line_dicts(body: TemplateCreate) -> list[dict]:
    return [line.model_dump() for line in body.lines]


@router.get("/templates")
def list_templates(request: Request, show_all: int = Query(0, alias="all")):
    with request.app.state.lock:
        return repo.list_templates(request.app.state.conn, active_only=not show_all)


@router.post("/templates", status_code=201)
def create_template(body: TemplateCreate, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        try:
            template_id = repo.create_template(
                conn, body.name, body.product_item_id, body.second_check, _line_dicts(body), now, commit=False
            )
        except ValueError as e:
            conn.rollback()
            raise _value_error(e)
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "template.create", "template", template_id,
              {"name": body.name, "lines": len(body.lines)})
        return repo.get_template(conn, template_id)


@router.get("/templates/{template_id}")
def get_template(template_id: int, request: Request):
    with request.app.state.lock:
        template = repo.get_template(request.app.state.conn, template_id)
    if template is None:
        raise _not_found()
    return template


@router.put("/templates/{template_id}")
def put_template(template_id: int, body: TemplateUpdate, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        current = repo.get_template(conn, template_id)
        if current is None:
            raise _not_found()
        active = current["active"] if body.active is None else body.active
        try:
            repo.replace_template(
                conn, template_id, body.name, body.product_item_id, active, body.second_check,
                _line_dicts(body), now, commit=False,
            )
        except ValueError as e:
            conn.rollback()
            raise _value_error(e)
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "template.replace", "template", template_id,
              {"name": body.name, "lines": len(body.lines), "active": active})
        return repo.get_template(conn, template_id)


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: int, request: Request):
    conn = request.app.state.conn
    with request.app.state.lock:
        template = repo.get_template(conn, template_id)
        if template is None:
            raise _not_found()
        try:
            repo.delete_template(conn, template_id, commit=False)
        except ValueError:  # template_in_use
            conn.rollback()
            raise HTTPException(status_code=409, detail=t("error.template_in_use"))
        except Exception:
            conn.rollback()
            raise
        audit(conn, _now(), "expert", "template.delete", "template", template_id, {"name": template["name"]})
    return Response(status_code=204)


@router.post("/templates/{template_id}/duplicate", status_code=201)
def duplicate_template(template_id: int, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        try:
            new_id = repo.duplicate_template(conn, template_id, now, commit=False)
        except KeyError:
            conn.rollback()
            raise _not_found()
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "template.duplicate", "template", new_id, {"source_id": template_id})
        return repo.get_template(conn, new_id)


# --- BOM import -----------------------------------------------------------------------------

def _is_true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_false(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


def _bom_qty(qty: float) -> int:
    """Template quantity of a BOM quantity: integral stays, a fraction rounds up; 1..999."""
    whole = int(qty) if qty == int(qty) else math.ceil(qty)
    return min(max(whole, 1), repo.MAX_QTY)


def _bom_label(row: importer.BomRow, known_name: str) -> str:
    label = row.label or known_name or row.item_id
    if row.version:
        label = f"{label} \u00b7 Version {row.version}"
    return label[: repo.MAX_NAME]


@router.post("/import/bom")
def import_bom(
    request: Request,
    file: UploadFile = File(...),
    dry_run: str | None = Form(None),
    dry_run_query: str | None = Query(None, alias="dry_run"),
    name: str = Form(""),
    product_item_id: str = Form(""),
    include_zero_qty: str = Form("0"),
    mode: str = Form("new"),
):
    data = _read_upload(file)
    try:
        bom = importer.parse_bom(data)  # pure CPU work, outside the lock
    except importer.ImportError_:
        raise HTTPException(status_code=400, detail=t("error.bad_csv"))
    # Dry run is the default; the form field wins over the ?dry_run= query parameter of the spec.
    # Only an explicit "0"/"false" writes anything.
    dry = not _is_false(dry_run if dry_run is not None else dry_run_query if dry_run_query is not None else "1")
    include_zero = _is_true(include_zero_qty)
    mode = mode.strip() or "new"
    replace_id = None
    if mode != "new":
        head, _, tail = mode.partition(":")
        if head != "replace" or not tail.isdigit():
            raise HTTPException(status_code=422, detail=t("error.invalid_request"))
        replace_id = int(tail)

    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        names = repo.item_names(conn, [r.item_id for r in bom.rows] + [bom.product_item_id])
        product_known = bom.product_item_id in names
        form_product = product_item_id.strip()
        chosen_product = form_product or (bom.product_item_id if product_known else None) or None
        lines = []
        for row in bom.rows:
            lines.append(
                {
                    "position": row.position,
                    "item_id": row.item_id,
                    "item_name": names.get(row.item_id, ""),
                    "label": row.label,
                    "version": row.version,
                    "qty": int(row.qty) if row.qty == int(row.qty) else row.qty,
                    "notes": row.notes,
                    "known": row.item_id in names,
                    "skipped_zero_qty": row.qty == 0 and not include_zero,
                }
            )
        taken = [ln for ln in lines if not ln["skipped_zero_qty"]]
        result = {
            "bom_number": bom.bom_number,
            "bom_name": bom.bom_name,
            "product_item_id": bom.product_item_id,
            "product_name": bom.product_name,
            "product_known": product_known,
            "lines": lines,
            "unknown_items": [ln["item_id"] for ln in taken if not ln["known"]],
            "existing_template_id": repo.find_template_for_bom(conn, bom.bom_name, chosen_product),
            "template_id": None,
        }
        if dry:
            return result

        template_lines = [
            {
                "item_id": ln["item_id"] if ln["known"] else None,
                "label": _bom_label(row, ln["item_name"]),
                "instruction": ln["notes"][: repo.MAX_INSTRUCTION],
                "qty": _bom_qty(ln["qty"]),
                "photo_required": True,
                "serial_required": False,
                "quality_points": [],
            }
            for ln, row in zip(lines, bom.rows)
            if not ln["skipped_zero_qty"]
        ]
        try:
            if replace_id is None:
                template_name = (name.strip() or bom.bom_name or bom.bom_number)[: repo.MAX_NAME]
                template_id = repo.create_template(
                    conn, template_name, chosen_product, None, template_lines, now, commit=False
                )
            else:
                current = repo.get_template(conn, replace_id)
                if current is None:
                    raise _not_found()
                template_id = replace_id
                repo.replace_template(
                    conn, replace_id,
                    name.strip() or current["name"],
                    form_product or current["product_item_id"],
                    current["active"], current["second_check"], template_lines, now, commit=False,
                )
        except ValueError as e:
            conn.rollback()
            raise _value_error(e)
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        audit(conn, now, "expert", "bom.import", "template", template_id, {
            "bom_number": bom.bom_number, "mode": "new" if replace_id is None else "replace",
            "lines": len(template_lines), "unknown_items": len(result["unknown_items"]),
            "filename": file.filename or "",
        })
        result["template_id"] = template_id
        return result


# --- jobs -----------------------------------------------------------------------------------


class JobCustomisation(BaseModel):
    """Office customisation: withhold a template line, or add an extra line (see repo.create_jobs)."""

    template_line_id: int | None = None
    withheld: bool | None = None
    item_id: str | None = Field(default=None, max_length=80)
    label: str | None = Field(default=None, max_length=repo.MAX_NAME)
    qty: int | None = Field(default=None, ge=1, le=repo.MAX_QTY)
    reason: str = Field(default="", max_length=repo.MAX_REASON)


class ExpertJobCreate(BaseModel):
    kind: Literal["delivery", "rma"] = "delivery"
    template_id: int
    order_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=repo.MAX_ORDER_REF)]
    customer: Annotated[str, StringConstraints(strip_whitespace=True, max_length=repo.MAX_CUSTOMER)] = ""
    count: int = Field(default=1, ge=1, le=repo.MAX_UNITS)
    customisations: list[JobCustomisation] = Field(default_factory=list, max_length=repo.MAX_CUSTOMISATIONS)
    note: str = Field(default="", max_length=500)


class JobStatusUpdate(BaseModel):
    status: Literal[repo.JOB_STATUSES]
    note: str = Field(default="", max_length=500)


@router.post("/jobs", status_code=201)
def create_jobs(body: ExpertJobCreate, request: Request):
    conn = request.app.state.conn
    now = _now()
    customisations = [c.model_dump(exclude_none=True) for c in body.customisations]
    with request.app.state.lock:
        with db_write(conn):
            ids = repo.create_jobs(
                conn, body.kind, body.template_id, body.order_ref, body.customer, body.count, customisations,
                "expert", now, note=body.note, commit=False,
            )
            for job_id in ids:
                audit(conn, now, "expert", "job.create", "job", job_id,
                      {"kind": body.kind, "template_id": body.template_id, "order_ref": body.order_ref,
                       "unit_count": body.count, "customisations": customisations})
        return [repo.job_to_dict(repo.get_job(conn, job_id)) for job_id in ids]


@router.patch("/jobs/{job_id}")
def update_job_status(job_id: int, body: JobStatusUpdate, request: Request):
    conn = request.app.state.conn
    now = _now()
    with request.app.state.lock:
        with db_write(conn):
            old = repo.set_job_status(conn, job_id, body.status, body.note, now, commit=False)
            audit(conn, now, "expert", "job.status", "job", job_id,
                  {"from": old, "to": body.status, "note": body.note})
        return repo.job_to_dict(repo.get_job(conn, job_id))
