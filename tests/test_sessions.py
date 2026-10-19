"""Unit tests for app.sessions — conversation-scoped upstream session ids."""

from app import opencode
from app.sessions import SessionRegistry


def test_registry_reuses_id_until_ttl():
    reg = SessionRegistry(ttl_seconds=10.0)
    sid = reg.resolve("fp", now=100.0)
    assert reg.resolve("fp", now=109.0) == sid  # sliding refresh to 109.0
    assert reg.resolve("fp", now=119.0) != sid  # idle == ttl -> new session


def test_registry_lru_cap():
    reg = SessionRegistry(ttl_seconds=1000.0, max_entries=2)
    first = reg.resolve("a", now=0.0)
    reg.resolve("b", now=1.0)
    reg.resolve("c", now=2.0)  # evicts "a"
    assert reg.resolve("a", now=3.0) != first


def test_registry_ids_are_valid_session_ids():
    reg = SessionRegistry()
    assert opencode.is_session_id(reg.resolve("fp"))
