# OpenRoute — DESIGN (frozen interface spec)

> **Status:** frozen for phase 1. Two parallel implementers build against this
> document. Where this spec disagrees with code, the spec wins; change the spec
> first, then the code. Module/class/function names below ARE the contract.

OpenRoute is an OpenAI-compatible API gateway. Clients talk to it with a client
key; it round-robins the request across a pool of **HTTP(S) proxies** and
forwards to **opencode.ai free-tier inference**. Unlike NVRoute there are **no
upstream tokens** — a pool member *is* a proxy. One proxy exit address maps to
one opencode.ai quota bucket (see §8).

---

## 1. Module inventory (`app/`)

| Module | Responsibility | Phase |
|---|---|---|
| `app/main.py` | FastAPI app (data plane + internal API). Auth deps, passthrough `/v1/{rest:path}`, retry loop, SSE passthrough, error handlers, lifespan, periodic refresh loop. | 2 |
| `app/config.py` | `Settings` (pydantic-settings) + `get_settings()` (`lru_cache`). Field set is frozen (§6). | 2 |
| `app/opencode.py` | **Shared, self-contained** opencode.ai integration: gate headers, `new_session_id`, system-message builder, body builder, `Retry-After` parser, SSE helpers. Pure/stdlib-only. | **1 (done)** |
| `app/proxies.py` | `Proxy` + `ProxyManager`: round-robin, circuit breaker, 429 cooldown/parking, local RPM cap, health probes, upstream `httpx` clients. Mirrors `NVRoute/app/accounts.py`. | 2 |
| `app/proxydb.py` | `ProxyDB`: PostgreSQL persistence of the pool (table `proxies`), replacing NVRoute's `acctdb.py`. | 2 |
| `app/keys.py` | `KeyStore` + `KeyInfo`/`AuthError`/`RateLimitError`: client auth, model allow-list, RPM/TPM. Verbatim port of NVRoute. | 2 |
| `app/keydb.py` | `KeyDB`: PostgreSQL persistence of client keys (table `keys`). Verbatim port of NVRoute. | 2 |
| `app/reqlog.py` | `RequestLog` + `LogEntry`: request log in PostgreSQL (table `request_log`). Verbatim port; `account` column = **masked proxy**. | 2 |
| `app/pg.py` | Shared psycopg3 pool + `SCHEMA` DDL + `ensure_schema`. | 2 |

Non-gateway services: `proxy6-service/` (implemented in phase 1),
`admin-service/` (scaffold in phase 1, finished phase 2), `edge/` (nginx).

---

## 2. Gateway endpoints (contracts)

All served on the internal port `8000`; only `edge` (host port
`OPENROUTE_EDGE_PORT`, default `53202`) is public.

### Public / client surface (via edge)

| Method(s) | Path | Auth | Contract |
|---|---|---|---|
| ANY | `/v1/{rest:path}` | client key (Bearer) | OpenAI passthrough; body gated and forwarded to `{OPENCODE_BASE_URL}/{rest}` through one proxy. Streaming when the caller asked (`stream` truthy) *or* — because the gate forces streaming upstream — always SSE-packaged (see §7). |
| GET | `/admin/status` | client key with `admin: true` | `ProxyManager.status()` + `keys` (key ids) + `usage` (`RequestLog.summary`). |
| GET | `/admin/logs?limit=N` | client key with `admin: true` | Last N `RequestLog` rows, newest first; `limit` clamped to `1..500` (default 100). |
| GET | `/health` | none | Liveness + pool counters (see §2.1). |
| GET | `/docs`, `/openapi.json` | none | FastAPI docs/schema. |

Auth dependencies:
- `require_auth(request) -> KeyInfo` — parse `Authorization: Bearer <key>`,
  constant-time sha256 compare, 401 on unknown/revoked. **No** RPM/TPM here.
- `require_admin(key = Depends(require_auth)) -> KeyInfo` — 403 unless
  `key.is_admin`.
- RPM/TPM gates run *after* the model allow-list (a 403 must not burn quota),
  in `proxy_v1`, before the retry loop.

`/health` response keys (frozen):
```json
{"status": "ok", "provider": "OpenRoute",
 "proxies_total": 0, "proxies_enabled": 0, "proxies_manually_disabled": 0,
 "proxies_cooling": 0, "proxies_rpm_capped": 0, "last_proxies_fetch": null}
```

### Internal surface (never exposed through edge)

JSON endpoints for `admin-service`, guarded by `X-Internal-Token` (§3):

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/internal/proxies-data` | — | `{"status": <ProxyManager.status()>, "usage": <summary>, "usage_by_proxy": <summary_by_account>, "recent": [<log rows>]}` |
| POST | `/internal/proxies/set` | `{"proxy": str, "enabled": bool}` | `{"ok": true, "message": str}` or 404 if unknown |
| POST | `/internal/proxies/delete` | `{"proxy": str}` | `{"ok": true, "message": str}` or 404 if unknown |
| GET | `/internal/keys-data` | — | `{"keys": [<KeyDB.all()>], "usage": <summary>}` |
| POST | `/internal/keys/add` | `{"key","key_id","name","admin","rpm","tpm","models"}` | `{"ok": bool, "message": str}` |
| POST | `/internal/keys/set` | `{"key_hash": str, "enabled": bool}` | `{"ok": true, "message": str}` or 404 |
| POST | `/internal/keys/delete` | `{"key_hash": str}` | `{"ok": true, "message": str}` or 404 |

Internal request bodies are JSON objects, capped at 64 KiB (413 over). A
non-object / invalid JSON body → 400. Mutations mirror NVRoute's keys handlers
(sync the in-memory manager/store after the DB write). Proxy-pool membership
has **no manual-add path**: it is seeded exclusively by the periodic refresh
(§4).

---

## 3. Internal API auth (frozen)

`_require_internal(request)` on every `/internal/*` handler:

1. If `settings.internal_api_token == ""` → **404** `Internal API disabled
   (set INTERNAL_API_TOKEN)` (the whole internal API is "off").
2. Else compare `request.headers.get("x-internal-token", "")` to the token with
   `hmac.compare_digest` over UTF-8 bytes; mismatch → **403** `Invalid internal
   token`.

`edge` never routes `/internal/*`, so this is the second line of defence.

---

## 4. PostgreSQL DDL (frozen — `app/pg.py::SCHEMA`)

`request_log` — **identical** to NVRoute; note the `account` column stores the
**masked proxy** (host:port) in OpenRoute:

```sql
CREATE TABLE IF NOT EXISTS request_log (
    ts DOUBLE PRECISION NOT NULL,
    key_id TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    model TEXT,
    status INTEGER NOT NULL,
    latency_ms DOUBLE PRECISION NOT NULL,
    stream BOOLEAN NOT NULL,
    account TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log (ts);
CREATE INDEX IF NOT EXISTS idx_request_log_key ON request_log (key_id);
```

`proxies` — replaces NVRoute's `accounts`:

```sql
CREATE TABLE IF NOT EXISTS proxies (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proxy TEXT NOT NULL UNIQUE,       -- "http://user:pass@host:port"
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    added_at DOUBLE PRECISION NOT NULL
);
```

`keys` — identical to NVRoute (`key_hash UNIQUE, key_id, name, admin, rpm, tpm,
models JSONB, enabled, added_at`).

`ensure_schema(conn)` runs all DDL idempotently under
`pg_advisory_xact_lock(740514)`; the caller commits in the same transaction.

### Pool-membership semantics (frozen)

`proxies` is the source of truth for **membership + manual `enabled`**:

- Membership is seeded **exclusively** by the refresh: a proxy enters the pool
  only when `PROXIES_URL` advertises it (the manual «add proxy» path was
  removed at the operator's request — no internal endpoint, no admin form).
- On each refresh, `PROXIES_URL`'s list is **upserted**: unknown proxies are
  inserted with `enabled = TRUE`; existing rows keep their flag; rows are
  **never auto-deleted**.
- Manual admin actions mutate the same table and keep the runtime pool in step
  (`set_manually_disabled` / `remove_proxy`); unknown proxies → 404.
- Consequently: to permanently exclude a proxy that is still advertised by the
  proxy service, **disable** it (do not delete — the next refresh re-adds it).
  Deleting removes it from the pool until the next refresh re-inserts it.

---

## 5. Pool / breaker contract (`app/proxies.py`)

`Proxy` fields: `url, client, requests, errors, consecutive_errors,
cooldown_until (time.monotonic), manually_disabled, disabled, last_error`;
internal `_fails, _cooldown_n, _rpm, _throttle_n`. `Proxy.enabled` == in pool
and not manually disabled.

`ProxyManager` public API (stable; see module docstrings):
`__init__(settings)`, `apply_proxies(rows)`,
`remove_proxy(proxy)->bool`, `set_manually_disabled(proxy, disabled)->bool`,
`refresh_proxies()->bool`, `ensure_client(proxy)->httpx.AsyncClient`,
`record_request(proxy)`, `next_proxy(exclude=None)->Proxy`,
`mark_failure(proxy, reason, retry_after=None)`, `mark_success(proxy)`,
`status()->dict`, `aclose()`.

`next_proxy` — uniform round-robin over the **stable** `self.proxies` list
(index `_rr` advances past non-ready entries). Ready = enabled (in pool, not
manually disabled) **and** not cooling **and** not on the local RPM cap **and**
not in `exclude`. None ready → `NoReadyProxyError(retry_after)` where
`retry_after` = seconds until the earliest candidate frees up (end of cooldown
or RPM-window slot), or `None` when waiting is pointless.

`refresh_proxies` — `GET {PROXIES_URL}` (httpx, ~15 s timeout); payload is
`{"proxies": [...]}` or a `[...]` list; each element is a proxy string or a
`{host, port[, user, password]}` dict (reuse NVRoute's `_proxy_url`
normaliser). Malformed elements are skipped individually; a malformed top-level
payload is a fetch error (`False`). Dedup by URL. Upsert into `ProxyDB`. Health
probe the pool (§5.1). Rebuild via `apply_proxies`. Returns `True` on success,
`False` on fetch/parse error (caller retries after `PROXY_RETRY_SECONDS`).

### 5.1 Health probe

`_probe_proxy(url)`: `GET {OPENCODE_MODELS_URL}` through the proxy; alive iff
status `< 500` and `!= 407`; any transport error ⇒ dead. Timeout:
`HEALTH_CHECK_TIMEOUT` (connect 5 s). Probes run concurrently
(`asyncio.gather`) outside the lock. A dead proxy is *not* removed from the DB;
it is simply not "ready" this cycle (mark it cooling / leave it cooling).
Proxies are **never permanently dead** (`disabled` stays `False`); sustained
failure is expressed purely via cooldown. On refresh, a proxy that probes alive
may have its cooldown lifted early (mirrors NVRoute).

### 5.2 Local RPM cap

`record_request(proxy)` appends `time.monotonic()` to `proxy._rpm`; the window
is `RPM_WINDOW_SECONDS = 60`. Cap `OPENROUTE_RPM_LIMIT <= 0` disables it. The
free tier is daily/IP-keyed, so this is an optional proactive valve, not the
primary limiter.

---

## 6. Environment variables (frozen names + defaults)

Gateway (`app/config.py`), read from `.env`:

| Env var | Field | Default | Meaning |
|---|---|---|---|
| `INTERNAL_API_TOKEN` | `internal_api_token` | `""` | shared secret for `/internal/*`; empty = disabled |
| `POSTGRES_DSN` | `postgres_dsn` | `postgresql://openroute:openroute@localhost:5432/openroute` | PostgreSQL DSN |
| `PROXIES_URL` | `proxies_url` | `http://openroute-proxy6:8000/proxies` | proxy list source |
| `PROXY_REFRESH_SECONDS` | `proxy_refresh_seconds` | `300` | re-read period |
| `PROXY_RETRY_SECONDS` | `proxy_retry_seconds` | `10` | retry after a failed fetch |
| `OPENCODE_BASE_URL` | `opencode_base_url` | `https://opencode.ai/inference/openai/v1` | chat upstream base |
| `OPENCODE_MODELS_URL` | `opencode_models_url` | `https://opencode.ai/inference/v1/models` | probe/catalogue URL |
| `OPENCODE_MODEL` | `opencode_model` | `deepseek-v4.1-flash-free` | default model when the caller omits one |
| `OPENROUTE_RPM_LIMIT` | `openroute_rpm_limit` | `0` | local per-proxy RPM cap (0 = off) |
| `UPSTREAM_TIMEOUT` | `upstream_timeout` | `300` | upstream timeout, s |
| `CONNECT_TIMEOUT` | `connect_timeout` | `10` | connect timeout, s |
| `PROXY_COOLDOWN_DEFAULT_SECONDS` | `proxy_cooldown_default_seconds` | `60` | 429 cooldown w/o Retry-After |
| `ACCOUNT_FAIL_THRESHOLD` | `account_fail_threshold` | `3` | failures-in-window → cooldown |
| `ACCOUNT_FAIL_WINDOW_SECONDS` | `account_fail_window_seconds` | `60` | rolling failure window, s |
| `ACCOUNT_COOLDOWN_SECONDS` | `account_cooldown_seconds` | `60` | base cooldown, s |
| `ACCOUNT_COOLDOWN_MAX_SECONDS` | `account_cooldown_max_seconds` | `600` | exponential cooldown ceiling, s |
| `ACCOUNT_THROTTLE_THRESHOLD` | `account_throttle_threshold` | `3` | consecutive-429 count → park |
| `ACCOUNT_THROTTLE_COOLDOWN_SECONDS` | `account_throttle_cooldown_seconds` | `3600` | park duration, s |
| `HEALTH_CHECK_TIMEOUT` | `health_check_timeout` | `8` | proxy probe timeout, s |
| `RETRY_BUDGET_SECONDS` | `retry_budget_seconds` | `90` | total per-request retry budget, s |
| `RETRY_BACKOFF_BASE_SECONDS` | `retry_backoff_base_seconds` | `0.5` | exponential backoff base, s |
| `MAX_BODY_BYTES` | `max_body_bytes` | `20971520` | request body limit (20 MiB) |
| `ALERT_WEBHOOK_URL` | `alert_webhook_url` | `""` | Slack-compatible alert webhook; empty = off |

The `ACCOUNT_*` names are kept from NVRoute even though the members are
proxies, so the two projects stay greppable side by side.

`proxy6-service` env: `PX6_API_KEY` (required, no default),
`PX6_API_BASE` (`https://px6.link/api`), `CACHE_HOURS` (`8`).

`admin-service` env: `ADMIN_PASSWORD` (`""` = UI off), `ADMIN_SESSION_TTL_SECONDS`
(`43200`), `REDIS_URL` (`redis://redis:6379/0`),
`GATEWAY_INTERNAL_URL` (`http://openroute:8000`), `INTERNAL_API_TOKEN` (`""`),
`INTERNAL_TIMEOUT_SECONDS` (`30`).

---

## 7. opencode gate-injection contract (`app/opencode.py`)

Upstream: `POST {OPENCODE_BASE_URL}/chat/completions`; models at
`OPENCODE_MODELS_URL`. Server gate requires all of:

- **Headers** — `build_gate_headers(session_id)` returns exactly:
  `Authorization: Bearer public`, `Content-Type: application/json`,
  `User-Agent: opencode/latest/2.0.26/cli`, `x-opencode-client: cli`, and all
  four of `x-opencode-session`, `x-session-affinity`, `x-session-id`,
  `x-opencode-session-id` set to the same valid session id.
- **Session id** — `new_session_id()` = `ses_` + 12 hex + 14 base62; the 12 hex
  are `~((unix_ms << 12) + counter)` masked to 48 bits. A same-shaped token
  without the hex prefix ⇒ 403. Generate ONE id per upstream request.
- **Body** — `prepare_body(raw, model)` parses the caller JSON (object or
  empty object), sets `model`, injects the gate into the system message via
  `inject_gate_into_messages` (replace the first `role == "system"` content, or
  prepend one when absent), and forces `stream = true` +
  `stream_options = {"include_usage": true}`.
  `build_system_message(user_system)` = `KEY1\nKEY2\n\nNEUTRALIZE\n\n` +
  (`user_system` or `DEFAULT_SYSTEM`). Two quotes are mandatory; one ⇒ 403.
  **Tool-bearing requests are the exception** (`"tools": [...]`): the upstream
  free tier fingerprints them against the real OpenCode agent prompt and
  rejects a title-quote-prefixed hybrid (`403 FreeTierError`, verified
  black-box 2026-10-19), so `prepare_body` leaves such bodies verbatim — the
  real OpenCode client always sends its agent prompt together with its tools,
  which the upstream accepts. A `FreeTierError` verdict is a body-level
  rejection: `main` passes it through (403 `upstream_error` with the upstream
  message) instead of retrying other proxies or cooling them down.

The gateway MUST strip the client's `Authorization`/hop headers
(`main.HOP_HEADERS`) and MUST NOT forward them; it always uses
`build_gate_headers(new_session_id())`.

**Response** (`main.proxy_v1`): opencode always answers SSE
(`text/event-stream`, `data: {...}` lines, terminated by `data: [DONE]`).
Usage is in the final chunk's `usage` (`prompt_tokens`, `completion_tokens`,
`total_tokens`). Headers observed: `x-opencode-log-id`, `x-zen-model`,
`server: cloudflare`. Because the gate forces upstream streaming, the gateway
streams the response to the client regardless of the caller's `stream` flag
(NVRoute's "stream if caller asked" logic is superseded here; still accept and
ignore the client's flag, and still pass through SSE verbatim). Usage is
parsed on the fly with `parse_sse_line` / `usage_from_payload` for TPM
accounting (fallback estimate: bytes/4 when absent).

---

## 8. 429 → cooldown / parking semantics (frozen)

`mark_failure(proxy, reason, retry_after=None)`:

- **429** (when `retry_after is not None` or `"429" in reason`):
  - duration = `retry_after` if provided else `PROXY_COOLDOWN_DEFAULT_SECONDS`
    (60 s). `retry_after` comes from `opencode.parse_retry_after(response)`
    which parses **both** integer seconds and an HTTP-date; absent/unparseable
    ⇒ `None` ⇒ default 60 s.
  - increment `_throttle_n`; once it reaches `ACCOUNT_THROTTLE_THRESHOLD` (3),
    the proxy is **parked**: duration = `max(duration,
    ACCOUNT_THROTTLE_COOLDOWN_SECONDS)` (3600 s), counter reset. (There is no
    "release the proxy" step — the member *is* the proxy.) `_throttle_n` resets
    only on success.
  - set `cooldown_until = max(cooldown_until, now + duration)`; clear `_fails`.
    **No sleep** — the request is retried on the next proxy immediately.
- **Network error / 5xx**: append to `_fails`; prune to
  `ACCOUNT_FAIL_WINDOW_SECONDS`; once `len(_fails) >= ACCOUNT_FAIL_THRESHOLD`,
  cooldown = `min(ACCOUNT_COOLDOWN_SECONDS * 2**(_cooldown_n-1),
  ACCOUNT_COOLDOWN_MAX_SECONDS)` (60→120→…→600), `_cooldown_n += 1`, clear
  `_fails`, then retry the next proxy **with** an exponential backoff sleep
  (`RETRY_BACKOFF_BASE_SECONDS * 2**(attempt-1)`), capped by the remaining
  retry budget.

`mark_success(proxy)` clears `consecutive_errors`, `_fails`, `_throttle_n`, and
`cooldown_until`.

### No-ready-proxy outcomes (`NoReadyProxyError`, mirrors NVRoute)

- All candidates already tried and failed → **502** `All upstream attempts failed`.
- First selection and the pool is merely saturated (cooling / RPM-capped but
  will free up) → **429 + `Retry-After`** (ceil seconds to the earliest slot).
- Pool permanently empty (no proxies configured, fetch failing) → **503**
  `No enabled proxies` (generic; diagnostics stay admin-only in
  `/admin/status` → `last_proxies_fetch`).
- Retry budget exhausted → **504** `Upstream retry budget exhausted`.

`main` keeps a per-request `tried: set[str]` of proxy URLs so each proxy is
attempted at most once per request.

---

## 9. Client keys (unchanged from NVRoute)

`KeyStore` (sha256 hashes only; `hmac.compare_digest`; 60 s sliding RPM/TPM
windows; TPM is a pre-request gate and a post-response recorder). 401 unknown/
revoked, 403 model outside allow-list, 429 over RPM/TPM with `Retry-After`.
`/admin/*` requires `admin: true`. Managed only through the admin UI.

---

## 10. Response/error envelope & conventions

- Errors are OpenAI-shaped: `{"error": {"message": str, "type": str,
  "code": int}}` (`invalid_request_error`, `rate_limit_error`, ...). 3xx with a
  `Location` header are real redirects, not errors. This applies to the gateway
  (`main`); `admin-service` uses FastAPI's `{"detail": ...}`.
- Upstream response headers: drop `content-encoding`, `content-length`,
  `transfer-encoding`, `connection` (`main.RESP_DROP_HEADERS`).
- Secrets are masked in logs/status: `proxies.mask_proxy` → `host:port`;
  client keys are never stored/echoed in plaintext.
- Single event loop, single worker (in-memory counters). SSE passthrough with
  no buffering; `stop_grace_period: 320s` in compose.
