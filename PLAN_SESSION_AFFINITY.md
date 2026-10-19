# OpenRoute — план: sticky-сессии по exit IP и балансировка S + R + T

> **Статус:** план реализован и развёрнут в рабочем дереве (код, тесты, docs); образ пересобран, контейнер healthy.
> **Дата:** 2026-10-19.
> **Область:** gateway `OpenRoute` (`app/`), PostgreSQL-схема, тесты, docs.

---

## 0. Кратко

- Каждая чат-сессия навсегда привязывается к одному купленному exit IP
  (`IP:port` из пула). Привязка не меняется из-за нагрузки.
- Новые и эвакуированные сессии распределяются по трём метрикам:
  `S` — число сессий, `R` — запросы за окно, `T` — токены за окно.
- Окно одной сессии — 1 час: сессия младше часа считается целиком, старше —
  только последний час. Без экстраполяции и деления на возраст.
- `429 FreeUsageLimitError` — дневной лимит exit IP. Вся IP-группа паркуется
  до 00:00 UTC, все её сессии эвакуируются по одной с пересчётом score после
  каждой.
- Другие `429` (провайдер, ключ, per-minute) не паркуют IP и не вызывают
  эвакуацию — ответ проходит клиенту.
- `403 abusive traffic` — бан всей IP-группы и такая же эвакуация.
- Удаление прокси из БД даёт `proxy = NULL`; следующая реплика сессии выбирает
  новый прокси по тому же score.
- Никаких «соседних портов»: используются только реально купленные URL из
  пула. Порты одного IP объединяются в одну egress-группу для квоты и бана.

---

## 1. Цели и ограничения

### 1.1. Цели

1. Один диалог / одна задача CTF-комбайна → один стабильный exit IP на всё
   время жизни сессии.
2. Новые сессии не сбиваются в одну «свежую» IP-группу: баланс по числу
   сессий, запросам и токенам.
3. При исчерпании дневной квоты IP его сессии не умирают, а переезжают на
   другие готовые IP.
4. После полуночи UTC освободившийся IP снова доступен для новых сессий.
5. Удаление или ручное отключение прокси не оставляет сессию в тупике.

### 1.2. Ограничения

- Балансировочная единица — **egress-группа** (exit IP), а не отдельный порт:
  upstream-квота и бан считаются по IP. Два купленных URL
  `217.29.62.211:10077` и `217.29.62.211:10078` — одна группа.
- Нельзя синтезировать порты или использовать порты, которых нет в пуле.
- Уже привязанная сессия не переезжает из-за того, что другой IP стал менее
  загруженным.
- Один процесс uvicorn / один worker. In-memory статистика и привязки
  рассчитаны на это. Для нескольких worker'ов понадобится Redis.
- `S` — «навсегда привязанные» сессии; завершённые задачи тоже занимают слот
  (см. открытый вопрос в §13).

---

## 2. Проверенное поведение upstream

Источники: `opencode-rev/packages/console/app/src/routes/zen/util/`.

- **Дневной лимит запросов на IP.** Считается per UTC day, сброс в `00:00 UTC`.
  При исчерпании upstream отвечает `429` с `Retry-After` до полуночи.
  Новый IP получает повышенный лимит (2x), пока суммарное число запросов за
  всё время меньше 7x базового лимита.
- **Trial-токены на IP.** Кумулятивные, никогда не сбрасываются, отдельного
  `429` на них нет.
- **Key limiter.** Per minute; для анонимного free-tier не является основным.
- **Provider 429** — это не лимит exit IP. Его нельзя парковать как дневную
  квоту.
- **Sticky-сессия upstream.** `stickyId = sessionId`: upstream уже ожидает, что
  один диалог приходит с одним session id. Наша привязка к IP — второй,
  независимый слой.
- **403 `abusive traffic`.** Бан exit IP за подозрительный трафик; лечится
  сменой IP, а не повторением того же запроса.
- **403 `FreeTierError`.** Body-level отказ, одинаковый через любой прокси;
  это свойство запроса, а не exit IP.

---

## 3. Терминология

| Термин | Значение |
|---|---|
| **Egress-группа** | Exit IP, выделенный из host части прокси-URL. Для IPv6 — `/64` (upstream усекает до `/64`). Сейчас пул IPv4. |
| **Прокси / URL** | Конкретная купленная запись `scheme://user:pass@host:port` в таблице `proxies`. |
| **Сессия** | Один диалог, идентифицированный `sid:<session_id>` или `fp:<fingerprint>`. |
| **One-shot** | Запрос без якоря для сессии: нет валидного `ses_...` и нет fingerprint. Привязки не создаёт. |
| **Bound session** | Сессия, у которой есть строка в `session_affinity` с непустым `proxy`. |
| **Готовая группа** | Есть хотя бы один URL, который не disabled, не в cooldown, не на RPM-лимите, и группа не в `quota_until` / `banned_until`. |
| **`quota_until`** | Wall-clock epoch, до которого вся группа запаркована дневным лимитом. |
| **`banned_until`** | Wall-clock epoch, до которого вся группа забанена за abusive traffic. |
| **`cooldown_until`** | Существующее поле `Proxy`, monotonic; короткий breaker/429-cooldown. Не путать с квотой. |

---

## 4. Модель данных и статистика

### 4.1. Таблица привязок

Добавляется в `app/pg.py` (frozen `SCHEMA`):

```sql
CREATE TABLE IF NOT EXISTS session_affinity (
    session_key TEXT PRIMARY KEY,
    proxy TEXT REFERENCES proxies(proxy) ON DELETE SET NULL,
    created_at DOUBLE PRECISION NOT NULL,
    last_used_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_affinity_proxy
    ON session_affinity (proxy);

CREATE INDEX IF NOT EXISTS idx_session_affinity_used
    ON session_affinity (last_used_at);
```

- `ON DELETE SET NULL` — удаление прокси из `proxies` автоматически обнуляет
  привязку.
- `created_at` — время создания привязки; при переезде не меняется.
- `last_used_at` — время последнего запроса сессии.
- Строка живёт «навсегда»; завершённые сессии не удаляются автоматически.
- В таблице хранится полный proxy URL (как в `proxies.proxy`), включая
  credentials. Это тот же уровень секретности, что и сама таблица пула.

### 4.2. Ключ сессии

`session_key` выбирается в `_resolve_session()`:

| Ситуация | Ключ | Upstream session id |
|---|---|---|
| Клиент прислал валидный `ses_...` в одном из `SESSION_HEADER_NAMES` | `sid:<session_id>` | этот же id |
| Валидного заголовка нет, но есть fingerprint по первому user-сообщению | `fp:<sha256>` | id из `SessionRegistry` |
| Нет ни заголовка, ни fingerprint | `None` (one-shot) | новый одноразовый id |

`conversation_fingerprint()` уже включает `key_id` и `x-opencode-project`,
поэтому одинаковый текст у разных ключей/проектов не склеивается.

Важно для комбайна: чтобы 30 задач дали 30 стабильных сессий, каждая задача
должна присылать свой валидный `ses_...` (например, в
`x-opencode-session`). Если присылать только последний ход без заголовка,
fingerprint будет меняться и одна задача распадётся на несколько сессий.

### 4.3. Статистика сессии

В памяти на каждую `session_key`:

```
SessionState:
    key: str
    proxy: str | None            # текущая привязка (дублирует БД)
    created_at: float            # wall clock
    last_used_at: float          # wall clock
    events: deque[Event]         # окно запросов
```

`Event`:

```
Event:
    ts: float                    # wall clock, время начала клиентского запроса
    tokens: int                  # total_tokens; 0, пока неизвестно
```

Правило одного события:

- Одно событие на **один клиентский запрос**, а не на каждую upstream-попытку.
- Событие добавляется при первой реальной отправке в upstream:
  `(ts=request_started_wall, tokens=0)`. Так `R` учитывает запрос сразу.
- После завершения запроса (стрим или обычный ответ) в это же событие
  добавляются фактически полученные `total_tokens`.
- Если клиент оборвал стрим и usage неизвестен, остаётся `tokens=0`.
- Если запрос не дошёл до upstream (нет готового прокси, отказ auth и т.п.),
  событие не создаётся.
- Retry-попытки внутри одного клиентского запроса не создают новые события.
  Это значение по умолчанию; вопрос «клиентский запрос vs upstream-попытка»
  вынесен в §13.

Почему `tokens=0` сразу: `R` должен попадать в окно в момент запроса, а не
после длинного стрима. `T` дозаписывается позже; окно считается по `ts`.

### 4.4. Окно 1 час

Для сессии `s` в момент `now`:

```
window_start = max(s.created_at, now - 3600)

r_s = |{ e in s.events : e.ts >= window_start }|
t_s = sum(e.tokens for e in s.events if e.ts >= window_start)
```

- Сессия живёт 10 минут → окно = все её 10 минут.
- Сессия живёт 3 часа → окно = последний час.
- Никакой экстраполяции, деления на возраст или прогноза «в час».

События старше `window_start` можно физически вычищать. Для защиты от
патологического RPM полезно ограничить `events` (например, 10 000 на сессию),
но в норме окно и так ограничено.

### 4.5. Агрегаты egress-группы

Для группы `g`:

```
S_g = число сессий, у которых proxy принадлежит g
R_g = Σ r_s по этим сессиям + one-shot запросы за последний час
T_g = Σ t_s по этим сессиям + one-shot токены за последний час
```

- Статистика сессии «переезжает» вместе с ней: при эвакуации её `r_s` / `t_s`
  сразу попадают в агрегаты нового IP.
- Это **прогнозная нагрузка**, которую сессия принесёт новому IP, а не
  историческая бухгалтерия квоты.
- One-shot трафик хранится в отдельном кольцевом буфере группы:
  `deque[(ts, tokens)]`, окно тоже 1 час.
- Сессия с `proxy = NULL` не входит ни в одну группу.

Пример: сессия за последний час съела 50k токенов на старом IP. При переезде
эти 50k сразу учитываются в score нового IP — это оценка нагрузки, а не
утверждение, что новый IP уже потратил квоту.

### 4.6. Что переживает рестарт

- Привязки `session_affinity` — да, загружаются из PostgreSQL при старте.
- `SessionState.events` и one-shot буферы — нет, сбрасываются.
- После рестарта `S` восстанавливается, `R` / `T` начинаются с нуля.
- Восстановление событий из `request_log` не делаем: там нет `session_key`.
  Если понадобится позже — это отдельная схема/миграция.
- `quota_until` / `banned_until` в первой версии живут в памяти. После
  рестарта первая попытка снова получит `429` и запаркует группу заново.
  Опционально можно позже persist-ить их в отдельную таблицу.

---

## 5. Score и выбор прокси

### 5.1. Формула

```
score(g) = wS * S_g / Smax
         + wR * R_g / Rmax
         + wT * T_g / Tmax
```

- `Smax`, `Rmax`, `Tmax` — максимумы среди **кандидатов** на текущий момент.
- Если максимум равен нулю, соответствующий член равен нулю.
- Веса по умолчанию `wS = wR = wT = 1.0`, конфигурируются.
- Выбирается минимальный score.
- При равенстве (в пределах `1e-12`) — round-robin по стабильному списку
  кандидатов.

Пример из обсуждения:

```
IP1: S=2, R=3000
IP2: S=3, R=1000
Smax=3, Rmax=3000

score(IP1) = 2/3 + 3000/3000 = 1.667
score(IP2) = 3/3 + 1000/3000 = 1.333  -> выбирается IP2
```

### 5.2. Кандидаты

В кандидаты попадает egress-группа, если:

- в ней есть хотя бы один URL, который `enabled`, не `manually_disabled`, не
  `disabled`, не в `cooldown_until`, не на локальном RPM-лимите;
- `quota_until <= now`;
- `banned_until <= now`.

Группа, которая просто `cooling` из-за короткого 429/сетевой ошибки, может
быть кандидатом только после окончания cooldown. Если все группы cooling,
`NoReadyProxyError.retry_after` считается по минимальному оставшемуся времени,
включая quota/banned.

### 5.3. Выбор конкретного URL

Score считается по группе. Внутри выбранной группы:

- для новой сессии — round-robin по готовым URL этой группы (если куплено
  несколько портов одного IP);
- для существующей сессии — ровно тот URL, который записан в привязке, пока он
  есть в пуле и готов;
- если URL удалён/отключён, а в группе есть другой купленный URL, сессия может
  переехать на него (это всё ещё тот же exit IP). Если группа недоступна —
  выбирается другая группа по score.

Никаких портов, которых нет в `proxies`, не создаётся.

---

## 6. Жизненный цикл сессии

| Ситуация | Поведение |
|---|---|
| Новая сессия, привязки нет | Выбрать готовую группу по score, затем готовый URL; создать строку `session_affinity`, `created_at = last_used_at = now`. |
| Сессия уже привязана, URL готов | Использовать ровно этот URL. Score не пересчитывается, сессия не переезжает из-за нагрузки. |
| URL удалён из БД | FK ставит `proxy = NULL`; следующая реплика выбирает новый URL по score. |
| URL удалён из runtime-пула, но строка ещё есть | При первом обращении обнаружить отсутствие URL, обнулить/перепривязать по score. |
| URL вручную disabled | Считать привязку недействительной и перепривязать по score (это действие админа, а не балансировка). |
| URL временно cooling / RPM-capped | Первая попытка идёт на другой готовый URL как временный fallback; постоянная привязка **не меняется**. Следующая реплика снова попробует родной URL. |
| Группа в `quota_until` / `banned_until` | Сессия должна быть эвакуирована (§7/§8). Если по какой-то причине осталась — эвакуировать перед выбором. |
| One-shot | Привязку не создавать; выбрать группу по score; записать запрос/токены в one-shot буфер группы. |
| Нет ни одного готового URL | `NoReadyProxyError(retry_after)` → клиенту `429` с `Retry-After` до ближайшего освобождения (cooldown / RPM / quota / ban). |

Временный fallback не меняет `session_affinity`. Статистика сессии при этом
остаётся на её постоянной группе: это прогноз нагрузки сессии, а не
историческая бухгалтерия одного запроса. Если такой перекос станет заметным,
позже можно хранить фактический proxy в событии.

---

## 7. `429 FreeUsageLimitError`

### 7.1. Классификация

Читаем тело ответа **до** закрытия:

```python
body = await upstream.aread()
await upstream.aclose()
```

Признак дневного лимита — строго:

```json
{"type": "error", "error": {"type": "FreeUsageLimitError", "message": "..."}}
```

То есть `data["error"]["type"] == "FreeUsageLimitError"`.

- Только этот тип паркует IP и запускает эвакуацию.
- `429` с `RateLimitError`, provider-специфичным типом, per-key или
  per-minute — **не** паркует IP и **не** эвакуирует.
- Пустое/нечитаемое тело или неизвестный тип — трактуем как «другой 429»,
  проходим клиенту. Консервативно: лучше не запарковать весь IP из-за
  per-minute лимита.

### 7.2. Время ожидания

Точная формула до ближайшей полуночи UTC:

```python
def seconds_to_utc_midnight(now: float | None = None) -> float:
    now_ms = int((now if now is not None else time.time()) * 1000)
    return math.ceil((86_400_000 - (now_ms % 86_400_000)) / 1000)
```

Парковка:

```python
wait = max(seconds_to_utc_midnight(), retry_after or 0.0) + 60.0
group.quota_until = time.time() + wait
```

- `+60` — запас на TTL ключа в Redis upstream: upstream ставит TTL в `track()`
  уже после ответа, поэтому ровно в полночь ключ может быть ещё жив.
- Если upstream прислал `Retry-After` больше, чем до полуночи, берём больше.
- `quota_until` — wall-clock epoch, не `monotonic`, потому что он сравнивается
  с UTC-расписанием и показывается в статусе.

### 7.3. Парковка

- Паркуется **вся egress-группа**: все URL с этим IP.
- `quota_until` — отдельное поле от `cooldown_until`.
- `mark_success`, health-probe и обычные успешные ответы **не снимают**
  `quota_until`.
- Запаркованные группы не участвуют в выборе прокси и не пробуются
  health-check'ом.
- Если запаркованы все группы, клиент получает `429` с `Retry-After` до
  ближайшего `quota_until` / `banned_until` / cooldown.

### 7.4. Эвакуация

Алгоритм (LPT — крупные сессии первыми):

```python
def evacuate(parked: EgressGroup, wait: float) -> None:
    parked.quota_until = time.time() + wait

    moving = [s for s in sessions if s.proxy and group_of(s.proxy) is parked]
    moving.sort(key=lambda s: (s.tokens_1h, s.requests_1h), reverse=True)

    for s in moving:
        candidates = [g for g in groups if g is not parked and g.ready()]
        if not candidates:
            s.unbind()                 # proxy = NULL
            db.clear_async(s.key)
            continue

        # score каждого кандидата уже учитывает ранее переехавших сессий;
        # вклад текущей сессии добавляется перед сравнением
        best = min(candidates, key=lambda g: score(g, extra=s))

        proxy = pick_ready_url(best)   # конкретный купленный URL
        s.bind(proxy)                  # last_used_at = now
        update_group_aggregates(s)     # следующие итерации видят новый вклад

    db.upsert_many_async(moving)
```

Детали:

- Сессии сортируются по `t_s` убыв., затем по `r_s` убыв. Крупные уходят
  первыми, распределение ровнее.
- После каждой привязки агрегаты группы обновляются **до** выбора следующей
  сессии. Это и есть «пересчёт после каждой итерации».
- Если кандидатов нет, сессия остаётся с `proxy = NULL`; следующая реплика
  выберет готовый IP, когда он появится.
- Запись в БД — батчем, асинхронно. В памяти привязка меняется сразу, чтобы
  текущий запрос уже пошёл на новый прокси.
- После эвакуации текущий клиентский запрос продолжает retry loop. Старый
  proxy уже в `tried`; `next_proxy` не вернёт запаркованную группу.
- Если готовых IP не осталось, текущий запрос получает `429` с временем до
  ближайшего сброса. Это не ошибка клиента.

### 7.5. Другие 429

- Не вызываем `mark_failure` как для «перегретого» прокси.
- Не трогаем `quota_until`, не запускаем эвакуацию.
- Возвращаем клиенту upstream `429`, тело и `Retry-After` (pass-through).
- Прокси остаётся готовым; это не свойство exit IP.

---

## 8. `403 abusive traffic`

Порядок классификации `403`:

1. `FreeTierError` — свойство запроса, не IP. Пропускаем ответ клиенту,
   прокси не наказываем (уже реализовано).
2. `abusive traffic` в теле (case-insensitive) — бан exit IP.
3. Иначе — обычная ошибка прокси: `mark_failure` и следующая попытка.

Для abusive-бана:

- `banned_until = time.time() + ABUSIVE_BAN_SECONDS` на всю группу.
- По умолчанию `ABUSIVE_BAN_SECONDS = 86_400` (24 часа). При повторе можно
  эскалировать до 7 суток — опционально, отдельной настройкой.
- Все сессии группы эвакуируются тем же алгоритмом, что и при quota 429.
- Текущий запрос можно повторить на другом IP: это не body-level отказ
  запроса. Если других IP нет — `502` / `429` с понятной ошибкой.
- `banned_until` так же не снимается `mark_success` и health-probe.

---

## 9. Готовность, breaker и health probes

### 9.1. Разделение полей

| Поле | Где | Шкала | Кто ставит | Кто снимает |
|---|---|---|---|---|
| `cooldown_until` | `Proxy` | `time.monotonic()` | `mark_failure` (сеть/5xx/прочие 401/403; generic 429 не наказывает — §7.5) | `mark_success`, истечение |
| `quota_until` | `EgressGroup` | epoch (`time.time()`) | `FreeUsageLimitError` | только истечение / новый UTC день |
| `banned_until` | `EgressGroup` | epoch (`time.time()`) | `403 abusive traffic` | только истечение |

### 9.2. Обязательные правила

- `mark_success(proxy)` сбрасывает `consecutive_errors`, `_fails`,
  `_throttle_n`, `cooldown_until`. **Не трогает** `quota_until` /
  `banned_until`.
- `refresh_proxies()` не пробует URL из запаркованной группы и не вызывает для
  них `mark_success`.
- `next_proxy()` никогда не возвращает URL из группы с
  `quota_until > now` или `banned_until > now`.
- `_earliest_ready_seconds()` учитывает `quota_until` и `banned_until`; если
  все группы запаркованы, возвращает время до ближайшего сброса.
- `status()` показывает группу: `ip`, `sessions`, `requests_1h`, `tokens_1h`,
  `quota_seconds_left`, `banned_seconds_left`, `ready`, `score`.
- `/health` можно расширить аддитивными счётчиками:
  `egress_groups_total`, `egress_groups_ready`, `egress_groups_quota_parked`,
  `egress_groups_banned`, `sessions_bound`, `sessions_unbound`.
  Существующие ключи не менять.
- При запарковке всей группы/всего пула — alert с временем до сброса.

---

## 10. Изменения по файлам

### `app/pg.py`

- Добавить `session_affinity` и два индекса в `SCHEMA`.
- DDL идемпотентный, как остальные таблицы.

### `app/affinity.py` (новый)

Ответственность:

- `SessionIdentity` / `SessionState`;
- загрузка привязок из БД;
- выбор и перепривязка прокси;
- учёт событий, окно 1 час;
- score и агрегаты групп;
- эвакуация;
- one-shot буферы;
- статус.

Предлагаемые сущности:

```python
@dataclass
class SessionIdentity:
    session_id: str          # upstream session id
    key: str | None          # "sid:..." / "fp:..." / None
    kind: str                # "sid" / "fp" / "one-shot"

@dataclass
class SessionState:
    key: str
    proxy: str | None
    created_at: float
    last_used_at: float
    events: deque

class AffinityManager:
    async def load(self) -> None
    def ensure_bound(self, identity: SessionIdentity) -> Proxy
    def next_proxy(self, identity: SessionIdentity,
                   exclude: set[str]) -> Proxy
    def note_started(self, identity, proxy: Proxy, ts: float) -> None
    def note_tokens(self, identity, tokens: int) -> None
    async def evacuate(self, group, *, quota_until=None,
                       banned_until=None) -> None
    def score(self, group, extra: SessionState | None = None) -> float
    def status(self) -> dict
```

`ensure_bound` / `next_proxy` синхронны (без `await` между чтением и записью
состояния) — это сохраняет атомарность в одном event loop. Запись в БД —
отдельная fire-and-forget задача с сильной ссылкой, как в `reqlog`.

### `app/affinitydb.py` (новый)

- `AffinityDB`: ленивый пул `psycopg_pool`, DDL `session_affinity` из `pg.py`.
- `load_all()`, `upsert_many()` (переезд не меняет `created_at`), `clear()`.
- FK `ON DELETE SET NULL`: admin-удаление прокси развязывает сессию; гонка
  writer'а с удалением ретраится по строкам — NULL получает только привязка
  к исчезнувшему прокси, соседние записи батча не страдают.
- Все методы блокирующие; `AffinityManager` вызывает их через
  `asyncio.to_thread`.

### `app/proxies.py`

- `EgressGroup`: `host`, `proxies`, `quota_until`, `banned_until`.
- `ProxyManager.egress_groups()`, `group_of(proxy)`,
  `next_proxy_in_group(group, exclude)`.
- `mark_quota_exhausted(group, until)`, `mark_banned(group, until)`.
- `apply_proxies()` перестраивает группы, сохраняя parking-состояние по host.
- `remove_proxy()` обновляет группы.
- `next_proxy()` / `_earliest_ready_seconds()` / `status()` — с учётом групп.
- `mark_success()` не снимает quota/ban.
- `refresh_proxies()` пропускает probes для запаркованных групп.

### `app/opencode.py`

Добавить чистые функции:

```python
def seconds_to_utc_midnight(now: float | None = None) -> float
def error_type(body: bytes | str | None) -> str | None
def is_free_usage_limit(body: bytes | str | None) -> bool
def is_abusive_ban(body: bytes | str | None) -> bool
```

- `error_type` читает `data["error"]["type"]` и никогда не бросает.
- `is_abusive_ban` — case-insensitive поиск `abusive traffic` в тексте тела.
- `is_free_usage_limit` — строгое равенство типу.

### `app/main.py`

- `_resolve_session_id()` → `_resolve_session()` возвращает
  `SessionIdentity`.
- Перед retry loop: `identity = _resolve_session(...)`.
- В цикле: `proxy = affinity.next_proxy(identity, exclude=tried)`; для
  one-shot — обычный `manager.next_proxy(exclude=tried)`.
- `note_started()` при первой реальной отправке в upstream.
- `note_tokens()`:
  - non-stream — после `_aggregate_sse`;
  - stream — в `_stream_gen` после сканирования usage;
  - non-chat — 0.
- `429`:
  - `body = await upstream.aread()` до `aclose`;
  - `is_free_usage_limit(body)` → `affinity.evacuate(group, quota_until=...)`,
    `continue`;
  - иначе — pass-through `429` с телом и `Retry-After`, без `mark_failure`.
- `403`:
  - `FreeTierError` → pass-through;
  - `is_abusive_ban(body)` → `affinity.evacuate(group, banned_until=...)`,
    `continue`;
  - иначе — текущая обработка.
- `_stream_gen` получает `identity` / event handle, чтобы дозаписать токены.
- `NoReadyProxyError` → `429` с `Retry-After` (уже есть), но теперь
  `retry_after` учитывает quota/ban.
- Логи: маскировать host, не светить credentials.

### `app/config.py`, `.env.example`

Новые настройки (имена примерные, согласовать при реализации):

```python
affinity_enabled: bool = True
affinity_window_seconds: float = 3600.0
affinity_weight_sessions: float = 1.0
affinity_weight_requests: float = 1.0
affinity_weight_tokens: float = 1.0
quota_reset_margin_seconds: float = 60.0
abusive_ban_seconds: float = 86400.0
```

### `DESIGN.md`

- Добавить `app/affinity.py` в module inventory.
- Описать `session_affinity` DDL.
- Обновить контракт пула: egress-группы, `quota_until`, `banned_until`,
  ready/`next_proxy`, health probes.
- Описать 429/403 классификацию и pass-through.
- Уточнить, что `/health` расширяется аддитивно.

### `README.md`

- Раздел про sticky-сессии и требования к комбайну: присылать валидный
  `ses_...` на задачу.
- Переменные `AFFINITY_*`.

### Тесты

Новые/обновлённые файлы:

- `tests/test_affinity.py` — score, окна, эвакуация, привязки.
- `tests/test_opencode.py` — `seconds_to_utc_midnight`, классификаторы.
- `tests/test_gateway.py` — интеграция 429/403, pass-through, смена прокси.
- `tests/test_proxies.py` (если есть/нужен) — egress-группы, readiness,
  `mark_success` не снимает quota.
- `tests/test_affinitydb.py` — DDL/FK `ON DELETE SET NULL`, upsert/clear,
  FK-гонка writer'а с удалением прокси.

---

## 11. План тестов

### 11.1. Balance / score

1. Пример `IP1: S=2,R=3000` vs `IP2: S=3,R=1000` → выбирается IP2.
2. Все метрики нулевые → round-robin, равномерно.
3. 30 новых сессий на 3 IP → 10/10/10 (при равных весах и нулевой истории).
4. Новая сессия не выбирает группу с `quota_until` в будущем.
5. Веса `wS=10, wR=0, wT=0` → выбор по минимальному S.
6. Tie-break не залипает на одном IP.

### 11.2. Окно

1. Сессия живёт 10 минут, 5 запросов → `r=5`, всё окно.
2. Сессия живёт 3 часа: старые события отброшены, считаются только последние
   60 минут.
3. Событие с `ts` ровно на границе окна.
4. Токены, дозаписанные после завершения стрима, попадают в то же событие.
5. Failed request без usage: `R+1`, `T+0`.
6. One-shot не создаёт `session_affinity`, но попадает в буфер группы.

### 11.3. Привязка

1. Один и тот же `sid:` → один и тот же proxy на повторных запросах.
2. Разные `sid:` → распределение по score.
3. `fp:` стабилен при повторной отправке полной истории.
4. Удаление прокси → FK `NULL` → следующий запрос выбирает новый.
5. URL исчез из runtime-пула, но строка осталась → перепривязка.
6. Ручное `enabled=false` → перепривязка.
7. Временный cooldown → fallback на другой URL, привязка не меняется.
8. После рестарта привязки загружаются, `R/T` = 0.

### 11.4. 429

1. `FreeUsageLimitError` → вся группа запаркована до ближайшей полуночи UTC
   (+60 c), все сессии эвакуированы.
2. Эвакуация идёт по убыванию `(t_s, r_s)`.
3. После каждой привязки score следующей сессии видит уже переехавшую.
4. Если кандидатов нет → `proxy = NULL`, клиент получает `429` с временем до
   сброса.
5. `Retry-After` больше времени до полуночи → берётся он.
6. `Retry-After` отсутствует → всё равно до полуночи.
7. Provider `429` → pass-through, без парковки и эвакуации.
8. `429` с пустым/неизвестным телом → pass-through.
9. `mark_success` и health probe не снимают `quota_until`.
10. После `quota_until` группа снова выбирается.
11. Если запаркованы все IP → `429` с `Retry-After` до ближайшего сброса.

### 11.5. 403 / ban

1. `FreeTierError` → pass-through, никакого бана.
2. `abusive traffic` → `banned_until` на группе, эвакуация.
3. Другой `403` → обычный `mark_failure` + следующая попытка.
4. `banned_until` не снимается success/probe.

### 11.6. Формула полуночи

1. Известный timestamp → точное число секунд.
2. Ровно полночь UTC.
3. `Retry-After` HTTP-date и число.
4. Часовой пояс не влияет (только UTC).

### 11.7. Регрессии

- Все существующие 185 тестов проходят.
- `/health` сохраняет старые ключи.
- Session-id fix (`f5553ea`) не ломается: один upstream session id на диалог.

---

## 12. Внедрение и живая проверка

### 12.1. Порядок

1. Реализовать schema + `app/affinity.py` + `app/proxies.py`.
2. Подключить `app/main.py`, классификаторы в `app/opencode.py`.
3. Обновить `DESIGN.md`, `README.md`, `.env.example`.
4. Прогнать тесты.
5. Пересобрать и перезапустить gateway.
6. Живые проверки.

### 12.2. Команды

```powershell
cd C:\Users\Никита\Desktop\service\OpenRoute
python -m pytest -q
docker compose up -d --build openroute
docker compose ps
```

Проверить, что контейнер использует новый образ, а не старый:

```powershell
docker inspect openroute-openroute-1 --format '{{.Image}} {{.Created}}'
docker compose logs --tail=100 openroute
```

### 12.3. Живые проверки

1. **Sticky.** Два запроса с одним `x-opencode-session: ses_...` → в логах
   один и тот же `account` (masked proxy).
2. **Разные задачи.** 30 разных `ses_...` → сессии распределены по IP;
   в `/admin/status` видно `sessions` по группам.
3. **429.** Дождаться/сэмулировать `FreeUsageLimitError` → группа
   запаркована, в логах эвакуация, `quota_until` до ближайшей полуночи.
4. **Другие 429.** Provider 429 → клиент получает 429, группа не запаркована.
5. **Удаление.** Удалить прокси через админку → в `session_affinity`
   `proxy = NULL`, следующая реплика сессии уходит на новый IP.
6. **Полночь.** После 00:00 UTC + 60 c группа снова становится ready.

SQL для проверки привязок:

```sql
SELECT session_key, proxy, created_at, last_used_at
FROM session_affinity
ORDER BY created_at;
```

Статус:

```powershell
curl.exe -H "Authorization: Bearer <admin-key>" http://localhost:53202/admin/status
```

### 12.4. Результат внедрения

- Образ gateway пересобран в `2026-10-19T16:32:57Z`; контейнер
  `openroute-openroute-1` — `healthy`.
- Полный suite: **247 passed / 2 skipped**. Skipped — live smoke без
  `OPENROUTE_LIVE_PROXY`.
- Живая проверка sticky пройдена: повторные запросы одной `ses_...` сессии
  идут через один и тот же exit IP.
- Живое распределение не удалось продемонстрировать: в текущем пуле был всего
  один прокси, поэтому spread отсутствовал. Распределение покрыто тестами
  (30 сессий, multi-group сценарии §11.1); живой multi-proxy spread стоит
  повторить при расширении пула.

---

## 13. Открытые вопросы и принятые по умолчанию решения

### 13.1. Требуют подтверждения перед реализацией

1. **`S`: все привязанные сессии или только активные за час?**
   - По умолчанию в плане: **все привязанные**, включая завершённые.
   - Плюс: соответствует «сессия привязана навсегда», старые задачи не дают
     перекосить IP новыми.
   - Минус: завершённые задачи навсегда занимают слот; со временем
     распределение может стать менее точным.
   - Альтернатива: `S_g` считать только по сессиям с
     `last_used_at >= now - 3600`. Меняется только функция `S_g`; остальная
     схема без изменений.

2. **`R`: один клиентский запрос или каждая upstream-попытка?**
   - По умолчанию в плане: **один клиентский запрос** (одно событие, один
     `R`), токены суммируются по факту.
   - Альтернатива: событие на каждую попытку; тогда retry-шторм сильнее
     нагружает счётчик. Для прогноза нагрузки лучше клиентский запрос.

### 13.2. Принятые по умолчанию, можно изменить позже

- Временный fallback при cooldown родного URL не меняет привязку.
- `abusive` бан — 24 часа; эскалация до 7 суток опционально.
- `quota_until` / `banned_until` не persist-ятся; после рестарта группа
  паркуется повторно при первом 429.
- События и one-shot буферы не восстанавливаются из БД.
- IPv6 группируется по `/64`; сейчас пул IPv4.
- Завершённые сессии не удаляются автоматически.

---

## 14. Приложение: состояние на момент составления плана

- Последний локальный коммит:
  `f5553ea5e35b1f3065d833b44845eab81f384c97` —
  `fix(session): один upstream session id на диалог`.
- 185 тестов проходили; рабочее дерево было чистым.
- Git remote не настроен; коммит локальный.
- Запущенный контейнер `openroute-openroute-1` **не пересобран** после этого
  коммита: в образе ещё старый код session-id. После реализации плана
  обязательна пересборка `docker compose up -d --build openroute`.
- Известные забаненные exit IP (403 abusive traffic):
  - `217.29.62.211:10077`
  - `217.29.62.211:10078` (тот же IP, что выше)
  - `217.29.63.91:12459`
  - `208.86.27.41:9441`
- Последний известный рабочий: `200.0.60.34:9454`.
- Наблюдение: бан пришёл примерно после 97 запросов за 55 минут на один IP.
  Похоже на репутацию/объём на datacenter-IP. Балансировка снижает риск, но
  не гарантирует его: если upstream банит сам диапазон/ASN, спасёт только
  смена пула.

---

## 15. Definition of Done

- [x] DDL `session_affinity` создаётся идемпотентно.
- [x] Новая сессия привязывается по score S+R+T.
- [x] Повторная сессия идёт на тот же exit IP.
- [x] Окно 1 час работает для молодой и старой сессии.
- [x] `FreeUsageLimitError` паркует всю IP-группу до 00:00 UTC + 60 c.
- [x] Эвакуация идёт по одной с пересчётом score после каждой.
- [x] Другие 429 не паркуют IP и проходят клиенту.
- [x] `403 abusive` банит группу и эвакуирует сессии.
- [x] Удаление прокси → `NULL` → новая привязка.
- [x] `mark_success` / health probes не снимают quota/ban.
- [x] Тесты покрывают §11; существующие тесты зелёные.
- [x] `DESIGN.md` / `README.md` обновлены.
- [x] Контейнер пересобран, живые проверки §12.3 пройдены (sticky — да;
  живой multi-proxy spread не продемонстрирован: в пуле был один прокси,
  распределение покрыто тестами §11.1).
