"""One process for the tender dashboard, page listing, and downloads."""

import asyncio
import json
import os
import threading
from pathlib import Path
from urllib.parse import quote

import psycopg
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

import db

ROOT = Path(__file__).resolve().parent
TENDERS = ROOT / "TENDERS"
app = FastAPI()
clients: set[WebSocket] = set()
loop: asyncio.AbstractEventLoop | None = None
stop_event = threading.Event()
job_lock = threading.Lock()
job = {
    "phase": "idle",
    "detail": "Idle",
    "page": 0,
    "pages": 0,
    "listed": 0,
}


def job_view() -> dict:
    with job_lock:
        return dict(job)


def set_job(**values) -> None:
    with job_lock:
        job.update(values)
    publish()


def publish() -> None:
    if loop is None:
        return
    message = {
        "type": "tick",
        "job": job_view(),
        "counts": db.counts(),
        "captcha": db.captcha_usage(),
    }
    asyncio.run_coroutine_threadsafe(broadcast(message), loop)


async def broadcast(message: dict) -> None:
    encoded = json.dumps(message)
    stale = []
    for client in list(clients):
        try:
            await client.send_text(encoded)
        except Exception:
            stale.append(client)
    for client in stale:
        clients.discard(client)


def listen() -> None:
    conn = psycopg.connect(db.database_url(), autocommit=True)
    conn.execute("LISTEN tender_status")
    while True:
        for _notice in conn.notifies(timeout=5):
            publish()


ACTIVE = {
    "listing",
    "downloading",
    "indexing",
    "extracting",
    "corr_refresh",
    "stopping",
}


def ensure_idle() -> None:
    if job_view()["phase"] in ACTIVE:
        raise HTTPException(status_code=409, detail="A job is already running.")


def run_list() -> None:
    import app as scraper

    set_job(phase="listing", detail="Opening the closing list", page=0, pages=0, listed=0)

    def progress(page_no: int, last: int, seen: int) -> None:
        set_job(
            page=page_no,
            pages=last,
            listed=seen,
            detail=f"Page {page_no}/{last} · {seen} unique",
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                stats = scraper.scrape_catalog(page, stop_event, progress)
            finally:
                browser.close()
        if isinstance(stats, dict):
            total = int(stats.get("unique") or 0)
            new = int(stats.get("new") or 0)
            rows = int(stats.get("rows") or 0)
            dups = int(stats.get("duplicates") or 0)
            pages = int(stats.get("pages") or 0)
            scanned = int(stats.get("pages_scanned") or pages)
            early = bool(stats.get("early_stop"))
        else:
            total, new, rows, dups, pages, scanned, early = (
                int(stats or 0),
                0,
                0,
                0,
                0,
                0,
                False,
            )
        if stop_event.is_set():
            detail = f"Listing stopped at {total} unique tenders."
        elif rows:
            mode = (
                f"early stop @ {stats.get('stopped_at')}/{pages}"
                if early and isinstance(stats, dict)
                else f"scanned {scanned}/{pages}"
            )
            detail = (
                f"Listed {new} new / {total} unique "
                f"({rows} rows; {mode}; {dups} duplicate IDs on site)."
            )
        else:
            detail = f"Listed {total} tenders."
        set_job(phase="idle", detail=detail, listed=total, page=scanned, pages=pages)
    except Exception as exc:
        set_job(phase="idle", detail=str(exc))


def run_download() -> None:
    import app as scraper

    db.reset_interrupted()
    expired = db.expire_stale()
    userid, apikey = scraper.captcha_credentials()
    workers, worker_reason = scraper.resolve_download_workers()
    queue = db.pending_tenders()
    detail = f"0 of {len(queue)} · {workers} workers ({worker_reason})"
    if expired:
        detail = (
            f"0 of {len(queue)} (skipped {expired} expired) · "
            f"{workers} workers ({worker_reason})"
        )
    print(f"download job: {worker_reason}", flush=True)
    set_job(
        phase="downloading",
        detail=detail,
        page=0,
        pages=len(queue),
        listed=len(queue),
    )

    def on_progress(done: int, error: str | None) -> None:
        set_job(page=done, detail=f"{done} of {len(queue)} · {workers}w")

    try:
        result = scraper.download_pending(
            queue,
            userid,
            apikey,
            workers=workers,
            stop_event=stop_event,
            on_progress=on_progress,
        )
        done = result["done"]
        backoffs = result.get("backoff_events") or 0
        extracted = result.get("extracted") or 0
        extract_errors = result.get("extract_errors") or 0
        extract_note = (
            f" · extracted {extracted}"
            + (f" / {extract_errors} errors" if extract_errors else "")
            if (extracted or extract_errors)
            else ""
        )
        if result.get("fatal"):
            detail = f"Download failed: {result['fatal']}"
        elif result["quota_hit"]:
            detail = "2Captcha balance is empty. Download stopped."
        elif stop_event.is_set():
            detail = (
                f"Download stopped. {done} processed · {workers}w{extract_note}."
            )
        else:
            detail = (
                f"Download finished. {done} tenders · {workers} workers"
                + (f" · {backoffs} backoffs" if backoffs else "")
                + extract_note
                + "."
            )
        set_job(phase="idle", detail=detail, page=done, pages=len(queue))
    except Exception as exc:
        set_job(phase="idle", detail=str(exc))


def run_index() -> None:
    import search

    try:
        queue = [
            tender
            for tender in db.downloaded_tenders()
            if (TENDERS / tender["tender_id"]).is_dir()
        ]
        total = len(queue)
        set_job(
            phase="indexing",
            detail=f"0 of {total}",
            page=0,
            pages=total,
            listed=total,
        )
        done = 0
        files = 0
        for tender in queue:
            if stop_event.is_set():
                break
            files += search.index_tender(tender, stop_event)
            if stop_event.is_set():
                break
            done += 1
            set_job(page=done, detail=f"{done} of {total}")
        if stop_event.is_set():
            detail = f"Indexing stopped. {files} files written."
        else:
            detail = f"Indexed {files} files from {done} tenders."
        set_job(phase="idle", detail=detail, page=done, pages=total)
    except Exception as exc:
        set_job(phase="idle", detail=str(exc))


def run_reextract() -> None:
    """Rebuild field extraction for downloaded tenders from saved files.

    Tenders still on the closing list that never had their detail page parsed
    get a live page parse first (fast fetch lookup); everything else is rebuilt
    offline from BOQ + PDFs.
    """
    import app as scraper
    import extract_pipeline

    try:
        queue = [
            tender
            for tender in db.tenders_for_reextract(only_missing=True)
            if (TENDERS / tender["tender_id"]).is_dir()
        ]
        total = len(queue)
        set_job(phase="extracting", detail=f"0 of {total}", page=0, pages=total, listed=total)

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        needs_page = [
            tender
            for tender in queue
            if not (tender.get("details") or {}).get("page")
            and tender.get("closing_at")
            and datetime.fromisoformat(tender["closing_at"]) > now
        ]
        page_details: dict[str, dict] = {}
        if needs_page:
            set_job(detail=f"Reading detail pages for {len(needs_page)} live tenders")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                browser_page = browser.new_page()
                try:
                    for tender in needs_page:
                        if stop_event.is_set():
                            break
                        tid = tender["tender_id"]
                        try:
                            scraper.open_listed_tender(
                                browser_page,
                                tid,
                                tender.get("list_page") or 1,
                                stop_event=stop_event,
                                closing_at=datetime.fromisoformat(tender["closing_at"]),
                            )
                            page_details[tid] = scraper.parse_tender_detail(browser_page)
                            print(f"reextract {tid}: page fields read", flush=True)
                        except scraper.JobStopped:
                            break
                        except Exception as exc:
                            print(f"reextract {tid}: page parse skipped ({exc})", flush=True)
                finally:
                    browser.close()

        done = 0
        for tender in queue:
            if stop_event.is_set():
                break
            tid = tender["tender_id"]
            try:
                extract_pipeline.reextract_one(tid, page_details.get(tid))
            except Exception as exc:
                print(f"reextract {tid}: {exc}", flush=True)
            done += 1
            set_job(page=done, detail=f"{done} of {total}")
        detail = "Extraction stopped." if stop_event.is_set() else f"Extracted {done} tenders."
        set_job(phase="idle", detail=detail, page=done, pages=total)
    except Exception as exc:
        set_job(phase="idle", detail=str(exc))


def run_corr_refresh() -> None:
    """Re-check open downloaded tenders for new or replaced corrigenda."""
    import app as scraper

    try:
        queue = [
            tender
            for tender in db.tenders_for_corr_refresh()
            if (TENDERS / tender["tender_id"]).is_dir()
        ]
        total = len(queue)
        set_job(
            phase="corr_refresh",
            detail=f"0 of {total}",
            page=0,
            pages=total,
            listed=total,
        )

        def on_progress(done: int, result: dict) -> None:
            tid = (result or {}).get("tender_id") or ""
            note = ""
            if result.get("still_none"):
                note = " · none"
            elif result.get("changed"):
                note = (
                    f" · +{result.get('new', 0)} new"
                    f" / {result.get('updated', 0)} updated"
                )
            elif result.get("error"):
                note = " · error"
            set_job(page=done, detail=f"{done} of {total}{note} {tid}".strip())

        summary = scraper.refresh_corrigenda_pending(
            queue, stop_event=stop_event, on_progress=on_progress
        )
        done = summary.get("done") or 0
        if summary.get("fatal"):
            detail = f"Corrigendum refresh failed: {summary['fatal']}"
        elif stop_event.is_set():
            detail = (
                f"Corrigendum refresh stopped. Checked {done}; "
                f"{summary.get('changed', 0)} changed."
            )
        else:
            detail = (
                f"Corrigendum refresh done. Checked {done}; "
                f"{summary.get('changed', 0)} changed "
                f"({summary.get('new', 0)} new / {summary.get('updated', 0)} updated); "
                f"{summary.get('still_none', 0)} still none."
            )
        set_job(phase="idle", detail=detail, page=done, pages=total)
    except Exception as exc:
        set_job(phase="idle", detail=str(exc))


def start_thread(target, phase: str) -> dict:
    ensure_idle()
    stop_event.clear()
    with job_lock:
        job["phase"] = phase
        job["detail"] = "Starting"
    threading.Thread(target=target, daemon=True).start()
    publish()
    return job_view()


@app.post("/api/job/list")
def start_list() -> dict:
    return start_thread(run_list, "listing")


@app.post("/api/job/download")
def start_download() -> dict:
    return start_thread(run_download, "downloading")


@app.post("/api/job/index")
def start_index() -> dict:
    return start_thread(run_index, "indexing")


@app.post("/api/job/extract")
def start_extract() -> dict:
    return start_thread(run_reextract, "extracting")


@app.post("/api/job/corr_refresh")
def start_corr_refresh() -> dict:
    return start_thread(run_corr_refresh, "corr_refresh")


@app.post("/api/tenders/{tender_id}/reextract")
def reextract_tender(tender_id: str) -> dict:
    import extract_pipeline

    details = extract_pipeline.reextract_one(tender_id)
    if details is None:
        raise HTTPException(status_code=404, detail="Unknown tender or no files on disk")
    return {"fields": details["fields"], "status": details["status"]}


@app.post("/api/tenders/{tender_id}/refresh_corrigenda")
def refresh_one_corrigenda(tender_id: str) -> dict:
    """Refresh corrigenda for a single downloaded tender (blocking)."""
    import app as scraper

    ensure_idle()
    record = db.fetch_one(tender_id)
    if not record or record.get("status") != "downloaded":
        raise HTTPException(status_code=404, detail="Downloaded tender not found")
    if not (TENDERS / tender_id).is_dir():
        raise HTTPException(status_code=404, detail="No packet folder on disk")
    stop_event.clear()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(accept_downloads=True)
        try:
            result = scraper.refresh_corrigenda_one(page, record, stop_event=stop_event)
        finally:
            browser.close()
    return result


@app.post("/api/job/stop")
def stop_job() -> dict:
    stop_event.set()
    if job_view()["phase"] in {
        "listing",
        "downloading",
        "indexing",
        "extracting",
        "corr_refresh",
        "stopping",
    }:
        set_job(phase="stopping", detail="Stopping…")
    return job_view()


EXPORT_COLUMNS = [
    ("S.No", None),
    ("Tender ID", "tender_id"),
    ("Tender Ref No", "tender_reference"),
    ("Organization", "organisation"),
    ("e-Published Date", "published_at"),
    ("Closing Date", "closing_at"),
    ("Opening Date", "opening_at"),
    ("Pre Bid Meeting", "pre_bid_meeting"),
    ("Title", "title"),
    ("Name of the Work", "name_of_work"),
    ("Work Description", "work_description"),
    ("Tender Fee (₹)", "tender_fee_inr"),
    ("EMD (₹)", "emd_inr"),
    ("Work Value (₹)", "work_value_inr"),
    ("Eligibility (%)", "eligibility_percent"),
    ("Eligibility", "eligibility"),
    ("Scope of the Work", "scope_of_work"),
    ("Single/Two Cover", "covers"),
    ("Location", "location"),
    ("Tender Notice", None),
    ("Tender Document", None),
    ("BoQ", None),
    ("Corrigendum", None),
    ("Corrigendum changes", None),
    ("Status", None),
]


def _file_names(entries: list | None) -> str:
    return "; ".join(e.get("name") or "" for e in entries or [] if e.get("name"))


def export_table(status: str | None) -> list[list]:
    rows = [[name for name, _ in EXPORT_COLUMNS]]
    for index, record in enumerate(db.export_rows(status or None), start=1):
        details = record.get("details") or {}
        fields = details.get("fields") or {}
        files = details.get("files") or {}
        corrigenda = record.get("corrigenda") or []

        def value(key: str):
            got = fields.get(key)
            if got in (None, ""):
                got = record.get(key)
            return got

        row = []
        for name, key in EXPORT_COLUMNS:
            if key is not None:
                row.append(value(key))
            elif name == "S.No":
                row.append(index)
            elif name == "Tender Notice":
                row.append(_file_names(files.get("tender_notice")))
            elif name == "Tender Document":
                row.append(_file_names(files.get("tender_document")))
            elif name == "BoQ":
                row.append(_file_names(files.get("boq")))
            elif name == "Corrigendum":
                view = details.get("corrigendum") or {}
                count = view.get("count") or record.get("corrigendum_count") or len(corrigenda)
                row.append("Yes" if count else "No")
            elif name == "Corrigendum changes":
                view = details.get("corrigendum") or {}
                parts = []
                if view.get("summary"):
                    parts.append(view["summary"])
                for change in view.get("changes") or []:
                    label = change.get("label") or change.get("field") or "Change"
                    before = change.get("original") or "—"
                    after = change.get("revised") or "—"
                    parts.append(f"{label}: {before} → {after}")
                row.append(" | ".join(parts))
            elif name == "Status":
                row.append(record.get("status"))
        rows.append(row)
    return rows


@app.get("/api/export.xlsx")
def export_xlsx(status: str | None = "downloaded"):
    import io

    from openpyxl import Workbook
    from fastapi.responses import Response

    book = Workbook()
    sheet = book.active
    sheet.title = "Tenders"
    for row in export_table(status):
        sheet.append(
            [cell if isinstance(cell, (int, float, str)) or cell is None else str(cell) for cell in row]
        )
    sheet.freeze_panes = "A2"
    buffer = io.BytesIO()
    book.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="tenders.xlsx"'},
    )


@app.get("/api/export.csv")
def export_csv(status: str | None = "downloaded"):
    import csv
    import io

    from fastapi.responses import Response

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in export_table(status):
        writer.writerow(["" if cell is None else cell for cell in row])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tenders.csv"'},
    )


@app.get("/api/departments")
def departments() -> dict:
    return {"departments": db.list_departments()}


@app.get("/api/timings")
def timings() -> dict:
    """Per-phase download/extract aggregates from tenders.timings JSONB."""
    return {"timings": db.timing_stats(), "captcha": db.captcha_usage()[:7]}


def _gemini_cost_inr(timings: dict) -> tuple[int, float, float]:
    """Estimated Gemini spend in INR from split token counts.

    Rates are rupees per million tokens, env-overridable:
    GEMINI_COST_IN_PER_MTOK_INR / GEMINI_COST_OUT_PER_MTOK_INR.
    Legacy records (before split tracking) are estimated with the observed
    input share, or 75/25 when nothing is known.
    """
    rate_in = float(os.environ.get("GEMINI_COST_IN_PER_MTOK_INR", "8.8"))
    rate_out = float(os.environ.get("GEMINI_COST_OUT_PER_MTOK_INR", "35.2"))
    t_in = int(timings.get("gemini_tokens_in") or 0)
    t_out = int(timings.get("gemini_tokens_out") or 0)
    t_total = int(timings.get("gemini_tokens") or 0)
    legacy = max(0, t_total - t_in - t_out)
    split_in = t_in / (t_in + t_out) if (t_in + t_out) else 0.75
    cost = (
        t_in * rate_in
        + t_out * rate_out
        + legacy * (split_in * rate_in + (1 - split_in) * rate_out)
    ) / 1_000_000
    return round(cost), rate_in, rate_out


def _typesense_status() -> dict:
    try:
        import search

        info = search.client().collections[search.COLLECTION].retrieve()
        return {
            "ok": True,
            "collection": search.COLLECTION,
            "documents": int(info.get("num_documents") or 0),
            "host": os.environ.get("TYPESENSE_HOST", "127.0.0.1"),
            "port": os.environ.get("TYPESENSE_PORT", "8108"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _postgres_status() -> dict:
    try:
        tallies = db.counts()
        return {
            "ok": True,
            "tenders": int(tallies.get("total") or 0),
            "downloaded": int(tallies.get("downloaded") or 0),
            "extracted": int(tallies.get("extracted") or 0),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


@app.get("/api/usage")
def usage() -> dict:
    """External dependencies and tracked usage (2Captcha, Gemini, infra)."""
    db._load_env()
    timings = db.timing_stats()
    est_cost, rate_in, rate_out = _gemini_cost_inr(timings)
    captcha = db.captcha_totals()
    try:
        import app as scraper

        captcha_mode = scraper.captcha_provider()
        captcha_key = bool(scraper.twocaptcha_api_key())
    except Exception:
        captcha_mode = os.environ.get("CAPTCHA_PROVIDER", "2captcha")
        captcha_key = bool(
            os.environ.get("TWOCAPTCHA_APIKEY")
            or os.environ.get("APIKEY_2CAPTCHA")
            or os.environ.get("TWO_CAPTCHA_API_KEY")
        )
    try:
        from llm_extract import gemini_config

        gemini_key, gemini_model = gemini_config()
    except Exception:
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        gemini_model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"

    zip_captcha = timings.get("zip_captcha") or 0
    zip_reuse = timings.get("zip_reuse") or 0
    zip_total = zip_captcha + zip_reuse
    reuse_pct = round(100 * zip_reuse / zip_total) if zip_total else None

    return {
        "captcha": captcha.get("days") or [],
        "dependencies": [
            {
                "id": "2captcha",
                "kind": "ext",
                "name": "2Captcha",
                "role": "Solves GePNIC ZIP download captchas when the session is cold.",
                "configured": captcha_key,
                "note": f"Provider mode: {captcha_mode}. Metered per image solved.",
                "metrics": [
                    {"label": "Images total", "value": captcha.get("images_total") or 0},
                    {"label": "ZIP captcha", "value": zip_captcha},
                    {"label": "ZIP reuse", "value": zip_reuse},
                    {
                        "label": "Reuse rate",
                        "value": f"{reuse_pct}%" if reuse_pct is not None else "—",
                    },
                ],
            },
            {
                "id": "gemini",
                "kind": "ai",
                "name": "Google Gemini",
                "role": "LLM extraction of closing / opening / fees / eligibility from PDFs.",
                "configured": bool(gemini_key),
                "note": (
                    f"Model: {gemini_model}. Est. rates ₹{rate_in:g}/M input, "
                    f"₹{rate_out:g}/M output tokens (GEMINI_COST_*_PER_MTOK_INR)."
                ),
                "metrics": [
                    {"label": "Tokens", "value": timings.get("gemini_tokens") or 0},
                    {"label": "Calls", "value": timings.get("gemini_calls") or 0},
                    {
                        "label": "Avg LLM ms",
                        "value": timings.get("llm_ms_avg") if timings.get("llm_ms_avg") is not None else "—",
                    },
                    {
                        "label": "Extracts",
                        "value": timings.get("with_extract") or 0,
                    },
                    {"label": "Est. cost (₹)", "value": est_cost},
                ],
            },
            {
                "id": "typesense",
                "kind": "data",
                "name": "Typesense",
                "role": "Full-text search over extracted packet filenames and document text.",
                "configured": bool(os.environ.get("TYPESENSE_API_KEY", "").strip()),
                "note": "Local HTTP search index (tender_files collection).",
                "metrics": _typesense_metrics(_typesense_status()),
            },
            {
                "id": "postgres",
                "kind": "data",
                "name": "PostgreSQL",
                "role": "Canonical tender rows, timings JSONB, corrigenda, captcha usage.",
                "configured": bool(os.environ.get("DATABASE_URL", "").strip()),
                "note": "Source of truth for listing, downloads, and extract status.",
                "metrics": _postgres_metrics(_postgres_status()),
            },
            {
                "id": "playwright",
                "kind": "portal",
                "name": "Playwright",
                "role": "Headless Chromium sessions for listing, detail pages, and ZIP download.",
                "configured": True,
                "note": (
                    f"Browsers path: {os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or 'default'}"
                ),
                "metrics": [
                    {
                        "label": "Lookup avg",
                        "value": (
                            f"{timings.get('lookup_ms_avg')} ms"
                            if timings.get("lookup_ms_avg") is not None
                            else "—"
                        ),
                    },
                    {
                        "label": "ZIP avg",
                        "value": (
                            f"{timings.get('zip_ms_avg')} ms"
                            if timings.get("zip_ms_avg") is not None
                            else "—"
                        ),
                    },
                    {
                        "label": "Fetches avg",
                        "value": timings.get("lookup_fetches_avg")
                        if timings.get("lookup_fetches_avg") is not None
                        else "—",
                    },
                ],
            },
            {
                "id": "gepnic",
                "kind": "portal",
                "name": "tntenders.gov.in",
                "role": "Tamil Nadu GePNIC closing list, detail pages, captcha ZIP, corrigenda.",
                "configured": True,
                "note": "External portal — rate-limited; sessions reused to avoid captchas.",
                "metrics": [
                    {"label": "Lookups timed", "value": timings.get("with_lookup") or 0},
                    {"label": "ZIPs timed", "value": timings.get("with_zip") or 0},
                    {
                        "label": "Total avg",
                        "value": (
                            f"{timings.get('total_ms_avg')} ms"
                            if timings.get("total_ms_avg") is not None
                            else "—"
                        ),
                    },
                ],
            },
        ],
        "timings": timings,
    }


def _typesense_metrics(status: dict) -> list[dict]:
    if not status.get("ok"):
        return [{"label": "Status", "value": "down"}, {"label": "Error", "value": status.get("error") or "—"}]
    return [
        {"label": "Documents", "value": status.get("documents") or 0},
        {"label": "Collection", "value": status.get("collection") or "—"},
        {"label": "Host", "value": f"{status.get('host')}:{status.get('port')}"},
    ]


def _postgres_metrics(status: dict) -> list[dict]:
    if not status.get("ok"):
        return [{"label": "Status", "value": "down"}, {"label": "Error", "value": status.get("error") or "—"}]
    return [
        {"label": "Tenders", "value": status.get("tenders") or 0},
        {"label": "Downloaded", "value": status.get("downloaded") or 0},
        {"label": "Extracted", "value": status.get("extracted") or 0},
    ]


@app.get("/api/tenders")
def tender_page(
    published_from: str | None = None,
    published_to: str | None = None,
    closing_from: str | None = None,
    closing_to: str | None = None,
    opening_from: str | None = None,
    opening_to: str | None = None,
    lifecycle: str | None = None,
    department: str | None = None,
    status: str | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    return db.search_tenders(
        published_from=published_from or None,
        published_to=published_to or None,
        closing_from=closing_from or None,
        closing_to=closing_to or None,
        opening_from=opening_from or None,
        opening_to=opening_to or None,
        lifecycle=lifecycle or None,
        department=department or None,
        status=status or None,
        q=q or None,
        limit=min(max(1, limit), 100),
        offset=max(0, offset),
    )


@app.get("/api/search")
def file_search(
    q: str = "",
    include_expired: bool = False,
    lifecycle: str | None = None,
    extension: str | None = None,
    page: int = 1,
) -> dict:
    import search

    try:
        return search.search_files(
            q,
            include_expired=include_expired,
            lifecycle=lifecycle or None,
            extension=extension or None,
            page=max(1, page),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


STATIC = ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

import api_v1

app.mount("/api/v1", api_v1.api)


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.websocket("/ws")
async def stream(socket: WebSocket) -> None:
    await socket.accept()
    clients.add(socket)
    try:
        await socket.send_text(
            json.dumps(
                {
                    "type": "tick",
                    "job": job_view(),
                    "counts": db.counts(),
                    "captcha": db.captcha_usage(),
                }
            )
        )
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        clients.discard(socket)


@app.get("/api/tenders/{tender_id}/files")
def tender_files(tender_id: str) -> dict:
    record = db.fetch_one(tender_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown tender")
    folder = (TENDERS / tender_id).resolve()
    files = []
    if folder.is_dir() and TENDERS.resolve() in folder.parents:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            files.append(
                {"name": relative, "url": f"/files/{quote(tender_id)}/{quote(relative)}"}
            )
    return {
        "title": record["title"],
        "corrigendum_count": record.get("corrigendum_count") or 0,
        "corrigenda": record.get("corrigenda") or [],
        "details": record.get("details") or {},
        "files": files,
    }


@app.get("/files/{tender_id}/{file_path:path}")
def download_file(tender_id: str, file_path: str):
    folder = (TENDERS / tender_id).resolve()
    target = (folder / file_path).resolve()
    if folder not in target.parents and target != folder:
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target)


@app.on_event("startup")
async def startup() -> None:
    global loop
    db.init_db()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, listen)


def main() -> None:
    import uvicorn

    db._load_env()
    port = int(os.environ.get("DASHBOARD_PORT", "8091"))
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
