"""Unit tests for app.opencode — the gated opencode.ai integration primitives.

Pure/stdlib-only: no network, no DB, no third-party imports beyond pytest.
"""

import json
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from app import opencode


# --- session id ------------------------------------------------------------


def test_session_id_shape():
    sid = opencode.new_session_id()
    assert sid.startswith("ses_")
    assert len(sid) == len("ses_") + 12 + 14
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", sid)


def test_session_id_alphabet_and_uniqueness():
    ids = {opencode.new_session_id(i) for i in range(50)}
    assert len(ids) == 50
    for sid in ids:
        assert all(c in opencode._ALPHABET for c in sid[4 + 12:])


def test_is_session_id_shape():
    assert opencode.is_session_id(opencode.new_session_id())
    for bad in ("", "abc123", "ses_" + "A" * 26, "ses_0123456789ab" + "!" * 14, None):
        assert not opencode.is_session_id(bad)


# --- conversation fingerprint ---------------------------------------------


def test_conversation_fingerprint_is_stable_as_history_grows():
    first = {"messages": [{"role": "user", "content": "hello"}]}
    second = {"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ]}
    a = opencode.conversation_fingerprint(json.dumps(first), key_id="k", project="p")
    b = opencode.conversation_fingerprint(json.dumps(second), key_id="k", project="p")
    assert a is not None and a == b


def test_conversation_fingerprint_scopes_by_key_and_project():
    body = json.dumps({"messages": [{"role": "user", "content": "hello"}]})
    base = opencode.conversation_fingerprint(body, key_id="k1", project="p1")
    assert base != opencode.conversation_fingerprint(body, key_id="k2", project="p1")
    assert base != opencode.conversation_fingerprint(body, key_id="k1", project="p2")


def test_conversation_fingerprint_none_without_anchor():
    assert opencode.conversation_fingerprint(
        b'{"messages": []}', key_id="k", project=None
    ) is None
    assert opencode.conversation_fingerprint(
        json.dumps({"messages": [{"role": "assistant", "content": "hi"}]}),
        key_id="k", project=None,
    ) is None


def test_conversation_fingerprint_reads_content_parts():
    body = json.dumps({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]}]})
    assert opencode.conversation_fingerprint(body, key_id="k", project=None) is not None


# --- headers ---------------------------------------------------------------


def test_gate_headers_complete():
    sid = opencode.new_session_id()
    h = opencode.build_gate_headers(sid)
    assert h["Authorization"] == "Bearer public"
    assert h["Content-Type"] == "application/json"
    assert h["User-Agent"].startswith("opencode/")
    assert h["x-opencode-client"] == "cli"
    for name in opencode.SESSION_HEADER_NAMES:
        assert h[name] == sid
    # All four affinity carriers present.
    assert len(opencode.SESSION_HEADER_NAMES) == 4


# --- system message --------------------------------------------------------


def test_build_system_message_contains_both_quotes_and_neutralizer():
    msg = opencode.build_system_message("Answer in French.")
    assert opencode.KEY1 in msg
    assert opencode.KEY2 in msg
    assert opencode.NEUTRALIZE in msg
    assert msg.rstrip().endswith("Answer in French.")
    # Gate block precedes the caller text.
    assert msg.index(opencode.KEY1) < msg.index("Answer in French.")


def test_build_system_message_default_tail_when_no_user_system():
    for empty in (None, ""):
        msg = opencode.build_system_message(empty)
        assert opencode.DEFAULT_SYSTEM in msg
        assert opencode.KEY1 in msg and opencode.KEY2 in msg


def test_inject_prepends_when_no_system():
    msgs = [{"role": "user", "content": "hi"}]
    out = opencode.inject_gate_into_messages(msgs)
    assert out[0]["role"] == "system"
    assert opencode.KEY1 in out[0]["content"]
    assert opencode.DEFAULT_SYSTEM in out[0]["content"]
    assert out[1] == {"role": "user", "content": "hi"}
    # input not mutated
    assert msgs[0] == {"role": "user", "content": "hi"}


def test_inject_replaces_first_system_only():
    msgs = [
        {"role": "system", "content": "SYS-A"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "SYS-B"},
    ]
    out = opencode.inject_gate_into_messages(msgs)
    assert "SYS-A" in out[0]["content"] and opencode.KEY1 in out[0]["content"]
    assert out[2]["content"] == "SYS-B"  # second system untouched


# --- body ------------------------------------------------------------------


def test_prepare_body_forces_stream_and_usage_and_model():
    raw = json.dumps({
        "model": "beam-free",
        "messages": [{"role": "system", "content": "Be terse."},
                     {"role": "user", "content": "hi"}],
        "stream": False,
    }).encode()
    out = json.loads(opencode.prepare_body(raw, "deepseek-v4.1-flash-free"))
    assert out["model"] == "deepseek-v4.1-flash-free"
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
    sys_msg = out["messages"][0]
    assert sys_msg["role"] == "system"
    assert opencode.KEY1 in sys_msg["content"] and "Be terse." in sys_msg["content"]
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_prepare_body_handles_garbage_and_missing_messages():
    out = json.loads(opencode.prepare_body(b"not json", "m"))
    assert out["model"] == "m"
    assert out["messages"][0]["role"] == "system"
    assert opencode.KEY2 in out["messages"][0]["content"]


def test_prepare_body_tools_skip_gate_injection():
    """Tool-bearing requests keep the caller's (agent) system message verbatim:
    the upstream gate rejects the title-quote/tools hybrid with FreeTierError."""
    tool = {"type": "function", "function": {"name": "bash", "parameters": {}}}
    raw = json.dumps({
        "model": "deepseek-v4.1-flash-free",
        "messages": [{"role": "system", "content": "REAL AGENT PROMPT"},
                     {"role": "user", "content": "hi"}],
        "tools": [tool],
    }).encode()
    out = json.loads(opencode.prepare_body(raw, "deepseek-v4.1-flash-free"))
    assert out["messages"][0]["content"] == "REAL AGENT PROMPT"  # untouched
    assert opencode.KEY1 not in out["messages"][0]["content"]
    assert out["tools"] == [tool]
    assert out["stream"] is True and out["stream_options"] == {"include_usage": True}


def test_prepare_body_empty_or_non_list_tools_still_inject():
    """Only a NON-EMPTY LIST of tools switches injection off (safe default)."""
    for tools_value in ([], None, {"a": 1}, "bash"):
        raw = json.dumps({
            "model": "m",
            "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "u"}],
            "tools": tools_value,
        }).encode()
        out = json.loads(opencode.prepare_body(raw, "m"))
        assert opencode.KEY1 in out["messages"][0]["content"], tools_value


def test_free_tier_rejection():
    body = json.dumps({"type": "error", "error": {
        "type": "FreeTierError",
        "message": "OpenCode's free tier can only be used from within OpenCode."}})
    assert opencode.free_tier_rejection(body) == (
        "OpenCode's free tier can only be used from within OpenCode.")
    assert opencode.free_tier_rejection(body.encode()) == (
        "OpenCode's free tier can only be used from within OpenCode.")
    # any other shape is NOT a body-level verdict (regular failover applies)
    assert opencode.free_tier_rejection(
        b'{"error": {"type": "OtherError", "message": "x"}}') is None
    assert opencode.free_tier_rejection(b"<html>cloudflare block</html>") is None
    assert opencode.free_tier_rejection(b"") is None
    assert opencode.free_tier_rejection(None) is None
    assert opencode.free_tier_rejection(b"[]") is None
    # a FreeTierError without a message still identifies the verdict
    assert opencode.free_tier_rejection(json.dumps(
        {"error": {"type": "FreeTierError"}}).encode()) == "FreeTierError"


# --- daily quota / abusive-ban classifiers --------------------------------


def test_error_type_and_is_free_usage_limit_strict():
    body = {"type": "error", "error": {"type": "FreeUsageLimitError", "message": "x"}}
    assert opencode.error_type(json.dumps(body)) == "FreeUsageLimitError"
    assert opencode.error_type(json.dumps(body).encode()) == "FreeUsageLimitError"
    assert opencode.is_free_usage_limit(json.dumps(body)) is True
    assert opencode.is_free_usage_limit(json.dumps(body).encode()) is True

    for other in (
        {"error": {"type": "RateLimitError"}},
        {"error": {"type": "FreeTierError"}},
        {"error": {"type": "freeusagelimiterror"}},  # case-sensitive type
        {"error": "FreeUsageLimitError"},             # wrong shape
        {"type": "FreeUsageLimitError"},              # not nested under error
    ):
        raw = json.dumps(other)
        assert opencode.is_free_usage_limit(raw) is False


def test_error_type_never_raises():
    for value in (None, b"", b"not json", b"[]", b"null", "\udcff", 42, b'{"error": null}'):
        assert opencode.error_type(value) is None
        assert opencode.is_free_usage_limit(value) is False
        assert opencode.is_abusive_ban(value) is False


def test_is_abusive_ban_case_insensitive_text_search():
    assert opencode.is_abusive_ban(b"Forbidden: Abusive Traffic detected")
    assert opencode.is_abusive_ban(b'{"error":{"message":"ABUSIVE TRAFFIC"}}')
    assert opencode.is_abusive_ban("We saw abusive traffic from this IP")
    assert not opencode.is_abusive_ban(b"FreeTierError")
    assert not opencode.is_abusive_ban(None)


def test_seconds_to_utc_midnight_exact_formula():
    import math

    for now in (0.0, 1_700_000_000.123, 86_399.5, 86_400.0, 123_456_789.987):
        now_ms = int(now * 1000)
        expected = math.ceil((86_400_000 - (now_ms % 86_400_000)) / 1000)
        assert opencode.seconds_to_utc_midnight(now) == expected


def test_seconds_to_utc_midnight_midnight_means_next_midnight():
    # 2026-10-19 00:00:00 UTC
    midnight = 1_761_177_600.0
    assert opencode.seconds_to_utc_midnight(midnight) == 86_400.0
    # One hour before midnight -> one hour (in whole seconds, rounded up).
    assert opencode.seconds_to_utc_midnight(midnight - 3600.0) == 3600.0
    # 23:59:59.500 -> 1 s (ceil); timezone is irrelevant (UTC epoch only).
    assert opencode.seconds_to_utc_midnight(midnight - 0.5) == 1.0


def test_seconds_to_utc_midnight_does_not_depend_on_local_timezone():
    now = 1_761_177_600.0 + 3.0
    assert opencode.seconds_to_utc_midnight(now) == 86_400.0 - 3.0


def test_extract_model():
    assert opencode.extract_model(b'{"model":"x"}') == "x"
    assert opencode.extract_model(b'{"model":5}') is None
    assert opencode.extract_model(b"[]") is None
    assert opencode.extract_model(b"") is None
    assert opencode.extract_model(b"nope") is None


# --- Retry-After -----------------------------------------------------------


@pytest.mark.parametrize("raw,lo,hi", [
    ("0", 0.0, 0.0),
    ("5", 5.0, 5.0),
    ("  12 ", 12.0, 12.0),
    ("garbage", None, None),
    ("", None, None),
    (None, None, None),
])
def test_parse_retry_after_seconds(raw, lo, hi):
    got = opencode.parse_retry_after(raw)
    if lo is None:
        assert got is None
    else:
        assert lo <= got <= hi


def test_parse_retry_after_http_date_future():
    future = datetime.now(timezone.utc) + __import__("datetime").timedelta(seconds=120)
    raw = format_datetime(future, usegmt=True)
    got = opencode.parse_retry_after(raw)
    assert 110 <= got <= 121


def test_parse_retry_after_http_date_past_is_zero():
    past = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=1)
    assert opencode.parse_retry_after(format_datetime(past, usegmt=True)) == 0.0


class _Resp:
    def __init__(self, value):
        self.headers = {"retry-after": value} if value is not None else {}


def test_parse_retry_after_accepts_response_like():
    assert opencode.parse_retry_after(_Resp("7")) == 7.0
    assert opencode.parse_retry_after(_Resp(None)) is None


# --- SSE -------------------------------------------------------------------


def test_parse_sse_line():
    assert opencode.parse_sse_line(b'data: {"a":1}') == '{"a":1}'
    assert opencode.parse_sse_line('data: [DONE]') == "[DONE]"
    assert opencode.parse_sse_line("event: ping") is None
    assert opencode.parse_sse_line("") is None
    assert opencode.parse_sse_line(": keep-alive") is None
    assert opencode.parse_sse_line("data:") == ""


def test_usage_from_payload():
    payload = json.dumps({"choices": [], "usage": {
        "prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}})
    assert opencode.usage_from_payload(payload) == {
        "prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}
    assert opencode.usage_from_payload("[DONE]") is None
    assert opencode.usage_from_payload("{}") is None
    assert opencode.usage_from_payload("not json") is None
    # non-int fields are dropped; remaining ints kept
    assert opencode.usage_from_payload(json.dumps(
        {"usage": {"prompt_tokens": "x", "total_tokens": 5}})) == {"total_tokens": 5}


def test_extract_usage_from_line():
    line = 'data: {"usage": {"total_tokens": 3}}'
    assert opencode.extract_usage_from_line(line) == {"total_tokens": 3}
