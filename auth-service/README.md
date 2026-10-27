# auth-service

Приватный auth/session-сервис админки OpenRoute. Единственный компонент,
который знает `ADMIN_PASSWORD`, проверяет пароль, ведёт серверные сессии в
Redis, проверяет CSRF и ограничивает перебор пароля. `web-admin` вызывает его
по HTTP и сам отвечает браузеру; браузер и `edge` до этого сервиса не
дотягиваются.

> **Статус: реализовано.** Парольный вход, Redis-сессии, CSRF, throttling и
> `/health`; сервис не публикует хост-порт и не маршрутизируется через edge.

## Границы безопасности

- `auth-service` подключён только к двум private-сетям:
  `openroute-auth-net` (с `web-admin`) и `openroute-redis-net` (с Redis).
- `web-admin` не получает `REDIS_URL`, не импортирует Redis-клиент и не
  содержит пароль/сессии. `ADMIN_PASSWORD` и `ADMIN_SESSION_TTL_SECONDS`
  задаются только в `auth-service/.env`.
- `edge` не маршрутизирует `/auth/*` и не подключён к `openroute-auth-net`;
  наружу сервис недоступен. Собственной аутентификации у этого приватного API
  нет: единственный клиент в его сети — `web-admin`.
- `auth-service` не проксирует данные админки и не ходит в gateway. Его
  задача — только аутентификация и сессии.
- При недоступном Redis сервис отвечает 503 (`Хранилище сессий недоступно`),
  а не пропускает запрос без проверки сессии.

## API

Все методы работают по HTTP внутри `openroute-auth-net`; публичного
`/docs`/OpenAPI нет.

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | служебный JSON (`{"service":"openroute-auth"}`) |
| GET | `/health` | liveness + `enabled` + число сессий в Redis |
| GET | `/auth/status` | `{"enabled": bool}`; не трогает Redis |
| POST | `/auth/login` | `{password, client_ip}` → `{sid, csrf, expires_at, max_age}`; 404/401/429/503 |
| POST | `/auth/validate` | `{sid, csrf, require_csrf}` → сессия; 404/401/403/503 |
| POST | `/auth/logout` | `{sid, csrf}` → 204; 404/401/403/503 |

Коды ошибок:

- `404` — `ADMIN_PASSWORD` пуст (админка выключена);
- `401` — неверный пароль или отсутствующая/истёкшая сессия;
- `403` — неверный CSRF-токен;
- `429` — больше 10 неудачных попыток входа за 60 секунд с одного IP,
  с `Retry-After`;
- `503` — недоступен Redis (сессию нельзя создать или проверить).

## Сессии и CSRF

- Ключ Redis: `openroute:session:<sid>`; значение —
  `{"csrf": "...", "expires": <unix>}`. TTL ключа равен
  `ADMIN_SESSION_TTL_SECONDS`, поле `expires` дублирует его для проверки.
  Формат совместим с сессиями прежнего `web-admin`, поэтому существующие
  сессии в томе `redisdata` не теряются.
- `sid` и `csrf` генерируются криптографически стойко
  (`secrets.token_urlsafe`). `web-admin` кладёт только `sid` в HttpOnly
  cookie; CSRF-токен возвращается браузеру через `/api/admin/session`.
- Проверка CSRF — constant-time (`hmac.compare_digest`), выполняется в
  `/auth/validate` при `require_csrf=true` и в `/auth/logout`.
- Throttling — in-memory по `client_ip`, который `web-admin` берёт из
  доверенного `X-Real-IP` edge. Счётчик сбрасывается при успешном входе и
  после окна в 60 секунд; перезапуск сервиса его очищает.

## Конфигурация

См. `.env.example`:

| Переменная | Назначение |
|------------|------------|
| `ADMIN_PASSWORD` | пароль входа; пусто — админка выключена (404) |
| `ADMIN_SESSION_TTL_SECONDS` | TTL сессии, секунды (по умолчанию 43200 = 12 ч) |
| `REDIS_URL` | Redis-хранилище сессий (`redis://redis:6379/0`) |

## Локальный запуск

Нужен доступный Redis (локально — например,
`docker run -p 127.0.0.1:6379:6379 redis:7-alpine`):

```bash
cd auth-service
cp .env.example .env              # ADMIN_PASSWORD, ADMIN_SESSION_TTL_SECONDS
pip install -r requirements.txt
REDIS_URL=redis://127.0.0.1:6379/0 \
  uvicorn app.main:app --host 127.0.0.1 --port 8011 --no-access-log
```

`web-admin` запускается с `AUTH_SERVICE_URL=http://127.0.0.1:8011` (см.
[`web-admin/README.md`](../web-admin/README.md)). В Docker порт `8000` не
публикуется на хост; он доступен только приватным сетям auth-контура
(`openroute-auth-net` для web-admin и `openroute-redis-net` для Redis).

## Тесты

```bash
pip install -r ../requirements-dev.txt
pytest ../tests/test_auth_service.py -q
```

Тесты герметичны: реальное приложение auth-service запускается через
`TestClient` с in-memory хранилищем сессий; Redis, PostgreSQL и сеть не нужны.
Клиент `web-admin` проверяется отдельно в
`pytest ../tests/test_auth_client.py -q`.
