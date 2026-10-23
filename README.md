# OpenRoute

OpenAI-совместимый API-шлюз, который балансирует клиентские запросы по пулу
**HTTP(S)-прокси** (sticky-сессии по прокси-URL + round-robin для новых задач) и
проксирует их в **free-tier инференс `opencode.ai`**. В отличие от NVRoute здесь
**нет апстрим-токенов**: член пула — это сам прокси. Каждый прокси-URL = одно
«ведро» квоты opencode.ai: входной IPv4 не считается выходным IP, потому что
провайдер может выпускать трафик через IPv6.

- При старте и периодически шлюз забирает список прокси из `px6-service`,
  который фильтрует их по комментарию **`OPENROUTE`** (аналог `USE_IN_API` в
  NVRoute) через API [px6.net](https://px6.net).
- В админке есть кнопка **«Обновить список»**: она заставляет gateway запросить
  `px6-service` с `?refresh=true` (сброс TTL-кэша), сразу обновить пул и
  перепроверить прокси.
- **Sticky-сессии**: задача (`x-opencode-session`, либо fingerprint диалога)
  закрепляется за одним прокси-URL — все её реплики идут через один и тот же
  выходной адрес, а новые задачи балансируются по score (сессии/запросы/токены).
- **Persona прокси**: у каждого прокси-URL своя синтетическая личность
  (session/project/trace), которая подставляется в upstream-заголовки; при
  исчерпании квоты или abusive-бане персона перегенерируется.
- **`429 FreeUsageLimitError`** (дневная квота) паркует только этот прокси-URL
  до ближайшей полуночи UTC; сессии эвакуируются на другие прокси. Любой другой
  429 — прозрачный passthrough без cooldown и ротации: лимиты провайдера/ключа
  не являются свойством конкретного прокси.
- **`403 abusive traffic`** навсегда банит только этот прокси-URL (флаг в БД),
  эвакуирует его сессии, и больше никогда его не выбирает.
- Round-robin по готовым прокси; при сбое запрос немедленно уходит на следующий
  прокси (каждый пробуется за запрос максимум один раз).
- Клиентская поверхность как у NVRoute: `/v1/*` (OpenAI passthrough) за
  клиентскими ключами (RPM/TPM, allow-list моделей), `/admin/status`,
  `/admin/logs`, `/health`, `/internal/*` (X-Internal-Token) и веб-админка.

Требования к клиенту — как у OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:53202/v1", api_key="sk-client-...")
client.chat.completions.create(model="deepseek-v4.1-flash-free",
                               messages=[{"role": "user", "content": "hi"}])
```

---

## Архитектура

### C4 — контекст (уровень 1)

```
                       ┌───────────────────────────────────────────────┐
   ┌──────────┐        │                  OpenRoute                    │
   │  Клиент  │        │   (OpenAI-совместимый шлюз над пулом прокси)  │
   │ (OpenAI  │ HTTPS  │                                               │
   │  SDK /   ├───────►│  Балансирует /v1/* по прокси и форвардит в    │
   │  curl /  │ :53202 │  free-tier инференс opencode.ai               │
   │  браузер)│        │                                               │
   └──────────┘        └───────┬───────────────────────────┬───────────┘
                               │ POST /inference/...       │ GET список
                               │ (через прокси)            │ прокси
                               ▼                           ▼
                     ┌───────────────────┐        ┌───────────────────┐
                     │   opencode.ai     │        │      px6.net      │
                     │ (free-tier инференс│        │ (источник прокси, │
                     │  + каталог моделей)│        │  descr=OPENROUTE) │
                     └───────────────────┘        └───────────────────┘
```

### C4 — контейнеры (уровень 2)

```
 Клиент/браузер ──► 0.0.0.0:53202 (edge, nginx — «стена», единственный
                     публичный порт; хост-порт задаёт OPENROUTE_EDGE_PORT)
                        ├─ /v1/*, /admin/status, /admin/logs, /health, /docs,
                        │    /openapi.json ─────────────────────────► [gateway] (data plane)
                        ├─ /login, /admin/ui*, /assets/* ──────────► [web-admin-ui] (React SPA)
                        └─ /api/admin/* ───────────────────────────► [web-admin] (JSON API)
                                                                         │
   (web-admin-ui — только статика; браузер сам ходит в /api/admin/* через edge)
   [gateway] ──► [openroute-px6] ──► px6.net       (список прокси, descr=OPENROUTE)
   [gateway] ──► [PostgreSQL]                       (request_log + proxies + keys + session_affinity + proxy_personas)
   [gateway] ──► opencode.ai                        (free-tier инференс через прокси)
   [web-admin] ──► [Redis]                          (серверные сессии админки)
   [web-admin] ──► [gateway] /internal/*            (X-Internal-Token, только внутри сети)
```

- **`edge/`** — nginx: единственная точка входа, маршрутизация на сервисы;
  `/internal/*` наружу не маршрутизируется вообще. SSE-стримы не буферизуются.
- **`gateway/`** — шлюз (data plane): `/v1/*`, пул прокси, клиентские
  ключи, лог запросов в PostgreSQL, internal API для админки. Своих хостовых
  портов не публикует.
- **`web-admin/`** — backend админки (control plane): JSON API
  `/api/admin/*`, сессии в Redis, CSRF, SSE; своими данными не владеет —
  ходит в шлюз по internal API.
- **`web-admin-ui/`** — React SPA (React + TypeScript + Vite + React Router +
  TanStack Query) в отдельном контейнере nginx. Секретов не получает; все
  данные — через `/api/admin/*`.
- **`px6-service/`** — микросервис списка прокси: обёртка над API px6.net с
  TTL-кэшем, фильтр `descr == "OPENROUTE"`.
- **`postgres`** — PostgreSQL 17: хранилище шлюза (`request_log`, `proxies`,
  `keys`, `session_affinity`, `proxy_personas`). Том `pgdata`; порт наружу не
  публикуется.
- **`redis`** — Redis 7: сессии админки (том `redisdata`), порт наружу не публикуется.


---

## Структура репозитория

```
OpenRoute/
├── README.md
├── docker-compose.yml        # весь стек одной командой (сеть openroute-net)
├── docker-compose.test.yml   # PostgreSQL для pytest (127.0.0.1:55433)
├── Dockerfile                # шлюз (python:3.12-slim)
├── requirements.txt          # запайнено: fastapi/uvicorn/httpx/pydantic-settings/psycopg
├── requirements-dev.txt      # + pytest/pytest-asyncio/redis
├── .env.example
├── gateway/                  # шлюз (data plane) — РЕАЛИЗОВАНО
│   ├── opencode.py           # gate-заголовки, session id, body, Retry-After, SSE, классификаторы 429/403
│   ├── sessions.py           # fingerprint диалога -> один upstream session id
│   ├── affinity.py           # sticky-сессии: привязки, score S/R/T, эвакуация
│   ├── affinitydb.py         # session_affinity: load/upsert/clear
│   ├── personas.py           # persona на прокси-bucket: кэш, ротация, retry/reconcile
│   ├── personadb.py          # proxy_personas: load/upsert с generation-гардом
│   ├── config.py  proxies.py  proxydb.py  keys.py  keydb.py  reqlog.py  pg.py
│   └── main.py               # FastAPI: passthrough /v1/*, ретраи, internal API, lifespan
├── edge/                     # nginx: nginx.conf, proxy_params_edge.conf, Dockerfile
├── px6-service/              # РЕАЛИЗОВАНО: px6.net → descr=OPENROUTE
├── web-admin/                # РЕАЛИЗОВАНО: JSON API (config, sessions, auth, gateway, schemas, main)
├── web-admin-ui/             # РЕАЛИЗОВАНО: React SPA (Vite, React Router, TanStack Query)
└── tests/                    # pytest: шлюз, прокси, ключи, лог, админка, opencode
```

---

## Правила пула

- Прокси автоматически подтягиваются из `PROXIES_URL`
  (`openroute-px6`, `descr=OPENROUTE`) каждые `PROXY_REFRESH_SECONDS`; при
  неудачном фетче — повтор через `PROXY_RETRY_SECONDS`.
- Добавить или удалить прокси вручную нельзя: членство в пуле определяется
  **только** фетчем; в админке доступны лишь включить/выключить существующий
  прокси.
- Список из `PROXIES_URL` синхронизируется с БД: новые прокси добавляются,
  пропавшие из источника помечаются `listed = FALSE` (soft-delete) и
  перестают участвовать в балансировке. Записи физически не удаляются, поэтому
  парковка/баны и привязки сессий переживают временное исчезновение прокси;
  если он снова появится в источнике, он возвращается в пул. Флаг `enabled`
  (ручное вкл/выкл) при синхронизации не сбрасывается.
- **Квоты**: каждый прокси-URL — отдельное ведро (`canonical_quota_key`).
  Прокси не объединяются по входному IPv4: выход может быть IPv6, и разные
  порты одного входного адреса могут иметь разные выходные адреса.
- **Парковка и баны переживают рестарт**: `quota_until`, `banned_until` и
  `permanently_banned` пишутся в таблицу `proxies` при получении
  `FreeUsageLimitError` / abusive-403 и загружаются при старте gateway. После
  пересборки контейнера админка снова показывает «лимит …» / «бан …», а
  запаркованные и навсегда забаненные прокси не опрашиваются и не выбираются.
- **Round-robin** по готовым прокси (не в cooldown, не отключён, не на локальном
  RPM-капе, не запаркован). Каждый прокси пробуется в запросе максимум один раз.
- **Circuit breaker** (сетевые ошибки / 5xx): ошибки копятся в скользящем окне
  `ACCOUNT_FAIL_WINDOW_SECONDS`; после `ACCOUNT_FAIL_THRESHOLD` — cooldown
  `ACCOUNT_COOLDOWN_SECONDS` с экспонентой ×2 до `ACCOUNT_COOLDOWN_MAX_SECONDS`
  (60 → 120 → … → 600 с). Успех сбрасывает счётчик и cooldown.
- **429 → классификация по телу** (см. «Sticky-сессии» ниже):
  `FreeUsageLimitError` → **парковка этого прокси-URL** до 00:00 UTC +
  `QUOTA_RESET_MARGIN_SECONDS`, сессии эвакуируются на другие прокси, персона
  bucket ротируется; любой другой 429 — прозрачный passthrough без cooldown,
  парковки и ротации.
  Механика cooldown/парковки отдельного прокси (`mark_failure` с 429,
  `ACCOUNT_THROTTLE_*`) сохранена для внутренних путей, но HTTP-обработчик
  её больше не вызывает.
- **403 `abusive traffic`** (case-insensitive поиск в теле) → **постоянный
  бан** этого прокси-URL (`permanently_banned = TRUE` в БД) с эвакуацией;
  персона bucket ротируется; `FreeTierError` — passthrough; прочие 403 —
  обычный failover (`mark_failure` + следующая попытка).
- **Ретраи**: 5xx/сетевые — повтор на следующем прокси с экспоненциальным
  backoff от `RETRY_BACKOFF_BASE_SECONDS`, суммарно не дольше
  `RETRY_BUDGET_SECONDS` (исчерпан → 504).
- **Сбой не убивает прокси навсегда**: устойчивые сетевые ошибки/5xx
  выражаются через cooldown, health-probe может снять его досрочно.
  Единственное необратимое состояние — abusive-бан выше.

### Квота: один прокси-URL = одно «ведро»

Квота free-tier opencode.ai привязана к IP клиента; для IPv6 сервер усекает
адрес до первых 4 групп (`/64`). Но из прокси-URL **нельзя узнать выходной
адрес**: вход может быть IPv4, а выход — IPv6, причём разные порты одного
входного IPv4 могут выходить через разные IPv6-адреса. Поэтому шлюз **не
объединяет прокси по входному IP**: каждое купленное подключение — отдельное
ведро (`canonical_quota_key`). Дневной лимит уточняется телами ответов:
`429 FreeUsageLimitError` → паркуется только этот прокси-URL до ближайшей
полуночи UTC (если `Retry-After` больше — берётся он); `403 abusive traffic` →
навсегда банится только этот прокси-URL.

---

## Sticky-сессии (affinity)

- Задача закрепляется за прокси-URL надолго: привязка хранится в
  `session_affinity` и не меняется, пока прокси не запаркован/забанен или
  пока он не отключён в админке.
- Идентификатор берётся из `x-opencode-session` / `x-session-affinity` /
  `x-session-id` (валидный `ses_...`); если его нет — из fingerprint диалога
  (ключ + проект + первое сообщение); одиночные запросы без истории привязку
  не создают, но учитываются в статистике прокси.
- Новые задачи распределяются по score `S+R+T`: S — число привязанных сессий,
  R — запросов за окно `AFFINITY_WINDOW_SECONDS`, T — реальных токенов за то
  же окно; веса — `AFFINITY_WEIGHT_*`. Кулдаун/локальный RPM-кап родного
  прокси не двигает привязку: текущий запрос временно уходит на другой адрес.
- **Требование к клиенту (комбайну)**: присылать стабильный валидный
  `x-opencode-session: ses_...` на задачу — тогда все её реплики гарантированно
  идут через один прокси (один выходной адрес). Без него шлюз сам выводит
  fingerprint диалога.
- Переменные: `AFFINITY_ENABLED`, `AFFINITY_WINDOW_SECONDS`,
  `AFFINITY_WEIGHT_SESSIONS`, `AFFINITY_WEIGHT_REQUESTS`,
  `AFFINITY_WEIGHT_TOKENS`, `QUOTA_RESET_MARGIN_SECONDS`. Abusive-бан не имеет
  настройки срока: он постоянный. В `/admin/status` — блок `affinity`
  (host-only), в `/health` — счётчики `egress_groups_*` и `sessions_*`.

---

## Persona прокси

- Одна персона на bucket `canonical_quota_key` (тот же bucket, что у парковки
  квоты; он же `manager.group_of(proxy).host`): валидные `session_id`
  (`ses_...`), `project_id` (40 hex), `trace_id` (32 hex) и `generation`.
- На каждом запросе через bucket клиентские `x-opencode-project`,
  `x-opencode-session`, `x-session-affinity`, `x-session-id`,
  `x-opencode-session-id`, `traceparent` и `b3` заменяются значениями
  персоны; клиентские `tracestate` и `x-b3-*` вычищаются. `traceparent` и
  `b3` пересобираются на каждый запрос: trace id — из персоны, span id —
  свежий. Multi-header B3 (`x-b3-*`) не синтезируется: только `traceparent`
  и одиночный `b3`.
- Разные диалоги и клиенты через один прокси делят один upstream session id,
  но affinity и балансировка не меняются: привязка по-прежнему ключуется
  клиентской сессией/fingerprint, персона применяется после выбора прокси.
- Ротация (generation + 1, новые id) — только при `429 FreeUsageLimitError`
  и `403 abusive traffic`. Обычный 429, `FreeTierError`, 401, 5xx и сетевые
  ошибки персону не ротируют.
- Персона хранится в PostgreSQL (`proxy_personas`, создаётся существующей
  схемой) и переживает рестарт; generation-гард не даёт старой записи
  перетереть новую. Запись — фоновая (fire-and-forget): при недоступной БД
  запросы не ждут и не ломаются, персона живёт в памяти, запись повторяется,
  а при расхождении стор примиряется с более новой строкой БД. Загрузка
  валидирует идентификаторы и заменяет битую строку валидной персоной.
- Переменная `PERSONA_ENABLED` (по умолчанию `true`); `false` — полный откат
  к прежнему форвардингу клиентских заголовков.

---

## Эндпоинты

Все пути — на едином порту edge (`http://127.0.0.1:53202`):

| Метод | Путь | Куда (edge) | Описание |
|-------|------|-------------|----------|
| ANY | `/v1/*` | gateway | Passthrough к opencode.ai (`chat/completions`, `models`, ...); стриминг поддержан |
| GET | `/` | edge | Редирект 307 на `/login` |
| GET | `/login` | web-admin-ui | SPA-страница входа (React Router) |
| GET | `/admin/ui`, `/admin/ui/keys` | web-admin-ui | SPA-страницы «Прокси» и «Ключи» |
| GET | `/assets/*` | web-admin-ui | Хэшированные статические файлы SPA |
| GET | `/api/admin/session` | web-admin | Текущая сессия + CSRF-токен (200 и без входа) |
| POST | `/api/admin/login` | web-admin | Вход по паролю; HttpOnly cookie сессии |
| POST | `/api/admin/logout` | web-admin | Выход (CSRF; сессия удаляется в Redis) |
| GET | `/api/admin/proxies?period=` | web-admin | JSON-снапшот пула: статусы, расходы, последние запросы |
| GET | `/api/admin/keys?period=` | web-admin | JSON-снапшот ключей и статистики |
| GET | `/api/admin/{proxies,keys}/events?period=` | web-admin | SSE с JSON-снапшотами (fallback — polling) |
| POST | `/api/admin/proxies/set` | web-admin | Включить/отключить прокси по `proxy_id` (CSRF) |
| POST | `/api/admin/proxies/refresh` | web-admin | Принудительно обновить список из px6 со сбросом TTL-кэша (CSRF) |
| POST | `/api/admin/keys/{add,set,delete}` | web-admin | Добавить/отозвать/удалить ключ (CSRF) |
| GET | `/admin/status` | gateway | Состояние пула: прокси, egress-группы, session affinity, счётчики, usage по ключам |
| GET | `/admin/logs?limit=N` | gateway | Последние запросы из лога (1..500, по умолчанию 100) |
| GET | `/health` | gateway | Liveness + счётчики пула, egress-групп и привязанных сессий |
| GET | `/docs`, `/openapi.json` | gateway | Swagger UI / схема |
| ANY | `/internal/*` | — (нет наружу) | Internal API шлюза; доступен только web-admin по `X-Internal-Token` |

`/admin/status` и `/admin/logs` требуют клиентский ключ с `"admin": true`.
`/internal/*` при пустом `INTERNAL_API_TOKEN` отвечает 404, при неверном токене
— 403.

---

## Таксономия ошибок

| Код | Когда | Тело |
|-----|-------|------|
| 401 | неизвестный/отозванный клиентский ключ | `{"error": {"message": "...", "type": "invalid_request_error", "code": 401}}` |
| 403 | модель вне allow-list ключа; неверный `X-Internal-Token` | OpenAI-конверт (шлюз отдаёт его и на `/internal/*`) |
| 403 | `abusive traffic` от opencode — навсегда банит прокси-URL и повторяет запрос на другом прокси | OpenAI-конверт, если не осталось прокси |
| 404 | внутренний API выключен (пустой `INTERNAL_API_TOKEN`); неизвестный прокси/ключ в мутации | OpenAI-конверт |
| 413 | тело запроса больше `MAX_BODY_BYTES` (без чтения тела при `Content-Length`) | OpenAI-конверт |
| 429 | превышены RPM/TPM ключа; все прокси запаркованы (квота до полуночи UTC / временный бан) — с `Retry-After` | `{"error": {"type": "rate_limit_error", ...}}` |
| 429 | любой другой 429 upstream (провайдер/ключ) | тело upstream (passthrough, без cooldown и ротации) |
| 502 | все прокси, которые пробовали, упали (`All upstream attempts failed`) | OpenAI-конверт |
| 503 | пул пуст (нет прокси / фетч не удаётся) | OpenAI-конверт |
| 504 | исчерпан бюджет ретраев `RETRY_BUDGET_SECONDS` | OpenAI-конверт |

---

## Запуск

### Docker (рекомендуется)

```bash
# перед первым запуском: скопируйте примеры и задайте секреты
cp .env.example .env                                   # INTERNAL_API_TOKEN
cp web-admin/.env.example web-admin/.env       # ADMIN_PASSWORD + тот же INTERNAL_API_TOKEN
cp px6-service/.env.example px6-service/.env     # PX6_API_KEY

docker compose up --build -d                 # весь стек одной командой; сеть openroute-net compose создаёт сам
docker compose logs -f edge                  # логи стены
docker compose logs -f gateway               # логи шлюза
docker compose down                          # остановка всего
```

Наружу смотрит **только edge** на http://127.0.0.1:53202 (хост-порт задаёт
`OPENROUTE_EDGE_PORT`): и API (`/v1`), и админка (`/`, `/login`, `/admin/ui`,
`/api/admin/*`).
Сервисы (gateway, web-admin, web-admin-ui, px6, postgres, redis) хостовых
портов не публикуют. Данные — в томах `pgdata`/`redisdata` (удаляются только
`docker compose down -v`). `docker compose stop` ждёт до 320 с
(`stop_grace_period`), чтобы стримы доработали до конца.

> **Сосуществование с NVRoute.** OpenRoute полностью изолирован от соседнего
> стека NVRoute: у него собственная docker-сеть `openroute-net` (не общая
> `service-net`), уникальные имена контейнеров (`openroute-px6`,
> `openroute-postgres`, `openroute-redis`; остальные — автоимена
> `openroute-gateway-*`, `openroute-web-admin-*` и `openroute-web-admin-ui-*`)
> и собственный хост-порт edge `OPENROUTE_EDGE_PORT` (по умолчанию 53202;
> NVRoute занимает 53201). Оба стека поднимаются одновременно и не
> конфликтуют.

### Локально без Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn gateway.main:app --host 127.0.0.1 --port 8000   # шлюз: /v1/*, /admin/*, /health, /internal/*
```

Нужен доступный PostgreSQL (по умолчанию
`POSTGRES_DSN=postgresql://openroute:openroute@localhost:5432/openroute`); схема
создаётся сама при первом обращении. Без Docker edge нет: backend админки
запускается отдельно (`web-admin/`), а SPA — через `npm run dev` в
`web-admin-ui/` (Vite проксирует `/api` на `127.0.0.1:8000`).

---

## Статус реализации

| Компонент | Статус |
|---|---|
| `gateway/` (шлюз: `main`, `config`, `proxies`, `proxydb`, `affinity`, `affinitydb`, `personas`, `personadb`, `keys`, `keydb`, `reqlog`, `pg`, `opencode`) | **реализовано** |
| `web-admin/` (JSON API: `config`, `sessions`, `auth`, `gateway`, `schemas`, `main`) | **реализовано** |
| `web-admin-ui/` (React SPA: Vite, React Router, TanStack Query, SSE) | **реализовано** |
| `px6-service/` | **реализовано** (px6.net → `descr=OPENROUTE`) |
| edge / compose / Dockerfile / requirements / .env.example | **готово** |
| `tests/` (`test_gateway`, `test_proxies`, `test_affinity`, `test_affinitydb`, `test_persona`, `test_personadb`, `test_keys`, `test_reqlog`, `test_admin_api`, `test_opencode`) | **реализовано** |

## Тесты

```bash
pip install -r requirements-dev.txt
pytest tests

cd web-admin-ui
npm ci
npm test
npm run typecheck
```

`tests/test_opencode.py` — чистые юнит-тесты интеграции с opencode.ai (не
требуют сети/БД); `tests/test_affinity.py` — unit-тесты привязок, score и
эвакуации (без БД); `tests/test_persona.py` — unit-тесты персоны (кэш,
ротация, retry/reconcile, fail-open, без БД); `tests/test_admin_api.py` —
герметичные тесты JSON API админки (fake gateway + in-memory sessions, без
Redis/PostgreSQL). Остальные наборы (`test_gateway`, `test_proxies`,
`test_affinitydb`, `test_personadb`, `test_keys`, `test_reqlog`) работают
герметично: upstream и админка подменяются, а живой PostgreSQL для хранилищ
шлюза (включая `session_affinity` и `proxy_personas`) поднимается из
`docker-compose.test.yml` (`127.0.0.1:55433`), DSN переопределяется
`OPENROUTE_TEST_DSN`. `test_personadb` проверяет generation-гард и загрузку
после рестарта. Фронтенд-тесты (`web-admin-ui`) — Vitest + Testing Library,
без сети и без backend.
