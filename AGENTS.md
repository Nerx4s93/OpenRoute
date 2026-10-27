# OpenRoute — техническая документация

OpenAI-совместимый шлюз: принимает `/v1/*` за клиентскими ключами, выбирает
HTTP(S)-прокси и проксирует запрос в free-tier `opencode.ai`. Апстрим-токенов
нет: единица пула и квоты — прокси-URL. Прокси не группируются по входному IP:
выход может быть IPv6, и разные порты одного IPv4 могут выходить с разных
адресов.

## Архитектура

```text
Клиент → edge:53202 → gateway (/v1, /health, /docs)
                    → web-admin-ui (/login, /admin, /assets)
                    → web-admin (/api/admin)
web-admin → gateway /internal/*; web-admin → auth-service → Redis
gateway → storage-service → PostgreSQL; gateway → px6-service; gateway → opencode.ai
```

- **`edge/`** — nginx, единственный публичный порт (`OPENROUTE_EDGE_PORT`,
  по умолчанию 53202): маршрутизация, SSE без буферизации; `/internal/*`
  наружу не маршрутизируется.
- **`gateway/`** — data plane: `/v1/*`, пул прокси, клиентские ключи,
  internal API. Состояние читает и пишет только через storage-service;
  к PostgreSQL, Redis и auth-service не подключён.
- **`storage-service/`** — единственный владелец SQL и `POSTGRES_DSN`.
  Домены: proxies, keys, affinity, personas, request-log/stats. HTTP без
  токена в приватной сети; без него gateway не стартует.
- **`auth-service/`** — единственный владелец `ADMIN_PASSWORD`,
  `ADMIN_SESSION_TTL_SECONDS`, Redis-сессий, CSRF и login-throttling.
  `web-admin` ходит к нему по HTTP.
- **`web-admin/`** — JSON API `/api/admin/*`, HttpOnly cookie и SSE.
  Пароль, сессии и CSRF не хранит; ходит в gateway и auth-service.
- **`web-admin-ui/`** — React SPA, только статика; данные — через
  `/api/admin/*`.
- **`px6-service/`** — обёртка API px6.net, единственный владелец
  `PX6_API_KEY`: каталог прокси, баланс, страны, цена, покупка.
- **`postgres`** — PostgreSQL 17, том `pgdata`.
- **`redis`** — Redis 7, сессии админки, том `redisdata`.

```text
openroute-net             edge, gateway, web-admin, web-admin-ui   (не internal)
openroute-storage-api-net gateway, storage-service                 (internal)
openroute-storage-db-net  storage-service, postgres                (internal)
openroute-auth-net        web-admin, auth-service                  (internal)
openroute-redis-net       auth-service, redis                      (internal)
openroute-px6-net         gateway, px6-service                     (не internal)
```

Все сети создаёт Compose; `docker network create` не нужен. Наружу
публикуется только edge. `internal: true` убирает default route:
storage-service, auth-service, PostgreSQL и Redis не ходят наружу, а gateway
не видит PostgreSQL, Redis и auth-service.

У storage-service и auth-service нет собственной аутентификации: единственный
клиент в их приватных сетях — gateway и web-admin соответственно.
`X-Internal-Token` остаётся defense-in-depth для `/internal/*`: edge этот
префикс не маршрутизирует даже при ошибке конфигурации. OpenRoute изолирован
от NVRoute: собственные сети, имена контейнеров и порт edge 53202 (NVRoute
занимает 53201).

## Прокси-пул

- Источник — `PROXIES_URL` (`px6-service`), обновление каждые
  `PROXY_REFRESH_SECONDS`, повтор при ошибке через `PROXY_RETRY_SECONDS`;
  возвращаются все активные прокси.
- Новые прокси, появившиеся при покупке или ручном обновлении списка,
  добавляются **включёнными** (`enabled = TRUE`) и сразу участвуют в
  ротации; новые прокси, впервые замеченные фоновым обновлением, добавляются
  выключенными (`enabled = FALSE`). Флаг `enabled` существующих прокси
  сохраняется при обновлении списка. Добавить или удалить прокси вручную
  нельзя — членство определяется только фетчем.
- Синхронизация идёт через storage-service (`POST /proxies/sync`): новые
  добавляются с флагом из `enable_new` (покупка/ручное обновление — `true`,
  фоновый цикл — `false`), исчезнувшие помечаются `listed = FALSE`
  (soft-delete) и не выбираются. Записи не удаляются, поэтому парковка, баны
  и affinity переживают временное исчезновение прокси. Если storage-service
  недоступен, снимок источника не применяется (fail-closed), а уже
  загруженный пул продолжает работать.
- Квота: каждый прокси-URL — отдельное ведро (`canonical_quota_key`).
  Прокси не объединяются по входному IPv4.
- Выбор — round-robin по готовым прокси (не выключен вручную, не в cooldown,
  не запаркован, не на локальном RPM-капе). Каждый прокси пробуется за запрос
  максимум один раз.
- **429 `FreeUsageLimitError`**: парковка только этого прокси-URL до
  ближайшей полуночи UTC + `QUOTA_RESET_MARGIN_SECONDS` (если `Retry-After`
  больше — берётся он), эвакуация сессий и ротация персоны. Любой другой
  429 — прозрачный passthrough без cooldown, парковки и ротации.
- **403 `abusive traffic`**: постоянный бан прокси-URL, эвакуация сессий и
  ротация персоны. `FreeTierError` — passthrough; прочие 403 — обычный
  failover на следующий прокси.
- **5xx/сетевые ошибки**: следующий прокси с backoff от
  `RETRY_BACKOFF_BASE_SECONDS`; исчерпан `RETRY_BUDGET_SECONDS` → 504.
  Circuit breaker: окно `ACCOUNT_FAIL_WINDOW_SECONDS`, порог
  `ACCOUNT_FAIL_THRESHOLD`, cooldown `ACCOUNT_COOLDOWN_SECONDS` с
  экспонентой ×2 до `ACCOUNT_COOLDOWN_MAX_SECONDS`; успех сбрасывает
  счётчик и cooldown, health-probe может снять cooldown досрочно.
- Парковка и баны (`quota_until`, `permanently_banned`) хранятся в
  storage-service и переживают рестарт gateway.

## Sticky-сессии (affinity)

- Задача закрепляется за прокси-URL; привязка хранится в
  `session_affinity` и не меняется, пока прокси не запаркован, не забанен
  или не отключён в админке.
- Идентификатор — `x-opencode-session` / `x-session-affinity` /
  `x-session-id` (валидный `ses_...`), иначе fingerprint диалога (ключ +
  проект + первое сообщение). Одиночные запросы без истории привязку не
  создают, но учитываются в статистике.
- Новые задачи распределяются по score `S+R+T`: S — число привязанных
  сессий, R — запросов за `AFFINITY_WINDOW_SECONDS`, T — реальных токенов
  за то же окно; веса настраиваются.
- Кулдаун или локальный RPM-кап родного прокси не двигает привязку: текущий
  запрос временно уходит на другой адрес.
- Запись привязок — фоновая и best-effort. После рестарта S
  восстанавливается из storage-service, R/T начинаются с нуля.
- Клиенту следует присылать стабильный `x-opencode-session: ses_...`, тогда
  все реплики задачи гарантированно идут через один прокси.

## Persona прокси

- Одна персона на bucket `canonical_quota_key`: валидные `session_id`
  (`ses_...`), `project_id` (40 hex), `trace_id` (32 hex) и `generation`.
- На каждом запросе identity-заголовки клиента заменяются значениями
  персоны; `traceparent` и `b3` пересобираются со свежими span id.
- **Строгий allowlist**: наружу уходят только заголовки реального OpenCode
  CLI. `x-stainless-*`, `origin`/`referer`/`sec-*`, `cookie`, `x-api-key`,
  `x-forwarded-*`, `baggage` и кастомные заголовки не форвардятся.
- Значения и порядок заголовков — в `gateway/opencode.py`. Тело
  chat-запроса приводится к клиенту: `stream: true`,
  `stream_options.include_usage: true`, дефолтный
  `max_completion_tokens: 32000` (клиентское значение не перетирается),
  `user` удаляется, query-параметры отбрасываются.
- Ротация (generation + 1, новые id) — только при
  `429 FreeUsageLimitError` и `403 abusive traffic`.
- Персона хранится в `proxy_personas` с generation-гардом; запись фоновая,
  при недоступном storage-service повторяется с backoff. Загрузка
  валидирует идентификаторы.
- `PERSONA_ENABLED=false` — форвардятся клиентские identity-заголовки;
  allowlist при этом всё равно действует.

## Статистика токенов

- На странице `/admin/stats` — расход по UTC-дням и таблица прокси. Периоды:
  `1d`, `3d`, `7d`, `30d`, `90d`, `all`; в выборку попадают только прокси,
  у которых были запросы за период.
- `GET /internal/stats-data?days=30` или `?period=7d` (`X-Internal-Token`)
  отдаёт `days`, `server_time` (epoch), `dates`, `totals`, `proxies`,
  `series_total`, `series_by_proxy` — параллельные массивы
  `tokens`/`requests`/`errors` по дням. Для `period=all` окно начинается с
  самого раннего дня `request_log`.
- `GET /api/admin/stats?days=30` или `?period=7d` (нужна сессия) отдаёт те же
  поля с ISO-8601 `server_time` в UTC и `Cache-Control: no-store`.
- `GET /api/admin/proxies` дополнительно отдаёт `totals.tokens_total`,
  `summary.proxies_enabled` и необязательный объект `px6` у строки.
  Учётные данные прокси в браузер не попадают.
- Агрегаты строятся storage-service по `request_log`; наружу выходят только
  masked-имена `host:port`.
- При недоступном storage-service статистика отвечает 200 с нулями и
  пустыми сериями, «последние запросы» берутся из локального deque gateway.
  Запись лога — fire-and-forget: записи, не доехавшие во время сбоя, могут
  не сохраниться; уже сохранённые данные не теряются.

## Прокси (px6.net)

- Покупка: версии **3 — IPv4 Shared**, **4 — IPv4**, **6 — IPv6**; периоды
  **3/7/14/30/60/90 дней**; страна ISO2, количество и необязательный
  комментарий. Новые и купленные прокси добавляются выключенными.
- После покупки gateway обновляет пул с `?refresh=true`. Если покупка
  прошла, а обновление нет, ответ успешен с `refresh_ok: false` — оператору
  нужно нажать «Обновить список».
- `PX6_API_KEY` знает только px6-service; логин/пароль прокси хранится в
  storage-service. Панель получает masked-имя `host:port`, HMAC-идентификатор
  и несекретные поля px6 (`id`, `version`, `country`, `date_end`, `descr`).
- `px6-service` кэширует `/proxies` на `CACHE_HOURS`; покупка инвалидирует
  кэш. Остальные методы ходят в px6.net напрямую.

## TLS-фингерпринт апстрима

- opencode.ai снимает фингерпринт TLS ClientHello: `httpx`, `curl` и
  имперсонация Chrome получают мгновенный `429 FreeUsageLimitError`
  независимо от тела запроса, а выходной IP помечается. Проходит только
  подлинный отпечаток реального клиента (Bun/BoringSSL).
- Поэтому upstream-транспорт — `gateway/curlclient.py` поверх `curl_cffi`:
  httpx-совместимый фасад, воспроизводящий ClientHello байт-в-поле. Константы
  (`OPENCODE_TLS_JA3`, `OPENCODE_TLS_SIGALGS`, `OPENCODE_ACCEPT_ENCODING`,
  `OPENCODE_HEADER_ORDER`) — в `gateway/opencode.py`; применяются к каждому
  запросу, включая health-пробу.
- `curl_cffi` запинован в requirements к версии, на которой отпечаток
  сверялся с живым ClientHello. Ошибки транспорта пробрасываются как
  `httpx.TransportError`.

## API

| Путь | Компонент | Назначение |
|------|-----------|------------|
| `/v1/*` | gateway | OpenAI passthrough, включая streaming |
| `/health`, `/docs`, `/openapi.json` | gateway | публичные |
| `/`, `/login`, `/admin/*`, `/assets/*` | web-admin-ui | SPA; `/` → 307 `/login`, `/admin` → `/admin/stats` |
| `/api/admin/*` | web-admin | JSON API, HttpOnly cookie, CSRF на мутациях |
| `/internal/*` | gateway | только web-admin по `X-Internal-Token`; edge не маршрутизирует |

`/api/admin`: `session`, `login`, `logout`, `proxies`, `stats`, `keys`,
`proxies/{set,refresh,account,countries,price,buy}`,
`keys/{add,rotate,set,delete}`, `{proxies,keys}/events` (SSE).

`/internal`: `proxies-data`, `keys-data`, `stats-data`,
`proxies/{refresh,set}`, `px6/{account,countries,price,buy}`,
`keys/{add,rotate,set,delete}`. Пустой `INTERNAL_API_TOKEN` → 404, неверный
токен → 403.

`storage-service` слушает `openroute-storage:8000` только в
`openroute-storage-api-net`; хост-порта, публичного `/docs` и собственной
аутентификации нет. Контракты — в `storage-service/README.md`.

`auth-service` доступен как `openroute-auth:8000` в `openroute-auth-net`
(второй интерфейс — Redis). Маршруты: `GET /health`, `GET /auth/status`,
`POST /auth/login`, `POST /auth/validate`, `POST /auth/logout`. Сессии лежат
в Redis под `openroute:session:<sid>` как `{"csrf", "expires"}`; throttling —
10 неудач за 60 секунд на client_ip. Пустой `ADMIN_PASSWORD` → 404 на
login/validate/logout, недоступный Redis → 503 без fail-open. При отказе
auth-service `/api/admin/*` и `/health` web-admin отвечают 503; пароль,
сессии и CSRF не кэшируются. Контракты — в `auth-service/README.md`.

## Клиентские ключи

- Секрет генерирует gateway: `openroute-` + 48 hex-символов. В
  storage-service хранится только SHA-256 (`key_hash`), а также `key_id`,
  `rpm`, `tpm`, `models`, `enabled`; plaintext не логируется.
- `add` и `rotate` возвращают секрет только в ответе на мутацию; UI
  показывает его один раз. Снапшоты и SSE plaintext не содержат, ответы идут
  с `Cache-Control: no-store`.
- Ключи не имеют ролей и дают доступ только к `/v1/*`; модели ограничиваются
  allow-list.
- RPM/TPM — in-memory скользящие окна на `key_id`; сбрасываются при
  рестарте и перевыпуске. `key_id` не гарантирует уникальность, статистика
  по одинаковым значениям агрегируется.
- Мутации идут через storage-service; при его недоступности — 503 без
  изменения in-memory состояния.

## Таксономия ошибок

Все ошибки gateway — OpenAI-конверт, кроме passthrough-ответов upstream.

| Код | Когда |
|-----|-------|
| 401 | неизвестный или отозванный клиентский ключ |
| 403 | модель вне allow-list; неверный `X-Internal-Token` |
| 403 | `abusive traffic` от opencode — прокси банится, запрос повторяется на другом |
| 404 | internal API выключен; неизвестный прокси/ключ в мутации |
| 413 | тело больше `MAX_BODY_BYTES` |
| 429 | превышены RPM/TPM ключа или все прокси запаркованы; с `Retry-After` |
| 429 | любой другой 429 upstream — passthrough без cooldown и ротации |
| 502 | все испробованные прокси упали |
| 503 | пул пуст; storage-service недоступен при мутации |
| 504 | исчерпан `RETRY_BUDGET_SECONDS` |

Админка: `404` — пустой `ADMIN_PASSWORD`; `401` — нет/истекла сессия или
неверный пароль; `403` — неверный CSRF; `429` — больше 10 неудачных попыток
входа за минуту с одного IP; `503` — auth-service или Redis недоступен.

## Запуск

```bash
cp .env.example .env
cp storage-service/.env.example storage-service/.env
cp auth-service/.env.example auth-service/.env
cp web-admin/.env.example web-admin/.env
cp px6-service/.env.example px6-service/.env

docker compose up --build -d
docker compose logs -f gateway
docker compose down
```

Наружу смотрит только edge на `http://127.0.0.1:53202`; порт задаёт
`OPENROUTE_EDGE_PORT`. Остальные сервисы хост-портов не публикуют. Данные — в
томах `pgdata`/`redisdata` (удаляются `docker compose down -v`).

`POSTGRES_DSN` получает только storage-service; `ADMIN_PASSWORD`,
`ADMIN_SESSION_TTL_SECONDS` и `REDIS_URL` — только auth-service;
`PX6_API_KEY` — только px6-service. Gateway знает `STORAGE_SERVICE_URL`,
web-admin — `AUTH_SERVICE_URL` и `GATEWAY_INTERNAL_URL`. Compose поднимает
auth-service после healthy Redis, web-admin — после gateway и auth-service.
Схема storage-service идемпотентно применяется при старте; отдельная
миграция не нужна.

Локально без Docker:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r storage-service/requirements.txt \
  -r auth-service/requirements.txt -r web-admin/requirements.txt

docker run --rm -p 127.0.0.1:6379:6379 redis:7-alpine

cd storage-service && uvicorn openroute_storage.main:app --port 8010 --no-access-log
cd px6-service && uvicorn app.main:app --port 8001
cd auth-service && REDIS_URL=redis://127.0.0.1:6379/0 \
  uvicorn app.main:app --port 8011 --no-access-log
STORAGE_SERVICE_URL=http://127.0.0.1:8010 \
PROXIES_URL=http://127.0.0.1:8001/proxies \
PX6_SERVICE_URL=http://127.0.0.1:8001 \
  uvicorn gateway.main:app --port 8003
cd web-admin && AUTH_SERVICE_URL=http://127.0.0.1:8011 \
  GATEWAY_INTERNAL_URL=http://127.0.0.1:8003 \
  uvicorn app.main:app --port 8000
```

Локальные порты: storage `8010`, px6 `8001`, auth `8011`, gateway `8003`,
web-admin `8000`. Без Docker edge нет; SPA запускается через `npm run dev` в
`web-admin-ui/` (Vite проксирует `/api` на `127.0.0.1:8000`).

## Тесты

```bash
pip install -r requirements-dev.txt
pytest tests

pytest tests/test_storage_service.py -q   # нужен PostgreSQL 127.0.0.1:55433

cd web-admin-ui
npm ci
npm test
npm run typecheck
npm run build
```

`tests/test_storage_service.py` — единственный набор с живым PostgreSQL:
`docker-compose.test.yml`, DSN переопределяется `OPENROUTE_TEST_DSN`.
Остальные тесты герметичны и используют `tests/fake_storage.py` или моки;
PostgreSQL для них не нужен. Фронтенд — Vitest + Testing Library.
