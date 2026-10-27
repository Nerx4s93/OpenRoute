# web-admin

Backend админки OpenRoute (control plane). Отдаёт **только JSON/SSE** по
`/api/admin/*`; React-интерфейс живёт в отдельном контейнере
[`web-admin-ui/`](../web-admin-ui/README.md) и получает статику через `edge`.
Своими данными сервис не владеет: прокси, ключи и статистика читаются и
меняются через internal API шлюза (`/internal/*`, `X-Internal-Token`), а
аутентификация, сессии и CSRF — через приватный
[`auth-service/`](../auth-service/README.md).

> **Статус: реализовано.** Парольный вход, серверные сессии, CSRF, снапшоты
> прокси/ключей, мутации и SSE-обновления. Redis и `ADMIN_PASSWORD` этому
> сервису больше не передаются: их знает только auth-service.

## Маршруты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | liveness + число живых сессий из auth-service |
| GET | `/api/admin/session` | текущая сессия + CSRF-токен (200 и без входа) |
| POST | `/api/admin/login` | вход по паролю (проверяет auth-service); ставит HttpOnly cookie |
| POST | `/api/admin/logout` | выход (CSRF; сессия удаляется в auth-service/Redis) |
| GET | `/api/admin/proxies?period=` | снапшот пула: статусы, расходы, последние запросы |
| GET | `/api/admin/proxies/events?period=` | SSE с JSON-снапшотами прокси |
| POST | `/api/admin/proxies/set` | включить/отключить прокси по `proxy_id` (CSRF) |
| POST | `/api/admin/proxies/refresh` | принудительно обновить список из px6 со сбросом TTL-кэша (CSRF) |
| GET | `/api/admin/stats` (`days` / `period`) | токены по прокси и UTC-дням: 1..365 дней (по умолчанию 30) или окно `period` (сессия, no-store) |
| GET | `/api/admin/keys?period=` | ключи + статистика по `key_id` |
| GET | `/api/admin/keys/events?period=` | SSE с JSON-снапшотами ключей |
| POST | `/api/admin/keys/add` | создать ключ; секрет генерируется и возвращается один раз (CSRF) |
| POST | `/api/admin/keys/rotate` | перевыпустить секрет существующего ключа (CSRF) |
| POST | `/api/admin/keys/set` | включить/отозвать ключ (CSRF) |
| POST | `/api/admin/keys/delete` | удалить ключ (CSRF) |

`period` — `1d`, `3d`, `7d`, `30d`, `90d` или `all` (неизвестное значение →
`all`). Окна 1–90 дней скользящие по UTC: включают сегодня и предыдущие
N−1 дней, начиная с 00:00. Старые `day`, `week`, `month` сохранены как
календарные алиасы (`day` — с 00:00 сегодня, `week` — с понедельника,
`month` — с 1-го числа). Для `/api/admin/stats` `period` задаёт окно графика
на странице «Статистика»; `days` зажимается в `1..365`, по умолчанию 30.

### Формат данных

- Прокси: `{server_time, summary, totals, proxies[], recent[]}`. У каждой строки
  есть `display` (`host:port`), `usage`, состояние (`enabled`, `manual`,
  `banned`, таймеры) и `id` — HMAC от полного URL. `totals` —
  `{tokens_total}`. `summary` и `usage` строки не изменились. **Полный URL с
  логином и паролем никогда не попадает в браузер**; backend хранит его
  только в памяти процесса и резолвит `proxy_id` при мутации.
- Статистика: `{server_time, days, dates[], totals, proxies[], series_total,
  series_by_proxy}` — `server_time` в ISO-8601 UTC, `dates` — UTC-дни,
  серии выровнены по `dates`, только masked `host:port`.
- Ключи: `{server_time, keys[], stats[]}`. `keys` — активные записи
  (`key_hash`, `key_id`, лимиты, модели), `stats` — usage из журнала запросов,
  включая `state: "deleted"` для удалённых ключей. Ответы `add`/`rotate`
  дополнительно содержат `key` — сгенерированный `openroute-...` секрет; он
  показывается один раз и в снапшоты/БД не попадает.
- Ошибки — `{"detail": "..."}`; ошибки internal API шлюза разворачиваются из
  OpenAI-конверта в понятный `detail`.
- SSE отправляет `data: <json-снапшот>` и heartbeat-комментарии; при обрыве
  фронтенд переходит на polling.

### Безопасность

- Cookie `openroute_session`: `HttpOnly`, `SameSite=Strict`, `Secure` при
  HTTPS (по `X-Forwarded-Proto` от edge), `Path=/`. Cookie содержит только
  непрозрачный `sid`, без подписанных claims.
- Сессии и CSRF-токены живут в auth-service (Redis-хранилище), пароль — в его
  конфигурации. `web-admin` не получает `REDIS_URL`, не импортирует
  Redis-клиент и не хранит `ADMIN_PASSWORD`.
- Каждая мутация требует `X-CSRF-Token` из `/api/admin/session`; проверку
  выполняет auth-service в том же вызове, что и проверку сессии.
- Логин ограничен 10 неудачными попытками в минуту на IP
  (`X-Real-IP` от edge), с `Retry-After`; счётчик живёт в auth-service.
- `proxy_id` — HMAC-SHA256 от полного URL с `INTERNAL_API_TOKEN`; перебор
  идентификаторов не раскрывает креды.
- Панель выключена (404), если auth-service сообщает, что `ADMIN_PASSWORD`
  пуст. При недоступном auth-service защищённые маршруты и `/health`
  отвечают 503, а не работают без аутентификации.

## Конфигурация

См. `.env.example`:

| Переменная | Назначение |
|------------|------------|
| `AUTH_SERVICE_URL` | база auth-service (`http://openroute-auth:8000`, сеть `openroute-auth-net`) |
| `AUTH_TIMEOUT_SECONDS` | таймаут запросов к auth-service (5 с) |
| `GATEWAY_INTERNAL_URL` | `http://gateway:8000` в сети `openroute-net` |
| `INTERNAL_API_TOKEN` | тот же секрет, что у шлюза |
| `INTERNAL_TIMEOUT_SECONDS` | таймаут internal API (30 с) |

`ADMIN_PASSWORD`, `ADMIN_SESSION_TTL_SECONDS` и `REDIS_URL` задаются только в
`auth-service/.env`.

## Локальный запуск

Сначала запустите auth-service (ему нужен Redis), затем web-admin:

```bash
cd auth-service
pip install -r requirements.txt
REDIS_URL=redis://127.0.0.1:6379/0 \
  uvicorn app.main:app --host 127.0.0.1 --port 8011

cd ../web-admin
pip install -r requirements.txt
AUTH_SERVICE_URL=http://127.0.0.1:8011 \
  uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Для фронтенда в соседнем терминале:

```bash
cd web-admin-ui
npm ci
npm run dev    # http://127.0.0.1:5173, /api проксируется на :8000
```

## Тесты

```bash
pip install -r ../requirements-dev.txt
pytest ../tests/test_admin_api.py ../tests/test_auth_client.py -q
```

Тесты герметичны: поднимают in-process fake gateway и fake auth-service, не
требуют Redis/PostgreSQL/сети. Сам auth-service проверяется отдельно:
`pytest ../tests/test_auth_service.py -q`.
