### [[-----EN-----]](../README.md) [-----RU-----]

# Установка и первый запуск OpenRoute

OpenRoute — шлюз, предназначенный исключительно для агентской системы
OpenCode. В других агентских системах и клиентах он работать не будет.

Шлюз принимает запросы по `/v1/*`, распределяет их через пул прокси и
работает с free-tier OpenCode. Прокси, клиентские ключи и статистика
управляются через веб-панель.

## Требования

Для работы OpenCode скачайте и установите [Node.js LTS](https://nodejs.org/),
затем установите OpenCode CLI:

```bash
npm install -g @opencode/cli
```

Также понадобятся:

- Git.
- Docker с плагином Compose v2. На Windows и macOS удобнее использовать
  Docker Desktop, на Linux — Docker Engine и `docker compose`.
- Свободный порт `53202` (по умолчанию) или возможность изменить его в
  конфигурации.
- Исходящий доступ в интернет: Docker Hub при первой сборке, а также
  `px6.net` и `opencode.ai` для работы шлюза.
- Аккаунт [px6.net](https://px6.net/e/807232) с положительным балансом и API-ключом.

Убедитесь, что Docker запущен и команда `docker compose version` работает.

## 1. Подготовка px6.net

1. Зарегистрируйтесь на [px6.net](https://px6.net/e/807232).
2. Пополните баланс. Для запуска хватит 100₽.
3. Откройте раздел [«Разработчикам» (API)](https://px6.net/ru/user/developers)
   и скопируйте API-ключ.

## 2. Клонирование репозитория

```bash
git clone https://github.com/Nerx4s93/OpenRoute.git
cd OpenRoute
```

Все дальнейшие команды выполняются из корня проекта.

## 3. Настройка

Скопируйте примеры конфигурации. Команды ниже подходят для PowerShell,
Git Bash и терминала Linux/macOS.

```bash
cp .env.example .env
cp storage-service/.env.example storage-service/.env
cp auth-service/.env.example auth-service/.env
cp web-admin/.env.example web-admin/.env
cp px6-service/.env.example px6-service/.env
```

### 3.1. Внутренний токен

Сгенерируйте длинную случайную строку.

Linux/macOS:

```bash
openssl rand -hex 32
```

Windows PowerShell:

```powershell
[guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
```

Укажите одно и то же значение в двух файлах:

```dotenv
# .env
INTERNAL_API_TOKEN=полученный-токен
```

```dotenv
# web-admin/.env
INTERNAL_API_TOKEN=полученный-токен
```

Если токен пустой, internal API отключён, и веб-панель не сможет получать
данные от шлюза.

### 3.2. Пароль администратора

В файле `auth-service/.env` задайте пароль для входа в веб-панель:

```dotenv
ADMIN_PASSWORD=надёжный-пароль
ADMIN_SESSION_TTL_SECONDS=43200
REDIS_URL=redis://redis:6379/0
```

`ADMIN_SESSION_TTL_SECONDS` — время жизни сессии в секундах (по умолчанию
12 часов).
Пустой `ADMIN_PASSWORD` отключает вход в панель.

### 3.3. API-ключ px6.net

В файле `px6-service/.env` укажите ключ, полученный в px6.net:

```dotenv
PX6_API_KEY=ваш-api-ключ
PX6_API_BASE=https://px6.link/api
CACHE_HOURS=8
```

Менять нужно только `PX6_API_KEY`; остальные значения можно оставить по
умолчанию.

### 3.4. Порт и остальные настройки

В корневом `.env` при необходимости измените порт:

```dotenv
OPENROUTE_EDGE_PORT=53202
```

Если порт `53202` уже занят, укажите другой свободный порт. Остальные
параметры `.env.example` можно оставить по умолчанию.

Файл `storage-service/.env` для запуска через Docker менять не нужно.

## 4. Запуск

```bash
docker compose up -d --build
```

```text
http://localhost:53202/
```

Если вы изменили `OPENROUTE_EDGE_PORT`, используйте свой порт. При установке
на удалённый сервер замените `localhost` на IP-адрес или домен. Проверить
доступность шлюза можно по адресу `http://localhost:53202/health`.

## 5. Первичная настройка

1. Войдите в веб-панель с паролем из `ADMIN_PASSWORD`.
2. Откройте раздел «Прокси». Если список пуст, нажмите «Обновить список»
   или «Купить прокси».
3. При покупке доступны следующие варианты:
   - **IPv6** — 3, 7, 14, 30, 60 или 90 дней; покупка на 3 дня доступна
     только для IPv6;
   - **IPv4** и **общий IPv4** — 7, 14, 30, 60 или 90 дней.
4. После покупки новые прокси добавляются **включёнными** и сразу участвуют
   в ротации. При необходимости их можно выключить переключателем в таблице;
   флаги существующих прокси сохраняются.
5. Откройте раздел «Ключи» и создайте клиентский ключ. Секрет показывается только один раз —
   сохраните его. При необходимости задайте RPM/TPM (лимиты запросов и
   токенов в минуту) и список разрешённых моделей.

Рекомендовано покупать IPv6. Квота учитывается отдельно для каждого
прокси-URL. По наблюдениям, один выходной IP даёт порядка 90 млн токенов в
сутки, но точное значение зависит от OpenCode. Несколько прокси увеличивают
суммарный доступный лимит, а шлюз распределяет запросы автоматически.

### 5.1. Подключение провайдера в OpenCode

Откройте файл `~\.config\opencode\opencode.json` (на Windows это
`C:\Users\<имя>\.config\opencode\opencode.json`, на Linux/macOS —
`~/.config/opencode/opencode.json`). Если файла нет, создайте его.

Добавьте провайдера в объект `providers`, не удаляя остальные настройки:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "OpenRoute": {
      "name": "OpenRoute",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:53202/v1",
        "apiKey": "openroute-..."
      },
      "models": {
        "deepseek-v4.1-flash-free": {
          "name": "DeepSeek V4.1 Flash",
          "variants": [
            { "id": "low", "settings": { "reasoningEffort": "low" } },
            { "id": "high", "settings": { "reasoningEffort": "high" } },
            { "id": "max", "settings": { "reasoningEffort": "max" } }
          ]
        }
      }
    }
  }
}
```

Вместо `openroute-...` укажите клиентский ключ, созданный в разделе «Ключи».
Если у ключа задан список разрешённых моделей, добавьте в него
`deepseek-v4.1-flash-free`. Если вы меняли `OPENROUTE_EDGE_PORT`, замените
порт в `baseURL`. При запуске OpenCode на другом хосте замените `127.0.0.1`
на адрес сервера.

После сохранения перезапустите OpenCode и выберите модель
`OpenRoute/deepseek-v4.1-flash-free`.

## 6. Обслуживание

Обновление после изменений в репозитории:

```bash
git pull
docker compose up -d --build
```

Остановка с сохранением данных:

```bash
docker compose down
```

Полное удаление контейнеров и данных PostgreSQL/Redis:

```bash
docker compose down -v
```

Данные хранятся в томах `pgdata` и `redisdata` (в Docker они называются
`openroute_pgdata` и `openroute_redisdata`). Команда `down -v` удаляет их без
возможности восстановления.

После изменения `.env` перезапустите сервисы:

```bash
docker compose up -d
```

Если настройки не применились, пересоздайте контейнеры:

```bash
docker compose up -d --force-recreate
```
