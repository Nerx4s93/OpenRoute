# OpenRoute

OpenAI-совместимый API-шлюз, который балансирует клиентские запросы по пулу
**HTTP(S)-прокси** (sticky-сессии по exit IP + round-robin для новых задач) и
проксирует их в **free-tier инференс `opencode.ai`**. В отличие от NVRoute здесь
**нет апстрим-токенов**: член пула — это сам прокси. Каждый выходной IP = одно
«ведро» квоты opencode.ai (порты одного IP — одна egress-группа).

- При старте и периодически шлюз забирает список прокси из `px6-service`,
  который фильтрует их по комментарию **`OPENROUTE`** (аналог `USE_IN_API` в
  NVRoute) через API [px6.net](https://px6.net).
- **Sticky-сессии**: задача (`x-opencode-session`, либо fingerprint диалога)
  закрепляется за одним exit IP — все её реплики идут через один и тот же
  выходной адрес, а новые задачи балансируются по score (сессии/запросы/токены).
- **`429 FreeUsageLimitError`** (дневная квота exit IP) паркует всю группу
  портов этого IP до ближайшей полуночи UTC, сессии эвакуируются на другие IP.
  Любой другой 429 — прозрачный passthrough без cooldown и ротации: лимиты
  провайдера/ключа не являются свойством выходного адреса.
- **`403 abusive traffic`** банит exit IP (по умолчанию 24 ч) и эвакуирует
  его сессии.
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
                        └─ /, /login, /admin/ui* ──────────────────► [web-admin] (control plane)
                                                                         │
   [gateway] ──► [openroute-px6] ──► px6.net       (список прокси, descr=OPENROUTE)
   [gateway] ──► [PostgreSQL]                       (request_log + proxies + keys + session_affinity)
   [gateway] ──► opencode.ai                        (free-tier инференс через прокси)
   [web-admin] ──► [Redis]                          (серверные сессии админки)
   [web-admin] ──► [gateway] /internal/*            (X-Internal-Token, только внутри сети)
```

- **`edge/`** — nginx: единственная точка входа, маршрутизация на сервисы;
  `/internal/*` наружу не маршрутизируется вообще. SSE-стримы не буферизуются.
- **`gateway/`** — шлюз (data plane): `/v1/*`, пул прокси, клиентские
  ключи, лог запросов в PostgreSQL, internal API для админки. Своих хостовых
  портов не публикует.
- **`web-admin/`** — сайт-админка (control plane): `/login`, `/admin/ui*`,
  сессии в Redis, CSRF; своими данными не владеет — ходит в шлюз по internal API.
- **`px6-service/`** — микросервис списка прокси: обёртка над API px6.net с
  TTL-кэшем, фильтр `descr == "OPENROUTE"`.
- **`postgres`** — PostgreSQL 17: хранилище шлюза (`request_log`, `proxies`,
  `keys`, `session_affinity`). Том `pgdata`; порт наружу не публикуется.
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
│   ├── config.py  proxies.py  proxydb.py  keys.py  keydb.py  reqlog.py  pg.py
│   └── main.py               # FastAPI: passthrough /v1/*, ретраи, internal API, lifespan
├── edge/                     # nginx: nginx.conf, proxy_params_edge.conf, Dockerfile
├── px6-service/              # РЕАЛИЗОВАНО: px6.net → descr=OPENROUTE
├── web-admin/                # РЕАЛИЗОВАНО: config, sessions, adminui, main
└── tests/                    # pytest: шлюз, прокси, ключи, лог, админка, opencode
```

---

## Правила пула

- Прокси автоматически подтягиваются из `PROXIES_URL`
  (`openroute-px6`, `descr=OPENROUTE`) каждые `PROXY_REFRESH_SECONDS`; при
  неудачном фетче — повтор через `PROXY_RETRY_SECONDS`.
- Добавить прокси вручную нельзя: членство в пуле определяется **только**
  фетчем; в админке доступны лишь включить/выключить/удалить существующий
  прокси.
- Список прокси **добавляется** в БД (upsert); существующие записи не удаляются
  и не сбрасывают свой флаг `enabled`. Чтобы навсегда исключить прокси, который
  всё ещё отдаётся сервисом, — **отключите** его (удалённый вернётся на
  следующем обновлении).
- **Egress-группы**: прокси с одним exit-хостом объединяются в группу — квота
  и бан привязаны к exit IP, а не к отдельному порту (`canonical_exit_host`;
  IPv6 нормализуется до `/64`).
- **Round-robin** по готовым прокси (не в cooldown, не отключён, не на локальном
  RPM-капе, группа не запаркована). Каждый прокси пробуется в запросе максимум
  один раз.
- **Circuit breaker** (сетевые ошибки / 5xx): ошибки копятся в скользящем окне
  `ACCOUNT_FAIL_WINDOW_SECONDS`; после `ACCOUNT_FAIL_THRESHOLD` — cooldown
  `ACCOUNT_COOLDOWN_SECONDS` с экспонентой ×2 до `ACCOUNT_COOLDOWN_MAX_SECONDS`
  (60 → 120 → … → 600 с). Успех сбрасывает счётчик и cooldown.
- **429 → классификация по телу** (см. «Sticky-сессии» ниже):
  `FreeUsageLimitError` → **парковка всей egress-группы** до 00:00 UTC +
  `QUOTA_RESET_MARGIN_SECONDS`, сессии эвакуируются на другие IP; любой другой
  429 — прозрачный passthrough без cooldown, парковки и ротации.
  Механика cooldown/парковки отдельного прокси (`mark_failure` с 429,
  `ACCOUNT_THROTTLE_*`) сохранена для внутренних путей, но HTTP-обработчик
  её больше не вызывает.
- **403 `abusive traffic`** (case-insensitive поиск в теле) → бан всей
  egress-группы на `ABUSIVE_BAN_SECONDS` (по умолчанию 86400 с) с эвакуацией;
  `FreeTierError` — passthrough; прочие 403 — обычный failover
  (`mark_failure` + следующая попытка).
- **Ретраи**: 5xx/сетевые — повтор на следующем прокси с экспоненциальным
  backoff от `RETRY_BACKOFF_BASE_SECONDS`, суммарно не дольше
  `RETRY_BUDGET_SECONDS` (исчерпан → 504).
- **Прокси никогда не «мёртв» навсегда**: устойчивый сбой выражается только
  через cooldown; health-probe периодически проверяет прокси и может снять
  cooldown досрочно.

### Один exit IP = одно «ведро» квоты

Квота free-tier opencode.ai привязана к IP клиента; для IPv6 сервер усекает
адрес до первых 4 групп (`/64`), т.е. **одна /64 = одно ведро**. Ротация внутри
одной /64 бесполезна, а exit-адрес узла общий для всех его пользователей —
бюджет делится. Поэтому пул строится из **разных** выходных адресов, а порты
одного IP объединяются в одну egress-группу: квота, бан и эвакуация действуют
на всю группу сразу. Дневной лимит уточняется телами ответов:
`429 FreeUsageLimitError` → группа паркуется до ближайшей полуночи UTC
(если `Retry-After` больше — берётся он); `403 abusive traffic` → бан группы
на `ABUSIVE_BAN_SECONDS`.

---

## Sticky-сессии (affinity)

- Задача закрепляется за exit IP надолго: привязка хранится в
  `session_affinity` и не меняется, пока группа не запаркована/забанена, а
  прокси не удалён/отключён в админке.
- Идентификатор берётся из `x-opencode-session` / `x-session-affinity` /
  `x-session-id` (валидный `ses_...`); если его нет — из fingerprint диалога
  (ключ + проект + первое сообщение); одиночные запросы без истории привязку
  не создают, но учитываются в статистике группы.
- Новые задачи распределяются по score `S+R+T`: S — число привязанных сессий,
  R — запросов за окно `AFFINITY_WINDOW_SECONDS`, T — реальных токенов за то
  же окно; веса — `AFFINITY_WEIGHT_*`. Кулдаун/локальный RPM-кап родного
  прокси не двигает привязку: текущий запрос временно уходит на другой адрес.
- **Требование к клиенту (комбайну)**: присылать стабильный валидный
  `x-opencode-session: ses_...` на задачу — тогда все её реплики гарантированно
  идут через один exit IP. Без него шлюз сам выводит fingerprint диалога.
- Переменные: `AFFINITY_ENABLED`, `AFFINITY_WINDOW_SECONDS`,
  `AFFINITY_WEIGHT_SESSIONS`, `AFFINITY_WEIGHT_REQUESTS`,
  `AFFINITY_WEIGHT_TOKENS`, `QUOTA_RESET_MARGIN_SECONDS`,
  `ABUSIVE_BAN_SECONDS`. В `/admin/status` — блок `affinity` (host-only),
  в `/health` — счётчики `egress_groups_*` и `sessions_*`.

---

## Эндпоинты

Все пути — на едином порту edge (`http://127.0.0.1:53202`):

| Метод | Путь | Куда (edge) | Описание |
|-------|------|-------------|----------|
| ANY | `/v1/*` | gateway | Passthrough к opencode.ai (`chat/completions`, `models`, ...); стриминг поддержан |
| GET | `/` | web-admin | Редирект на `/login` |
| GET/POST | `/login` | web-admin | Вход в админку (только пароль) |
| GET | `/admin/status` | gateway | Состояние пула: прокси, egress-группы, session affinity, счётчики, usage по ключам |
| GET | `/admin/logs?limit=N` | gateway | Последние запросы из лога (1..500, по умолчанию 100) |
| GET | `/admin/ui` | web-admin | Веб-админка: страница «Прокси» |
| GET | `/admin/ui/keys` | web-admin | Веб-админка: страница «Ключи» |
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
| 403 | `abusive traffic` от opencode — банит exit IP группы | тело upstream (passthrough) |
| 404 | внутренний API выключен (пустой `INTERNAL_API_TOKEN`); неизвестный прокси/ключ в мутации | OpenAI-конверт |
| 413 | тело запроса больше `MAX_BODY_BYTES` (без чтения тела при `Content-Length`) | OpenAI-конверт |
| 429 | превышены RPM/TPM ключа; все egress-группы запаркованы (квота до полуночи UTC / бан) — с `Retry-After` | `{"error": {"type": "rate_limit_error", ...}}` |
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
`OPENROUTE_EDGE_PORT`): и API (`/v1`), и админка (`/`, `/login`, `/admin/ui`).
Сервисы (gateway, web-admin, px6, postgres, redis) хостовых
портов не публикуют. Данные — в томах `pgdata`/`redisdata` (удаляются только
`docker compose down -v`). `docker compose stop` ждёт до 320 с
(`stop_grace_period`), чтобы стримы доработали до конца.

> **Сосуществование с NVRoute.** OpenRoute полностью изолирован от соседнего
> стека NVRoute: у него собственная docker-сеть `openroute-net` (не общая
> `service-net`), уникальные имена контейнеров (`openroute-px6`,
> `openroute-postgres`, `openroute-redis`; остальные — автоимена
> `openroute-gateway-*` и `openroute-web-admin-*`)
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
создаётся сама при первом обращении. Без Docker edge нет — админка запускается
отдельно (см. `web-admin/`).

---

## Статус реализации

| Компонент | Статус |
|---|---|
| `gateway/` (шлюз: `main`, `config`, `proxies`, `proxydb`, `affinity`, `affinitydb`, `keys`, `keydb`, `reqlog`, `pg`, `opencode`) | **реализовано** |
| `web-admin/` (`config`, `sessions`, `adminui`, `main`) | **реализовано** |
| `px6-service/` | **реализовано** (px6.net → `descr=OPENROUTE`) |
| edge / compose / Dockerfile / requirements / .env.example | **готово** |
| `tests/` (`test_gateway`, `test_proxies`, `test_affinity`, `test_affinitydb`, `test_keys`, `test_reqlog`, `test_adminui`, `test_opencode`) | **реализовано** |

## Тесты

```bash
pip install -r requirements-dev.txt
pytest tests
```

`tests/test_opencode.py` — чистые юнит-тесты интеграции с opencode.ai (не
требуют сети/БД); `tests/test_affinity.py` — unit-тесты привязок, score и
эвакуации (без БД). Остальные наборы (`test_gateway`, `test_proxies`,
`test_affinitydb`, `test_keys`, `test_reqlog`, `test_adminui`) работают
герметично: upstream и админка подменяются, а живой PostgreSQL для хранилищ
шлюза (включая `session_affinity`) поднимается из `docker-compose.test.yml`
(`127.0.0.1:55433`), DSN переопределяется `OPENROUTE_TEST_DSN`.
