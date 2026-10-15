"""Versioned read API for tenders: /api/v1 (mounted by dashboard.py).

Payloads are explicit Pydantic models; interactive docs at /api/v1/docs.
Auth: if API_KEY is set in .env, every request needs an X-API-Key header.
"""

import csv
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
TENDERS = ROOT / "TENDERS"

SORT_CHOICES = Literal[
    "closing_at_asc",
    "closing_at_desc",
    "published_desc",
    "published_asc",
    "updated_desc",
]
STATUS_CHOICES = Literal["seen", "downloading", "downloaded", "failed", "extracted"]
LIFECYCLE_CHOICES = Literal["open", "closing_soon", "expired", "undated"]


def check_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    import db

    db._load_env()
    key = os.environ.get("API_KEY", "").strip()
    if key and x_api_key != key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


api = FastAPI(
    title="tender-dnl API",
    version="1.0.0",
    description="Read access to Tamil Nadu GePNIC tenders, corrigenda, and packet files.",
    dependencies=[Depends(check_api_key)],
)


# ---------------------------------------------------------------- models

class ListMeta(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int


class TenderSummary(BaseModel):
    tender_id: str
    tender_reference: str | None = None
    title: str | None = None
    organisation: str | None = None
    department: str | None = None
    status: str | None = None
    lifecycle: str | None = None
    published_at: str | None = None
    closing_at: str | None = None
    opening_at: str | None = None
    tender_fee_inr: float | None = None
    emd_inr: float | None = None
    work_value_inr: float | None = None
    has_corrigendum: bool = False


class TenderListResponse(BaseModel):
    data: list[TenderSummary]
    meta: ListMeta


class CorrigendumItem(BaseModel):
    serial: str | None = None
    title: str | None = None
    corr_type: str | None = None
    description: str | None = None
    published: str | None = None
    revised_dates: dict = {}
    original_dates: dict = {}
    pdf_url: str | None = None
    pdf_missing: bool = True


class TenderDetail(BaseModel):
    tender_id: str
    tender_reference: str | None = None
    title: str | None = None
    organisation: str | None = None
    department: str | None = None
    status: str | None = None
    lifecycle: str | None = None
    published_at: str | None = None
    closing_at: str | None = None
    opening_at: str | None = None
    tender_fee_inr: float | None = None
    emd_inr: float | None = None
    work_value_inr: float | None = None
    has_corrigendum: bool = False
    fields: dict = {}
    field_sources: dict = {}
    corrigenda: list[CorrigendumItem] = []
    files: list[dict] = []
    downloaded_at: str | None = None
    error: str | None = None


class CorrigendaResponse(BaseModel):
    tender_id: str
    count: int
    summary: str | None = None
    changes: list[dict] = []
    corrigenda: list[CorrigendumItem] = []


class FileListingResponse(BaseModel):
    tender_id: str
    title: str | None = None
    count: int
    files: list[dict]


class DepartmentStat(BaseModel):
    department: str
    total: int
    open: int
    closing_soon: int
    expired: int
    downloaded: int


class DepartmentsResponse(BaseModel):
    data: list[DepartmentStat]


class StatsResponse(BaseModel):
    data: dict


class SearchResponse(BaseModel):
    data: list[dict]
    meta: dict
    facets: dict


# ---------------------------------------------------------------- helpers

def _department_of(organisation: str | None) -> str | None:
    if not organisation or not organisation.strip():
        return None
    return organisation.split("||")[0].strip() or "Unknown"


def _money(fields: dict, key: str) -> float | None:
    value = fields.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _lifecycle_of(closing_at: str | None) -> str | None:
    if not closing_at:
        return None
    try:
        closing = datetime.fromisoformat(str(closing_at))
    except ValueError:
        return None
    if closing.tzinfo is None:
        return None
    now = datetime.now(timezone.utc)
    if closing <= now:
        return "expired"
    if closing <= now + timedelta(hours=72):
        return "closing_soon"
    return "open"


def _summary(record: dict) -> dict:
    fields = (record.get("details") or {}).get("fields") or {}
    lifecycle = record.get("lifecycle") or _lifecycle_of(record.get("closing_at"))
    return {
        "tender_id": record.get("tender_id"),
        "tender_reference": fields.get("tender_reference"),
        "title": fields.get("title") or record.get("title"),
        "organisation": record.get("organisation"),
        "department": _department_of(record.get("organisation")),
        "status": record.get("status"),
        "lifecycle": lifecycle,
        "published_at": record.get("published_at"),
        "closing_at": record.get("closing_at"),
        "opening_at": record.get("opening_at"),
        "tender_fee_inr": _money(fields, "tender_fee_inr"),
        "emd_inr": _money(fields, "emd_inr"),
        "work_value_inr": _money(fields, "work_value_inr"),
        "has_corrigendum": bool(record.get("corrigendum_count")),
    }


def _packet_files(tender_id: str) -> list[dict]:
    folder = (TENDERS / tender_id).resolve()
    if not (folder.is_dir() and TENDERS.resolve() in folder.parents):
        return []
    files = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(folder).as_posix()
        files.append(
            {
                "name": relative,
                "url": f"/api/v1/tenders/{quote(tender_id)}/files/{quote(relative)}",
            }
        )
    return files


def _corrigendum_items(record: dict) -> list[dict]:
    pdf_by_serial: dict[str, str] = {}
    for phase in ((record.get("details") or {}).get("files") or {}).get("corrigendum") or []:
        serial = str(phase.get("serial") or "")
        file_rel = phase.get("file")
        if serial and file_rel:
            pdf_by_serial[serial] = file_rel
    items = []
    for item in record.get("corrigenda") or []:
        serial = str(item.get("serial") or "")
        file_rel = pdf_by_serial.get(serial)
        items.append(
            {
                "serial": item.get("serial"),
                "title": item.get("title"),
                "corr_type": item.get("corr_type"),
                "description": item.get("description"),
                "published": item.get("published"),
                "revised_dates": item.get("revised_dates") or {},
                "original_dates": item.get("original_dates") or {},
                "pdf_url": (
                    f"/api/v1/tenders/{quote(record['tender_id'])}/files/{quote(file_rel)}"
                    if file_rel
                    else None
                ),
                "pdf_missing": not bool(file_rel),
            }
        )
    return items


# ---------------------------------------------------------------- endpoints

@api.get("/tenders", response_model=TenderListResponse)
def list_tenders(
    q: str | None = None,
    status: STATUS_CHOICES | None = None,
    lifecycle: LIFECYCLE_CHOICES | None = None,
    department: str | None = None,
    published_from: str | None = None,
    published_to: str | None = None,
    closing_from: str | None = None,
    closing_to: str | None = None,
    opening_from: str | None = None,
    opening_to: str | None = None,
    sort: SORT_CHOICES = "closing_at_asc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    import db

    result = db.search_tenders(
        published_from=published_from or None,
        published_to=published_to or None,
        closing_from=closing_from or None,
        closing_to=closing_to or None,
        opening_from=opening_from or None,
        opening_to=opening_to or None,
        lifecycle=lifecycle,
        department=department or None,
        status=status,
        q=q or None,
        sort=sort,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    total = result.get("total") or 0
    return {
        "data": [_summary(record) for record in result.get("tenders") or []],
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, -(-total // page_size)),
        },
    }


@api.get("/tenders/{tender_id}", response_model=TenderDetail)
def tender_detail(tender_id: str) -> dict:
    import db

    record = db.fetch_one(tender_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown tender")
    out = _summary(record)
    out.update(
        {
            "fields": (record.get("details") or {}).get("fields") or {},
            "field_sources": (record.get("details") or {}).get("field_sources") or {},
            "corrigenda": _corrigendum_items(record),
            "files": _packet_files(tender_id),
            "downloaded_at": record.get("downloaded_at"),
            "error": record.get("error"),
        }
    )
    return out


@api.get("/tenders/{tender_id}/corrigenda", response_model=CorrigendaResponse)
def tender_corrigenda(tender_id: str) -> dict:
    import db

    record = db.fetch_one(tender_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown tender")
    corr_view = (record.get("details") or {}).get("corrigendum") or {}
    return {
        "tender_id": tender_id,
        "count": record.get("corrigendum_count") or len(record.get("corrigenda") or []),
        "summary": corr_view.get("summary"),
        "changes": corr_view.get("changes") or [],
        "corrigenda": _corrigendum_items(record),
    }


@api.get("/tenders/{tender_id}/files", response_model=FileListingResponse)
def tender_file_listing(tender_id: str) -> dict:
    import db

    record = db.fetch_one(tender_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown tender")
    files = _packet_files(tender_id)
    return {
        "tender_id": tender_id,
        "title": record.get("title"),
        "count": len(files),
        "files": files,
    }


@api.get("/tenders/{tender_id}/files/{file_path:path}")
def tender_file(tender_id: str, file_path: str) -> FileResponse:
    folder = (TENDERS / tender_id).resolve()
    target = (folder / file_path).resolve()
    if folder not in target.parents and target != folder:
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target)


@api.get("/search", response_model=SearchResponse)
def file_search(
    q: str = "",
    include_expired: bool = False,
    lifecycle: LIFECYCLE_CHOICES | None = None,
    extension: str | None = None,
    page: int = Query(default=1, ge=1),
) -> dict:
    import search

    try:
        result = search.search_files(
            q,
            include_expired=include_expired,
            lifecycle=lifecycle,
            extension=extension or None,
            page=page,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "data": result.get("groups") or [],
        "meta": {"found": result.get("found") or 0, "page": result.get("page") or page},
        "facets": result.get("facets") or {},
    }


@api.get("/departments", response_model=DepartmentsResponse)
def departments() -> dict:
    import db

    return {"data": db.list_departments()}


@api.get("/stats", response_model=StatsResponse)
def stats() -> dict:
    import db

    return {"data": db.counts()}


@api.get("/export")
def export(status: str = "downloaded", format: Literal["xlsx", "csv"] = "xlsx") -> Response:
    from dashboard import export_table

    rows = export_table(status or None)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for row in rows:
            writer.writerow(["" if cell is None else cell for cell in row])
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="tenders.csv"'},
        )
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Tenders"
    for row in rows:
        sheet.append(
            [
                cell if isinstance(cell, (int, float, str)) or cell is None else str(cell)
                for cell in row
            ]
        )
    sheet.freeze_panes = "A2"
    out = io.BytesIO()
    book.save(out)
    return Response(
        content=out.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="tenders.xlsx"'},
    )