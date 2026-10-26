# web-admin-ui

React + TypeScript SPA для админки OpenRoute. Отдельный контейнер, который
раздаёт только статику через nginx и **не получает никаких секретов**. Все
данные и мутации — через JSON API `web-admin` по `/api/admin/*`; edge
проксирует запросы браузера к обоим контейнерам.

- React 19, React Router, TanStack Query, Vite
- тёмный дизайн, страницы «Прокси», «Ключи» и «Статистика»
- SSE-обновления (`/api/admin/{proxies,keys}/events`) с fallback-поллингом
- вкладка «Статистика» (`/admin/ui/stats`): график токенов по UTC-дням за
  7/30/90 дней, таблица по прокси, обновление раз в 30 с (без SSE); на
  «Прокси» добавлены карточки за всё время и после лимита, а также столбцы
  «После лимита» и «Последний лимит».
- кнопка «Обновить список» на странице «Прокси»: принудительный запрос к px6
  со сбросом TTL-кэша через `POST /api/admin/proxies/refresh`
- HttpOnly cookie + CSRF-заголовок; токен CSRF приходит из
  `GET /api/admin/session`

## Разработка

```bash
npm ci
npm run dev        # http://127.0.0.1:5173, /api проксируется на :8000
```

Для локального запуска backend (`web-admin`) должен быть доступен на
`127.0.0.1:8000`. Vite-прокси настроен в `vite.config.ts`.

## Сборка и проверки

```bash
npm run typecheck
npm test
npm run build
```

`dist/` собирается в Dockerfile (multi-stage: Node → nginx). В production
контейнер слушает `8080`; снаружи доступен только через `edge`.
