# web-admin

Веб-админка OpenRoute (control plane) как отдельный микросервис. Вход — только
по паролю (`ADMIN_PASSWORD`), серверные сессии в Redis, CSRF, страницы
«Прокси» и «Ключи». **Своими данными сервис не владеет**: он ходит в internal
API шлюза (`/internal/*`) с общим секретом `X-Internal-Token`.

> **Статус: реализовано.** Сессии, `/login`, CSRF, проксирование мутаций в
> internal API и рендеринг страниц (`app/adminui.py`).

## Маршруты

| Метод | Путь                    | Описание                                          |
|-------|-------------------------|---------------------------------------------------|
| GET   | `/`                     | редирект на `/login`                              |
| GET/POST | `/login`             | вход по паролю `ADMIN_PASSWORD`                   |
| POST  | `/admin/ui/logout`      | выход (POST + CSRF, сессия удаляется на сервере)  |
| GET   | `/admin/ui`             | страница «Прокси»                                 |
| GET   | `/admin/ui/keys`        | страница «Ключи»                                  |
| GET   | `/admin/ui/data`        | HTML-фрагменты «Прокси» для автообновления        |
| GET   | `/admin/ui/keys/data`   | HTML-фрагменты «Ключи» для автообновления         |
| POST  | `/admin/ui/proxies/{set,delete}` | мутации пула → `/internal/proxies/*`  |
| POST  | `/admin/ui/keys/{add,set,delete}`    | мутации ключей → `/internal/keys/*`   |
| GET   | `/health`               | liveness + число живых сессий в Redis             |

## Конфигурация

См. `.env.example`: `ADMIN_PASSWORD`, `ADMIN_SESSION_TTL_SECONDS`,
`REDIS_URL`, `GATEWAY_INTERNAL_URL` (в docker-сети `http://gateway:8000`),
`INTERNAL_API_TOKEN` (тот же, что у шлюза), `INTERNAL_TIMEOUT_SECONDS`.
