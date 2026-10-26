"""Tests for keys.py: auth, RPM/TPM, model allow-list, hashing and incremental
KeyStore mutations. Persistence is covered by the storage-service suite."""

import pytest

import gateway.keys as keys_mod
from gateway.keys import (
    AuthError,
    KeyInfo,
    KeyStore,
    RateLimitError,
    generate_key,
    hash_key,
)


def row(e):
    """dict in the storage-service shape for entry e (plaintext "key" + fields)."""
    return {
        "key_hash": hash_key(e["key"]),
        "key_id": e.get("key_id", "k1"),
        "rpm": e.get("rpm"),
        "tpm": e.get("tpm"),
        "models": e.get("models"),
        "enabled": e.get("enabled", True),
    }


def make_store(entries):
    ks = KeyStore()
    ks.load([row(e) for e in entries])
    return ks


def test_generate_key_prefix_and_entropy():
    first = generate_key()
    second = generate_key()
    assert first.startswith("openroute-")
    assert len(first) == len("openroute-") + 48
    assert set(first.removeprefix("openroute-")) <= set("0123456789abcdef")
    assert first != second
    assert hash_key(first) != first


# --- auth ---


def test_authenticate_ok_and_limits():
    ks = make_store([
        {"key": "sk-abc", "key_id": "k1",
         "rpm": 10, "tpm": 1000, "models": ["m1"]},
        {"key": "sk-xyz", "key_id": "k2"},
    ])
    info = ks.authenticate("Bearer sk-abc")
    assert info.key_id == "k1"
    assert info.rpm == 10 and info.tpm == 1000 and info.models == ["m1"]
    assert info.enabled is True

    info2 = ks.authenticate("Bearer sk-xyz")
    assert info2.rpm is None and info2.tpm is None
    assert info2.models is None and info2.enabled is True


def test_authenticate_wrong_or_missing_key():
    ks = make_store([{"key": "sk-abc", "key_id": "k1"}])
    with pytest.raises(AuthError) as exc:
        ks.authenticate("Bearer wrong-key")
    assert exc.value.status_code == 401
    with pytest.raises(AuthError):
        ks.authenticate("")
    with pytest.raises(AuthError):
        ks.authenticate("Basic sk-abc")


def test_authenticate_disabled_key():
    ks = make_store([{"key": "sk-dis", "key_id": "k1", "enabled": False}])
    with pytest.raises(AuthError) as exc:
        ks.authenticate("Bearer sk-dis")
    assert exc.value.status_code == 401


# --- RPM ---


def test_rpm_limit_triggers_within_window():
    ks = make_store([{"key": "sk-r", "key_id": "r1", "rpm": 2}])
    key = ks.authenticate("Bearer sk-r")
    ks.check_rate(key)
    ks.check_rate(key)
    with pytest.raises(RateLimitError):
        ks.check_rate(key)


def test_rpm_window_slides(monkeypatch):
    ks = make_store([{"key": "sk-r", "key_id": "r1", "rpm": 2}])
    key = ks.authenticate("Bearer sk-r")
    clock = {"now": 1000.0}
    monkeypatch.setattr(keys_mod.time, "monotonic", lambda: clock["now"])

    ks.check_rate(key)
    ks.check_rate(key)
    with pytest.raises(RateLimitError):
        ks.check_rate(key)

    clock["now"] += 61.0
    ks.check_rate(key)
    ks.check_rate(key)
    with pytest.raises(RateLimitError):
        ks.check_rate(key)


def test_rpm_error_carries_retry_after(monkeypatch):
    ks = make_store([{"key": "sk-r", "key_id": "r1", "rpm": 2}])
    key = ks.authenticate("Bearer sk-r")
    clock = {"now": 1000.0}
    monkeypatch.setattr(keys_mod.time, "monotonic", lambda: clock["now"])

    ks.check_rate(key)
    ks.check_rate(key)
    with pytest.raises(RateLimitError) as exc:
        ks.check_rate(key)
    ra = exc.value.retry_after
    assert ra is not None and 0 < ra <= 60
    assert ra == pytest.approx(60.0)


# --- TPM ---


def test_tpm_count_tokens_records_never_raises():
    ks = make_store([{"key": "sk-t", "key_id": "t1", "tpm": 100}])
    key = ks.authenticate("Bearer sk-t")
    ks.count_tokens(key, 60)
    ks.count_tokens(key, 40)
    ks.count_tokens(key, 1)       # boundary overshoot — recorded, never raises
    ks.count_tokens(key, 10_000)


def test_tpm_ignores_nonpositive_and_none():
    ks = make_store([{"key": "sk-t", "key_id": "t1", "tpm": 100}])
    key = ks.authenticate("Bearer sk-t")
    ks.count_tokens(key, 0)
    ks.count_tokens(key, -5)
    ks.count_tokens(key, 100)


def test_check_tpm_gates_after_saturation(monkeypatch):
    ks = make_store([{"key": "sk-t", "key_id": "t1", "tpm": 100}])
    key = ks.authenticate("Bearer sk-t")
    clock = {"now": 1000.0}
    monkeypatch.setattr(keys_mod.time, "monotonic", lambda: clock["now"])

    ks.check_tpm(key)
    ks.count_tokens(key, 60)
    ks.check_tpm(key)
    ks.count_tokens(key, 40)
    with pytest.raises(RateLimitError) as exc:
        ks.check_tpm(key)
    ra = exc.value.retry_after
    assert ra is not None and 0 < ra <= 60

    clock["now"] += 61.0
    ks.check_tpm(key)


def test_tpm_boundary_overshoot_allowed_once_then_gated():
    ks = make_store([{"key": "sk-t", "key_id": "t1", "tpm": 100}])
    key = ks.authenticate("Bearer sk-t")
    ks.count_tokens(key, 80)
    ks.check_tpm(key)
    ks.count_tokens(key, 50)
    with pytest.raises(RateLimitError) as exc:
        ks.check_tpm(key)
    assert exc.value.retry_after is not None and exc.value.retry_after > 0


def test_check_tpm_unlimited_or_unknown():
    ks = make_store([
        {"key": "sk-a", "key_id": "a1"},
        {"key": "sk-b", "key_id": "b1", "tpm": 0},
    ])
    a = ks.authenticate("Bearer sk-a")
    b = ks.authenticate("Bearer sk-b")
    ks.check_tpm(a)
    ks.check_tpm(b)
    ks.check_tpm("a1")
    ks.check_tpm("no-such")


def test_no_limits_means_unlimited():
    ks = make_store([{"key": "sk-free", "key_id": "f1"}])
    key = ks.authenticate("Bearer sk-free")
    for _ in range(50):
        ks.check_rate(key)
        ks.check_tpm(key)
        ks.count_tokens(key, 10_000)


# --- model allow-list ---


def test_model_allow_list_403():
    ks = make_store([{"key": "sk-m", "key_id": "m1", "models": ["deepseek-v4.1-flash-free"]}])
    key = ks.authenticate("Bearer sk-m")
    ks.check_model(key, "deepseek-v4.1-flash-free")
    ks.check_model(key, None)
    with pytest.raises(AuthError) as exc:
        ks.check_model(key, "some/paid-model")
    assert exc.value.status_code == 403


def test_model_any_allowed_when_no_list():
    ks = make_store([{"key": "sk-m", "key_id": "m1"}])
    key = ks.authenticate("Bearer sk-m")
    ks.check_model(key, "anything/at-all")


# --- load: hashes, no plaintext ---


def test_load_hashes_keys_no_plaintext_retained():
    secret = "sk-super-secret-value"
    ks = make_store([{"key": secret, "key_id": "k1"}])
    assert set(ks._by_hash) == {hash_key(secret)}
    assert all(len(h) == 64 for h in ks._by_hash)
    blob = repr(vars(ks)) + repr(ks._rpm) + repr(ks._tpm)
    assert secret not in blob


def test_key_ids_sorted():
    ks = make_store([
        {"key": "sk-b", "key_id": "zz"},
        {"key": "sk-a", "key_id": "aa"},
    ])
    assert ks.key_ids() == ["aa", "zz"]


def test_load_empty_rows_means_no_auth():
    ks = KeyStore()
    ks.load([])
    with pytest.raises(AuthError):
        ks.authenticate("Bearer anything")


# --- incremental mutations (admin UI) ---


def test_upsert_remove_set_enabled_without_window_reset():
    ks = make_store([{"key": "sk-a", "key_id": "a1", "tpm": 100}])
    a = ks.authenticate("Bearer sk-a")
    ks.count_tokens(a, 70)

    h = hash_key("sk-new")
    assert ks.upsert(h, KeyInfo(key_id="n1",
                                rpm=None, tpm=None, models=None, enabled=True)) is True
    assert ks.authenticate("Bearer sk-new").key_id == "n1"
    assert ks.upsert(h, KeyInfo(key_id="n1",
                                rpm=None, tpm=None, models=None, enabled=True)) is False
    assert ks.authenticate("Bearer sk-new").key_id == "n1"

    ks.check_tpm("a1")  # a1's window survived the upsert: 70 < 100

    assert ks.set_enabled(h, False) is True
    with pytest.raises(AuthError):
        ks.authenticate("Bearer sk-new")
    ks.count_tokens(ks.authenticate("Bearer sk-a"), 30)
    with pytest.raises(RateLimitError):
        ks.check_tpm("a1")

    assert ks.remove(h) is True
    assert ks.remove(h) is False
    with pytest.raises(AuthError):
        ks.authenticate("Bearer sk-new")


def test_set_enabled_revokes_and_restores():
    ks = make_store([{"key": "sk-x", "key_id": "x1"}])
    h = hash_key("sk-x")
    assert ks.set_enabled(h, False) is True
    with pytest.raises(AuthError):
        ks.authenticate("Bearer sk-x")
    assert ks.set_enabled(h, True) is True
    assert ks.authenticate("Bearer sk-x").key_id == "x1"
    assert ks.set_enabled(hash_key("no-such"), False) is False


def test_rotate_replaces_hash_and_resets_windows():
    ks = make_store([{"key": "sk-old", "key_id": "r1", "tpm": 100}])
    old = ks.authenticate("Bearer sk-old")
    ks.count_tokens(old, 80)

    old_hash = hash_key("sk-old")
    new_hash = hash_key("sk-new")
    info = ks.rotate(old_hash, new_hash)
    assert info is not None and info.key_id == "r1"
    assert ks.rotate(old_hash, hash_key("sk-never")) is None
    with pytest.raises(AuthError):
        ks.authenticate("Bearer sk-old")

    new = ks.authenticate("Bearer sk-new")
    assert new.key_id == "r1" and new.tpm == 100
    ks.count_tokens(new, 80)
    ks.check_tpm(new)  # the old key's 80-token window was reset
    assert hash_key("sk-old") not in ks._by_hash
    assert new_hash in ks._by_hash
