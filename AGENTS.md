# OpenRoute — техническая документация

> Этот файл (`AGENTS.md`) — основной технический документ проекта: архитектура,
> принципы работы, API, запуск и тесты. Он предназначен для разработчиков и
> AI-агентов. Пользовательская инструкция по установке будет в `README.md`.

OpenAI-совместимый API-шлюз, который балансирует клиентские запросы по пулу
**HTTP(S)-прокси** (sticky-сессии по прокси-URL + round-robin для новых задач) и
проксирует их в **free-tier инференс `opencode.ai`**. В отличие от NVRoute здесь
**нет апстрим-токенов**: член пула — это сам прокси. Каждый прокси-URL = одно
«ведро» квоты opencode.ai: входной IPv4 не считается выходным IP, потому что
провайдер может выпускать трафик через IPv6.

- **Хранение**: пул прокси, клиентские ключи, sticky-привязки, personas, лог
  запросов и статистика принадлежат приватному `storage-service` —
  единственному компоненту со SQL и `POSTGRES_DSN`. Gateway ходит в него по
  HTTP без токена: сервис доступен только в приватной сети
  `openroute-storage-api-net`, а DSN gateway не получает никогда; без
  storage-service он не стартует.
- **Аутентификация админки**: пароль, серверные сессии, CSRF и throttling
  принадлежат приватному `auth-service`. `web-admin` не получает
  `REDIS_URL`/`ADMIN_PASSWORD` и ходит в auth-service по HTTP в сети
  `openroute-auth-net`; только auth-service подключён к Redis.
- **Сети**: Compose сам создаёт `openroute-net` (edge/front), приватные
  `openroute-storage-api-net`, `openroute-storage-db-net`,
  `openroute-auth-net`, `openroute-redis-net` и `openroute-px6-net`; вручную
  `docker network create` не нужен. Gateway не подключён к сетям PostgreSQL,
  Redis и auth-service.
- При старте и периодически шлюз забирает **все активные прокси** из
  `px6-service` (обёртка над API [px6.net](https://px6.net)). Комментарий
  `descr` больше не фильтруется: новые прокси добавляются в пул **выключенными**,
  и только явно включённые оператором могут нести трафик.
- В админке есть кнопка **«Обновить список»**: она заставляет gateway запросить
  `px6-service` с `?refresh=true` (сброс TTL-кэша), сразу обновить пул и
  перепроверить прокси.
- В админке три страницы: **«Статистика»** (`/admin/stats`), **«Прокси»**
  (`/admin/proxies`) и **«Ключи»** (`/admin/keys`); `/admin`
  перенаправляет на `/admin/stats`. На странице «Прокси»
  видны баланс px6.net, каталог и покупка прокси, а также включение/выключение
  каждого прокси; на «Статистике» — карточки, график «Токены по дням», таблица
  «Прокси» за выбранный период и последние запросы. Период выбирается кнопками
  **1д / 3д / 7д / 30д / 90д / Всё время** (см. «Статистика токенов»).
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
- **`403 abusive traffic`** навсегда банит только этот прокси-URL (флаг в
  `storage-service`), эвакуирует его сессии, и больше никогда его не выбирает.
- Round-robin по готовым прокси; при сбое запрос немедленно уходит на следующий
  прокси (каждый пробуется за запрос максимум один раз).
- Клиентская поверхность: `/v1/*` (OpenAI passthrough) за клиентскими
  ключами (RPM/TPM, allow-list моделей). У ключей нет ролей или админ-флагов:
  они дают доступ только к `/v1/*`. `/health` и `/docs` публичны,
  `/internal/*` (X-Internal-Token) доступен только web-admin, а веб-админка —
  по отдельному паролю.

Требования к клиенту — как у OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:53202/v1", api_key="openroute-...")
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
   │  curl /  │ :53202 │  free-tier инференс opencode.ai; состояние    │
   │  браузер)│        │  хранит в storage-service                     │
   └──────────┘        └───────┬───────────────────────────┬───────────┘
                               │ POST /inference/...       │ GET список
                               │ (через прокси)            │ прокси
                               ▼                           ▼
                     ┌───────────────────┐        ┌───────────────────┐
                     │   opencode.ai     │        │      px6.net      │
                     │ (free-tier инференс│        │ (источник прокси, │
                     │  + каталог моделей)│        │ баланс/покупка)   │
                     └───────────────────┘        └───────────────────┘

   Внутри OpenRoute (сегментированные сети, см. ниже):
   [gateway] ── HTTP (private net, без токена) ──► [storage-service] ── SQL ──► [PostgreSQL]
   [web-admin] ── openroute-auth-net ──► [auth-service] ── openroute-redis-net ──► [redis]
   [web-admin] ── openroute-net ──► [gateway] /internal/*
   [gateway] ── openroute-px6-net ──► [px6-service]
```

### C4 — контейнеры (уровень 2)

```
 Клиент/браузер ──► 0.0.0.0:53202 (edge, nginx — «стена», единственный
                     публичный порт; хост-порт задаёт OPENROUTE_EDGE_PORT)
                        ├─ /v1/*, /health, /docs, /openapi.json ───► [gateway] (data plane)
                        ├─ /login, /admin*, /assets/* ────────────► [web-admin-ui] (React SPA)
                        └─ /api/admin/* ───────────────────────────► [web-admin] (JSON API)
                                                                         │
   (web-admin-ui — только статика; браузер сам ходит в /api/admin/* через edge)
   [gateway] ── openroute-px6-net ──► [openroute-px6] ──► px6.net  (каталог, баланс, покупка)
   [gateway] ── openroute-storage-api-net ──► [openroute-storage]
   [openroute-storage] ── openroute-storage-db-net ──► [PostgreSQL]
                                                     (proxies + keys + session_affinity
                                                      + proxy_personas + request_log)
   [gateway] ──► opencode.ai                        (free-tier инференс через прокси)
   [web-admin] ── openroute-net ──► [gateway] /internal/*
                                                      (X-Internal-Token; edge /internal/* не маршрутизирует)
   [web-admin] ── openroute-auth-net ──► [auth-service] ── openroute-redis-net ──► [Redis]
                                                      (пароль, сессии, CSRF, throttling)
```

- **`edge/`** — nginx: единственная точка входа, маршрутизация на сервисы;
  `/internal/*` наружу не маршрутизируется вообще. SSE-стримы не буферизуются.
  Подключён только к `openroute-net`.
- **`gateway/`** — шлюз (data plane): `/v1/*`, пул прокси, клиентские
  ключи, internal API для админки; всё состояние читает и пишет через
  `storage-service`. Своих хостовых портов не публикует. Подключён к
  `openroute-net` (edge), `openroute-storage-api-net` (StorageClient) и
  `openroute-px6-net`; к PostgreSQL и Redis не подключён.
- **`storage-service/`** — приватный FastAPI-сервис хранения (образ
  `openroute-storage`, контейнер `openroute-storage`): единственный компонент
  со SQL и `POSTGRES_DSN`. Отдаёт gateway домены `proxies`, `keys`,
  `affinity`, `personas`, `request-log/stats` по HTTP без токена: сервис
  доступен только gateway в приватной сети. Подключён к
  `openroute-storage-api-net` (gateway) и
  `openroute-storage-db-net` (PostgreSQL); слушает `8000` только в первой,
  хост-порт не публикуется, edge его не маршрутизирует, публичного `/docs` нет.
- **`web-admin/`** — backend админки (control plane): JSON API
  `/api/admin/*`, HttpOnly cookie сессии и SSE; своими данными не владеет —
  ходит в шлюз по internal API и в auth-service за проверкой пароля,
  сессии и CSRF. Подключён к `openroute-net` (edge/gateway) и
  `openroute-auth-net` (auth-service). Redis и `ADMIN_PASSWORD` ему не
  передаются.
- **`auth-service/`** — приватный FastAPI-сервис аутентификации
  (`openroute-auth`): единственный владелец `ADMIN_PASSWORD`,
  `ADMIN_SESSION_TTL_SECONDS` и Redis-сессий. Отдаёт `login`, `validate`,
  `logout`, `status` и `/health`; хранит сессии в Redis под
  `openroute:session:<sid>`. Подключён к `openroute-auth-net` (web-admin) и
  `openroute-redis-net` (Redis); хост-порт не публикует, edge его не
  маршрутизирует.
- **`web-admin-ui/`** — React SPA (React + TypeScript + Vite + React Router +
  TanStack Query) в отдельном контейнере nginx. Секретов не получает; все
  данные — через `/api/admin/*`. Подключён только к `openroute-net`.
- **`px6-service/`** — микросервис px6.net: обёртка над API с TTL-кэшем;
  отдаёт все активные прокси, баланс аккаунта, страны, цену и покупку. Это
  единственный компонент, который знает `PX6_API_KEY`. Подключён только к
  `openroute-px6-net`.
- **`postgres`** — PostgreSQL 17: хранилище `storage-service` (`request_log`,
  `proxies`, `keys`, `session_affinity`, `proxy_personas`). Том
  `pgdata`; подключён только к `openroute-storage-db-net`, порт наружу не
  публикуется, DSN знает только storage-service.
- **`redis`** — Redis 7: сессии админки (том `redisdata`); подключён только к
  `openroute-redis-net`, где единственный клиент — auth-service. Порт наружу
  не публикуется, web-admin и gateway сети Redis не видят.

### Docker-сети

Compose создаёт все сети сам; `docker network create` не нужен. Наружу
публикуется только порт edge; внутренние сервисы хост-портов не имеют.

| Сеть | internal | Кто подключён | Назначение |
|------|----------|---------------|------------|
| `openroute-net` | нет | edge, gateway, web-admin, web-admin-ui | ingress от edge; gateway ходит в интернет |
| `openroute-storage-api-net` | да | gateway, storage-service | HTTP без токена (только gateway) |
| `openroute-storage-db-net` | да | storage-service, postgres | SQL; gateway сюда не подключён |
| `openroute-auth-net` | да | web-admin, auth-service | HTTP auth API; edge/gateway сюда не подключены |
| `openroute-redis-net` | да | auth-service, redis | сессии админки; web-admin сюда не подключён |
| `openroute-px6-net` | нет | gateway, px6-service | px6.net: нужен внешний egress |

`internal: true` убирает default route у сети: storage-service, auth-service,
PostgreSQL и Redis не ходят наружу, а gateway не видит PostgreSQL, Redis и
auth-service. Это сегментация от случайного/бокового трафика между
контейнерами, но не граница от root на хосте. `storage-service` не имеет
собственной аутентификации: единственный клиент в его сети — gateway.
`auth-service` тоже без отдельного токена: единственный клиент в
`openroute-auth-net` — web-admin, а edge этот контур не маршрутизирует. Токен
`X-Internal-Token` остаётся defense-in-depth для `/internal/*` шлюза:
единственный клиент — web-admin, а edge этот префикс не маршрутизирует, в том
числе при ошибке конфигурации.

### Отказ storage-service

- **Старт**: gateway проверяет `GET /health` и список прокси
  (`StorageClient.check()`), затем загружает прокси, ключи, affinity и
  personas. Недоступный сервис или ошибка загрузки
  прокси/ключей/affinity/personas — отказ старта, а не работа без
  персистентности. Ошибка отдельной загрузки parking старт не блокирует.
- **После старта**: in-memory кэши (пул, ключи, привязки, personas) продолжают
  обслуживать `/v1/*`; проксирование не ждёт storage-service.
- **Мутации** ключей и включение/выключение прокси через internal API
  отвечают 503 и не меняют in-memory состояние; статистика отвечает 200 с
  нулями/пустыми сериями.
- **Запись**: лог запросов — fire-and-forget; affinity, personas и parking —
  фоновые best-effort записи (ошибка логируется, повтор — при следующем
  изменении/backoff). Уже сохранённые в PostgreSQL данные не теряются и снова
  видны после восстановления.

### Отказ auth-service

- **Старт**: `web-admin` ждёт здоровый `auth-service` (compose
  `condition: service_healthy`), а тот — здоровый Redis. Без auth-service
  админка не стартует; gateway и `/v1/*` от него не зависят.
- **После старта**: при недоступном auth-service `web-admin` не работает без
  аутентификации: `/api/admin/session`, `/api/admin/login`, защищённые
  `/api/admin/*` и `/health` отвечают 503, SSE-поток завершается. Пароль,
  сессии и CSRF не кэшируются в web-admin.
- **Redis**: auth-service отвечает 503 (`Хранилище сессий недоступно`) на
  login/validate/logout, если не может создать, проверить или удалить сессию;
  `/auth/status` и `/health` при выключенной админке не трогают Redis. Уже
  сохранённые в томе `redisdata` сессии переживают рестарт auth-service.
- **Выключенная админка**: пустой `ADMIN_PASSWORD` → auth-service отвечает
  404 на login/validate/logout, поэтому web-admin отдаёт 404 на
  `/api/admin/*` (включая `/api/admin/session`). `/health` остаётся
  liveness-проверкой, `/auth/status` продолжает отвечать `enabled: false`.

---

## Структура репозитория

```
OpenRoute/
├── AGENTS.md                 # техническая документация (этот файл)
├── README.md                 # пользовательская инструкция по установке (TODO)
├── docker-compose.yml        # весь стек одной командой; сети создаёт compose
├── docker-compose.test.yml   # PostgreSQL для real-SQL тестов storage-service (127.0.0.1:55433)
├── Dockerfile                # шлюз (python:3.12-slim)
├── requirements.txt          # gateway: fastapi/uvicorn/httpx/pydantic-settings/curl_cffi (без psycopg)
├── requirements-dev.txt      # + pytest/pytest-asyncio/redis/psycopg (psycopg — только для тестов storage-service)
├── .env.example              # настройки gateway (STORAGE_*); DSN отсутствует
├── gateway/                  # шлюз (data plane) — РЕАЛИЗОВАНО
│   ├── opencode.py           # gate-заголовки, session id, body, Retry-After, SSE, классификаторы 429/403
│   ├── sessions.py           # fingerprint диалога -> один upstream session id
│   ├── affinity.py           # sticky-сессии: привязки, score S/R/T, эвакуация; persist через storage-service
│   ├── personas.py           # persona на прокси-bucket: кэш, ротация, retry/reconcile; фоновая запись в storage-service
│   ├── storage.py            # async StorageClient — типизированный HTTP-фасад над storage-service
│   ├── reqlog.py             # лог запросов: fire-and-forget запись в storage-service + deque fallback
│   ├── config.py  proxies.py  keys.py  px6.py  curlclient.py
│   └── main.py               # FastAPI: passthrough /v1/*, ретраи, internal API, lifespan
├── storage-service/          # РЕАЛИЗОВАНО: приватный сервис хранения, единственный SQL/DSN
│   ├── openroute_storage/    # FastAPI: main, config, db, schema, schemas, services
│   ├── Dockerfile
│   ├── requirements.txt      # fastapi/uvicorn/pydantic-settings/psycopg
│   ├── .env.example          # POSTGRES_DSN + настройки пула (реальный .env не коммитится)
│   └── README.md             # контракты storage-service
├── edge/                     # nginx: nginx.conf, proxy_params_edge.conf, Dockerfile
├── px6-service/              # РЕАЛИЗОВАНО: px6.net → все активные прокси, баланс, покупка
├── auth-service/             # РЕАЛИЗОВАНО: приватный auth/session-сервис админки
│   ├── app/                  # FastAPI: config, sessions (Redis), schemas, main
│   ├── Dockerfile
│   ├── requirements.txt      # fastapi/uvicorn/pydantic-settings/redis
│   ├── .env.example          # ADMIN_PASSWORD + TTL + REDIS_URL (реальный .env не коммитится)
│   └── README.md             # границы безопасности, API, сессии/CSRF
├── web-admin/                # РЕАЛИЗОВАНО: JSON API (config, auth_client, auth, gateway, schemas, main)
├── web-admin-ui/             # РЕАЛИЗОВАНО: React SPA (Vite, React Router, TanStack Query)
└── tests/                    # pytest: шлюз, storage-service/storage client, прокси, ключи, лог, админка/auth, px6, opencode
```

---

## Правила пула

- Прокси автоматически подтягиваются из `PROXIES_URL`
  (`openroute-px6`) каждые `PROXY_REFRESH_SECONDS`; при неудачном фетче —
  повтор через `PROXY_RETRY_SECONDS`. `px6-service` возвращает **все активные**
  прокси без фильтра по комментарию.
- **Новые прокси добавляются выключенными** (`enabled = FALSE`). Включение —
  явное действие оператора на странице «Прокси»; только включённые прокси
  участвуют в балансировке. Ранее включённые прокси сохраняют свой флаг при
  обновлении списка.
- Добавить или удалить прокси вручную нельзя: членство в пуле определяется
  **только** фетчем; в админке доступны лишь включить/выключить существующий
  прокси и купить новые через px6.net.
- Список из `PROXIES_URL` синхронизируется через `storage-service`
  (`POST /proxies/sync`) с таблицей `proxies`: новые прокси добавляются,
  пропавшие из источника помечаются `listed = FALSE` (soft-delete) и
  перестают участвовать в балансировке. Записи физически не удаляются, поэтому
  парковка/баны и привязки сессий переживают временное исчезновение прокси;
  если он снова появится в источнике, он возвращается в пул. Флаг `enabled`
  (ручное вкл/выкл) при синхронизации не сбрасывается.
- Если `storage-service` временно недоступен, снимок источника не применяется:
  gateway сохраняет текущие флаги уже известных прокси и оставляет новые
  выключенными (fail-closed), а не включает весь полученный список. Уже
  загруженный пул продолжает обслуживать трафик.
- **Квоты**: каждый прокси-URL — отдельное ведро (`canonical_quota_key`).
  Прокси не объединяются по входному IPv4: выход может быть IPv6, и разные
  порты одного входного адреса могут иметь разные выходные адреса.
- **Парковка и баны переживают рестарт**: `quota_until`, `banned_until` и
  `permanently_banned` пишутся `storage-service` в таблицу `proxies` при
  получении `FreeUsageLimitError` / abusive-403 (фоновая best-effort запись)
  и загружаются при старте gateway из storage-service. После пересборки
  контейнера админка снова показывает «лимит …» / «бан …», а запаркованные и
  навсегда забаненные прокси не опрашиваются и не выбираются.
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
  бан** этого прокси-URL (`permanently_banned = TRUE` в storage-service) с эвакуацией;
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
  `storage-service` (таблица `session_affinity`, загружается при старте
  gateway) и не меняется, пока прокси не запаркован/забанен или пока он не
  отключён в админке.
- Идентификатор берётся из `x-opencode-session` / `x-session-affinity` /
  `x-session-id` (валидный `ses_...`); если его нет — из fingerprint диалога
  (ключ + проект + первое сообщение); одиночные запросы без истории привязку
  не создают, но учитываются в статистике прокси.
- Новые задачи распределяются по score `S+R+T`: S — число привязанных сессий,
  R — запросов за окно `AFFINITY_WINDOW_SECONDS`, T — реальных токенов за то
  же окно; веса — `AFFINITY_WEIGHT_*`. Кулдаун/локальный RPM-кап родного
  прокси не двигает привязку: текущий запрос временно уходит на другой адрес.
- Запись привязок — фоновая (coalescing writer), best-effort: ошибка
  storage-service не блокирует и не ломает запрос, привязка остаётся в памяти,
  а следующее изменение состояния планирует повторную запись. После рестарта
  S восстанавливается из storage-service; R/T — in-memory и начинаются с нуля.
- **Требование к клиенту (комбайну)**: присылать стабильный валидный
  `x-opencode-session: ses_...` на задачу — тогда все её реплики гарантированно
  идут через один прокси (один выходной адрес). Без него шлюз сам выводит
  fingerprint диалога.
- Переменные: `AFFINITY_ENABLED`, `AFFINITY_WINDOW_SECONDS`,
  `AFFINITY_WEIGHT_SESSIONS`, `AFFINITY_WEIGHT_REQUESTS`,
  `AFFINITY_WEIGHT_TOKENS`, `QUOTA_RESET_MARGIN_SECONDS`. Abusive-бан не имеет
  настройки срока: он постоянный. В `/health` — счётчики `egress_groups_*`
  и `sessions_*`; полный host-only снапшот affinity доступен только внутри
  gateway и больше не публикуется по HTTP.

---

## Persona прокси

- Одна персона на bucket `canonical_quota_key` (тот же bucket, что у парковки
  квоты; он же `manager.group_of(proxy).host`): валидные `session_id`
  (`ses_...`), `project_id` (40 hex), `trace_id` (32 hex) и `generation`.
- На каждом запросе через bucket клиентские `x-opencode-project`,
  `x-opencode-session`, `x-session-affinity`, `x-session-id`,
  `x-opencode-session-id`, `traceparent` и `b3` заменяются значениями
  персоны. `traceparent` и `b3` пересобираются на каждый запрос: trace id —
  из персоны, span id и parent span id (4-й компонент `b3`, как у реального
  клиента) — свежие. Multi-header B3 (`x-b3-*`) не синтезируется: только
  `traceparent` и одиночный `b3`.
- **Upstream-заголовки — строгий allowlist**: наружу уходит ровно множество
  заголовков реального OpenCode CLI и ничего сверх него. Любые остальные
  заголовки клиента (`x-stainless-*` от OpenAI SDK, `origin`/`referer`/
  `sec-*` от браузеров, `baggage`, `x-request-id`, `cookie`, `x-api-key`,
  `x-forwarded-*`, кастомные идентификаторы) не форвардятся вообще — по
  любому из них апстрим мог бы коррелировать весь трафик шлюза в одну
  «учётку» с общей квотой.
- Заголовки соответствуют перехваченному реальному OpenCode CLI:
  `User-Agent: opencode/latest/2.1.3/cli`, `Accept: */*`,
  `x-opencode-client: cli`, `Authorization: Bearer public`; порядок заголовков
  нормализуется под порядок реального клиента (см. ниже),
  `accept-encoding: gzip, deflate, br, zstd` задаёт curl-транспорт.
- Тело chat-запроса тоже приводится к виду реального клиента:
  `stream: true`, `stream_options.include_usage: true` и дефолтный
  `max_completion_tokens: 32000` (клиентское значение не перетирается).
  Идентификатор конечного пользователя `user` вычищается, query-параметры
  (в т.ч. `?api_key=...`) в URL chat-запроса не попадают — реальный клиент
  шлёт их без параметров.
- Разные диалоги и клиенты через один прокси делят один upstream session id,
  но affinity и балансировка не меняются: привязка по-прежнему ключуется
  клиентской сессией/fingerprint, персона применяется после выбора прокси.
- Ротация (generation + 1, новые id) — только при `429 FreeUsageLimitError`
  и `403 abusive traffic`. Обычный 429, `FreeTierError`, 401, 5xx и сетевые
  ошибки персону не ротируют.
- Персона хранится в `storage-service` (таблица `proxy_personas`, схема
  применяется при его старте) и переживает рестарт; generation-гард на стороне
  сервиса не даёт старой записи перетереть новую. Запись — фоновая
  (fire-and-forget): при недоступном storage-service запросы не ждут и не
  ломаются, персона живёт в памяти, dirty-очередь повторяет запись с backoff,
  а при расхождении стор примиряется с более новой строкой. Загрузка
  валидирует идентификаторы и заменяет битую строку валидной персоной.
- Переменная `PERSONA_ENABLED` (по умолчанию `true`); `false` — форвардятся
  клиентские identity-заголовки (`x-opencode-project`, `traceparent`,
  `tracestate`, `b3`, `x-b3-*`) вместо значений персоны; все прочие клиентские
  заголовки при этом всё равно не форвардятся (allowlist действует всегда).

---

## Статистика токенов

- На странице **«Статистика»** (`/admin/stats`) — график «Токены по дням»,
  таблица «Прокси» и последние запросы. Период выбирается общими кнопками
  **1д / 3д / 7д / 30д / 90д / Всё время**; окна 1–90 дней включают сегодня
  и предыдущие UTC-дни. График показывает расход по **UTC-дням**; при
  наведении на столбец видны дата и расход за день. В нём можно выбрать один
  прокси; без выбора показываются топ-8 по расходу и строка
  «Другие». Там же три карточки (запросы и токены за выбранный период,
  прокси в ротации). Таблица «Прокси» показывает токены, запросы и ошибки
  строго за выбранный период и отметку «В пуле»/«Вне пула»; прокси без
  запросов в этом окне не показываются. График и его список прокси также
  строятся только по запросам выбранного периода. Канонический URL страницы —
  `/admin/stats`; `/admin` перенаправляется на него с сохранением
  query-параметров.
- **Хранилище**: дневные агрегаты строятся `storage-service` по `request_log`;
  для них добавлен индекс `request_log(account, ts)`. DDL идемпотентен и
  применяется при старте storage-service; gateway SQL не выполняет. Отдельная
  миграция не нужна, достаточно перезапуска сервисов.
- **API шлюза**: `GET /internal/stats-data?days=30` (`X-Internal-Token`,
  `days` зажимается в `1..365`) или `GET /internal/stats-data?period=all`
  (`1d`/`3d`/`7d`/`30d`/`90d`/`all`, как на странице «Статистика»; старые
  `day`/`week`/`month` тоже принимаются). Шлюз получает агрегаты из
  storage-service и отдаёт прежний контракт: `days`, `server_time`
  (epoch), `dates` (UTC-дни по возрастанию),
  `totals`, `proxies`, `series_total` и `series_by_proxy` — параллельные
  массивы `tokens`/`requests`/`errors` на каждый день `dates`. `totals`,
  `proxies` и `series_by_proxy` ограничены выбранным окном: в список попадают
  только прокси, у которых были запросы за этот период. Для `period=all`
  окно начинается с самого раннего дня в `request_log`. Наружу выходят только
  masked-имена `host:port`, без схемы и учётных данных.
- **API панели**: `GET /api/admin/stats?days=30` или
  `GET /api/admin/stats?period=7d` (нужна сессия админки,
  `Cache-Control: no-store`) отдаёт те же поля с нормализованным ISO-8601
  `server_time` (UTC).
- **`GET /api/admin/proxies`** расширен аддитивно: на верхнем уровне появился
  `totals` (`tokens_total`), в `summary` — `proxies_enabled`, а у каждой строки
  — необязательный объект `px6` (`id`, `version`, `country`, `date_end`,
  `descr`). Прежние поля не изменились: `summary` и `usage` строки сохраняют
  контракт. Учётные данные прокси по-прежнему не попадают в браузер.
- **Устойчивость**: при недоступном storage-service (включая недоступный
  PostgreSQL за ним) эндпоинты статистики всё равно отвечают 200 — нулями и
  пустыми сериями; «последние запросы» откатываются на локальный deque шлюза.
  Опрос панели (`/internal/proxies-data`, ~1 раз в секунду) использует
  gateway-кэш агрегатов (TTL ~10 с), а дневные серии формируются для графика
  на странице «Статистика» (обновление ~30 с). Запись лога запросов —
  fire-and-forget: сбой хранилища не блокирует проксирование, но записи,
  не доехавшие во время сбоя, могут не сохраниться; уже записанные данные не
  теряются и снова видны после восстановления.

---

## Прокси (px6.net)

- Страница **«Прокси»** (`/admin/proxies`) показывает баланс аккаунта
  px6.net, форму покупки и таблицу всех прокси. Баланс обновляется раз в 30 с.
- Покупка: версия **3 — IPv4 Shared**, **4 — IPv4**, **6 — IPv6**; период
  **3/7/14/30/60/90 дней**; количество, страна (ISO2) и необязательный
  комментарий до 50 символов. Страна выбирается из `getcountry`, стоимость
  пересчитывается через `getprice` с задержкой 500 мс (px6.net допускает не
  более 3 запросов/с). Перед покупкой показывается подтверждение с ценой и
  остатком баланса; фактическая цена фиксируется px6.net в момент покупки.
- **Купленные и новые прокси добавляются выключенными.** В таблице их можно
  включить; только включённые прокси попадают в пул gateway. Комментарий
  `descr` ни на что не влияет и показывается как справочная информация.
- После успешной покупки gateway вызывает `px6-service` и сразу обновляет пул
  с `?refresh=true`. Если покупка прошла, а обновление списка — нет, ответ
  остаётся успешным (`refresh_ok: false`) с предупреждением: деньги уже
  списаны, оператору нужно нажать «Обновить список».
- **Секреты**: `PX6_API_KEY` знает только `px6-service`; логин/пароль прокси
  хранятся в `storage-service` (таблица `proxies`) и никогда не отдаются в
  браузер. Панель получает
  masked-имя `host:port`, непрозрачный HMAC-идентификатор прокси и несекретные
  поля px6 (`id`, `version`, `country`, `date_end`, `descr`).
- **Внутренние API**: gateway `GET /internal/px6/account`,
  `GET /internal/px6/countries?version=`, `GET /internal/px6/price?...`,
  `POST /internal/px6/buy` (все требуют `X-Internal-Token`); web-admin
  проксирует их в `/api/admin/proxies/{account,countries,price,buy}`, где
  `buy` защищён CSRF. Наружу `/internal/*` не маршрутизируется.
- `px6-service` кэширует `/proxies` на `CACHE_HOURS`; покупка инвалидирует
  кэш. Остальные методы (`/account`, `/countries`, `/price`, `/buy`) ходят в
  px6.net напрямую.

---

## TLS-фингерпринт апстрима

Апстрим (opencode.ai) снимает фингерпринт TLS ClientHello (замер 2026-10-24):

- `httpx` (OpenSSL), `curl` (Schannel) и имперсонация Chrome (curl_cffi
  `impersonate="chrome"`) получают **мгновенный `429 FreeUsageLimitError`**
  независимо от байтов запроса (проверено и побайтовым реплеем настоящего
  клиента);
- выходной IP при этом **помечается**: после ~7 «синтетических» запросов с
  /64 даже **настоящий** opencode CLI получает оттуда 429 до конца UTC-дня;
- проходит только подлинный отпечаток реального клиента (Bun/BoringSSL).

Поэтому upstream-транспорт — не httpx, а `gateway/curlclient.py`:
httpx-совместимый фасад над `curl_cffi` (BoringSSL, то же семейство TLS),
воспроизводящий ClientHello реального клиента **байт-в-поле**: JA3 с
X25519MLKEM768, полный список из 11 signature_algorithms (включая ed25519
и legacy SHA-1), ALPN `http/1.1` без h2. Константы — в
`gateway/opencode.py` (`OPENCODE_TLS_JA3`, `OPENCODE_TLS_SIGALGS`,
`OPENCODE_ACCEPT_ENCODING`, `OPENCODE_HEADER_ORDER`), применяются к КАЖДОМУ
запросу, включая health-пробу (неотличима от запроса реального клиента).
`curl_cffi` запинован в requirements к версии, на которой отпечаток
сверялся с подлинным живым ClientHello.

Фасад повторяет используемую шлюзом часть API `httpx.AsyncClient`
(`build_request`/`send`/`aread`/`aiter_bytes`/`aclose`), ошибки транспорта
пробрасывает как `httpx.TransportError` — вся логика retry/парковки в
`main.py` и скриптованные транспорты тестов не изменились.

---

## Эндпоинты

Все пути — на едином порту edge (`http://127.0.0.1:53202`):

| Метод | Путь | Куда (edge) | Описание |
|-------|------|-------------|----------|
| ANY | `/v1/*` | gateway | Passthrough к opencode.ai (`chat/completions`, `models`, ...); стриминг поддержан |
| GET | `/` | edge | Редирект 307 на `/login` |
| GET | `/login` | web-admin-ui | SPA-страница входа (React Router) |
| GET | `/admin/stats`, `/admin/proxies`, `/admin/keys` | web-admin-ui | SPA-страницы «Статистика», «Прокси» и «Ключи» |
| GET | `/admin` | web-admin-ui | SPA-редирект на `/admin/stats` с сохранением query |
| GET | `/assets/*` | web-admin-ui | Хэшированные статические файлы SPA |
| GET | `/api/admin/session` | web-admin | Текущая сессия + CSRF-токен (200 и без входа; данные из auth-service) |
| POST | `/api/admin/login` | web-admin | Вход по паролю (проверяет auth-service); HttpOnly cookie сессии |
| POST | `/api/admin/logout` | web-admin | Выход (CSRF; сессия удаляется в auth-service/Redis) |
| GET | `/api/admin/proxies?period=` | web-admin | JSON-снапшот пула: статусы, расходы, последние запросы, `totals` |
| GET | `/api/admin/stats` (`days` / `period`) | web-admin | Токены по прокси и UTC-дням: 1..365 дней или окно `1d/3d/7d/30d/90d/all` |
| GET | `/api/admin/keys?period=` | web-admin | JSON-снапшот ключей и статистики |
| GET | `/api/admin/{proxies,keys}/events?period=` | web-admin | SSE с JSON-снапшотами (fallback — polling) |
| POST | `/api/admin/proxies/set` | web-admin | Включить/отключить прокси по `proxy_id` (CSRF) |
| POST | `/api/admin/proxies/refresh` | web-admin | Принудительно обновить список из px6 со сбросом TTL-кэша (CSRF) |
| GET | `/api/admin/proxies/account` | web-admin | Баланс и валюта аккаунта px6.net |
| GET | `/api/admin/proxies/countries?version=` | web-admin | Страны, доступные для покупки (3/4/6) |
| GET | `/api/admin/proxies/price?version=&count=&period=` | web-admin | Оценка стоимости заказа |
| POST | `/api/admin/proxies/buy` | web-admin | Купить прокси и обновить пул (CSRF) |
| POST | `/api/admin/keys/{add,rotate,set,delete}` | web-admin | Создать/перевыпустить/отозвать/удалить ключ (CSRF) |
| GET | `/health` | gateway | Liveness + счётчики пула, egress-групп и привязанных сессий |
| GET | `/docs`, `/openapi.json` | gateway | Swagger UI / схема |
| ANY | `/internal/*` | — (нет наружу) | Internal API шлюза; доступен только web-admin по `X-Internal-Token` |

Клиентские ключи не имеют ролей и дают доступ только к `/v1/*`. Секрет
генерируется сервисом (`openroute-...`), показывается один раз при создании
или перевыпуске и хранится в storage-service только как SHA-256 хэш. Маршруты
`/admin/status` и `/admin/logs` удалены. `/internal/*` при пустом
`INTERNAL_API_TOKEN` отвечает 404, при неверном токене — 403.

### Internal API шлюза

Все маршруты требуют `X-Internal-Token`; наружу через edge `/internal/*`
не маршрутизируется.

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/internal/proxies-data?period=` | снапшот пула для страницы «Прокси» |
| GET | `/internal/keys-data?period=` | ключи и статистика по `key_id` |
| GET | `/internal/stats-data` (`days` / `period`) | дневные серии токенов |
| POST | `/internal/proxies/refresh` | принудительный фетч прокси со сбросом TTL-кэша px6 |
| POST | `/internal/proxies/set` | включить/выключить прокси |
| GET | `/internal/px6/account` | баланс и валюта px6.net |
| GET | `/internal/px6/countries?version=` | страны для покупки |
| GET | `/internal/px6/price?version=&count=&period=` | оценка стоимости прокси |
| POST | `/internal/px6/buy` | покупка прокси и обновление пула |
| POST | `/internal/keys/add` | создать ключ; возвращает секрет один раз |
| POST | `/internal/keys/rotate` | перевыпустить секрет; возвращает новый ключ один раз |
| POST | `/internal/keys/set` | включить/отозвать ключ |
| POST | `/internal/keys/delete` | удалить ключ |

### storage-service (приватный API)

`storage-service` слушает `openroute-storage:8000` только внутри
`openroute-storage-api-net` (internal): у него нет хост-порта, edge его не
маршрутизирует, публичного `/docs`/OpenAPI нет. PostgreSQL доступен ему
только по `openroute-storage-db-net`, куда gateway не подключён. Собственной
аутентификации у сервиса нет: единственный клиент в его сети — gateway,
поэтому все методы, включая `GET /health`, открыты на этом приватном
контуре. `GET /health` отдаёт `{"status":"ok","database":"ok"}` либо 503 при
недоступном PostgreSQL. Доменные контракты (proxies, keys, affinity,
personas, request-log/stats) — в `storage-service/README.md`; gateway
обращается к ним через `gateway/storage.py`. Наружу этот API не выходит.

### auth-service (приватный API)

`auth-service` доступен как `openroute-auth:8000` в `openroute-auth-net`
(internal), куда подключён лишь `web-admin`; второй интерфейс —
`openroute-redis-net` для Redis. Хост-порта нет, edge его не маршрутизирует,
публичного `/docs`/OpenAPI нет. Сервис — единственный владелец
`ADMIN_PASSWORD`, `ADMIN_SESSION_TTL_SECONDS` и Redis-сессий; `web-admin` не
получает `REDIS_URL`.

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | liveness + `enabled` + число сессий в Redis |
| GET | `/auth/status` | `{"enabled": bool}`; не трогает Redis |
| POST | `/auth/login` | `{password, client_ip}` → `{sid, csrf, expires_at, max_age}`; 401/429 |
| POST | `/auth/validate` | `{sid, csrf, require_csrf}` → сессия; 401/403 |
| POST | `/auth/logout` | `{sid, csrf}` → 204; 401/403 |

Сессии лежат в Redis под `openroute:session:<sid>` как
`{"csrf": str, "expires": float}`; формат совместим с прежними сессиями
`web-admin`. CSRF проверяется constant-time при `require_csrf=true` и при
logout. Login throttling — 10 неудач за 60 секунд на `client_ip`, который
`web-admin` берёт из доверенного `X-Real-IP` edge. Пустой
`ADMIN_PASSWORD` → 404 на login/validate/logout; недоступный Redis → 503
(`Хранилище сессий недоступно`), без fail-open. Контракты и запуск —
в `auth-service/README.md`.

## Клиентские ключи

- Секрет генерирует gateway через `gateway/keys.py:generate_key()`:
  `openroute-` + 48 hex-символов, всего 58 символов. Пользователь не вводит
  ключ вручную; генерация и хэширование остаются в gateway.
- В `storage-service` (таблица `keys` в PostgreSQL) передаётся и хранится
  только `key_hash` (SHA-256), а также `key_id`, `rpm`, `tpm`, `models`,
  `enabled` и `added_at`. Plaintext не сохраняется и не логируется.
- `POST /internal/keys/add` создаёт ключ и возвращает `key` + `key_id`;
  `POST /internal/keys/rotate` заменяет хэш, сохраняет `key_id`, лимиты,
  модели и `enabled`, сбрасывает RPM/TPM-окна и сразу делает старый секрет
  недействительным.
- В админке `POST /api/admin/keys/add` и `POST /api/admin/keys/rotate`
  возвращают секрет только в ответе на мутацию. UI показывает его в
  одноразовом модальном окне с копированием и очищает кэш React Query.
  Снапшоты и SSE-события plaintext не содержат; ответы API идут с
  `Cache-Control: no-store`.
- RPM/TPM — in-memory скользящие окна на `key_id`; они сбрасываются при
  рестарте и перевыпуске. `key_id` — только метка: уникальность не
  гарантируется, статистика по одинаковым `key_id` агрегируется.
- Все мутации ключей идут через storage-service. При его недоступности
  add/rotate/set/delete отвечают 503, а in-memory состояние не меняется;
  чтение снапшота деградирует к in-memory копии без plaintext.

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
| 503 | storage-service недоступен при мутации ключа или прокси | OpenAI-конверт (`Storage service unavailable`); состояние не меняется |
| 504 | исчерпан бюджет ретраев `RETRY_BUDGET_SECONDS` | OpenAI-конверт |

Админка (`web-admin` + auth-service) использует те же коды для своих ошибок:

- `404` — `ADMIN_PASSWORD` пуст (админка выключена);
- `401` — нет/истекла сессия или неверный пароль;
- `403` — неверный `X-CSRF-Token`;
- `429` — больше 10 неудачных попыток входа за минуту с одного IP
  (`Retry-After`);
- `503` — auth-service или его Redis недоступен; запрос не выполняется без
  проверки сессии.

---

## Запуск для разработки

### Docker

```bash
# перед первым запуском: скопируйте примеры и задайте секреты
cp .env.example .env                                   # INTERNAL_API_TOKEN
cp storage-service/.env.example storage-service/.env   # POSTGRES_DSN
cp auth-service/.env.example auth-service/.env         # ADMIN_PASSWORD + REDIS_URL
cp web-admin/.env.example web-admin/.env               # AUTH_SERVICE_URL + тот же INTERNAL_API_TOKEN
cp px6-service/.env.example px6-service/.env           # PX6_API_KEY

docker compose up --build -d                 # весь стек одной командой; сети создаёт compose
docker compose logs -f edge                  # логи стены
docker compose logs -f gateway               # логи шлюза
docker compose logs -f storage-service       # логи сервиса хранения
docker compose down                          # остановка всего
```

Все шесть сетей создаются Compose автоматически; `docker network create` не
нужен. При обновлении со старой плоской схемы `docker compose up -d --build`
создаст недостающие private-сети (`openroute-auth-net` в том числе) и
пересоздаст контейнеры; тома `pgdata` и `redisdata` сохраняются.

Токена для storage-service нет: gateway ходит в него по
`STORAGE_SERVICE_URL` в приватной сети `openroute-storage-api-net`, куда
подключён только gateway. `POSTGRES_DSN` получает **только**
`storage-service` (в compose — `postgres:5432`); gateway его не знает и
зависит от `storage-service` (healthy), а не от `postgres`.

`auth-service` не имеет отдельного токена: единственный клиент в
`openroute-auth-net` — `web-admin`, а `edge` этот контур не маршрутизирует.
`web-admin` знает только `AUTH_SERVICE_URL`; `ADMIN_PASSWORD`,
`ADMIN_SESSION_TTL_SECONDS` и `REDIS_URL` получает **только** auth-service.
Compose запускает auth-service после healthy Redis, а web-admin — после
healthy gateway и auth-service.

Наружу смотрит **только edge** на http://127.0.0.1:53202 (хост-порт задаёт
`OPENROUTE_EDGE_PORT`): и API (`/v1`), и админка (`/`, `/login`,
`/admin/*`, `/api/admin/*`).
Сервисы (gateway, storage-service, auth-service, web-admin, web-admin-ui, px6,
postgres, redis) хостовых портов не публикуют; у `storage-service` и
`auth-service` нет и маршрута в edge. Данные — в томах `pgdata`/`redisdata`
(удаляются только
`docker compose down -v`). `docker compose stop` ждёт до 320 с
(`stop_grace_period`), чтобы стримы доработали до конца; storage-service
останавливается за 30 с.

> **Сосуществование с NVRoute.** OpenRoute полностью изолирован от соседнего
> стека NVRoute: у него собственные сегментированные docker-сети
> (`openroute-net`, `openroute-storage-api-net`, `openroute-storage-db-net`,
> `openroute-auth-net`, `openroute-redis-net`, `openroute-px6-net`; не общая
> `service-net`), уникальные имена контейнеров (`openroute-px6`,
> `openroute-storage`, `openroute-auth`, `openroute-postgres`,
> `openroute-redis`; остальные — автоимена `openroute-gateway-*`,
> `openroute-web-admin-*` и `openroute-web-admin-ui-*`) и собственный
> хост-порт edge
> `OPENROUTE_EDGE_PORT` (по умолчанию 53202;
> NVRoute занимает 53201). Оба стека поднимаются одновременно и не
> конфликтуют.

### Локально без Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r storage-service/requirements.txt \
  -r auth-service/requirements.txt -r web-admin/requirements.txt

# терминал 1: Redis для сессий админки (или свой локальный Redis на :6379)
docker run --rm -p 127.0.0.1:6379:6379 redis:7-alpine

# терминал 2: storage-service (нужен доступный PostgreSQL)
cd storage-service
cp .env.example .env          # POSTGRES_DSN
uvicorn openroute_storage.main:app --host 127.0.0.1 --port 8010 --no-access-log

# терминал 3: px6-service (из корня репозитория)
cd px6-service && uvicorn app.main:app --host 127.0.0.1 --port 8001  # PX6_API_KEY=...

# терминал 4: auth-service (из корня репозитория)
cd auth-service
cp .env.example .env          # ADMIN_PASSWORD, ADMIN_SESSION_TTL_SECONDS
REDIS_URL=redis://127.0.0.1:6379/0 \
  uvicorn app.main:app --host 127.0.0.1 --port 8011 --no-access-log

# терминал 5: gateway (из корня репозитория; 8003 — чтобы не конфликтовать
# с web-admin на 8000)
STORAGE_SERVICE_URL=http://127.0.0.1:8010 \
PROXIES_URL=http://127.0.0.1:8001/proxies \
PX6_SERVICE_URL=http://127.0.0.1:8001 \
  uvicorn gateway.main:app --host 127.0.0.1 --port 8003

# терминал 6: web-admin (из корня репозитория)
cd web-admin && AUTH_SERVICE_URL=http://127.0.0.1:8011 \
  GATEWAY_INTERNAL_URL=http://127.0.0.1:8003 \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`STORAGE_SERVICE_URL` — база storage-service; локально выбран порт `8010`,
чтобы не конфликтовать с gateway на `8003` (в контейнере сервис слушает
`8000`). Токен не нужен: локально сервис слушает loopback и доступен только
запущенным на этой же машине процессам. `POSTGRES_DSN` задаётся **только** в
`storage-service/.env` (по умолчанию
`postgresql://openroute:openroute@localhost:5432/openroute`); схема создаётся
storage-service при первом подключении, а gateway без доступного
storage-service не стартует. `PX6_SERVICE_URL` — база px6-service (без
`/proxies`) для баланса, стран, цены и покупки. `auth-service` локально
запускается на `8011` с `REDIS_URL=redis://127.0.0.1:6379/0`; `web-admin`
знает только `AUTH_SERVICE_URL=http://127.0.0.1:8011` и
`GATEWAY_INTERNAL_URL=http://127.0.0.1:8003`, а сам слушает `8000`.
Без Docker edge нет: backend админки запускается отдельно (`web-admin/`), а
SPA — через `npm run dev` в `web-admin-ui/` (Vite проксирует `/api` на
`127.0.0.1:8000`).

Обновление с предыдущей версии: отдельная миграция не нужна — storage-service
идемпотентно применяет ту же схему к существующему тому `pgdata`, включая
индекс `request_log(account, ts)`. После `docker compose up --build -d`
Compose сам создаст private-сети (`openroute-auth-net` в том числе) и
пересоздаст контейнеры; тома `pgdata` и `redisdata` сохраняются. Нужно
перенести `POSTGRES_DSN` в `storage-service/.env`, а `ADMIN_PASSWORD`,
`ADMIN_SESSION_TTL_SECONDS` и `REDIS_URL` — из `web-admin/.env` в
`auth-service/.env`; отдельный секрет для storage больше не нужен, его можно
удалить из обоих `.env`. Формат сессий в Redis не меняется, поэтому активные
сессии админки переживают переключение на auth-service.

---

## Статус реализации

| Компонент | Статус |
|---|---|
| `gateway/` (шлюз: `main`, `config`, `proxies`, `affinity`, `personas`, `keys`, `reqlog`, `storage`, `opencode`, `sessions`, `px6`, `curlclient`) | **реализовано**; прямые SQL-модули удалены, остался только `StorageClient` |
| `storage-service/` (`openroute_storage`: FastAPI, `config`, `db`, `schema`, `schemas`, `services`; Dockerfile, `.env.example`, README) | **реализовано** — единственный владелец SQL/PostgreSQL |
| `auth-service/` (`app`: FastAPI, `config`, `sessions` (Redis), `schemas`, `main`; Dockerfile, `.env.example`, README) | **реализовано** — пароль, сессии, CSRF, throttling; единственный клиент Redis |
| `web-admin/` (JSON API: `config`, `auth_client`, `auth`, `gateway`, `schemas`, `main`) | **реализовано**; Redis/пароля/сессий не содержит |
| `web-admin-ui/` (React SPA: Vite, React Router, TanStack Query, SSE) | **реализовано** |
| `px6-service/` | **реализовано** (все активные прокси, баланс, страны, цена, покупка) |
| edge / compose / Dockerfile / requirements / .env.example | **готово** (сегментированные private-сети, storage-service и auth-service в compose, у gateway нет DSN, у web-admin нет Redis) |
| `tests/` (`test_gateway`, `test_gateway_storage`, `test_storage_client`, `test_fake_storage`, `test_storage_service`, `test_storage_affinity_persona`, `test_proxies`, `test_affinity`, `test_persona`, `test_keys`, `test_reqlog`, `test_admin_api`, `test_auth_service`, `test_auth_client`, `test_opencode`, `test_proxy6_client`, `test_px6_service`, `test_sessions`, `test_curlclient`) | **реализовано** |

## Тесты

```bash
pip install -r requirements-dev.txt
pytest tests                              # 406 passed, 2 skipped
pytest tests/test_storage_service.py -q   # 10 passed (нужен живой PostgreSQL)

cd web-admin-ui
npm ci
npm test                                  # 32 tests
npm run typecheck
npm run build
```

`tests/test_storage_service.py` — единственный набор с живым PostgreSQL:
изолированная БД на каждый тест (`pg_dsn`, создание/удаление), `TestClient`,
проверки открытого приватного API, контрактов и идемпотентности схемы. Нужен
`docker-compose.test.yml` (`127.0.0.1:55433`); DSN переопределяется
`OPENROUTE_TEST_DSN`. `tests/test_storage_client.py` — `StorageClient` на
`httpx.MockTransport` (без сети/БД), матрица ошибок и отсутствие утечки
секретов/тела; `tests/test_fake_storage.py` — семантика in-memory
`FakeStorage`; `tests/test_gateway_storage.py` — startup-контракт, мутации
ключей/прокси через storage, 503 при сбое, refresh/parking;
`tests/test_storage_affinity_persona.py` — affinity/personas через storage и
отказ старта при неудачной загрузке.

Остальные наборы (`test_gateway`, `test_proxies`, `test_affinity`,
`test_persona`, `test_keys`, `test_reqlog`, `test_sessions`,
`test_admin_api`, `test_auth_service`, `test_auth_client`, `test_curlclient`)
герметичны: upstream, админка/auth и storage подменяются
(`tests/fake_storage.py`), PostgreSQL для gateway-тестов не нужен. Прежние прямые DB-наборы удалены
вместе с SQL-модулями; их сценарии покрыты storage-service и `test_storage_*`.
`tests/test_opencode.py` — чистые юнит-тесты интеграции с
opencode.ai (без сети/БД); `tests/test_admin_api.py` — герметичные тесты JSON
API админки (fake gateway + fake auth-service, без Redis/PostgreSQL/сети);
`tests/test_auth_service.py` — реальное приложение auth-service на
`TestClient` с in-memory хранилищем: вход, сессии, CSRF, throttling, expiry,
503 при недоступном Redis; `tests/test_auth_client.py` — клиент web-admin к
auth-service на `httpx.MockTransport`: разбор сессии, маппинг 401/403/404/429
и 503, отсутствие утечки пароля; `tests/test_px6_service.py` — герметичные
тесты px6-service (fake px6.net); `tests/test_proxy6_client.py` — клиент
px6.net, маскирование ключа в логах и маппинг ошибок. Фронтенд-тесты
(`web-admin-ui`) — Vitest + Testing Library (32 теста), без сети и без
backend; дополнительно проходят typecheck и build.
