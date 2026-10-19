"""Live smoke test against the real opencode.ai free tier (opt-in, offline by default).

Skipped unless ``OPENROUTE_LIVE_PROXY`` is set to a real proxy URL (for example
``http://user:pass@1.2.3.4:8080``). The module-level :data:`pytestmark` is
evaluated at collection time, so the PostgreSQL fixtures are never instantiated
in a normal run: the default ``pytest tests`` stays fully hermetic and offline.

Run it explicitly::

    OPENROUTE_LIVE_PROXY=http://user:pass@host:port \
        pytest tests/test_opencode_live_smoke.py -v -s

The test drives the gateway through the same edge shim the hermetic suite uses
(:class:`gw.Gateway`): it sends a NON-stream request while the gateway forces
``stream:true`` upstream, buffers the SSE and converts it back into a standard
OpenAI JSON ``chat.completion`` object. A real proxy exit IP is required
because the free tier keys its quota on the client IP.
"""

from __future__ import annotations

import json
import os

import pytest

# Make the project root + tests dir importable the same way conftest.py does.
import gw  # noqa: E402  (path is prepared by conftest.py)

LIVE_PROXY = os.environ.get("OPENROUTE_LIVE_PROXY", "").strip()
FREE_MODEL = "deepseek-v4.1-flash-free"

pytestmark = pytest.mark.skipif(
    not LIVE_PROXY,
    reason="set OPENROUTE_LIVE_PROXY to a real proxy URL to run the live smoke test",
)

_LIVE_SETTINGS = {
    "opencode_base_url": "https://opencode.ai/inference/openai/v1",
    "opencode_models_url": "https://opencode.ai/inference/v1/models",
    "opencode_model": FREE_MODEL,
    "upstream_timeout": 120.0,
    "connect_timeout": 20.0,
}


def _post_chat(gw, *, stream: bool):
    return gw.client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={
            "model": FREE_MODEL,
            "stream": stream,
            "messages": [{"role": "user", "content": "Reply with exactly the single word: pong"}],
        },
    )


def _usage_from_sse(body: bytes) -> dict | None:
    """Final ``usage`` block of an SSE body (opencode puts it in the last chunk)."""
    usage = None
    for raw in body.splitlines():
        line = raw.decode("utf-8", "replace")
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            usage = data["usage"]
    return usage


def _assert_usage(usage: dict | None, label: str) -> None:
    print(f"{label} usage: {usage}")
    assert isinstance(usage, dict), usage
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert isinstance(usage.get(field), int), (field, usage)


def test_live_nonstream_chat_completion(make_gateway):
    """The gateway buffers the upstream SSE and answers with a JSON completion."""
    gw = make_gateway(proxies=(LIVE_PROXY,), settings_over=_LIVE_SETTINGS)

    r = _post_chat(gw, stream=False)

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["object"] == "chat.completion"
    content = data["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content.strip(), data
    _assert_usage(data.get("usage"), "live non-stream")


def test_live_stream_passthrough(make_gateway):
    """A streaming caller receives the upstream SSE untouched (with [DONE])."""
    gw = make_gateway(proxies=(LIVE_PROXY,), settings_over=_LIVE_SETTINGS)

    with gw.client.stream("POST", "/v1/chat/completions",
                          headers={"Authorization": "Bearer test-key"},
                          json={
                              "model": FREE_MODEL,
                              "stream": True,
                              "messages": [{"role": "user", "content": "Say hi in one word."}],
                          }) as r:
        assert r.status_code == 200, r.read()
        body = r.read()

    assert b"data:" in body
    assert b"[DONE]" in body
    _assert_usage(_usage_from_sse(body), "live stream")
