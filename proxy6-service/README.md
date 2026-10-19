# proxy6-service

Микросервис-обёртка над API [px6.net](https://px6.net): отдаёт только те прокси,
у которых поле-комментарий `descr` равно **`OPENROUTE`** (аналог `USE_IN_API` в
NVRoute). Шлюз OpenRoute читает список по
`PROXIES_URL=http://openroute-proxy6:8000/proxies`.

## Эндпоинты

| Метод | Путь       | Описание                                                   |
|-------|------------|------------------------------------------------------------|
| GET   | `/proxies` | `{"count": N, "proxies": [...]}`; `?refresh=true` — мимо кэша |
| GET   | `/health`  | `{"status": "ok", "cached": bool}`                          |

Ответ кэшируется на `CACHE_HOURS` часов. Элемент списка (`ProxyItem`) содержит
`id, version, ip, host, port, user, password, type, country, date, date_end,
unixtime, unixtime_end, descr, active` — как в NVRoute.

## Конфигурация (`app/config.py` / `.env`)

```
PX6_API_KEY=
PX6_API_BASE=https://px6.link/api
CACHE_HOURS=8
```

## Запуск (в составе стека)

```bash
docker compose up --build -d proxy6
```

Хостовый порт **не публикуется**: `/proxies` отдаёт креды прокси в открытом
виде, наружу ему торчать незачем. Диагностика из docker-сети:

```bash
docker run --rm --network openroute-net curlimages/curl http://openroute-proxy6:8000/proxies
```
