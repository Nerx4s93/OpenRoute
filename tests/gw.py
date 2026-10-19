"""Shared helpers for OpenRoute hermetic tests.

No real network: upstream clients go to a ``ScriptedTransport`` and the
background proxy fetch is replaced with a quiet no-op. All data lives in an
isolated PostgreSQL database created by the ``pg_dsn`` fixture (conftest.py):
tables ``request_log``/``proxies``/``keys``, as after a "first run" — the pool
upserted exactly like ``refresh_proxies`` does it, client keys as if added via
the admin UI. The project directory is never used for writes.

Edge routing is modelled with ``_EdgeShim``: like nginx in production, it
forwards ``/v1/*``, ``/admin/*``, ``/health``, ``/docs`` to the gateway and
hides ``/internal/*`` (404). The ``internal`` client exposes ``/internal/*``
for tests of that surface.
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import app.main as main

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.affinity import AffinityManager  # noqa: E402
from app.affinitydb import AffinityDB  # noqa: E402
from app.config import Settings  # noqa: E402
from app.keydb import KeyDB  # noqa: E402
from app.keys import KeyStore, hash_key  # noqa: E402
from app.proxies import ProxyManager  # noqa: E402
from app.proxydb import ProxyDB  # noqa: E402
from app.reqlog import RequestLog  # noqa: E402


def build_settings(pg_dsn, **over):
    """Settings with every side effect pointed at an isolated DB.

    Explicit kwargs override env/.env, so tests do not depend on its contents.
    """
    s = dict(
        internal_api_token="test-internal-token",
        postgres_dsn=pg_dsn,
        proxies_url="http://127.0.0.1:1/proxies",  # closed port (refresh is a no-op)
        opencode_base_url="http://upstream.invalid/v1",
        opencode_models_url="http://upstream.invalid/models",
        opencode_model="deepseek-v4.1-flash-free",
        alert_webhook_url="",
        proxy_refresh_seconds=3600.0,
        proxy_retry_seconds=3600.0,
    )
    s.update(over)
    return Settings(**s)


class ScriptedTransport(httpx.AsyncBaseTransport):
    """Transport "scriptwriter": the upstream response is built by router(request)."""

    def __init__(self, router):
        self.router = router
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.router(request)


class TrackingStream(httpx.AsyncByteStream):
    """Stream with telemetry: how many chunks were served, failed, closed."""

    def __init__(self, chunks, fail_after=None):
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.yielded = 0
        self.closed = False
        self.failed = False

    async def __aiter__(self):
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i >= self.fail_after:
                self.failed = True
                raise httpx.ReadError(f"proxy reset after {self.yielded} chunks")
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def _quiet_refresh_loop():
    """Replacement for main.refresh_loop: real proxy fetching is disabled."""
    while True:
        await asyncio.sleep(3600)


class _AsyncioShim:
    """main.asyncio with a patched sleep: retry delays are RECORDED, not waited."""

    create_task = staticmethod(asyncio.create_task)
    CancelledError = asyncio.CancelledError
    to_thread = staticmethod(asyncio.to_thread)  # admin: SQL writes via ProxyDB/KeyDB

    def __init__(self, log):
        self._log = log

    async def sleep(self, seconds):
        self._log.append(seconds)
        await asyncio.sleep(0)


_GATEWAY_PREFIXES = ("/v1/", "/admin/", "/openapi.json", "/docs", "/redoc", "/health")


class _EdgeShim:
    """ASGI composite modelling the edge (nginx) routing.

    ``/v1/*``, ``/admin/*``, ``/health``, ``/docs`` go to the gateway;
    ``/internal/*`` is hidden (404) unless ``expose_internal`` is set (the
    tests' stand-in for the internal port). The lifespan scope is completed
    locally so that the gateway lifespan is run exactly once (by the dedicated
    lifespan client in ``Gateway``).
    """

    def __init__(self, gw_app, *, expose_internal: bool = False):
        self._gw = gw_app
        self._expose_internal = expose_internal

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        path = scope.get("path", "")
        if path in ("/v1",) or path.startswith(_GATEWAY_PREFIXES):
            return await self._gw(scope, receive, send)
        if self._expose_internal and path.startswith("/internal"):
            return await self._gw(scope, receive, send)
        body = b'{"detail":"Not Found"}'
        await send({
            "type": "http.response.start", "status": 404,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


class Gateway:
    """Hermetic gateway instance (proxies + keys in an isolated PostgreSQL DB).

    ``self.client`` — the edge-facing client (``/internal/*`` is 404);
    ``self.internal`` — exposes ``/internal/*``.
    """

    def __init__(self, monkeypatch, pg_dsn, *, keys=None,
                 proxies=("http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"),
                 settings_over=None, affinity_rows=None):
        settings_over = dict(settings_over or {})
        self.settings = build_settings(pg_dsn, **settings_over)
        entries = keys if keys is not None else [
            {"key": "test-key", "key_id": "default", "admin": True},
        ]

        # "First run": fill PostgreSQL and close the pools — the lifespan will
        # open its own on the same DB (main.proxydb / main.keydb).
        pdb = ProxyDB(self.settings.postgres_dsn)
        written = pdb.upsert_many(proxies)
        assert written == len(set(proxies)), "proxies were not written to proxies"
        pdb.close()
        kdb = KeyDB(self.settings.postgres_dsn)
        for e in entries:
            assert kdb.add(
                hash_key(e["key"]), e.get("key_id") or "", e.get("name") or "",
                admin=bool(e.get("admin")), rpm=e.get("rpm"), tpm=e.get("tpm"),
                models=e.get("models"),
            ), f"key {e.get('key_id')!r} was not written to keys"
        key_rows = kdb.all()
        kdb.close()

        self.manager = ProxyManager(self.settings)
        self.manager.apply_proxies([(p, True) for p in proxies])
        assert len(self.manager.proxies) == len(proxies), "proxies did not load"

        # Affinity is built here (before the lifespan) so tests can seed
        # bindings and drive a deterministic wall clock. main.affinity /
        # main.affinitydb are pre-set; the lifespan reuses them.
        self.wall_clock = {"value": None}  # None -> real time.time()
        self.affinitydb = AffinityDB(self.settings.postgres_dsn)
        if affinity_rows:
            self.affinitydb.upsert_many(list(affinity_rows))
        self.affinity = AffinityManager(
            self.settings, self.manager, db=self.affinitydb,
            now_wall=self.wall_now,
        )

        self.keystore = KeyStore()
        self.keystore.load(key_rows)
        self.reqlog = RequestLog(self.settings.postgres_dsn)
        self.sleeps: list[float] = []
        self._mp = monkeypatch
        monkeypatch.setattr(main, "settings", self.settings)
        monkeypatch.setattr(main, "manager", self.manager)
        monkeypatch.setattr(main, "keystore", self.keystore)
        monkeypatch.setattr(main, "reqlog", self.reqlog)
        monkeypatch.setattr(main, "affinity", self.affinity)
        monkeypatch.setattr(main, "affinitydb", self.affinitydb)
        monkeypatch.setattr(main, "refresh_loop", _quiet_refresh_loop)
        monkeypatch.setattr(main, "asyncio", _AsyncioShim(self.sleeps))
        # Reset the model catalogue cache between gateways.
        monkeypatch.setattr(main, "_models_cache", None)
        monkeypatch.setattr(main, "_models_cache_ts", 0.0)
        # Reset the conversation -> upstream session id registry between gateways.
        monkeypatch.setattr(main, "session_registry", main.sessions.SessionRegistry())

        # A dedicated client to run the gateway lifespan (not used for requests).
        self._gw_lifespan_client = TestClient(main.app)
        self._gw_lifespan_client.__enter__()

        self.client = TestClient(_EdgeShim(main.app))
        self.client.__enter__()
        self.internal = TestClient(_EdgeShim(main.app, expose_internal=True))
        self.internal.__enter__()

    # --- helpers ---

    def proxy(self, url):
        return next(p for p in self.manager.proxies if p.url == url)

    # --- wall clock control (affinity quota/ban/window tests) ---

    def wall_now(self) -> float:
        value = self.wall_clock.get("value")
        return time.time() if value is None else value

    def set_wall(self, value: float) -> None:
        self.wall_clock["value"] = value

    def advance_wall(self, seconds: float) -> None:
        self.wall_clock["value"] = self.wall_now() + seconds

    def freeze_wall(self, value: float | None = None) -> None:
        self.wall_clock["value"] = time.time() if value is None else value

    def script_upstream(self, router):
        """ensure_client -> every upstream client goes through ScriptedTransport(router)."""
        transport = ScriptedTransport(router)

        def ensure_client(proxy):
            if proxy.client is None:
                proxy.client = httpx.AsyncClient(
                    base_url=self.settings.opencode_base_url.rstrip("/"),
                    transport=transport,
                )
                proxy._client_proxy = proxy.url
            return proxy.client

        self._mp.setattr(self.manager, "ensure_client", ensure_client)
        return transport

    def flush_affinity(self):
        """Run the coalescing affinity writer to completion in the gateway loop."""
        self._gw_lifespan_client.portal.call(self.affinity.flush)

    def close(self):
        self.internal.__exit__(None, None, None)
        self.client.__exit__(None, None, None)
        self._gw_lifespan_client.__exit__(None, None, None)
