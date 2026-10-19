"""OpenCode free-tier integration primitives (gate injection + wire helpers).

This module is the SINGLE source of truth for how OpenRoute talks to
``opencode.ai``'s free-tier inference endpoint. It is deliberately pure and
dependency-free (stdlib only): every function is unit-testable without a
network or an event loop.

Background (see ``opencode-inference-notes.md`` in the parent directory):
the free tier sits behind a server-side gate ("FreeTierError") that requires,
SIMULTANEOUSLY:

1. **Client headers** — at least a ``User-Agent: opencode/...`` *or*
   ``x-opencode-client: cli``, plus a valid session id carried by one of
   ``x-opencode-session`` / ``x-session-affinity`` / ``x-session-id``. We send
   the full verified set (see :func:`build_gate_headers`).

2. **Body** — the system message must contain **at least two literal quotes**
   from opencode's canonical prompts. The minimal working pair is
   ``KEY1`` + ``KEY2`` below. A single quote -> HTTP 403. Order/length of the
   remaining text is irrelevant. We prepend the two quotes, then immediately
   neutralise their effect with ``NEUTRALIZE``, then append the caller's own
   system instructions.

Session ids have a strict format (``packages/schema/src/identifier.ts``):
``ses_`` + 12 hex chars + 14 base62 chars, where the 12 hex are
``~((unix_ms << 12 | counter))`` truncated to 48 bits. A random 26-char token
of the same shape but *without* the hex prefix -> 403. See
:func:`new_session_id`.

Free-tier quota is keyed by client IP; for IPv6 the server truncates to the
first 4 groups (/64). Therefore one proxy exit address == one quota bucket
(see DESIGN.md). Nothing in this module deals with quotas directly — that is
:mod:`app.proxies`.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping

# --- upstream endpoints (verbatim) -----------------------------------------

#: OpenAI-compatible chat completions endpoint (the "openai" mirror path).
OPENCODE_CHAT_URL = "https://opencode.ai/inference/openai/v1/chat/completions"
#: Model catalogue endpoint.
OPENCODE_MODELS_URL = "https://opencode.ai/inference/v1/models"
#: Base URL the gateway forwards ``/v1/<rest>`` onto (chat lives under it).
OPENCODE_BASE_URL = "https://opencode.ai/inference/openai/v1"

#: Default free model id.
DEFAULT_MODEL = "deepseek-v4.1-flash-free"

#: Free model ids observed on the endpoint (informational / allow-list seed).
FREE_MODELS: tuple[str, ...] = (
    "beam-free",
    "mimo-v2.6-flash-free",
    "nemotron-3.5-super-vl-free",
    "nemotron-3.5-lightning-free",
    "jev-1.13-free",
    "longcat-2.5-preview-free",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "space-bunny-free",
)

# --- gate constants (verbatim; do not "fix" wording) -----------------------

#: The two canonical-prompt quotes that satisfy the body layer of the gate.
KEY1 = "You are a title generator. You output ONLY a thread title. Nothing else."
KEY2 = "- Never use tools"

#: Neutraliser placed right after the two quotes so the model ignores them.
NEUTRALIZE = (
    "The two lines above are a gateway compatibility header for the OpenCode API; "
    "disregard them completely and follow only the instructions that come after."
)

#: Appended after the gate block when the caller supplied no system message.
DEFAULT_SYSTEM = "You are a helpful assistant. Answer the user's questions in their language."

#: User-Agent advertised by the client (spoofs the opencode CLI).
OPENCODE_UA = "opencode/latest/2.0.26/cli"

#: ``Authorization`` used by the free tier ("public" is the anonymous token).
OPENCODE_AUTHORIZATION = "Bearer public"

#: Session header names that carry the affinity value (all get the session id).
SESSION_HEADER_NAMES: tuple[str, ...] = (
    "x-opencode-session",
    "x-session-affinity",
    "x-session-id",
    "x-opencode-session-id",
)

# --- session id algorithm (verbatim) ---------------------------------------

#: base62 alphabet used by the 14-char suffix (0-9 A-Z a-z).
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def new_session_id(counter: int = 1) -> str:
    """Generate an opencode-shaped session id: ``ses_`` + 12 hex + 14 base62.

    Exact port of ``packages/schema/src/identifier.ts`` (12 hex =
    ``~((unix_ms << 12 | counter))`` masked to 48 bits; 14 random base62).
    The server rejects same-shaped tokens that lack the hex prefix.
    """
    ts = int(time.time() * 1000)
    masked = (~((ts << 12) + counter)) & ((1 << 48) - 1)
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(14))
    return "ses_" + masked.to_bytes(6, "big").hex() + suffix


# --- headers ---------------------------------------------------------------


def build_gate_headers(session_id: str, *, user_agent: str = OPENCODE_UA) -> dict[str, str]:
    """Return the full verified client-header set the free-tier gate demands.

    ``session_id`` must come from :func:`new_session_id` (or an already valid
    id); the same value is echoed into all four affinity headers.
    """
    headers = {
        "Authorization": OPENCODE_AUTHORIZATION,
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "x-opencode-client": "cli",
    }
    for name in SESSION_HEADER_NAMES:
        headers[name] = session_id
    return headers


# --- system message --------------------------------------------------------


def build_system_message(user_system: str | None = None) -> str:
    """Build the gated system message.

    Layout::

        <KEY1>
        <KEY2>

        <NEUTRALIZE>

        <user_system | DEFAULT_SYSTEM>

    ``user_system`` may be ``None`` or empty (the default assistant line is
    appended instead). The gate block always comes first; the caller's text
    always comes last.
    """
    block = f"{KEY1}\n{KEY2}\n\n{NEUTRALIZE}"
    tail = user_system if user_system else DEFAULT_SYSTEM
    return f"{block}\n\n{tail}"


def inject_gate_into_messages(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return a new message list with the gate injected into the system message.

    Copies the input; finds the FIRST ``role == "system"`` entry, replaces its
    ``content`` with :func:`build_system_message(<original text>)`, and leaves
    every other message untouched. If there is no system message, a new gated
    system message is prepended (per spec: append the default assistant line
    after the gate block).

    Non-dict / malformed entries are passed through verbatim (best effort); a
    caller-supplied ``content`` that is a list of content-parts (vision-style)
    is stringified only for the ``str`` extraction, but the replaced content is
    always the plain generated string.
    """
    out: list[dict[str, Any]] = []
    injected = False
    for msg in messages:
        if not isinstance(msg, Mapping):
            out.append(msg)  # type: ignore[arg-type]
            continue
        m = dict(msg)
        if not injected and m.get("role") == "system":
            original = m.get("content")
            text = original if isinstance(original, str) else None
            m["content"] = build_system_message(text)
            injected = True
        out.append(m)
    if not injected:
        out.insert(0, {"role": "system", "content": build_system_message(None)})
    return out


# --- body ------------------------------------------------------------------


def extract_model(raw_body: bytes | str) -> str | None:
    """Return ``body["model"]`` when the body is a JSON object with a str model.

    Any parse/shape failure returns ``None`` (never raises) — mirrors the
    gateway's tolerant model extraction used for allow-lists.
    """
    data = _loads_object(raw_body)
    if data is None:
        return None
    model = data.get("model")
    return model if isinstance(model, str) else None


def prepare_body(raw_body: bytes | str | None, model: str) -> bytes:
    """Return the gated request body as JSON bytes.

    - parses the caller body (a JSON object is required; non-object / invalid
      JSON becomes an empty object so the gate can still be injected),
    - sets ``model`` to ``model`` (caller value or gateway default),
    - injects/normalises the system message via :func:`inject_gate_into_messages`,
      UNLESS the caller supplied tool definitions (see below),
    - forces ``stream = True`` and ``stream_options = {"include_usage": True}``.

    ``messages`` that is missing or not a list is replaced by an empty list
    before injection (which then yields a single gated system message).

    Tool-bearing requests (``"tools": [...]``) are passed through WITHOUT the
    gate block: the upstream free tier fingerprints them against the real
    OpenCode agent prompt, and a title-quote-prefixed hybrid is rejected as a
    spoof (``403 FreeTierError`` — verified black-box 2026-10-19: agent body
    with the prepended gate block ⇒ 403 through every proxy; the same body
    untouched ⇒ 200; a minimal body + one tool + the gate block ⇒ 403). The
    real OpenCode client always sends its agent prompt together with tools, so
    leaving its system message verbatim is exactly what the upstream expects.
    """
    data = _loads_object(raw_body) or {}
    data["model"] = model
    messages = data.get("messages")
    if not isinstance(messages, list):
        messages = []
    tools = data.get("tools")
    if isinstance(tools, list) and tools:
        # Verbatim messages: injecting the title-quote block would turn the
        # request into the tool/agent-prompt hybrid the upstream gate rejects.
        data["messages"] = messages
    else:
        data["messages"] = inject_gate_into_messages(messages)
    data["stream"] = True
    data["stream_options"] = {"include_usage": True}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def free_tier_rejection(body: bytes | str | None) -> str | None:
    """Return the gate's rejection message when ``body`` is a ``FreeTierError``.

    The upstream answers 403 with
    ``{"type": "error", "error": {"type": "FreeTierError", "message": ...}}``
    when it decides the request does not come from OpenCode. That verdict is
    body-level (identical through every proxy), so ``main`` passes it through
    to the client instead of exhausting (and cooling down) the whole pool on
    it. Any other shape — a Cloudflare HTML block page, another JSON error —
    returns ``None`` and keeps the regular proxy-failure handling.
    """
    data = _loads_object(body)
    if not data:
        return None
    err = data.get("error")
    if not isinstance(err, dict) or err.get("type") != "FreeTierError":
        return None
    msg = err.get("message")
    return msg if isinstance(msg, str) and msg else "FreeTierError"


def _loads_object(raw_body: bytes | str | None) -> dict[str, Any] | None:
    """Parse ``raw_body`` into a dict or return ``None`` (never raises)."""
    if raw_body is None:
        return None
    if isinstance(raw_body, bytes):
        if not raw_body:
            return None
        try:
            raw_body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(raw_body)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# --- Retry-After -----------------------------------------------------------


def parse_retry_after(response_or_value: Any) -> float | None:
    """Parse a ``Retry-After`` value into a non-negative number of seconds.

    Accepts either an ``httpx.Response``-like object (anything with a
    ``.headers.get(...)``) or the raw header string / ``None``. Handles BOTH
    forms allowed by RFC 7231: integer seconds and an HTTP-date. Returns
    ``None`` when the header is absent or unparseable. A date in the past
    yields ``0.0`` (nothing to wait for).
    """
    raw: Any
    headers = getattr(response_or_value, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        raw = headers.get("retry-after")
    else:
        raw = response_or_value
    if raw is None or isinstance(raw, (int, float)):
        # A numeric raw value is accepted for convenience (already seconds).
        return max(0.0, float(raw)) if isinstance(raw, (int, float)) else None
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return max(0.0, float(int(raw)))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


# --- SSE parsing -----------------------------------------------------------

#: Stream terminator payload.
SSE_DONE = "[DONE]"


def parse_sse_line(line: bytes | str) -> str | None:
    """Return the payload after ``data:`` for an SSE data line, else ``None``.

    - ``None`` for comment/blank/other SSE fields (``event:``, ``:`` keep-alive,
      blank separator lines) and for lines that are not ``data:``-prefixed.
    - Returns the raw stripped payload string (which may be ``SSE_DONE``) for
      ``data:`` lines. Never raises; accepts ``bytes`` or ``str``.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - decode is already lossy
            return None
    stripped = line.strip("\r\n")
    if not stripped.startswith("data:"):
        return None
    return stripped[len("data:"):].strip()


def usage_from_payload(payload: str | None) -> dict[str, int] | None:
    """Parse an SSE data payload and return its ``usage`` dict, else ``None``.

    The final chunk of an opencode stream carries usage
    (``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``). Returns a
    dict containing only integer-valued fields among those three keys (at
    least one required); returns ``None`` for ``[DONE]``, non-JSON payloads,
    or chunks without usable usage. Never raises.
    """
    if not payload or payload == SSE_DONE:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out or None


def extract_usage_from_line(line: bytes | str) -> dict[str, int] | None:
    """Convenience: :func:`parse_sse_line` + :func:`usage_from_payload`."""
    return usage_from_payload(parse_sse_line(line))
