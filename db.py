"""PostgreSQL records for tenders already seen or downloaded."""

import json
import os
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    override = {
        "CAPTCHA_PROVIDER",
        "CAPTCHA_ATTEMPTS",
        "TWOCAPTCHA_APIKEY",
        "APIKEY_2CAPTCHA",
        "TWO_CAPTCHA_API_KEY",
    }
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        key = name.strip()
        cleaned = value.strip().strip("'\"")
        if key in override:
            if cleaned:
                os.environ[key] = cleaned
        else:
            os.environ.setdefault(key, cleaned)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    tender_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    organisation TEXT,
    published_at TIMESTAMPTZ,
    closing_at TIMESTAMPTZ,
    opening_at TIMESTAMPTZ,
    list_page INTEGER,
    status TEXT NOT NULL DEFAULT 'seen',
    zip_name TEXT,
    extract_dir TEXT,
    error TEXT,
    corrigendum_count INTEGER NOT NULL DEFAULT 0,
    corrigenda JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    downloaded_at TIMESTAMPTZ
);
"""

MIGRATIONS = [
    """
    ALTER TABLE tenders
    ADD COLUMN IF NOT EXISTS corrigendum_count INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE tenders
    ADD COLUMN IF NOT EXISTS corrigenda JSONB NOT NULL DEFAULT '[]'::jsonb
    """,
    """
    ALTER TABLE tenders
    ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb
    """,
    """
    ALTER TABLE tenders
    ADD COLUMN IF NOT EXISTS timings JSONB NOT NULL DEFAULT '{}'::jsonb
    """,
    """
    ALTER TABLE tenders
    ADD COLUMN IF NOT EXISTS left_list_at TIMESTAMPTZ
    """,
]

# Failures that should not be re-queued until the next list refresh finds the tender again.
LEFT_LIST_MARKERS = (
    "no longer on the closing list",
    "closing date has passed",
)


def is_left_list_error(error: str | None) -> bool:
    text = (error or "").lower()
    return any(marker in text for marker in LEFT_LIST_MARKERS)


def database_url() -> str:
    _load_env()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("Add DATABASE_URL to .env.")
    return url


def connect():
    return psycopg.connect(database_url(), row_factory=dict_row)


USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS truecaptcha_usage (
    used_on DATE PRIMARY KEY,
    images INTEGER NOT NULL DEFAULT 0
);
"""

LIST_SWEEP_SCHEMA = """
CREATE TABLE IF NOT EXISTS list_sweep (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_pages INTEGER,
    pages_scanned INTEGER,
    catalog_unique INTEGER,
    new_count INTEGER,
    deepest_new_page INTEGER,
    early_stop BOOLEAN NOT NULL DEFAULT false,
    full_sweep BOOLEAN NOT NULL DEFAULT true,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_full_at TIMESTAMPTZ
);
"""


def init_db() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        conn.execute(USAGE_SCHEMA)
        conn.execute(LIST_SWEEP_SCHEMA)
        conn.execute(
            """
            ALTER TABLE list_sweep
            ADD COLUMN IF NOT EXISTS last_full_at TIMESTAMPTZ
            """
        )
        for statement in MIGRATIONS:
            conn.execute(statement)
        conn.commit()


def known_tender_ids() -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT tender_id FROM tenders").fetchall()
    return {row["tender_id"] for row in rows}


def last_list_sweep() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM list_sweep WHERE id = 1").fetchone()
    if not row:
        return None
    out = dict(row)
    for key in ("finished_at", "last_full_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


def save_list_sweep(stats: dict) -> None:
    full = bool(stats.get("full_sweep", True))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO list_sweep (
                id, last_pages, pages_scanned, catalog_unique, new_count,
                deepest_new_page, early_stop, full_sweep, finished_at, last_full_at
            )
            VALUES (
                1, %(last_pages)s, %(pages_scanned)s, %(catalog_unique)s,
                %(new_count)s, %(deepest_new_page)s, %(early_stop)s,
                %(full_sweep)s, now(),
                CASE WHEN %(full_sweep)s THEN now() ELSE NULL END
            )
            ON CONFLICT (id) DO UPDATE SET
                last_pages = EXCLUDED.last_pages,
                pages_scanned = EXCLUDED.pages_scanned,
                catalog_unique = EXCLUDED.catalog_unique,
                new_count = EXCLUDED.new_count,
                deepest_new_page = COALESCE(
                    EXCLUDED.deepest_new_page, list_sweep.deepest_new_page
                ),
                early_stop = EXCLUDED.early_stop,
                full_sweep = EXCLUDED.full_sweep,
                finished_at = now(),
                last_full_at = CASE
                    WHEN EXCLUDED.full_sweep THEN now()
                    ELSE list_sweep.last_full_at
                END
            """,
            {
                "last_pages": int(stats.get("pages") or 0),
                "pages_scanned": int(stats.get("pages_scanned") or 0),
                "catalog_unique": int(stats.get("unique") or 0),
                "new_count": int(stats.get("new") or 0),
                "deepest_new_page": stats.get("deepest_new_page"),
                "early_stop": bool(stats.get("early_stop")),
                "full_sweep": full,
            },
        )
        conn.commit()


def record_captcha_image() -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO truecaptcha_usage (used_on, images)
            VALUES ((now() AT TIME ZONE 'Asia/Kolkata')::date, 1)
            ON CONFLICT (used_on) DO UPDATE
            SET images = truecaptcha_usage.images + 1
            """
        )
        conn.commit()


def captcha_usage() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT used_on, images
            FROM truecaptcha_usage
            ORDER BY used_on DESC
            """
        ).fetchall()
    return [
        {"used_on": row["used_on"].isoformat(), "images": row["images"]}
        for row in rows
    ]


def reset_interrupted() -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET status = 'seen', error = NULL, updated_at = now()
            WHERE status = 'downloading'
            """
        )
        conn.commit()


def upsert_seen(tender: dict) -> None:
    """Upsert a listed tender. Reappearing on the list clears the left-list hold."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tenders (
                tender_id, title, organisation, published_at, closing_at,
                opening_at, list_page
            )
            VALUES (
                %(tender_id)s, %(title)s, %(organisation)s, %(published_at)s,
                %(closing_at)s, %(opening_at)s, %(list_page)s
            )
            ON CONFLICT (tender_id) DO UPDATE SET
                title = EXCLUDED.title,
                organisation = EXCLUDED.organisation,
                published_at = EXCLUDED.published_at,
                closing_at = EXCLUDED.closing_at,
                opening_at = EXCLUDED.opening_at,
                list_page = EXCLUDED.list_page,
                -- Back on the closing list: drop the negative-cache hold and
                -- return failed-left-list rows to seen so download can retry.
                status = CASE
                    WHEN tenders.status IN ('downloaded', 'downloading')
                        THEN tenders.status
                    WHEN tenders.left_list_at IS NOT NULL
                        THEN 'seen'
                    ELSE tenders.status
                END,
                error = CASE
                    WHEN tenders.status IN ('downloaded', 'downloading')
                        THEN tenders.error
                    WHEN tenders.left_list_at IS NOT NULL
                        THEN NULL
                    ELSE tenders.error
                END,
                left_list_at = NULL,
                updated_at = now()
            """,
            tender,
        )
        conn.commit()


def update_list_page(tender_id: str, list_page: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET list_page = %s, updated_at = now()
            WHERE tender_id = %s
            """,
            (list_page, tender_id),
        )
        conn.commit()


def save_corrigenda(tender_id: str, items: list[dict]) -> None:
    payload = json.dumps(items)
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET corrigendum_count = %s,
                corrigenda = %s::jsonb,
                updated_at = now()
            WHERE tender_id = %s
            """,
            (len(items), payload, tender_id),
        )
        conn.execute("SELECT pg_notify('tender_status', %s)", (tender_id,))
        conn.commit()


def save_details(tender_id: str, details: dict) -> None:
    payload = json.dumps(details, ensure_ascii=False, default=str)
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET details = %s::jsonb,
                updated_at = now()
            WHERE tender_id = %s
            """,
            (payload, tender_id),
        )
        conn.execute("SELECT pg_notify('tender_status', %s)", (tender_id,))
        conn.commit()


LIFECYCLE_SQL = """
CASE
    WHEN closing_at IS NULL THEN 'undated'
    WHEN closing_at <= now() THEN 'expired'
    WHEN closing_at <= now() + interval '72 hours' THEN 'closing_soon'
    ELSE 'open'
END
"""


def pending_tenders() -> list[dict]:
    """Tenders still worth downloading.

    Skips expired rows and anything negative-cached via left_list_at (confirmed
    miss or past closing). Closing-soon first so workers race the deadline.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tenders
            WHERE status IN ('seen', 'failed')
              AND left_list_at IS NULL
              AND (closing_at IS NULL OR closing_at > now())
            ORDER BY closing_at ASC NULLS LAST, list_page, tender_id
            """
        ).fetchall()
    return rows


def expire_stale() -> int:
    """Mark past-closing tenders as left-list without spending a lookup."""
    message = "closing date has passed; tender left the closing list"
    with connect() as conn:
        rows = conn.execute(
            """
            UPDATE tenders
            SET status = 'failed',
                error = %s,
                left_list_at = COALESCE(left_list_at, now()),
                updated_at = now()
            WHERE status IN ('seen', 'failed', 'downloading')
              AND closing_at IS NOT NULL
              AND closing_at <= now()
            RETURNING tender_id
            """,
            (message,),
        ).fetchall()
        for row in rows:
            conn.execute("SELECT pg_notify('tender_status', %s)", (row["tender_id"],))
        conn.commit()
    return len(rows)


def hold_left_list(tender_id: str) -> None:
    """Negative-cache a confirmed miss until the next list refresh upserts it."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET left_list_at = COALESCE(left_list_at, now()),
                updated_at = now()
            WHERE tender_id = %s
            """,
            (tender_id,),
        )
        conn.commit()


def downloaded_tenders() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT tender_id, title, organisation, closing_at, extract_dir
            FROM tenders
            WHERE status = 'downloaded'
            ORDER BY tender_id
            """
        ).fetchall()
    return rows


def tenders_for_corr_refresh() -> list[dict]:
    """Downloaded tenders still on the closing window — candidates for corrigendum refresh."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT tender_id, title, organisation, published_at, closing_at,
                   opening_at, list_page, status, details, corrigenda,
                   corrigendum_count, extract_dir
            FROM tenders
            WHERE status = 'downloaded'
              AND (closing_at IS NULL OR closing_at > now())
            ORDER BY
              CASE WHEN COALESCE(corrigendum_count, 0) > 0 THEN 0 ELSE 1 END,
              closing_at ASC NULLS LAST,
              tender_id
            """
        ).fetchall()
    return [_public(row) for row in rows]


def claim(tender_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE tenders
            SET status = 'downloading', error = NULL, updated_at = now()
            WHERE tender_id = %s
              AND status IN ('seen', 'failed')
              AND left_list_at IS NULL
            RETURNING tender_id
            """,
            (tender_id,),
        ).fetchone()
        if row:
            conn.execute("SELECT pg_notify('tender_status', %s)", (tender_id,))
        conn.commit()
    return row is not None


def release_claim(tender_id: str) -> None:
    """Return a mid-download tender to seen (used when Stop interrupts a job)."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET status = 'seen', error = NULL, updated_at = now()
            WHERE tender_id = %s AND status = 'downloading'
            """,
            (tender_id,),
        )
        conn.execute("SELECT pg_notify('tender_status', %s)", (tender_id,))
        conn.commit()


def mark_downloaded(
    tender_id: str,
    zip_name: str,
    extract_dir: str,
    timings: dict | None = None,
) -> None:
    payload = json.dumps(timings or {}, ensure_ascii=False, default=str)
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET status = 'downloaded',
                zip_name = %s,
                extract_dir = %s,
                error = NULL,
                timings = %s::jsonb,
                downloaded_at = now(),
                updated_at = now()
            WHERE tender_id = %s
            """,
            (zip_name, extract_dir, payload, tender_id),
        )
        conn.execute("SELECT pg_notify('tender_status', %s)", (tender_id,))
        conn.commit()


def mark_failed(
    tender_id: str,
    error: str,
    timings: dict | None = None,
    left_list: bool | None = None,
) -> None:
    """Mark failed. left_list=True (or auto from error text) holds the row out of the queue."""
    if left_list is None:
        left_list = is_left_list_error(error)
    with connect() as conn:
        if timings is not None:
            payload = json.dumps(timings, ensure_ascii=False, default=str)
            conn.execute(
                """
                UPDATE tenders
                SET status = 'failed',
                    error = %s,
                    timings = %s::jsonb,
                    left_list_at = CASE
                        WHEN %s THEN COALESCE(left_list_at, now())
                        ELSE left_list_at
                    END,
                    updated_at = now()
                WHERE tender_id = %s
                """,
                (error[:500], payload, left_list, tender_id),
            )
        else:
            conn.execute(
                """
                UPDATE tenders
                SET status = 'failed',
                    error = %s,
                    left_list_at = CASE
                        WHEN %s THEN COALESCE(left_list_at, now())
                        ELSE left_list_at
                    END,
                    updated_at = now()
                WHERE tender_id = %s
                """,
                (error[:500], left_list, tender_id),
            )
        conn.execute("SELECT pg_notify('tender_status', %s)", (tender_id,))
        conn.commit()


def merge_timings(tender_id: str, patch: dict) -> None:
    """Shallow-merge keys into tenders.timings (e.g. index job adds index ms)."""
    if not patch:
        return
    payload = json.dumps(patch, ensure_ascii=False, default=str)
    with connect() as conn:
        conn.execute(
            """
            UPDATE tenders
            SET timings = COALESCE(timings, '{}'::jsonb) || %s::jsonb,
                updated_at = now()
            WHERE tender_id = %s
            """,
            (payload, tender_id),
        )
        conn.commit()


def timing_stats() -> dict:
    """Aggregates over tenders that have per-phase timings (Step 1 telemetry)."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                count(*) FILTER (WHERE timings ? 'lookup') AS with_lookup,
                count(*) FILTER (WHERE timings ? 'zip') AS with_zip,
                count(*) FILTER (WHERE timings ? 'extract') AS with_extract,
                round(avg((timings->'lookup'->>'ms')::numeric)
                      FILTER (WHERE timings ? 'lookup')) AS lookup_ms_avg,
                round(avg((timings->'lookup'->>'fetches')::numeric)
                      FILTER (WHERE timings ? 'lookup')) AS lookup_fetches_avg,
                round(avg((timings->'zip'->>'ms')::numeric)
                      FILTER (WHERE timings ? 'zip')) AS zip_ms_avg,
                count(*) FILTER (
                    WHERE (timings->'zip'->>'captcha')::boolean IS TRUE
                ) AS zip_captcha,
                count(*) FILTER (
                    WHERE timings ? 'zip'
                      AND COALESCE((timings->'zip'->>'captcha')::boolean, false) IS FALSE
                ) AS zip_reuse,
                round(avg((timings->'extract'->>'ms')::numeric)
                      FILTER (WHERE timings ? 'extract')) AS extract_ms_avg,
                round(avg((timings->'extract'->>'llm_ms')::numeric)
                      FILTER (WHERE timings->'extract' ? 'llm_ms')) AS llm_ms_avg,
                round(avg((timings->>'total_ms')::numeric)
                      FILTER (WHERE timings ? 'total_ms')) AS total_ms_avg,
                coalesce(sum((timings->'extract'->>'tokens')::numeric)
                         FILTER (WHERE timings->'extract' ? 'tokens'), 0) AS gemini_tokens,
                coalesce(sum((timings->'extract'->>'tokens_in')::numeric)
                         FILTER (WHERE timings->'extract' ? 'tokens_in'), 0) AS gemini_tokens_in,
                coalesce(sum((timings->'extract'->>'tokens_out')::numeric)
                         FILTER (WHERE timings->'extract' ? 'tokens_out'), 0) AS gemini_tokens_out,
                count(*) FILTER (WHERE timings->'extract' ? 'tokens') AS gemini_calls
            FROM tenders
            WHERE timings IS NOT NULL AND timings <> '{}'::jsonb
            """
        ).fetchone()
    if not row:
        return {}
    out = {}
    for key, value in row.items():
        if value is None:
            out[key] = None
        else:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                out[key] = value
    return out


def captcha_totals() -> dict:
    """Lifetime and recent 2Captcha image counts."""
    days = captcha_usage()
    total = sum(int(item.get("images") or 0) for item in days)
    return {"images_total": total, "days": days}


def list_departments() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                CASE
                    WHEN organisation IS NULL OR btrim(organisation) = '' THEN 'Unknown'
                    ELSE split_part(organisation, '||', 1)
                END AS department,
                count(*) AS total,
                count(*) FILTER (WHERE ({LIFECYCLE_SQL}) = 'open') AS open,
                count(*) FILTER (WHERE ({LIFECYCLE_SQL}) = 'closing_soon') AS closing_soon,
                count(*) FILTER (WHERE ({LIFECYCLE_SQL}) = 'expired') AS expired,
                count(*) FILTER (WHERE status = 'downloaded') AS downloaded
            FROM tenders
            GROUP BY 1
            ORDER BY total DESC, department
            """
        ).fetchall()
    return [
        {
            "department": row["department"],
            "total": row["total"],
            "open": row["open"],
            "closing_soon": row["closing_soon"],
            "expired": row["expired"],
            "downloaded": row["downloaded"],
        }
        for row in rows
    ]


def search_tenders(
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
    limit: int = 40,
    offset: int = 0,
    sort: str | None = None,
) -> dict:
    clauses: list[str] = []
    params: list = []

    def add_range(column: str, start: str | None, end: str | None) -> None:
        day = f"({column} AT TIME ZONE 'Asia/Kolkata')::date"
        if start:
            clauses.append(f"{day} >= %s")
            params.append(start)
        if end:
            clauses.append(f"{day} <= %s")
            params.append(end)

    add_range("published_at", published_from, published_to)
    add_range("closing_at", closing_from, closing_to)
    add_range("opening_at", opening_from, opening_to)
    if lifecycle in {"open", "closing_soon", "expired", "undated"}:
        clauses.append(f"({LIFECYCLE_SQL}) = %s")
        params.append(lifecycle)
    if status == "extracted":
        clauses.append("status = 'downloaded' AND details ? 'fields'")
    elif status in {"seen", "downloading", "downloaded", "failed"}:
        clauses.append("status = %s")
        params.append(status)
    if q:
        needle = f"%{q.strip()}%"
        clauses.append(
            "(tender_id ILIKE %s OR COALESCE(title, '') ILIKE %s OR COALESCE(organisation, '') ILIKE %s)"
        )
        params.extend([needle, needle, needle])
    if department:
        if department == "Unknown":
            clauses.append("(organisation IS NULL OR btrim(organisation) = '')")
        else:
            clauses.append(
                """
                CASE
                    WHEN organisation IS NULL OR btrim(organisation) = '' THEN 'Unknown'
                    ELSE split_part(organisation, '||', 1)
                END = %s
                """
            )
            params.append(department)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_by = {
        "closing_at_asc": "closing_at ASC NULLS LAST, tender_id",
        "closing_at_desc": "closing_at DESC NULLS LAST, tender_id",
        "published_desc": "published_at DESC NULLS LAST, tender_id",
        "published_asc": "published_at ASC NULLS LAST, tender_id",
        "updated_desc": "updated_at DESC, tender_id",
    }.get(sort or "", "closing_at ASC NULLS LAST, tender_id")
    with connect() as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM tenders {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT tender_id, title, organisation, published_at, closing_at,
                   opening_at, list_page, status, zip_name, extract_dir, error,
                   corrigendum_count, corrigenda, details, timings,
                   downloaded_at, updated_at,
                   ({LIFECYCLE_SQL}) AS lifecycle
            FROM tenders
            {where}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "tenders": [_public(row) for row in rows], "department": department}


def fetch_all() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT tender_id, title, organisation, published_at, closing_at,
                   opening_at, list_page, status, zip_name, extract_dir, error,
                   corrigendum_count, corrigenda,
                   downloaded_at, updated_at
            FROM tenders
            ORDER BY closing_at NULLS LAST, tender_id
            """
        ).fetchall()
    return [_public(row) for row in rows]


def fetch_one(tender_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT tender_id, title, organisation, published_at, closing_at,
                   opening_at, list_page, status, zip_name, extract_dir, error,
                   corrigendum_count, corrigenda, details, timings,
                   downloaded_at, updated_at
            FROM tenders WHERE tender_id = %s
            """,
            (tender_id,),
        ).fetchone()
    return _public(row) if row else None


def counts() -> dict:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, count(*) AS n FROM tenders GROUP BY status"
        ).fetchall()
        life = conn.execute(
            f"""
            SELECT
                count(*) FILTER (WHERE ({LIFECYCLE_SQL}) = 'open') AS open,
                count(*) FILTER (WHERE ({LIFECYCLE_SQL}) = 'closing_soon') AS closing_soon,
                count(*) FILTER (WHERE ({LIFECYCLE_SQL}) = 'expired') AS expired,
                count(*) FILTER (WHERE ({LIFECYCLE_SQL}) = 'undated') AS undated
            FROM tenders
            """
        ).fetchone()
    tallies = {row["status"]: row["n"] for row in rows}
    tallies["total"] = sum(tallies.values())
    tallies.update(
        {
            "open": life["open"],
            "closing_soon": life["closing_soon"],
            "expired": life["expired"],
            "undated": life["undated"],
        }
    )
    with connect() as conn:
        extracted = conn.execute(
            """
            SELECT count(*) AS n FROM tenders
            WHERE status = 'downloaded' AND details ? 'fields'
            """
        ).fetchone()
    tallies["extracted"] = extracted["n"]
    return tallies


def export_rows(status: str | None = "downloaded") -> list[dict]:
    """Rows for the field-listing export, including merged details."""
    clause = "WHERE status = %s" if status else ""
    params = (status,) if status else ()
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT tender_id, title, organisation, published_at, closing_at,
                   opening_at, status, details, corrigenda, corrigendum_count
            FROM tenders {clause}
            ORDER BY closing_at NULLS LAST, tender_id
            """,
            params,
        ).fetchall()
    return [_public(row) for row in rows]


def tenders_for_reextract(only_missing: bool = True) -> list[dict]:
    """Downloaded tenders that still need (or should refresh) field extraction."""
    clause = "AND NOT (details ? 'fields')" if only_missing else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT tender_id, title, organisation, published_at, closing_at,
                   opening_at, list_page, status, details, corrigenda
            FROM tenders
            WHERE status = 'downloaded' {clause}
            ORDER BY tender_id
            """
        ).fetchall()
    return [_public(row) for row in rows]


def _public(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif key == "corrigenda" and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                out[key] = []
        elif key in {"details", "timings"} and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                out[key] = {}
        else:
            out[key] = value
    if "corrigenda" not in out:
        out["corrigenda"] = []
    if "corrigendum_count" not in out:
        out["corrigendum_count"] = 0
    if "timings" not in out or out["timings"] is None:
        out["timings"] = {}
    return out
