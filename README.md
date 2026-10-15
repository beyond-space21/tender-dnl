# tender-dnl

Tamil Nadu GePNIC (tntenders.gov.in) tender operations: catalog scraping, packet
downloads (captcha-aware), corrigendum tracking, Gemini field extraction,
Typesense full-text search, and a dashboard on port 8091 (PM2: `tender-dnl`).

Run: `./venv/bin/python dashboard.py` (or `pm2 start ecosystem.config.cjs`).

## Versioned read API — `/api/v1`

Interactive docs: `http://<host>:8091/api/v1/docs` (Swagger) and
`/api/v1/redoc`. The OpenAPI schema is at `/api/v1/openapi.json`.

### Auth

Optional. If `API_KEY` is set in `.env`, every `/api/v1` request must send:

```
X-API-Key: <your key>
```

Unset `API_KEY` means the API is open.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/tenders` | Paginated tender list with filters |
| `GET /api/v1/tenders/{tender_id}` | Full tender payload (fields, sources, corrigenda, files) |
| `GET /api/v1/tenders/{tender_id}/corrigenda` | Corrigendum list + change map |
| `GET /api/v1/tenders/{tender_id}/files` | Packet file listing |
| `GET /api/v1/tenders/{tender_id}/files/{path}` | Download one packet file |
| `GET /api/v1/search` | Full-text search over packet files (Typesense) |
| `GET /api/v1/departments` | Department rollups |
| `GET /api/v1/stats` | Counts and lifecycle summary |
| `GET /api/v1/export` | xlsx/csv export (`?status=downloaded&format=csv`) |

### `GET /api/v1/tenders` query params

| Param | Values |
|---|---|
| `q` | substring match on id / title / organisation |
| `status` | `seen`, `downloading`, `downloaded`, `failed`, `extracted` |
| `lifecycle` | `open`, `closing_soon`, `expired`, `undated` |
| `department` | first segment of the organisation chain |
| `published_from` / `published_to`, `closing_from` / `closing_to`, `opening_from` / `opening_to` | `YYYY-MM-DD` inclusive ranges (IST) |
| `sort` | `closing_at_asc` (default), `closing_at_desc`, `published_desc`, `published_asc`, `updated_desc` |
| `page` / `page_size` | 1-based paging, `page_size` max 100 |

### Response shape

Lists use a consistent envelope:

```json
{
  "data": [ ... ],
  "meta": { "total": 4355, "page": 1, "page_size": 20, "pages": 218 }
}
```

`TenderSummary` fields: `tender_id`, `tender_reference`, `title`,
`organisation`, `department`, `status`, `lifecycle`, `published_at`,
`closing_at`, `opening_at`, `tender_fee_inr`, `emd_inr`, `work_value_inr`,
`has_corrigendum`.

`TenderDetail` adds `fields` (merged extraction), `field_sources`
(per-field provenance: `page`, `boq`, `llm`, `corrigendum_history`,
`corrigendum_pdf`, `page+corrigendum`), `corrigenda[]` (with `pdf_url`),
`files[]` (with `/api/v1` download URLs), `downloaded_at`, `error`.

Timestamps are ISO-8601; amounts are numbers or `null`; keys are never omitted.

### Examples

```bash
curl 'http://localhost:8091/api/v1/tenders?lifecycle=open&status=downloaded&page=1&page_size=10'
curl 'http://localhost:8091/api/v1/tenders/2026_TNPL_694483_1'
curl -H "X-API-Key: $API_KEY" 'http://localhost:8091/api/v1/search?q=boq'
```