"""Build the final per-tender field record from page scrape, BOQ xls, and Gemini.

Details structure stored in DB (tenders.details) and TENDERS/<id>/details.json:

{
  "page":   {...flat detail-page fields..., "documents": [...]},
  "boq":    {"name_of_work", "contract_no", "inviting_authority",
             "item_count", "estimated_total_inr", "source"},
  "llm":    {...Gemini output...},
  "fields": {...final merged, normalized values (single source of truth)...},
  "field_sources": {field: {"source": "page|boq|llm|corrigendum_history|...", "note?"}},
  "files":  {"tender_notice": [...], "tender_document": [...], "boq": [...],
             "corrigendum": [...], "other": [...]},
  "corrigendum": {"present", "count", "summary", "changes": [...]},
  "status": {"page_ok", "boq_ok", "llm_ok", "errors": [], "extracted_at"}
}

Precedence: page and BOQ beat packet Gemini. Date fields reconcile page vs
corrigendum history changed-state (later timestamp when both differ). Non-date
corrigendum PDF overlays fee / EMD / eligibility / scope.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

MONEY_JUNK = re.compile(r"[₹,/\-\s]|Rs\.?|INR", re.I)


def money(value) -> float | None:
    """'24,000' / 'Rs. 2,270/-' / 4600.0 -> float; None when absent/unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = MONEY_JUNK.sub("", str(value))
    if not cleaned or cleaned.upper() == "NA":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def when_iso(value) -> str | None:
    """'05-Oct-2026 11:30 AM', '05.10.2026 03:30 PM', or ISO string -> ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = " ".join(str(value).split())
    if not text or text.upper() == "NA":
        return None
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        pass
    for fmt in (
        "%d.%m.%Y %I:%M %p",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y %I:%M %p",
        "%d-%m-%Y %H:%M",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    import app

    parsed = app.parse_when(text)
    return parsed.isoformat() if parsed else None


def covers_label(value) -> str | None:
    text = str(value or "").strip()
    if text == "1":
        return "Single"
    if text == "2":
        return "Two"
    return text or None


# ---------------------------------------------------------------- BOQ parsing

BOQ_HEADER_PREFIXES = {
    "name_of_work": "Name of Work:",
    "contract_no": "Contract No:",
    "inviting_authority": "Tender Inviting Authority:",
}


def _boq_rows(path: Path) -> list[list]:
    if path.suffix.lower() == ".xls":
        import xlrd

        book = xlrd.open_workbook(str(path))
        sheet = next(
            (book.sheet_by_name(n) for n in book.sheet_names() if n.lower() != "macros"),
            book.sheet_by_index(0),
        )
        return [
            [sheet.cell_value(r, c) for c in range(sheet.ncols)]
            for r in range(sheet.nrows)
        ]
    import openpyxl

    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    sheet = next(
        (book[n] for n in book.sheetnames if n.lower() != "macros"),
        book[book.sheetnames[0]],
    )
    return [list(row) for row in sheet.iter_rows(values_only=True)]


def parse_boq(folder: Path) -> dict:
    """Parse the standardized GePNIC BoQ template (header info + estimated total)."""
    boqs = sorted(folder.glob("*.xls")) + sorted(folder.glob("*.xlsx"))
    if not boqs:
        return {}
    path = boqs[0]
    out: dict = {"source": path.name}
    try:
        rows = _boq_rows(path)
    except Exception as exc:
        out["error"] = f"could not read {path.name}: {exc}"
        return out

    header_row = None
    qty_col = rate_col = None
    for index, row in enumerate(rows[:40]):
        first = str(row[0] or "").strip() if row else ""
        for key, prefix in BOQ_HEADER_PREFIXES.items():
            if first.startswith(prefix):
                out[key] = first[len(prefix):].strip() or None
        texts = [str(c or "").replace("\n", " ").strip() for c in row]
        # Header row: first cell is 'Sl. No.' (holds across BoQ template variants).
        if header_row is None and texts and re.match(r"Sl\.?\s*No", texts[0], re.I):
            header_row = index
            for col, text in enumerate(texts):
                if re.match(r"Quantity", text, re.I):
                    qty_col = col
                if re.search(r"Estimated Rate|Upset Price|Estimated Cost|Estimated Amount", text, re.I):
                    rate_col = col

    items = 0
    total = 0.0
    if header_row is not None:
        for row in rows[header_row + 1:]:
            desc = str(row[1] or "").strip() if len(row) > 1 else ""
            slno = row[0] if row else None
            if not desc or not isinstance(slno, (int, float)):
                continue
            try:  # the template has a column-numbering row (1, 2, 3, ...): skip it
                float(desc)
                continue
            except ValueError:
                pass
            items += 1
            if qty_col is not None and rate_col is not None:
                qty = row[qty_col] if len(row) > qty_col else None
                rate = row[rate_col] if len(row) > rate_col else None
                if isinstance(qty, (int, float)) and isinstance(rate, (int, float)) and rate > 0:
                    total += float(qty) * float(rate)
    out["item_count"] = items
    out["estimated_total_inr"] = round(total, 2) if total > 0 else None
    return out


# ------------------------------------------------------------- file mapping

def map_files(folder: Path, page: dict, corrigenda: list[dict] | None) -> dict:
    """Join the detail page's typed document list to the files on disk."""
    files = {"tender_notice": [], "tender_document": [], "boq": [], "corrigendum": [], "other": []}
    local = {p.name: p.relative_to(folder).as_posix() for p in folder.glob("*") if p.is_file()}
    claimed: set[str] = set()

    for doc in (page or {}).get("documents") or []:
        name = doc.get("name") or ""
        rel = local.get(name)
        entry = {
            "name": name,
            "description": doc.get("description"),
            "doc_type": doc.get("doc_type"),
            "file": rel,
        }
        if doc.get("category") == "nit":
            files["tender_notice"].append(entry)
        elif (doc.get("doc_type") or "").upper() == "BOQ":
            files["boq"].append(entry)
        else:
            files["tender_document"].append(entry)
        if rel:
            claimed.add(name)

    # Fallbacks for packets downloaded before page parsing existed.
    for name, rel in local.items():
        if name in claimed or name in ("details.json", "corrigenda.json"):
            continue
        lower = name.lower()
        if lower.startswith("boq") and lower.endswith((".xls", ".xlsx")):
            files["boq"].append({"name": name, "file": rel, "doc_type": "BOQ"})
        elif lower.endswith(".pdf"):
            files["tender_document"].append({"name": name, "file": rel, "doc_type": "PDF"})
        elif not lower.endswith(".json"):
            files["other"].append({"name": name, "file": rel})

    corr_root = folder / "corrigendum"
    for index, item in enumerate(corrigenda or []):
        serial = str(item.get("serial") or index + 1)
        sub = next(iter(sorted(corr_root.glob(f"{serial}_*"))), None)
        pdf_rel = None
        history = {}
        if sub and sub.is_dir():
            pdf = sub / "corrigendum.pdf"
            if not pdf.is_file():
                pdf = next(iter(sorted(sub.glob("*.pdf"))), None)
            if pdf and pdf.is_file():
                pdf_rel = pdf.relative_to(folder).as_posix()
            hist_path = sub / "history.json"
            if hist_path.is_file():
                try:
                    history = json.loads(hist_path.read_text(encoding="utf-8"))
                except Exception:
                    history = {}
        files["corrigendum"].append(
            {
                "serial": serial,
                "title": item.get("title"),
                "corr_type": item.get("corr_type"),
                "description": item.get("description") or history.get("description"),
                "folder": sub.relative_to(folder).as_posix() if sub else None,
                "file": pdf_rel,
                "pdf_missing": bool(item.get("pdf_missing")) or pdf_rel is None,
                "revised_dates": item.get("revised_dates") or history.get("revised") or {},
                "original_dates": item.get("original_dates") or history.get("original") or {},
            }
        )
    return files


HISTORY_DATE_FIELDS = {
    "Publish Date": ("published_at", "e-Published Date"),
    "Bid Opening Date": ("opening_at", "Opening Date"),
    "Bid Submission End Date": ("closing_at", "Closing Date"),
    "Bid Submission Start Date": ("bid_submission_start", "Bid Submission Start"),
    "Document Download/Sale Start Date": ("doc_download_start", "Document Download Start"),
    "Document Download/Sale End Date": ("doc_download_end", "Document Download End"),
    "Clarification Start Date": ("clarification_start", "Clarification Start"),
    "Clarification End Date": ("clarification_end", "Clarification End"),
}

CARD_FIELDS = {
    "published_at",
    "closing_at",
    "opening_at",
    "tender_fee_inr",
    "emd_inr",
    "eligibility_percent",
    "eligibility",
    "scope_of_work",
}


def _blank(value) -> bool:
    text = " ".join(str(value or "").split())
    return not text or text.upper() == "NA"


def _same(left, right) -> bool:
    return " ".join(str(left or "").split()).upper() == " ".join(str(right or "").split()).upper()


DATE_CARD_FIELDS = {"published_at", "closing_at", "opening_at"}


def _change(
    *,
    field: str | None,
    label: str,
    original,
    revised,
    source: str,
    phase: dict,
    detail: str | None = None,
    change_kind: str | None = None,
    quote: str | None = None,
) -> dict:
    out = {
        "field": field,
        "label": label,
        "original": None if _blank(original) else str(original).strip(),
        "revised": None if _blank(revised) else str(revised).strip(),
        "source": source,
        "serial": phase.get("serial"),
        "title": phase.get("title"),
        "corr_type": phase.get("corr_type"),
        "file": phase.get("file"),
        "detail": None if _blank(detail) else str(detail).strip(),
        "change_kind": None if _blank(change_kind) else str(change_kind).strip(),
        "quote": None if _blank(quote) else str(quote).strip(),
    }
    return out


def _dt_pair(value) -> tuple[str | None, datetime | None]:
    iso = when_iso(value)
    if not iso:
        return None, None
    try:
        parsed = datetime.fromisoformat(iso)
        return iso, parsed
    except ValueError:
        return iso, None


def _set_source(sources: dict, field: str, source: str, note: str | None = None) -> None:
    if not field or not source:
        return
    entry: dict = {"source": source}
    if note:
        entry["note"] = note
    sources[field] = entry


def history_changes(phase: dict) -> list[dict]:
    original = phase.get("original_dates") or {}
    revised = phase.get("revised_dates") or {}
    found = []
    for label, (field, display) in HISTORY_DATE_FIELDS.items():
        before = original.get(label)
        after = revised.get(label)
        if _blank(after) or _same(before, after):
            continue
        serial = phase.get("serial") or ""
        found.append(
            _change(
                field=field,
                label=display,
                original=before,
                revised=after,
                source="history",
                phase=phase,
                change_kind="date",
                detail=(
                    f"{display} revised in corrigendum history"
                    + (f" #{serial}" if serial else "")
                ),
            )
        )
    return found


def pdf_changes(phase: dict, llm: dict) -> tuple[list[dict], dict]:
    found = []
    overlay = {}
    if not llm:
        return found, overlay
    for item in llm.get("changes") or []:
        if not isinstance(item, dict):
            continue
        # Bilingual PDFs: prefer the English rendering, keep the raw (Tamil) text.
        raw_original = item.get("original")
        raw_revised = item.get("revised")
        original = raw_original
        revised = raw_revised
        en = item.get("revised_en")
        if _blank(revised) and not _blank(en):
            revised = en
        elif not _blank(en) and not _same(en, raw_revised):
            revised = en
        en = item.get("original_en")
        if _blank(original) and not _blank(en):
            original = en
        elif not _blank(en) and not _same(en, raw_original):
            original = en
        if _blank(revised) and _blank(original):
            continue
        if _same(original, revised):
            continue
        field = item.get("field") if item.get("field") != "other" else None
        kind = item.get("change_kind")
        if _blank(kind):
            if field in DATE_CARD_FIELDS:
                kind = "date"
            elif field in {"tender_fee_inr", "emd_inr"}:
                kind = "fee"
            elif field in {"eligibility", "eligibility_percent"}:
                kind = "eligibility"
            elif field == "scope_of_work":
                kind = "scope"
            else:
                kind = "other"
        change = _change(
            field=field,
            label=item.get("label") or field or "Change",
            original=original,
            revised=revised,
            source="pdf",
            phase=phase,
            detail=item.get("detail"),
            change_kind=kind,
            quote=item.get("quote"),
        )
        # Preserve the as-written bilingual values for the UI detail line.
        if not _same(change.get("original"), raw_original) and not _blank(raw_original):
            change["original_raw"] = str(raw_original).strip()
        if not _same(change.get("revised"), raw_revised) and not _blank(raw_revised):
            change["revised_raw"] = str(raw_revised).strip()
        found.append(change)
    for key in ("tender_fee_inr", "emd_inr", "eligibility_percent"):
        value = llm.get(key)
        if value not in (None, ""):
            overlay[key] = value
    for key in ("eligibility", "scope_of_work"):
        value = llm.get(key)
        if not _blank(value):
            overlay[key] = value
    return found, overlay


def _latest_history_change(changes: list[dict], field: str) -> dict | None:
    """Pick the history change for a date field with the latest revised timestamp."""
    best = None
    best_dt = None
    for item in changes:
        if item.get("source") != "history" or item.get("field") != field:
            continue
        _, revised_dt = _dt_pair(item.get("revised"))
        if best is None:
            best = item
            best_dt = revised_dt
            continue
        if revised_dt is not None and (best_dt is None or revised_dt >= best_dt):
            best = item
            best_dt = revised_dt
    return best


def reconcile_date_value(page_value, change: dict | None) -> tuple[str | None, str]:
    """Compare page vs corrigendum revised; return (iso, source) for the changed state."""
    page_iso, page_dt = _dt_pair(page_value)
    if not change:
        return page_iso, "page" if page_iso else ""

    revised_iso, revised_dt = _dt_pair(change.get("revised"))
    if revised_iso and not page_iso:
        return revised_iso, "corrigendum_history"
    if page_iso and not revised_iso:
        return page_iso, "page"
    if not page_iso and not revised_iso:
        return None, ""

    if _same(page_iso, revised_iso):
        return page_iso, "page+corrigendum"

    # Both look like updated states — prefer the later timestamp.
    if page_dt and revised_dt:
        if revised_dt >= page_dt:
            return revised_iso, "corrigendum_history"
        return page_iso, "page"

    # Incomplete parse on one side: prefer the corrigendum changed (revised) value.
    return revised_iso or page_iso, "corrigendum_history" if revised_iso else "page"


def apply_corrigendum_fields(
    fields: dict,
    changes: list[dict],
    overlay: dict,
    field_sources: dict | None = None,
) -> tuple[dict, dict]:
    """Reconcile page vs corrigendum into live fields; update field_sources.

    Dates: compare page current value with history revised; use the changed-state
    value (later timestamp when both differ). PDF dates do not set card dates.
    PDF overlays fee, EMD, eligibility, scope.
    """
    sources = dict(field_sources or {})
    claimed: set[str] = set()

    for field in DATE_CARD_FIELDS:
        change = _latest_history_change(changes, field)
        if change is None:
            continue
        chosen, source = reconcile_date_value(fields.get(field), change)
        if chosen:
            fields[field] = chosen
            _set_source(
                sources,
                field,
                source,
                note=f"serial {change.get('serial')}" if change.get("serial") else None,
            )
            claimed.add(field)

    def write_non_date(field: str, revised, source_label: str) -> None:
        if field not in CARD_FIELDS or field in DATE_CARD_FIELDS or _blank(revised):
            return
        if field in claimed:
            return
        if field in {"tender_fee_inr", "emd_inr", "eligibility_percent"}:
            parsed = money(revised)
            if parsed is not None:
                fields[field] = parsed
                _set_source(sources, field, source_label)
                claimed.add(field)
        else:
            fields[field] = revised
            _set_source(sources, field, source_label)
            claimed.add(field)

    # PDF/list change rows (non-date): apply when history did not claim the field.
    for item in changes:
        if item.get("source") == "history":
            continue
        field = item.get("field") or ""
        if field in DATE_CARD_FIELDS:
            continue
        write_non_date(field, item.get("revised"), "corrigendum_pdf")

    if overlay.get("tender_fee_inr") is not None and "tender_fee_inr" not in claimed:
        fields["tender_fee_inr"] = money(overlay["tender_fee_inr"])
        _set_source(sources, "tender_fee_inr", "corrigendum_pdf")
        claimed.add("tender_fee_inr")
    if overlay.get("emd_inr") is not None and "emd_inr" not in claimed:
        fields["emd_inr"] = money(overlay["emd_inr"])
        _set_source(sources, "emd_inr", "corrigendum_pdf")
        claimed.add("emd_inr")
    if overlay.get("eligibility_percent") is not None and "eligibility_percent" not in claimed:
        fields["eligibility_percent"] = overlay["eligibility_percent"]
        _set_source(sources, "eligibility_percent", "corrigendum_pdf")
        claimed.add("eligibility_percent")
    if overlay.get("eligibility") and "eligibility" not in claimed:
        fields["eligibility"] = overlay["eligibility"]
        _set_source(sources, "eligibility", "corrigendum_pdf")
        claimed.add("eligibility")
    if overlay.get("scope_of_work") and "scope_of_work" not in claimed:
        fields["scope_of_work"] = overlay["scope_of_work"]
        _set_source(sources, "scope_of_work", "corrigendum_pdf")
        claimed.add("scope_of_work")
        if _blank(fields.get("work_description")):
            fields["work_description"] = overlay["scope_of_work"]
            _set_source(sources, "work_description", "corrigendum_pdf")
    return fields, sources


def analyze_corrigenda(
    tender_id: str,
    folder: Path,
    files: dict,
    run_llm: bool = True,
    stop_event=None,
) -> tuple[dict, dict, list[str]]:
    """Build the corrigendum change list from history dates and optional PDF Gemini."""
    errors: list[str] = []
    phases = list(files.get("corrigendum") or [])
    changes: list[dict] = []
    overlay: dict = {}
    summaries: list[str] = []
    seen: set[tuple] = set()

    def add(items: list[dict]) -> None:
        for item in items:
            key = (
                item.get("field"),
                item.get("label"),
                item.get("original"),
                item.get("revised"),
                item.get("serial"),
            )
            if key in seen:
                continue
            seen.add(key)
            changes.append(item)

    for phase in phases:
        add(history_changes(phase))
        pdf_rel = phase.get("file")
        pdf_path = folder / pdf_rel if pdf_rel else None
        if run_llm and pdf_path and pdf_path.is_file():
            if stop_event is not None and stop_event.is_set():
                break
            try:
                import llm_extract

                llm = (
                    llm_extract.extract_corrigendum(
                        tender_id, pdf_path, stop_event=stop_event
                    )
                    or {}
                )
            except Exception as exc:
                errors.append(f"corrigendum pdf {phase.get('serial')}: {exc}")
                llm = {}
            extra, extra_overlay = pdf_changes(phase, llm)
            add(extra)
            overlay.update({k: v for k, v in extra_overlay.items() if v not in (None, "")})
            if llm.get("summary"):
                summaries.append(str(llm["summary"]).strip())
            phase["llm_summary"] = llm.get("summary")
            if llm.get("_tokens") is not None:
                phase["llm_tokens"] = llm.get("_tokens")
            if llm.get("_tokens_in") is not None:
                phase["llm_tokens_in"] = llm.get("_tokens_in")
            if llm.get("_tokens_out") is not None:
                phase["llm_tokens_out"] = llm.get("_tokens_out")
        description = phase.get("description")
        if description and not _blank(description):
            summaries.append(str(description).strip())

    if phases and not changes:
        for phase in phases:
            add(
                [
                    _change(
                        field=None,
                        label=phase.get("title") or "Corrigendum",
                        original="Original requirement",
                        revised=phase.get("corr_type") or "Issued",
                        source="list",
                        phase=phase,
                        change_kind="other",
                        detail="Corrigendum issued; no structured field diffs extracted yet.",
                    )
                ]
            )

    unique_summaries = []
    for text in summaries:
        if text and text not in unique_summaries:
            unique_summaries.append(text)

    return (
        {
            "present": bool(phases),
            "count": len(phases),
            "summary": " ".join(unique_summaries) or None,
            "changes": changes,
            "phases": phases,
        },
        overlay,
        errors,
    )


# ---------------------------------------------------------------- merge step

def _pick_sourced(*candidates: tuple) -> tuple:
    """Return (value, source) for the first non-empty candidate."""
    for value, source in candidates:
        if value not in (None, "", 0):
            return value, source
    return None, None


def merge_fields(record: dict, page: dict, boq: dict, llm: dict) -> tuple[dict, dict]:
    """Final normalized values with deterministic-first precedence + field_sources."""
    page = page or {}
    boq = boq or {}
    llm = llm or {}
    sources: dict = {}

    emd_page = money(page.get("emd_amount_inr"))
    emd_llm = money(llm.get("emd_inr"))
    emd_mismatch = (
        emd_page is not None
        and emd_llm is not None
        and abs(emd_page - emd_llm) > max(1.0, emd_page * 0.01)
    )

    pre_bid_parts = [
        when_iso(page.get("pre_bid_meeting_date")),
        page.get("pre_bid_meeting_place"),
    ]

    def take(field: str, *candidates: tuple):
        value, source = _pick_sourced(*candidates)
        if value is not None and source:
            _set_source(sources, field, source)
        return value

    organisation = take(
        "organisation",
        (record.get("organisation"), "page"),
        (page.get("organisation_chain"), "page"),
    )
    published_at = take(
        "published_at",
        (when_iso(page.get("published_date")), "page"),
        (when_iso(record.get("published_at")), "page"),
    )
    closing_at = take(
        "closing_at",
        (when_iso(page.get("bid_submission_end")), "page"),
        (when_iso(record.get("closing_at")), "page"),
    )
    opening_at = take(
        "opening_at",
        (when_iso(page.get("bid_opening_date")), "page"),
        (when_iso(record.get("opening_at")), "page"),
    )
    title = take(
        "title",
        (page.get("title"), "page"),
        (record.get("title"), "page"),
    )
    name_of_work = take(
        "name_of_work",
        (page.get("title"), "page"),
        (boq.get("name_of_work"), "boq"),
        (record.get("title"), "page"),
    )
    work_description = take(
        "work_description",
        (page.get("work_description"), "page"),
        (llm.get("scope_of_work"), "llm"),
    )
    tender_fee = money(page.get("tender_fee_inr"))
    if tender_fee is not None:
        _set_source(sources, "tender_fee_inr", "page")
    emd_inr = take("emd_inr", (emd_page, "page"), (emd_llm, "llm"))
    work_value = take(
        "work_value_inr",
        (money(page.get("tender_value_inr")), "page"),
        (boq.get("estimated_total_inr"), "boq"),
        (money(llm.get("estimated_value_inr")), "llm"),
    )
    eligibility_percent = llm.get("eligibility_percent")
    if eligibility_percent not in (None, ""):
        _set_source(sources, "eligibility_percent", "llm")
    eligibility = take(
        "eligibility",
        (llm.get("eligibility"), "llm"),
        (page.get("tenderer_class"), "page"),
    )
    scope_of_work = take(
        "scope_of_work",
        (llm.get("scope_of_work"), "llm"),
        (page.get("work_description"), "page"),
    )
    covers = covers_label(page.get("no_of_covers"))
    if covers:
        _set_source(sources, "covers", "page")
    inviting = take(
        "inviting_authority",
        (page.get("inviting_authority_name"), "page"),
        (boq.get("inviting_authority"), "boq"),
    )
    if page.get("tender_reference"):
        _set_source(sources, "tender_reference", "page")
    if page.get("location"):
        _set_source(sources, "location", "page")
    pre_bid = " | ".join(p for p in pre_bid_parts if p) or None
    if pre_bid:
        _set_source(sources, "pre_bid_meeting", "page")

    fields = {
        "tender_id": record.get("tender_id"),
        "tender_reference": page.get("tender_reference"),
        "organisation": organisation,
        "published_at": published_at,
        "closing_at": closing_at,
        "opening_at": opening_at,
        "pre_bid_meeting_date": when_iso(page.get("pre_bid_meeting_date")),
        "pre_bid_meeting_place": page.get("pre_bid_meeting_place"),
        "pre_bid_meeting": pre_bid,
        "title": title,
        "name_of_work": name_of_work,
        "work_description": work_description,
        "tender_fee_inr": tender_fee,
        "emd_inr": emd_inr,
        "emd_mismatch": emd_mismatch,
        "work_value_inr": work_value,
        "eligibility_percent": eligibility_percent,
        "eligibility": eligibility,
        "scope_of_work": scope_of_work,
        "covers": covers,
        "location": page.get("location"),
        "pincode": page.get("pincode"),
        "product_category": page.get("product_category"),
        "contract_type": page.get("contract_type"),
        "period_of_work_days": page.get("period_of_work_days"),
        "bid_validity_days": page.get("bid_validity_days"),
        "inviting_authority": inviting,
    }
    return fields, sources


def preferred_packet_names(files: dict) -> list[str]:
    """Notice + tender-document PDF names for Gemini, preserving map order."""
    names: list[str] = []
    seen: set[str] = set()
    for kind in ("tender_notice", "tender_document"):
        for entry in files.get(kind) or []:
            name = entry.get("name") or ""
            file_rel = entry.get("file") or ""
            candidate = name or Path(file_rel).name
            if not candidate or not candidate.lower().endswith(".pdf"):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            names.append(candidate)
    return names


# ------------------------------------------------------------ orchestration

def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _llm_reusable(folder: Path, existing: dict) -> tuple[bool, dict]:
    """True when the packet PDFs are byte-identical to the last Gemini run.

    Compares the shas recorded in status.llm_pdf_shas, and requires that no
    new top-level PDFs appeared (they would deserve extraction).
    """
    old_llm = existing.get("llm") or {}
    old_shas = (existing.get("status") or {}).get("llm_pdf_shas") or {}
    if not old_llm.get("_model") or not old_shas:
        return False, old_llm
    current = sorted(
        p.name for p in folder.glob("*.pdf") if p.is_file()
    )
    if current != sorted(old_shas):
        return False, old_llm
    for name, sha in old_shas.items():
        if _sha256_file(folder / name) != sha:
            return False, old_llm
    return True, old_llm


def finalize(
    tender_id: str,
    folder: Path,
    record: dict,
    page: dict | None,
    corrigenda: list[dict] | None = None,
    run_llm: bool = True,
    llm_precomputed: dict | None = None,
    stop_event=None,
) -> dict:
    """Produce the complete details dict from everything available on disk."""
    errors: list[str] = []
    page = page or {}
    started = time.monotonic()
    phase: dict = {}

    try:
        files = map_files(folder, page, corrigenda)
    except Exception as exc:
        errors.append(f"files: {exc}")
        files = {}

    boq: dict = {}
    t0 = time.monotonic()
    try:
        boq = parse_boq(folder)
        if boq.get("error"):
            errors.append(boq["error"])
    except Exception as exc:
        errors.append(f"boq: {exc}")
    phase["boq_ms"] = int(round((time.monotonic() - t0) * 1000))

    llm: dict = {}
    t0 = time.monotonic()
    if llm_precomputed is not None:
        llm = llm_precomputed
        phase["llm_ms"] = 0
    elif run_llm:
        try:
            import llm_extract

            llm = (
                llm_extract.extract_from_folder(
                    tender_id,
                    folder,
                    preferred_names=preferred_packet_names(files),
                    stop_event=stop_event,
                )
                or {}
            )
        except Exception as exc:
            errors.append(f"llm: {exc}")
        phase["llm_ms"] = int(round((time.monotonic() - t0) * 1000))
    else:
        phase["llm_ms"] = int(round((time.monotonic() - t0) * 1000))
    if llm.get("_tokens") is not None:
        phase["tokens"] = llm.get("_tokens")
    if llm.get("_tokens_in") is not None:
        phase["tokens_in"] = llm.get("_tokens_in")
    if llm.get("_tokens_out") is not None:
        phase["tokens_out"] = llm.get("_tokens_out")
    if llm.get("_ms") is not None:
        phase["llm_api_ms"] = llm.get("_ms")
    llm_shas: dict = {}
    for name in llm.get("_sources") or []:
        digest = _sha256_file(folder / name)
        if digest:
            llm_shas[name] = digest

    corr_view: dict = {"present": False, "count": 0, "summary": None, "changes": [], "phases": []}
    overlay: dict = {}
    t0 = time.monotonic()
    try:
        corr_view, overlay, corr_errors = analyze_corrigenda(
            tender_id, folder, files, run_llm=run_llm, stop_event=stop_event
        )
        errors.extend(corr_errors)
        files["corrigendum"] = corr_view.get("phases") or files.get("corrigendum") or []
    except Exception as exc:
        errors.append(f"corrigendum: {exc}")
    phase["corr_ms"] = int(round((time.monotonic() - t0) * 1000))
    # Fold any corrigendum-PDF Gemini token usage into the extract totals.
    corr_tokens = 0
    corr_tokens_in = 0
    corr_tokens_out = 0
    for item in corr_view.get("phases") or []:
        extra = item.get("llm_tokens")
        if isinstance(extra, int):
            corr_tokens += extra
        extra_in = item.get("llm_tokens_in")
        if isinstance(extra_in, int):
            corr_tokens_in += extra_in
        extra_out = item.get("llm_tokens_out")
        if isinstance(extra_out, int):
            corr_tokens_out += extra_out
    if corr_tokens:
        phase["tokens"] = int(phase.get("tokens") or 0) + corr_tokens
    if corr_tokens_in:
        phase["tokens_in"] = int(phase.get("tokens_in") or 0) + corr_tokens_in
    if corr_tokens_out:
        phase["tokens_out"] = int(phase.get("tokens_out") or 0) + corr_tokens_out

    fields, field_sources = merge_fields(record, page, boq, llm)
    fields, field_sources = apply_corrigendum_fields(
        fields, corr_view.get("changes") or [], overlay, field_sources
    )
    fields["has_corrigendum"] = bool(corr_view.get("present"))
    phase.update(
        {
            "ms": int(round((time.monotonic() - started) * 1000)),
            "page": bool(page),
            "boq": bool(boq.get("item_count")),
            "llm": bool(llm.get("_model")),
        }
    )
    return {
        "page": page,
        "boq": boq,
        "llm": llm,
        "fields": fields,
        "field_sources": field_sources,
        "files": files,
        "corrigendum": corr_view,
        "status": {
            "page_ok": bool(page),
            "boq_ok": bool(boq.get("item_count")),
            "llm_ok": bool(llm.get("_model")),
            "llm_pdf_shas": llm_shas,
            "errors": errors,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "timings": phase,
        },
    }


def save(tender_id: str, folder: Path, details: dict) -> None:
    import db

    (folder / "details.json").write_text(
        json.dumps(details, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    db.save_details(tender_id, details)


def reextract_one(tender_id: str, page_details: dict | None = None, run_llm: bool = True) -> dict | None:
    """Rebuild details for one tender from saved files (idempotent)."""
    import app
    import db

    record = db.fetch_one(tender_id)
    if not record:
        return None
    folder = app.TENDERS_DIR / tender_id
    if not folder.is_dir():
        return None
    existing = record.get("details") or {}
    page = page_details or existing.get("page") or {}
    if not page and "documents" in existing:
        # Early builds stored page fields flat at the top level.
        page = {k: v for k, v in existing.items() if k != "llm"}
    corrigenda = record.get("corrigenda") or []
    # Skip the packet Gemini call when the PDFs are byte-identical to the
    # last extraction — reuse the stored llm block instead.
    llm_precomputed: dict | None = None
    if run_llm:
        reusable, old_llm = _llm_reusable(folder, existing)
        if reusable:
            llm_precomputed = old_llm
            print(f"reextract {tender_id}: llm skipped (packet pdfs unchanged)")
    details = finalize(
        tender_id,
        folder,
        record,
        page,
        corrigenda,
        run_llm=run_llm,
        llm_precomputed=llm_precomputed,
    )
    save(tender_id, folder, details)
    extract_timings = (details.get("status") or {}).get("timings")
    if extract_timings:
        db.merge_timings(tender_id, {"extract": extract_timings})
    return details


def main() -> None:
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python extract_pipeline.py <tender_id>")
    details = reextract_one(sys.argv[1])
    if details is None:
        raise SystemExit("tender not found or no folder on disk")
    print(json.dumps(details["fields"], indent=2, ensure_ascii=False))
    print("status:", json.dumps(details["status"]))


if __name__ == "__main__":
    main()
