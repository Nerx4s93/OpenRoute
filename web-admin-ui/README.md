# web-admin-ui

React + TypeScript SPA для админки OpenRoute. Отдельный контейнер, который
раздаёт только статику через nginx и **не получает никаких секретов**. Все
данные и мутации — через JSON API `web-admin` по `/api/admin/*`; edge
проксирует запросы браузера к обоим контейнерам.

- React 19, React Router, TanStack Query, Vite
- тёмный дизайн, страницы «Статистика» и «Ключи»
- SSE-обновления (`/api/admin/{proxies,keys}/events`) с fallback-поллингом
- страница «Статистика» (`/admin/ui`): карточки, график «Токены по дням»
  (периоды **1д / 3д / 7д / 30д / 90д / Всё время**), таблица «Прокси»
  (за всё время) и последние запросы. Старый URL `/admin/ui/stats`
  перенаправляется сюда.
- кнопка «Обновить список» на странице «Статистика»: принудительный запрос
  к px6 со сбросом TTL-кэша через `POST /api/admin/proxies/refresh`
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
