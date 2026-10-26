# web-admin

Backend админки OpenRoute (control plane). Отдаёт **только JSON/SSE** по
`/api/admin/*`; React-интерфейс живёт в отдельном контейнере
[`web-admin-ui/`](../web-admin-ui/README.md) и получает статику через `edge`.
Своими данными сервис не владеет: прокси, ключи и статистика читаются и
меняются через internal API шлюза (`/internal/*`, `X-Internal-Token`).

> **Статус: реализовано.** Парольный вход, серверные сессии в Redis, CSRF,
> снапшоты прокси/ключей, мутации и SSE-обновления.

## Маршруты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | служебный JSON (`{"service":"openroute-web-admin"}`) |
| GET | `/health` | liveness + число живых сессий в Redis |
| GET | `/api/admin/session` | текущая сессия + CSRF-токен (200 и без входа) |
| POST | `/api/admin/login` | вход по `ADMIN_PASSWORD`; ставит HttpOnly cookie |
| POST | `/api/admin/logout` | выход (CSRF; сессия удаляется на сервере) |
| GET | `/api/admin/proxies?period=` | снапшот пула: статусы, расходы, последние запросы |
| GET | `/api/admin/proxies/events?period=` | SSE с JSON-снапшотами прокси |
| POST | `/api/admin/proxies/set` | включить/отключить прокси по `proxy_id` (CSRF) |
| POST | `/api/admin/proxies/refresh` | принудительно обновить список из px6 со сбросом TTL-кэша (CSRF) |
| GET | `/api/admin/stats?days=` | токены по прокси и UTC-дням за 1..365 дней (по умолчанию 30; сессия, no-store) |
| GET | `/api/admin/keys?period=` | ключи + статистика по `key_id` |
| GET | `/api/admin/keys/events?period=` | SSE с JSON-снапшотами ключей |
| POST | `/api/admin/keys/add` | добавить ключ (CSRF) |
| POST | `/api/admin/keys/set` | включить/отозвать ключ (CSRF) |
| POST | `/api/admin/keys/delete` | удалить ключ (CSRF) |

`period` — `day`, `week`, `month` или `all` (неизвестное значение → `all`).
Границы календарные по UTC: `day` — с 00:00 текущего дня, `week` — с
понедельника 00:00 текущей недели, `month` — с 1-го числа 00:00 текущего месяца.
`days` зажимается в `1..365`, по умолчанию 30.

### Формат данных

- Прокси: `{server_time, summary, totals, proxies[], recent[]}`. У каждой строки
  есть `display` (`host:port`), `usage`, состояние (`enabled`, `manual`,
  `disabled`, `banned`, таймеры) и `id` — HMAC от полного URL. `totals` —
  `{tokens_total}`. `summary` и `usage` строки не изменились. **Полный URL с
  логином и паролем никогда не попадает в браузер**; backend хранит его
  только в памяти процесса и резолвит `proxy_id` при мутации.
- Статистика: `{server_time, days, dates[], totals, proxies[], series_total,
  series_by_proxy}` — `server_time` в ISO-8601 UTC, `dates` — UTC-дни,
  серии выровнены по `dates`, только masked `host:port`.
- Ключи: `{server_time, keys[], stats[]}`. `keys` — активные записи
  (`key_hash`, `key_id`, лимиты, модели), `stats` — usage из журнала запросов,
  включая `state: "deleted"` для удалённых ключей.
- Ошибки — `{"detail": "..."}`; ошибки internal API шлюза разворачиваются из
  OpenAI-конверта в понятный `detail`.
- SSE отправляет `data: <json-снапшот>` и heartbeat-комментарии; при обрыве
  фронтенд переходит на polling.

### Безопасность

- Cookie `openroute_session`: `HttpOnly`, `SameSite=Strict`, `Secure` при
  HTTPS (по `X-Forwarded-Proto` от edge), `Path=/`.
- Сессии — только в Redis; cookie не содержит подписанных claims.
- Каждая мутация требует `X-CSRF-Token` из `/api/admin/session`.
- Логин ограничен 10 неудачными попытками в минуту на IP
  (`X-Real-IP` от edge), с `Retry-After`.
- `proxy_id` — HMAC-SHA256 от полного URL с `INTERNAL_API_TOKEN`; перебор
  идентификаторов не раскрывает креды.
- Панель выключена (404), если `ADMIN_PASSWORD` пуст.

## Конфигурация

См. `.env.example`:

| Переменная | Назначение |
|------------|------------|
| `ADMIN_PASSWORD` | пароль входа; пусто — админка выключена |
| `ADMIN_SESSION_TTL_SECONDS` | TTL сессии (по умолчанию 43200) |
| `REDIS_URL` | хранилище сессий (`redis://redis:6379/0`) |
| `GATEWAY_INTERNAL_URL` | `http://gateway:8000` в docker-сети |
| `INTERNAL_API_TOKEN` | тот же секрет, что у шлюза |
| `INTERNAL_TIMEOUT_SECONDS` | таймаут internal API (30 с) |

## Локальный запуск

```bash
cd web-admin
pip install -r requirements.txt
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
pytest ../tests/test_admin_api.py -q
```

Тесты герметичны: поднимают in-process fake gateway и in-memory session store,
не требуют Redis/PostgreSQL/сети.
