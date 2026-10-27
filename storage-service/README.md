# storage-service

Private persistence microservice for OpenRoute. It is the **only** component
that talks to PostgreSQL: the gateway and every other service use this domain
HTTP API. The database schema is a frozen, idempotent DDL historically derived
from the gateway's former in-process SQL layer; storage-service is now the sole
SQL owner. There are no migrations, no new tables and no new columns, so an
existing `pgdata` volume keeps working unchanged.

The service is not published by the edge, has no host port and exposes no
public docs/OpenAPI route. It listens on `8000` in `openroute-storage-api-net`
(gateway) and reaches PostgreSQL through the separate private
`openroute-storage-db-net`.

## Security

- No authentication: the service is reachable only by the gateway on the
  private, `internal` Docker network `openroute-storage-api-net`. It has no
  host port and no edge route, so network isolation is the access boundary.
- Every endpoint (`/health` included) is therefore open to the gateway on that
  network; no token is sent or required.
- Database failures return a fixed `503`/`500` message; driver messages, SQL
  and the DSN are never returned to callers.
- Startup waits for the first PostgreSQL connection and applies the schema.
  If PostgreSQL is unreachable the service **fails to start** with a clear,
  DSN-free error instead of serving half-broken requests.

### Error envelope

All application errors (404/409/503) use:

```json
{
  "error": {
    "message": "Proxy not found",
    "type": "not_found_error",
    "code": 404
  }
}
```

Validation errors use FastAPI's standard `422` body. `500` is deliberately
generic (`"Storage operation failed"`) and never contains driver details.

## Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `POSTGRES_DSN` | `postgresql://openroute:openroute@localhost:5432/openroute` | The only DSN in the stack |
| `DB_POOL_MIN_SIZE` | `1` | Async pool minimum |
| `DB_POOL_MAX_SIZE` | `8` | Async pool maximum |
| `DB_POOL_TIMEOUT_SECONDS` | `5` | Wait for a pooled connection |
| `DB_CONNECT_TIMEOUT_SECONDS` | `5` | TCP connect timeout |
| `DB_STARTUP_TIMEOUT_SECONDS` | `15` | Startup waits this long for the DB |
| `DB_APPLICATION_NAME` | `openroute-storage` | `application_name` in `pg_stat_activity` |

Copy `.env.example` to `.env` and set:

- `POSTGRES_DSN` — the only DSN in the stack. The gateway never receives it;
  compose overrides it with the `postgres` service host.

The repository root `.env` needs no storage credential: the gateway only
needs `STORAGE_SERVICE_URL` and reaches this service over the private network.

## Quick start (Docker Compose)

From the repository root:

```bash
cp .env.example .env                                  # if not created yet
cp storage-service/.env.example storage-service/.env  # POSTGRES_DSN
docker compose up --build -d
```

Compose wiring is complete: the stack includes `storage-service` (container
`openroute-storage`, private port `8000`, no host port), it depends on
`postgres`, and the gateway depends on this service being healthy. Only `edge`
publishes a host port; there is no edge route to this service.

## Endpoints

All bodies are JSON. Timestamps/epochs are `time.time()`-style wall-clock
seconds unless stated otherwise.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | `SELECT 1` check; open on the private network |
| GET | `/proxies?listed=` | All proxy rows in insertion order |
| POST | `/proxies/sync` | Apply one px6 catalogue snapshot |
| POST | `/proxies/set-enabled` | Manual on/off of one proxy |
| POST | `/proxies/parking` | Persist quota parking / bans |
| GET | `/keys` | All client keys (hashes only) |
| POST | `/keys` | Insert a key by hash |
| POST | `/keys/rotate` | Replace a key hash in place |
| POST | `/keys/set-enabled` | Revoke / enable by hash |
| DELETE | `/keys/{key_hash}` | Delete by hash |
| GET | `/affinity` | All sticky bindings |
| POST | `/affinity` | Upsert bindings |
| POST | `/affinity/clear` | Unbind one or more sessions |
| GET | `/personas` | All personas |
| GET | `/personas/{quota_key}` | One persona (`quota_key` URL-encoded) |
| POST | `/personas` | Upsert with generation guard |
| POST | `/request-log` | Append one request-log row |
| GET | `/request-log/recent?limit=` | Newest rows first |
| GET | `/stats/summary?since=` | Per-key aggregates + `total` |
| GET | `/stats/by-account?since=` | Per-masked-proxy aggregates |
| GET | `/stats/daily?since=` | Per-UTC-day/per-account series |

---

## Health

`GET /health` — open on the private gateway network.

```json
{"status": "ok", "database": "ok"}
```

If the database cannot answer `SELECT 1`, returns `503`:

```json
{"status": "degraded", "database": "unavailable"}
```

## Proxies

### `GET /proxies?listed=`

- omit `listed` → every row, including soft-removed ones (needed to restore
  parking/ban state);
- `listed=true` → the live pool;
- `listed=false` → rows absent from the latest source snapshot.

Rows are ordered by insertion id. Credentials in `proxy` are the real proxy
URLs; this service is private and the gateway is their only consumer.

```json
{
  "proxies": [
    {
      "proxy": "http://u:p@proxy-a.invalid:8080",
      "enabled": true,
      "added_at": 1779235200.0,
      "quota_until": 0.0,
      "banned_until": 0.0,
      "permanently_banned": false,
      "listed": true
    }
  ]
}
```

### `POST /proxies/sync`

One successful `PROXIES_URL` snapshot. Duplicate and empty strings are ignored.
The whole operation runs in one transaction:

- new URLs are inserted with `enabled = enable_new` (optional request field,
  default `false`);
- known rows keep `enabled`, parking and ban state;
- every fetched URL becomes `listed = true`, every other row
  `listed = false` (soft delete, never a physical delete);
- returns the number of newly inserted rows and the resulting **all** rows.

Request:

```json
{"proxies": ["http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"]}
```

`enable_new: true` inserts newly discovered URLs enabled; existing rows keep
their current flag. The gateway uses it for purchases and explicit admin
refreshes, while the background loop leaves new rows disabled.

Response:

```json
{
  "added": 2,
  "proxies": [
    {"proxy": "http://proxy-a.invalid:8080", "enabled": false, "added_at": 1779235200.1, "quota_until": 0.0, "banned_until": 0.0, "permanently_banned": false, "listed": true},
    {"proxy": "http://proxy-b.invalid:8080", "enabled": false, "added_at": 1779235200.1, "quota_until": 0.0, "banned_until": 0.0, "permanently_banned": false, "listed": true}
  ]
}
```

An empty `proxies` list is valid: it marks every existing row `listed=false`
and adds nothing.

### `POST /proxies/set-enabled`

```json
{"proxy": "http://proxy-a.invalid:8080", "enabled": true}
```

Response `200`:

```json
{"proxy": "http://proxy-a.invalid:8080", "enabled": true, "updated": true}
```

Unknown proxy → `404`.

### `POST /proxies/parking`

Writes both epoch values as-is for every URL of one quota bucket and ORs
`permanently_banned` (a permanent ban is never removed). Expired epochs are
harmless and are overwritten by the next mark.

```json
{
  "proxies": ["http://proxy-a.invalid:8080"],
  "quota_until": 1779321600.0,
  "banned_until": 0.0,
  "permanently_banned": false
}
```

Response:

```json
{"updated": 1}
```

## Keys

Only SHA-256 hex digests are stored. `key_hash` must be exactly 64 lowercase
hex characters; plaintext secrets sent here are rejected with `422`.
`key_hash` (UNIQUE) identifies the exact row. `key_id` is a **non-unique
label**, so every mutation targets `key_hash`, never `key_id`.

### `GET /keys`

Rows in insertion order:

```json
{
  "keys": [
    {
      "key_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "key_id": "k1",
      "rpm": 60,
      "tpm": null,
      "models": ["deepseek-v4.1-flash-free"],
      "enabled": true
    }
  ]
}
```

`rpm`/`tpm` `null` = unlimited; `models` `null` = all models.

### `POST /keys`

```json
{
  "key_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "key_id": "k1",
  "rpm": 60,
  "tpm": null,
  "models": ["deepseek-v4.1-flash-free"]
}
```

Response:

```json
{"created": true}
```

A duplicate `key_hash` is not an error: returns `{"created": false}` and leaves
the existing row untouched (old `ON CONFLICT DO NOTHING` semantics).

### `POST /keys/rotate`

```json
{
  "key_hash": "<old hash>",
  "new_key_hash": "<new hash>"
}
```

The row keeps `key_id`, limits, models and `enabled`; the old hash stops
working immediately.

- success: `{"rotated": true, "already_rotated": false, "key_id": "k1"}`
- retry after a lost response with the **same** pair: the old hash is gone and
  the new one exists → `{"rotated": false, "already_rotated": true, "key_id": "k1"}`
  (idempotent success);
- neither hash exists → `404`;
- the new hash already belongs to a different row → `409`.

### `POST /keys/set-enabled`

```json
{"key_hash": "<hash>", "enabled": false}
```

Response: `{"key_hash": "<hash>", "enabled": false, "updated": true}`;
unknown hash → `404`.

### `DELETE /keys/{key_hash}`

Response: `{"key_hash": "<hash>", "deleted": true}`; unknown hash → `404`.

## Affinity

`schema.session_affinity` keeps `proxy REFERENCES proxies(proxy) ON DELETE SET
NULL`. A move updates only `proxy` and `last_used_at`; `created_at` is never
changed. Rows are never deleted on a move; `clear` stores `proxy = NULL` while
keeping `created_at` and the session's S slot.

### `GET /affinity`

Ordered by `created_at`:

```json
{
  "bindings": [
    {
      "session_key": "sid:ses_abc",
      "proxy": "http://proxy-a.invalid:8080",
      "created_at": 1779235200.0,
      "last_used_at": 1779235260.0
    }
  ]
}
```

### `POST /affinity`

```json
{
  "bindings": [
    {
      "session_key": "sid:ses_abc",
      "proxy": "http://proxy-a.invalid:8080",
      "created_at": 1779235200.0,
      "last_used_at": 1779235260.0
    }
  ]
}
```

Response:

```json
{"written": 1, "unbound": 0}
```

If a referenced proxy has disappeared (race with a soft/hard removal), the
batch is retried row by row and that binding is stored with `proxy = null`
(`unbound` counts these rows). Healthy bindings in the same batch are kept —
the same row-by-row FK fallback the gateway used before the split.

`GET /affinity` after a proxy row is physically deleted shows
`"proxy": null` for its sessions (FK `ON DELETE SET NULL`).

### `POST /affinity/clear`

```json
{"session_keys": ["sid:ses_abc", "fp:deadbeef"]}
```

Response: `{"cleared": 2}` — number of rows updated (missing keys are ignored).

## Personas

One persona per canonical quota bucket (`host:port`, credentials stripped).
`generation` is a guard: an older generation can never overwrite a newer one;
a retry with the same generation may rewrite the row (old `<=` semantics).

### `GET /personas`

Ordered by `created_at`:

```json
{
  "personas": [
    {
      "quota_key": "proxy-a.invalid:8080",
      "session_id": "ses_...",
      "project_id": "<40 hex>",
      "trace_id": "<32 hex>",
      "created_at": 1779235200.0,
      "generation": 3
    }
  ]
}
```

### `GET /personas/{quota_key}`

`quota_key` is URL-encoded (IPv6 brackets/colons included). Returns the same
object, or `404`.

### `POST /personas`

```json
{
  "quota_key": "proxy-a.invalid:8080",
  "session_id": "ses_...",
  "project_id": "<40 hex>",
  "trace_id": "<32 hex>",
  "created_at": 1779235200.0,
  "generation": 3
}
```

Response:

```json
{"written": true}
```

`written=false` means the generation guard rejected a stale write; the stored
persona is unchanged.

## Request log and stats

### `POST /request-log`

One row, exactly the gateway's `LogEntry` shape (the payload is committed
before the response):

```json
{
  "ts": 1779235200.0,
  "key_id": "k1",
  "method": "POST",
  "path": "chat/completions",
  "model": "deepseek-v4.1-flash-free",
  "status": 200,
  "latency_ms": 123.4,
  "stream": true,
  "account": "1.2.3.4:8080",
  "prompt_tokens": 10,
  "completion_tokens": 5,
  "error": null
}
```

Response: `{"logged": true}`.

### `GET /request-log/recent?limit=100`

`limit` is clamped by validation to `1..1000`. Newest first:

```json
{
  "entries": [
    {
      "ts": 1779235200.0,
      "key_id": "k1",
      "method": "POST",
      "path": "chat/completions",
      "model": "deepseek-v4.1-flash-free",
      "status": 200,
      "latency_ms": 123.4,
      "stream": true,
      "account": "1.2.3.4:8080",
      "prompt_tokens": 10,
      "completion_tokens": 5,
      "error": null
    }
  ],
  "limit": 100
}
```

### `GET /stats/summary?since=`

Old `RequestLog.summary`: per-key aggregates plus `total`. `since` is an epoch
cutoff (`ts >= since`, inclusive); omit it for all time. Errors are counted
when `status >= 400 OR error IS NOT NULL`; tokens are
`COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)`; latency is the
mean in ms rounded to one decimal.

```json
{
  "k1": {"requests": 2, "errors": 1, "tokens": 160, "avg_latency_ms": 200.0},
  "k2": {"requests": 1, "errors": 0, "tokens": 50, "avg_latency_ms": 200.0},
  "total": {"requests": 3, "errors": 1, "tokens": 210, "avg_latency_ms": 200.0}
}
```

### `GET /stats/by-account?since=`

Old `summary_by_account`: per masked proxy (`account`), NULL accounts excluded.
Used by `/internal/proxies-data` (lifetime usage) and the stats page.

```json
{
  "1.2.3.4:8080": {"requests": 3, "errors": 1, "tokens": 115},
  "5.6.7.8:3128": {"requests": 1, "errors": 0, "tokens": 2}
}
```

Without `since`, this is the lifetime per-proxy snapshot (the gateway's TTL
cache is a gateway-side concern; this endpoint always reads the committed log).

### `GET /stats/daily?since=0`

Old `daily_by_account`: one row per `(UTC day, account)` at or after `since`,
sorted by `(day, account)`. The bucket is `FLOOR(ts / 86400)` formatted as a
UTC `YYYY-MM-DD`, independent of the DB/session timezone. NULL accounts are
excluded. Used by `/internal/stats-data` for the token chart.

```json
[
  {"day": "2026-05-20", "account": "a:1", "requests": 2, "errors": 1, "tokens": 16},
  {"day": "2026-05-21", "account": "b:2", "requests": 1, "errors": 0, "tokens": 5}
]
```

## Running locally

```bash
cd storage-service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # POSTGRES_DSN
uvicorn openroute_storage.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Or with `PYTHONPATH=storage-service` from the repository root:

```bash
uvicorn openroute_storage.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Compose wiring is complete (see Quick start above); to build the image alone:

```bash
docker build -t openroute-storage ./storage-service
```

## Tests

```bash
pytest tests/test_storage_service.py -q
```

Tests use the isolated PostgreSQL database from `tests/conftest.py`
(`pg_dsn`, one throwaway database per test) and `fastapi.testclient.TestClient`;
no network or real secrets are needed. They cover the open private surface,
every endpoint contract/error path, schema creation and repeat-startup
idempotency.
This is the only suite that needs PostgreSQL: use `docker-compose.test.yml`
(`127.0.0.1:55433`) or point `OPENROUTE_TEST_DSN` at a live instance. Gateway
tests are hermetic and use `tests/fake_storage.py`.
