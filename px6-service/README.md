# px6-service

Микросервис-обёртка над API [px6.net](https://px6.net). Это **единственный**
компонент OpenRoute, который знает `PX6_API_KEY`. Шлюз читает список по
`PROXIES_URL=http://openroute-px6:8000/proxies` и ходит сюда же за балансом,
каталогом и покупкой (`PX6_SERVICE_URL`).

Сервис **не фильтрует прокси по комментарию**: `/proxies` возвращает все
активные прокси. Новые прокси в gateway добавляются выключенными, и только
оператор решает, какие из них могут нести трафик.

## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/proxies` | `{"count": N, "proxies": [...]}`; `?refresh=true` — мимо TTL-кэша |
| GET | `/account` | `{"user_id", "balance", "currency"}` — баланс аккаунта |
| GET | `/countries?version=` | `{"version", "countries": ["ru", ...]}`; версии `3`, `4`, `6` |
| GET | `/price?version=&count=&period=` | `{"price", "price_single", "balance", "currency", ...}` |
| POST | `/buy` | Покупка; **учётные данные купленных прокси не возвращаются** |
| GET | `/health` | `{"status": "ok", "cached": bool}` |

Элемент списка (`ProxyItem`) содержит
`id, version, ip, host, port, user, password, type, country, date, date_end,
unixtime, unixtime_end, descr, active`. Креды нужны только серверной части
(gateway); в браузер они не попадают.

### Покупка

Тело `POST /buy`:

```json
{
  "count": 1,
  "period": 30,
  "country": "ru",
  "version": 6,
  "descr": "необязательный комментарий",
  "auto_prolong": false
}
```

Допустимые версии — `3` (IPv4 Shared), `4` (IPv4), `6` (IPv6); период — целое
число дней (UI предлагает `3/7/14/30/60/90`). Ответ содержит `order_id`,
`count`, `price`, `price_single`, `period`, `country`, `version`, `currency`,
`balance`, но **не содержит** список прокси с паролями. После успешной покупки
кэш `/proxies` инвалидируется, поэтому gateway сразу видит новые записи.

Ошибки px6.net отдаются как HTTP-ошибки с безопасным текстом (без API-ключа и
сырого ответа апстрима). Коды: `400` — некорректные параметры, `402` —
недостаточно средств, `404` — прокси/заказ не найден, `409` — нет свободных
прокси, `429` — лимит px6.net (не более 3 запросов/с), `502` — неверный
API-ключ, IP-ограничение или сбой px6.net.

## Конфигурация (`app/config.py` / `.env`)

```
PX6_API_KEY=
PX6_API_BASE=https://px6.link/api
CACHE_HOURS=8
```

## Запуск (в составе стека)

```bash
docker compose up --build -d px6
```

Хостовый порт **не публикуется**: `/proxies` отдаёт креды прокси в открытом
виде, а `/buy` тратит деньги. Диагностика из docker-сети:

```bash
docker run --rm --network openroute-px6-net curlimages/curl http://openroute-px6:8000/health
```

## Тесты

```bash
pytest tests/test_px6_service.py tests/test_proxy6_client.py
```

Тесты герметичны: px6.net подменяется in-memory fake, реальные запросы и
API-ключ не нужны.
