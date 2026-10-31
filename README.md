### [-----EN-----] [[-----RU-----]](docs/README_RU.md)

# Installing and Running OpenRoute for the First Time

OpenRoute is a gateway intended exclusively for the OpenCode agent system. It
will not work with other agent systems or clients.

The gateway accepts requests at `/v1/*`, distributes them across a proxy pool,
and works with the OpenCode free tier. Proxies, client keys, and statistics are
managed through the web panel.

## Requirements

To use OpenCode, download and install [Node.js LTS](https://nodejs.org/), then
install the OpenCode CLI:

```bash
npm install -g @opencode/cli
```

You will also need:

- Git.
- Docker with the Compose v2 plugin. Docker Desktop is the easiest option on
  Windows and macOS; on Linux, use Docker Engine and `docker compose`.
- A free port `53202` (the default) or the ability to change it in the
  configuration.
- Outbound internet access: Docker Hub for the first build, plus `px6.net` and
  `opencode.ai` for the gateway to operate.
- A [px6.net](https://px6.net/e/807232) account with a positive balance and an
  API key.

Make sure Docker is running and `docker compose version` works.

## 1. Setting Up px6.net

1. Register at [px6.net](https://px6.net/e/807232).
2. Top up your balance. 100 RUB is enough to get started.
3. Open the [Developers (API)](https://px6.net/ru/user/developers) section and
   copy your API key.

## 2. Cloning the Repository

```bash
git clone https://github.com/Nerx4s93/OpenRoute.git
cd OpenRoute
```

All subsequent commands are run from the project root.

## 3. Configuration

Copy the configuration examples. The commands below work in PowerShell, Git
Bash, and Linux/macOS terminals.

```bash
cp .env.example .env
cp storage-service/.env.example storage-service/.env
cp auth-service/.env.example auth-service/.env
cp web-admin/.env.example web-admin/.env
cp px6-service/.env.example px6-service/.env
```

### 3.1. Internal Token

Generate a long random string.

Linux/macOS:

```bash
openssl rand -hex 32
```

Windows PowerShell:

```powershell
[guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
```

Use the same value in both files:

```dotenv
# .env
INTERNAL_API_TOKEN=your-generated-token
```

```dotenv
# web-admin/.env
INTERNAL_API_TOKEN=your-generated-token
```

If the token is empty, the internal API is disabled and the web panel will not
be able to receive data from the gateway.

### 3.2. Administrator Password

In `auth-service/.env`, set the password for signing in to the web panel:

```dotenv
ADMIN_PASSWORD=strong-password
ADMIN_SESSION_TTL_SECONDS=43200
REDIS_URL=redis://redis:6379/0
```

`ADMIN_SESSION_TTL_SECONDS` is the session lifetime in seconds (12 hours by
default). An empty `ADMIN_PASSWORD` disables panel login.

### 3.3. px6.net API Key

In `px6-service/.env`, enter the key you received from px6.net:

```dotenv
PX6_API_KEY=your-api-key
PX6_API_BASE=https://px6.link/api
CACHE_HOURS=8
```

Only `PX6_API_KEY` needs to be changed; the other values can be left at their
defaults.

### 3.4. Port and Other Settings

In the root `.env`, change the port if necessary:

```dotenv
OPENROUTE_EDGE_PORT=53202
```

If port `53202` is already in use, choose another free port. The other
`.env.example` parameters can be left at their defaults.

You do not need to change `storage-service/.env` when running with Docker.

## 4. Launch

```bash
docker compose up -d --build
```

```text
http://localhost:53202/
```

If you changed `OPENROUTE_EDGE_PORT`, use your port. When installing on a remote
server, replace `localhost` with the IP address or domain. You can check
gateway availability at `http://localhost:53202/health`.

## 5. Initial Setup

1. Sign in to the web panel with the password from `ADMIN_PASSWORD`.
2. Open the **Proxies** section. If the list is empty, click **Refresh list**
   or **Buy proxies**.
3. The following purchase options are available:
   - **IPv6** — 3, 7, 14, 30, 60, or 90 days; the 3-day option is available
     only for IPv6.
   - **IPv4** and **Shared IPv4** — 7, 14, 30, 60, or 90 days.
4. Newly purchased proxies are added **enabled** and immediately join the
   rotation. You can disable any of them with the toggle in the table; the
   flags of existing proxies are preserved.
5. Open the **Keys** section and create a client key. The secret is shown only
   once; save it. If needed, set RPM/TPM (requests per minute and tokens per
   minute limits) and the allowed models list.

IPv6 is recommended. Quota is tracked separately for each proxy URL. Based on
observations, one egress IP provides around 10 million tokens per day, but the
exact value depends on OpenCode. Multiple proxies increase the total available
limit, and the gateway distributes requests automatically.

### 5.1. Connecting the Provider in OpenCode

Open `~\.config\opencode\opencode.json` (on Windows, this is
`C:\Users\<name>\.config\opencode\opencode.json`; on Linux/macOS,
`~/.config/opencode/opencode.json`). If the file does not exist, create it.

Add the provider to the `providers` object without removing other settings:

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

Replace `openroute-...` with the client key created in the **Keys** section. If
the key has an allowed-models list, add `deepseek-v4.1-flash-free` to it. If you
changed `OPENROUTE_EDGE_PORT`, update the port in `baseURL`. If OpenCode runs on
a different host, replace `127.0.0.1` with the server address.

After saving, restart OpenCode and select the
`OpenRoute/deepseek-v4.1-flash-free` model.

## 6. Maintenance

Update after repository changes:

```bash
git pull
docker compose up -d --build
```

Stop the services while preserving data:

```bash
docker compose down
```

Remove the containers and PostgreSQL/Redis data completely:

```bash
docker compose down -v
```

Data is stored in the `pgdata` and `redisdata` volumes (in Docker they are named
`openroute_pgdata` and `openroute_redisdata`). The `down -v` command deletes
them permanently.

After changing `.env`, restart the services:

```bash
docker compose up -d
```

If the settings did not apply, recreate the containers:

```bash
docker compose up -d --force-recreate
```
