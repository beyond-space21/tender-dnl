"""Extract tender fields that are only in PDFs, using Gemini with structured output.

The detail page gives most fields deterministically (see app.parse_tender_detail).
Gemini fills in what only lives inside the tender document PDFs: eligibility,
scope of work, and the estimated value when the page says NA. PDFs are sent as
raw bytes so Gemini handles scanned pages and Tamil fonts natively (local text
extraction garbles the Tamil glyph encoding these PDFs use).
"""

import json
import os
import time
from pathlib import Path

MAX_PDF_BYTES = 15 * 1024 * 1024  # keep the request well under the 20MB API limit
MAX_PDFS = 4
RETRIES = 3

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "eligibility_percent": {
            "type": "number",
            "nullable": True,
            "description": "Eligibility turnover/experience percentage if stated, e.g. 50 for '50% of estimated value'.",
        },
        "eligibility": {
            "type": "string",
            "nullable": True,
            "description": "Eligibility / qualification criteria for bidders, summarised in English (max 2000 chars).",
        },
        "scope_of_work": {
            "type": "string",
            "nullable": True,
            "description": "Scope of the work/supply, summarised in English (max 2000 chars).",
        },
        "estimated_value_inr": {
            "type": "number",
            "nullable": True,
            "description": "Estimated tender/work value in INR if stated anywhere.",
        },
        "emd_inr": {
            "type": "number",
            "nullable": True,
            "description": "EMD amount in INR if stated.",
        },
    },
    "required": [
        "eligibility_percent",
        "eligibility",
        "scope_of_work",
        "estimated_value_inr",
        "emd_inr",
    ],
}

PROMPT = """You are reading the official tender documents (PDFs) of one Tamil Nadu
e-procurement tender. Documents may be in English, Tamil, or both.

Extract ONLY what the documents state; use null when a field is not present.
Amounts: plain numbers in INR without separators (e.g. 1669400).
Summaries: English, concise, factual.
"""

CORR_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "nullable": True,
            "description": "What this corrigendum changes vs the original tender, in English (max 1500 chars).",
        },
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "nullable": True,
                        "description": "closing_at, opening_at, published_at, tender_fee_inr, emd_inr, eligibility_percent, eligibility, scope_of_work, or other.",
                    },
                    "label": {
                        "type": "string",
                        "nullable": True,
                        "description": "Human label for the changed item.",
                    },
                    "original": {
                        "type": "string",
                        "nullable": True,
                        "description": "Value before this corrigendum, if stated, exactly as written (may be Tamil).",
                    },
                    "revised": {
                        "type": "string",
                        "nullable": True,
                        "description": "Value after this corrigendum, exactly as written (may be Tamil).",
                    },
                    "original_en": {
                        "type": "string",
                        "nullable": True,
                        "description": "English rendering of original when it is written in Tamil; null when already English.",
                    },
                    "revised_en": {
                        "type": "string",
                        "nullable": True,
                        "description": "English rendering of revised when it is written in Tamil; null when already English.",
                    },
                    "detail": {
                        "type": "string",
                        "nullable": True,
                        "description": "Short English explanation of what specifically changed (max 400 chars).",
                    },
                    "change_kind": {
                        "type": "string",
                        "nullable": True,
                        "description": "One of: date, fee, eligibility, scope, other.",
                    },
                    "quote": {
                        "type": "string",
                        "nullable": True,
                        "description": "Brief supporting snippet from the PDF (max 240 chars), or null.",
                    },
                },
                "required": [
                    "field",
                    "label",
                    "original",
                    "revised",
                    "original_en",
                    "revised_en",
                    "detail",
                    "change_kind",
                    "quote",
                ],
            },
        },
        "tender_fee_inr": {
            "type": "number",
            "nullable": True,
            "description": "Revised tender fee in INR if this corrigendum states a new fee.",
        },
        "emd_inr": {
            "type": "number",
            "nullable": True,
            "description": "Revised EMD in INR if this corrigendum states a new EMD.",
        },
        "eligibility_percent": {"type": "number", "nullable": True},
        "eligibility": {
            "type": "string",
            "nullable": True,
            "description": "Revised eligibility text if this corrigendum changes qualification rules.",
        },
        "scope_of_work": {
            "type": "string",
            "nullable": True,
            "description": "Revised scope if this corrigendum changes the work/supply.",
        },
    },
    "required": [
        "summary",
        "changes",
        "tender_fee_inr",
        "emd_inr",
        "eligibility_percent",
        "eligibility",
        "scope_of_work",
    ],
}

CORR_PROMPT = """You are reading an official GePNIC corrigendum PDF for a Tamil Nadu tender.
It is usually bilingual: English and Tamil side by side.

Extract what changed compared with the original tender requirement.
Use null when a value is not stated. Amounts: plain INR numbers (e.g. 500).
Dates: keep the document's date-time text, written numerically as
DD.MM.YYYY HH:MM AM/PM (e.g. 05.10.2026 03:30 PM) — never transliterate the
Tamil words for morning/afternoon/evening; convert them to AM/PM.
field must be one of: closing_at, opening_at, published_at, tender_fee_inr,
emd_inr, eligibility_percent, eligibility, scope_of_work, other.
change_kind must be one of: date, fee, eligibility, scope, other.
original/revised keep the value exactly as written (Tamil stays Tamil).
When a value is in Tamil, set original_en / revised_en to its English
rendering (translate காலை=morning AM, பிற்பகல்/மாலை=afternoon/evening PM,
நண்பகல்=noon, அளவில்=at). Leave _en null when the value is already English.
For each change, set detail to a clear English "what changed" sentence, and
quote a short supporting phrase from the PDF when available (otherwise null).
"""


def gemini_config() -> tuple[str, str]:
    """Return (api_key, model); api_key is '' when unset."""
    import db

    db._load_env()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
    return key, model


def packet_pdfs(folder: Path, preferred_names: list[str] | None = None) -> list[Path]:
    """Main-packet PDFs (corrigendum bundles excluded), capped by size/count.

    Prefer tender notice / tender document names when provided, then fill with
    remaining largest top-level PDFs.
    """
    available = {
        p.name: p
        for p in folder.glob("*.pdf")
        if p.is_file() and "corrigendum" not in p.parts
    }
    ordered: list[Path] = []
    seen: set[str] = set()
    for name in preferred_names or []:
        path = available.get(name)
        if path is None or name in seen:
            continue
        ordered.append(path)
        seen.add(name)
    rest = sorted(
        (p for name, p in available.items() if name not in seen),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    ordered.extend(rest)

    picked: list[Path] = []
    total = 0
    for pdf in ordered:
        size = pdf.stat().st_size
        if total + size > MAX_PDF_BYTES or len(picked) >= MAX_PDFS:
            break
        picked.append(pdf)
        total += size
    return picked


def _stopped(stop_event) -> bool:
    return stop_event is not None and stop_event.is_set()


def _ask_json(
    tender_id: str,
    pdfs: list[Path],
    prompt: str,
    schema: dict,
    label: str,
    stop_event=None,
) -> dict | None:
    key, model = gemini_config()
    if not key or not pdfs:
        return None
    if _stopped(stop_event):
        print(f"llm {tender_id}: skipped (stop requested)", flush=True)
        return None

    from google import genai
    from google.genai import types
    from google.genai.errors import APIError

    # Bound each request so a hung call cannot stall the extract drain forever.
    timeout_ms = max(30_000, int(float(os.environ.get("GEMINI_TIMEOUT_S", "180")) * 1000))
    try:
        client = genai.Client(
            api_key=key, http_options=types.HttpOptions(timeout=timeout_ms)
        )
    except Exception:
        client = genai.Client(api_key=key)
    parts = [types.Part.from_text(text=prompt)]
    total = 0
    used: list[Path] = []
    for pdf in pdfs:
        size = pdf.stat().st_size
        if total + size > MAX_PDF_BYTES:
            break
        parts.append(types.Part.from_bytes(data=pdf.read_bytes(), mime_type="application/pdf"))
        used.append(pdf)
        total += size
    if not used:
        return None

    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        if _stopped(stop_event):
            print(f"llm {tender_id}: {label} aborted (stop requested)", flush=True)
            return None
        started = time.monotonic()
        try:
            response = client.models.generate_content(
                model=model,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            data = json.loads(response.text or "{}")
            if not isinstance(data, dict):
                raise ValueError(f"unexpected response shape: {type(data)}")
            data["_model"] = model
            data["_sources"] = [p.name for p in used]
            data["_ms"] = int(round((time.monotonic() - started) * 1000))
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                prompt = getattr(usage, "prompt_token_count", 0) or 0
                candidates = getattr(usage, "candidates_token_count", 0) or 0
                total = getattr(usage, "total_token_count", None)
                if total is None and (prompt or candidates):
                    total = prompt + candidates
                if total is not None:
                    data["_tokens"] = int(total)
                if prompt:
                    data["_tokens_in"] = int(prompt)
                if candidates:
                    data["_tokens_out"] = int(candidates)
            print(
                f"llm {tender_id}: {label} from {len(used)} pdf(s) "
                f"({data['_ms']}ms"
                + (f", {data['_tokens']} tok" if data.get("_tokens") is not None else "")
                + ")",
                flush=True,
            )
            return data
        except APIError as exc:
            last_error = exc
            if exc.code in (429, 500, 502, 503) and attempt < RETRIES:
                time.sleep(5 * attempt)
                continue
            break
        except Exception as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(3 * attempt)
                continue
            break

    print(f"llm {tender_id}: {label} failed: {last_error}", flush=True)
    return None


def extract_from_folder(
    tender_id: str,
    folder: Path,
    preferred_names: list[str] | None = None,
    stop_event=None,
) -> dict | None:
    """Run Gemini over the tender's PDFs. Returns the fields dict, or None if skipped."""
    return _ask_json(
        tender_id,
        packet_pdfs(folder, preferred_names=preferred_names),
        PROMPT,
        RESPONSE_SCHEMA,
        "extracted",
        stop_event=stop_event,
    )


def extract_corrigendum(tender_id: str, pdf_path: Path, stop_event=None) -> dict | None:
    """Read one corrigendum PDF for before/after field changes."""
    if not pdf_path.is_file():
        return None
    return _ask_json(
        tender_id,
        [pdf_path],
        CORR_PROMPT,
        CORR_SCHEMA,
        f"corrigendum {pdf_path.name}",
        stop_event=stop_event,
    )


def main() -> None:
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python llm_extract.py TENDERS/<tender_id>")
    folder = Path(sys.argv[1]).resolve()
    result = extract_from_folder(folder.name, folder)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
