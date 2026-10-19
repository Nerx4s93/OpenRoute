# OpenRoute

OpenAI-совместимый API-шлюз, который балансирует клиентские запросы по пулу
**HTTP(S)-прокси** (round-robin) и проксирует их в **free-tier инференс
`opencode.ai`**. В отличие от NVRoute здесь **нет апстрим-токенов**: член пула —
это сам прокси. Каждый выходной адрес прокси = отдельное «ведро» квоты
opencode.ai.

- При старте и периодически шлюз забирает список прокси из `proxy6-service`,
  который фильтрует их по комментарию **`OPENROUTE`** (аналог `USE_IN_API` в
  NVRoute) через API [px6.net](https://px6.net).
- Round-robin по готовым прокси; при сбое запрос немедленно уходит на следующий
  прокси (каждый пробуется за запрос максимум один раз).
- **HTTP 429** от opencode → прокси уходит в cooldown по заголовку
  `Retry-After` (парсятся и целые секунды, и HTTP-date; без заголовка — 60 с).
  Несколько 429 подряд → долгая «парковка» (по умолчанию 3600 с).
- Клиентская поверхность как у NVRoute: `/v1/*` (OpenAI passthrough) за
  клиентскими ключами (RPM/TPM, allow-list моделей), `/admin/status`,
  `/admin/logs`, `/health`, `/internal/*` (X-Internal-Token) и веб-админка.

Полный контракт (модули, эндпоинты, DDL, env, семантика 429) — в
[`DESIGN.md`](DESIGN.md).

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
                        │    /openapi.json ─────────────────────────► [openroute] (data plane)
                        └─ /, /login, /admin/ui* ──────────────────► [admin-service] (control plane)
                                                                         │
   [openroute] ──► [openroute-proxy6] ──► px6.net      (список прокси, descr=OPENROUTE)
   [openroute] ──► [PostgreSQL]                        (request_log + proxies + keys)
   [openroute] ──► opencode.ai                         (free-tier инференс через прокси)
   [admin-service] ──► [Redis]                         (серверные сессии админки)
   [admin-service] ──► [openroute] /internal/*         (X-Internal-Token, только внутри сети)
```

- **`edge/`** — nginx: единственная точка входа, маршрутизация на сервисы;
  `/internal/*` наружу не маршрутизируется вообще. SSE-стримы не буферизуются.
- **`app/` (openroute)** — шлюз (data plane): `/v1/*`, пул прокси, клиентские
  ключи, лог запросов в PostgreSQL, internal API для админки. Своих хостовых
  портов не публикует.
- **`admin-service/`** — сайт-админка (control plane): `/login`, `/admin/ui*`,
  сессии в Redis, CSRF; своими данными не владеет — ходит в шлюз по internal API.
- **`proxy6-service/`** — микросервис списка прокси: обёртка над API px6.net с
  TTL-кэшем, фильтр `descr == "OPENROUTE"`.
- **`postgres`** — PostgreSQL 17: хранилище шлюза (`request_log`, `proxies`,
  `keys`). Том `pgdata`; порт наружу не публикуется.
- **`redis`** — Redis 7: сессии админки (том `redisdata`), порт наружу не публикуется.

---

## Структура репозитория

```
OpenRoute/
├── DESIGN.md                 # замороженный контракт (модули, эндпоинты, DDL, env)
├── README.md
├── docker-compose.yml        # весь стек одной командой (сеть openroute-net)
├── docker-compose.test.yml   # PostgreSQL для pytest (127.0.0.1:55433)
├── Dockerfile                # шлюз (python:3.12-slim)
├── requirements.txt          # запайнено: fastapi/uvicorn/httpx/pydantic-settings/psycopg
├── requirements-dev.txt      # + pytest/pytest-asyncio/redis
├── .env.example
├── app/                      # шлюз (data plane) — РЕАЛИЗОВАНО
│   ├── opencode.py           # gate-заголовки, session id, body, Retry-After, SSE
│   ├── sessions.py           # fingerprint диалога -> один upstream session id
│   ├── config.py  proxies.py  proxydb.py  keys.py  keydb.py  reqlog.py  pg.py
│   └── main.py               # FastAPI: passthrough /v1/*, ретраи, internal API, lifespan
├── edge/                     # nginx: nginx.conf, proxy_params_edge.conf, Dockerfile
├── proxy6-service/           # РЕАЛИЗОВАНО: px6.net → descr=OPENROUTE
├── admin-service/            # РЕАЛИЗОВАНО: config, sessions, adminui, main
└── tests/                    # pytest: шлюз, прокси, ключи, лог, админка, opencode
```

---

## Правила пула

- Прокси автоматически подтягиваются из `PROXIES_URL`
  (`openroute-proxy6`, `descr=OPENROUTE`) каждые `PROXY_REFRESH_SECONDS`; при
  неудачном фетче — повтор через `PROXY_RETRY_SECONDS`.
- Добавить прокси вручную нельзя: членство в пуле определяется **только**
  фетчем; в админке доступны лишь включить/выключить/удалить существующий
  прокси.
- Список прокси **добавляется** в БД (upsert); существующие записи не удаляются
  и не сбрасывают свой флаг `enabled`. Чтобы навсегда исключить прокси, который
  всё ещё отдаётся сервисом, — **отключите** его (удалённый вернётся на
  следующем обновлении). См. `DESIGN.md §4`.
- **Round-robin** по готовым прокси (не в cooldown, не отключён, не на локальном
  RPM-капе). Каждый прокси пробуется в запросе максимум один раз.
- **Circuit breaker** (сетевые ошибки / 5xx): ошибки копятся в скользящем окне
  `ACCOUNT_FAIL_WINDOW_SECONDS`; после `ACCOUNT_FAIL_THRESHOLD` — cooldown
  `ACCOUNT_COOLDOWN_SECONDS` с экспонентой ×2 до `ACCOUNT_COOLDOWN_MAX_SECONDS`
  (60 → 120 → … → 600 с). Успех сбрасывает счётчик и cooldown.
- **429 → cooldown**: `Retry-After` (секунды или HTTP-date) принадлежит
  *конкретному прокси*: он уходит в cooldown на это время (без заголовка —
  `PROXY_COOLDOWN_DEFAULT_SECONDS`, 60 с), а запрос **немедленно** уходит на
  следующий прокси — без ожидания. `ACCOUNT_THROTTLE_THRESHOLD` (3) подряд
  429-кулдаунов → **парковка** на `ACCOUNT_THROTTLE_COOLDOWN_SECONDS` (час).
- **Ретраи**: 5xx/сетевые — повтор на следующем прокси с экспоненциальным
  backoff от `RETRY_BACKOFF_BASE_SECONDS`, суммарно не дольше
  `RETRY_BUDGET_SECONDS` (исчерпан → 504).
- **Прокси никогда не «мёртв» навсегда**: устойчивый сбой выражается только
  через cooldown; health-probe периодически проверяет прокси и может снять
  cooldown досрочно.

### Один прокси = одно «ведро» IP

Квота free-tier opencode.ai привязана к IP клиента; для IPv6 сервер усекает
адрес до первых 4 групп (`/64`), т.е. **одна /64 = одно ведро**. Ротация внутри
одной /64 бесполезна, а exit-адрес узла общий для всех его пользователей —
бюджет делится. Поэтому пул строится из **разных** прокси-выходов: один прокси
= один независимый дневной лимит. 429 обычно означает «лимит до полуночи UTC»,
и его `Retry-After` — секунды до полуночи (прокси уходит в cooldown до сброса).

---

## Эндпоинты

Все пути — на едином порту edge (`http://127.0.0.1:53202`):

| Метод | Путь | Куда (edge) | Описание |
|-------|------|-------------|----------|
| ANY | `/v1/*` | openroute | Passthrough к opencode.ai (`chat/completions`, `models`, ...); стриминг поддержан |
| GET | `/` | admin-service | Редирект на `/login` |
| GET/POST | `/login` | admin-service | Вход в админку (только пароль) |
| GET | `/admin/status` | openroute | Состояние пула: прокси, счётчики, usage по ключам |
| GET | `/admin/logs?limit=N` | openroute | Последние запросы из лога (1..500, по умолчанию 100) |
| GET | `/admin/ui` | admin-service | Веб-админка: страница «Прокси» |
| GET | `/admin/ui/keys` | admin-service | Веб-админка: страница «Ключи» |
| GET | `/health` | openroute | Liveness + счётчики пула |
| GET | `/docs`, `/openapi.json` | openroute | Swagger UI / схема |
| ANY | `/internal/*` | — (нет наружу) | Internal API шлюза; доступен только admin-service по `X-Internal-Token` |

`/admin/status` и `/admin/logs` требуют клиентский ключ с `"admin": true`.
`/internal/*` при пустом `INTERNAL_API_TOKEN` отвечает 404, при неверном токене
— 403.

---

## Таксономия ошибок

| Код | Когда | Тело |
|-----|-------|------|
| 401 | неизвестный/отозванный клиентский ключ | `{"error": {"message": "...", "type": "invalid_request_error", "code": 401}}` |
| 403 | модель вне allow-list ключа; неверный `X-Internal-Token` | OpenAI-конверт (шлюз отдаёт его и на `/internal/*`) |
| 404 | внутренний API выключен (пустой `INTERNAL_API_TOKEN`); неизвестный прокси/ключ в мутации | OpenAI-конверт |
| 413 | тело запроса больше `MAX_BODY_BYTES` (без чтения тела при `Content-Length`) | OpenAI-конверт |
| 429 | превышены RPM/TPM ключа; пул прокси насыщен (cooldown/кап) — с `Retry-After` | `{"error": {"type": "rate_limit_error", ...}}` |
| 502 | все прокси, которые пробовали, упали (`All upstream attempts failed`) | OpenAI-конверт |
| 503 | пул пуст (нет прокси / фетч не удаётся) | OpenAI-конверт |
| 504 | исчерпан бюджет ретраев `RETRY_BUDGET_SECONDS` | OpenAI-конверт |

---

## Запуск

### Docker (рекомендуется)

```bash
# перед первым запуском: скопируйте примеры и задайте секреты
cp .env.example .env                                   # INTERNAL_API_TOKEN
cp admin-service/.env.example admin-service/.env       # ADMIN_PASSWORD + тот же INTERNAL_API_TOKEN
cp proxy6-service/.env.example proxy6-service/.env     # PX6_API_KEY

docker compose up --build -d                 # весь стек одной командой; сеть openroute-net compose создаёт сам
docker compose logs -f edge                  # логи стены
docker compose logs -f openroute             # логи шлюза
docker compose down                          # остановка всего
```

Наружу смотрит **только edge** на http://127.0.0.1:53202 (хост-порт задаёт
`OPENROUTE_EDGE_PORT`): и API (`/v1`), и админка (`/`, `/login`, `/admin/ui`).
Сервисы (openroute, admin-service, openroute-proxy6, postgres, redis) хостовых
портов не публикуют. Данные — в томах `pgdata`/`redisdata` (удаляются только
`docker compose down -v`). `docker compose stop` ждёт до 320 с
(`stop_grace_period`), чтобы стримы доработали до конца.

> **Сосуществование с NVRoute.** OpenRoute полностью изолирован от соседнего
> стека NVRoute: у него собственная docker-сеть `openroute-net` (не общая
> `service-net`), уникальные имена контейнеров (`openroute-proxy6`,
> `openroute-postgres`, `openroute-redis`; остальные — автоимена `openroute-*`)
> и собственный хост-порт edge `OPENROUTE_EDGE_PORT` (по умолчанию 53202;
> NVRoute занимает 53201). Оба стека поднимаются одновременно и не
> конфликтуют.

### Локально без Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000   # шлюз: /v1/*, /admin/*, /health, /internal/*
```

Нужен доступный PostgreSQL (по умолчанию
`POSTGRES_DSN=postgresql://openroute:openroute@localhost:5432/openroute`); схема
создаётся сама при первом обращении. Без Docker edge нет — админка запускается
отдельно (см. `admin-service/`).

---

## Статус реализации

| Компонент | Статус |
|---|---|
| `app/` (шлюз: `main`, `config`, `proxies`, `proxydb`, `keys`, `keydb`, `reqlog`, `pg`, `opencode`) | **реализовано** |
| `admin-service/` (`config`, `sessions`, `adminui`, `main`) | **реализовано** |
| `proxy6-service/` | **реализовано** (px6.net → `descr=OPENROUTE`) |
| edge / compose / Dockerfile / requirements / .env.example | **готово** |
| `tests/` (`test_gateway`, `test_proxies`, `test_keys`, `test_reqlog`, `test_adminui`, `test_opencode`) | **реализовано** |
| `DESIGN.md` | замороженный контракт |

## Тесты

```bash
pip install -r requirements-dev.txt
pytest tests
```

`tests/test_opencode.py` — чистые юнит-тесты интеграции с opencode.ai (не
требуют сети/БД). Остальные наборы (`test_gateway`, `test_proxies`, `test_keys`,
`test_reqlog`, `test_adminui`) работают герметично: upstream и админка
подменяются, а живой PostgreSQL для хранилищ шлюза поднимается из
`docker-compose.test.yml` (`127.0.0.1:55433`), DSN переопределяется
`OPENROUTE_TEST_DSN`.
