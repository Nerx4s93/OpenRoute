# OpenRoute

OpenAI-совместимый API-шлюз, который балансирует клиентские запросы по пулу
**HTTP(S)-прокси** (sticky-сессии по прокси-URL + round-robin для новых задач) и
проксирует их в **free-tier инференс `opencode.ai`**. В отличие от NVRoute здесь
**нет апстрим-токенов**: член пула — это сам прокси. Каждый прокси-URL = одно
«ведро» квоты opencode.ai: входной IPv4 не считается выходным IP, потому что
провайдер может выпускать трафик через IPv6.

- При старте и периодически шлюз забирает **все активные прокси** из
  `px6-service` (обёртка над API [px6.net](https://px6.net)). Комментарий
  `descr` больше не фильтруется: новые прокси добавляются в пул **выключенными**,
  и только явно включённые оператором могут нести трафик.
- В админке есть кнопка **«Обновить список»**: она заставляет gateway запросить
  `px6-service` с `?refresh=true` (сброс TTL-кэша), сразу обновить пул и
  перепроверить прокси.
- В админке три страницы: **«Статистика»** (`/admin/ui`), **«Прокси»**
  (`/admin/ui/proxies`) и **«Ключи»** (`/admin/ui/keys`). На странице «Прокси»
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
- **`403 abusive traffic`** навсегда банит только этот прокси-URL (флаг в БД),
  эвакуирует его сессии, и больше никогда его не выбирает.
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
                     │  + каталог моделей)│        │ баланс/покупка)   │
                     └───────────────────┘        └───────────────────┘
```

### C4 — контейнеры (уровень 2)

```
 Клиент/браузер ──► 0.0.0.0:53202 (edge, nginx — «стена», единственный
                     публичный порт; хост-порт задаёт OPENROUTE_EDGE_PORT)
                        ├─ /v1/*, /health, /docs, /openapi.json ───► [gateway] (data plane)
                        ├─ /login, /admin/ui*, /assets/* ──────────► [web-admin-ui] (React SPA)
                        └─ /api/admin/* ───────────────────────────► [web-admin] (JSON API)
                                                                         │
   (web-admin-ui — только статика; браузер сам ходит в /api/admin/* через edge)
   [gateway] ──► [openroute-px6] ──► px6.net       (каталог, баланс, покупка прокси)
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
- **`px6-service/`** — микросервис px6.net: обёртка над API с TTL-кэшем;
  отдаёт все активные прокси, баланс аккаунта, страны, цену и покупку. Это
  единственный компонент, который знает `PX6_API_KEY`.
- **`postgres`** — PostgreSQL 17: хранилище шлюза (`request_log`, `proxies`,
  `keys`, `session_affinity`, `proxy_personas`). Том
  `pgdata`; порт наружу не публикуется.
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
├── px6-service/              # РЕАЛИЗОВАНО: px6.net → все активные прокси, баланс, покупка
├── web-admin/                # РЕАЛИЗОВАНО: JSON API (config, sessions, auth, gateway, schemas, main)
├── web-admin-ui/             # РЕАЛИЗОВАНО: React SPA (Vite, React Router, TanStack Query)
└── tests/                    # pytest: шлюз, прокси, ключи, лог, админка, px6, opencode
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
- Список из `PROXIES_URL` синхронизируется с БД: новые прокси добавляются,
  пропавшие из источника помечаются `listed = FALSE` (soft-delete) и
  перестают участвовать в балансировке. Записи физически не удаляются, поэтому
  парковка/баны и привязки сессий переживают временное исчезновение прокси;
  если он снова появится в источнике, он возвращается в пул. Флаг `enabled`
  (ручное вкл/выкл) при синхронизации не сбрасывается.
- Если PostgreSQL временно недоступен, gateway сохраняет текущие флаги уже
  известных прокси и оставляет новые выключенными (fail-closed), а не включает
  весь полученный список.
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
- Персона хранится в PostgreSQL (`proxy_personas`, создаётся существующей
  схемой) и переживает рестарт; generation-гард не даёт старой записи
  перетереть новую. Запись — фоновая (fire-and-forget): при недоступной БД
  запросы не ждут и не ломаются, персона живёт в памяти, запись повторяется,
  а при расхождении стор примиряется с более новой строкой БД. Загрузка
  валидирует идентификаторы и заменяет битую строку валидной персоной.
- Переменная `PERSONA_ENABLED` (по умолчанию `true`); `false` — форвардятся
  клиентские identity-заголовки (`x-opencode-project`, `traceparent`,
  `tracestate`, `b3`, `x-b3-*`) вместо значений персоны; все прочие клиентские
  заголовки при этом всё равно не форвардятся (allowlist действует всегда).

---

## Статистика токенов

- На странице **«Статистика»** (`/admin/ui`) — график «Токены по дням»,
  таблица «Прокси» и последние запросы. Период выбирается общими кнопками
  **1д / 3д / 7д / 30д / 90д / Всё время**; окна 1–90 дней включают сегодня
  и предыдущие UTC-дни. График показывает расход по **UTC-дням**. В нём можно
  выбрать один прокси; без выбора показываются топ-8 по расходу и строка
  «Другие». Там же три карточки (запросы и токены за выбранный период,
  прокси в ротации). Таблица «Прокси» показывает токены, запросы и ошибки
  строго за выбранный период и отметку «В пуле»/«Вне пула»; прокси без
  запросов в этом окне не показываются. График и его список прокси также
  строятся только по запросам выбранного периода. Старая страница
  `/admin/ui/stats` удалена; этот URL перенаправляется на `/admin/ui`.
- **Хранилище**: дневные агрегаты строятся по `request_log`; для них добавлен
  индекс `request_log(account, ts)`. DDL идемпотентен и применяется при старте
  gateway; отдельная миграция не нужна, достаточно перезапуска.
- **API шлюза**: `GET /internal/stats-data?days=30` (`X-Internal-Token`,
  `days` зажимается в `1..365`) или `GET /internal/stats-data?period=all`
  (`1d`/`3d`/`7d`/`30d`/`90d`/`all`, как на странице «Статистика»; старые
  `day`/`week`/`month` тоже принимаются). Отдаёт `days`, `server_time`
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
- **Устойчивость**: при недоступном PostgreSQL эндпоинты статистики всё равно
  отвечают 200 — нулями и пустыми сериями. Опрос панели
  (`/internal/proxies-data`, ~1 раз в секунду) использует кэш агрегатов
  (TTL ~10 с), а дневные серии формируются для графика на странице
  «Статистика» (обновление ~30 с).

---

## Прокси (px6.net)

- Страница **«Прокси»** (`/admin/ui/proxies`) показывает баланс аккаунта
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
  хранятся в gateway и никогда не отдаются в браузер. Панель получает
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
| GET | `/admin/ui`, `/admin/ui/proxies`, `/admin/ui/keys` | web-admin-ui | SPA-страницы «Статистика», «Прокси» и «Ключи» |
| GET | `/assets/*` | web-admin-ui | Хэшированные статические файлы SPA |
| GET | `/api/admin/session` | web-admin | Текущая сессия + CSRF-токен (200 и без входа) |
| POST | `/api/admin/login` | web-admin | Вход по паролю; HttpOnly cookie сессии |
| POST | `/api/admin/logout` | web-admin | Выход (CSRF; сессия удаляется в Redis) |
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
| POST | `/api/admin/keys/{add,set,delete}` | web-admin | Добавить/отозвать/удалить ключ (CSRF) |
| GET | `/health` | gateway | Liveness + счётчики пула, egress-групп и привязанных сессий |
| GET | `/docs`, `/openapi.json` | gateway | Swagger UI / схема |
| ANY | `/internal/*` | — (нет наружу) | Internal API шлюза; доступен только web-admin по `X-Internal-Token` |

Клиентские ключи не имеют ролей и дают доступ только к `/v1/*`; маршруты
`/admin/status` и `/admin/logs` удалены. `/internal/*` при пустом
`INTERNAL_API_TOKEN` отвечает 404, при неверном токене — 403.

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
cd px6-service && uvicorn app.main:app --host 127.0.0.1 --port 8001  # PX6_API_KEY=...
# в соседнем терминале, из корня:
PROXIES_URL=http://127.0.0.1:8001/proxies \
PX6_SERVICE_URL=http://127.0.0.1:8001 \
  uvicorn gateway.main:app --host 127.0.0.1 --port 8000
```

`PX6_SERVICE_URL` — база px6-service (без `/proxies`) для баланса, стран,
цены и покупки. Нужен доступный PostgreSQL (по умолчанию
`POSTGRES_DSN=postgresql://openroute:openroute@localhost:5432/openroute`); схема
создаётся сама при первом обращении. Без Docker edge нет: backend админки
запускается отдельно (`web-admin/`), а SPA — через `npm run dev` в
`web-admin-ui/` (Vite проксирует `/api` на `127.0.0.1:8000`).

Обновление с предыдущей версии — только перезапуск: индекс
`request_log(account, ts)` создаётся идемпотентно при старте gateway.
Отдельных шагов миграции и правок compose/`.env` не требуется.

---

## Статус реализации

| Компонент | Статус |
|---|---|
| `gateway/` (шлюз: `main`, `config`, `proxies`, `proxydb`, `affinity`, `affinitydb`, `personas`, `personadb`, `keys`, `keydb`, `reqlog`, `pg`, `opencode`) | **реализовано** |
| `web-admin/` (JSON API: `config`, `sessions`, `auth`, `gateway`, `schemas`, `main`) | **реализовано** |
| `web-admin-ui/` (React SPA: Vite, React Router, TanStack Query, SSE) | **реализовано** |
| `px6-service/` | **реализовано** (все активные прокси, баланс, страны, цена, покупка) |
| edge / compose / Dockerfile / requirements / .env.example | **готово** |
| `tests/` (`test_gateway`, `test_proxies`, `test_affinity`, `test_affinitydb`, `test_persona`, `test_personadb`, `test_keys`, `test_reqlog`, `test_admin_api`, `test_opencode`, `test_proxy6_client`, `test_px6_service`) | **реализовано** |

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
Redis/PostgreSQL); `tests/test_px6_service.py` — герметичные тесты px6-service
(fake px6.net); `tests/test_proxy6_client.py` — клиент px6.net, маскирование
ключа в логах и маппинг ошибок. Остальные наборы (`test_gateway`,
`test_proxies`, `test_affinitydb`, `test_personadb`, `test_keys`, `test_reqlog`)
работают герметично: upstream и админка подменяются, а живой PostgreSQL для
хранилищ шлюза (включая `session_affinity` и `proxy_personas`) поднимается из
`docker-compose.test.yml` (`127.0.0.1:55433`), DSN переопределяется
`OPENROUTE_TEST_DSN`. `test_personadb` проверяет generation-гард и загрузку
после рестарта. Фронтенд-тесты (`web-admin-ui`) — Vitest + Testing Library,
без сети и без backend.
